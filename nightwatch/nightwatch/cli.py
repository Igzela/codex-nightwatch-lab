from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .git import GitError, repo_root, snapshot
from .models import State, plan_progress
from .quota import AppServerQuotaProvider, QuotaError, make_quota_provider
from .storage import NightwatchStore, StateIntegrityError, SupervisorAlreadyRunning, make_run_id, now_iso, redact
from .supervisor import Supervisor, build_report, pid_alive


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nightwatch", description="Fail-closed unattended supervisor for one OpenAI Codex thread")
    parser.add_argument("--version", action="version", version=f"nightwatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a new supervised goal")
    run.add_argument("goal")
    run.add_argument("--repo", default=None)
    run.add_argument("--no-inhibit", action="store_true", help="do not wrap the foreground supervisor in systemd-inhibit")
    run.add_argument("--service", action="store_true", help="persist the new goal, then start the repo-bound user systemd service")
    run.add_argument("--verify", action="append", default=[], metavar="COMMAND", help="trusted final verification command; frozen before Codex starts (repeatable)")

    for name, help_text in (("status", "show current durable status"), ("log", "show human-readable supervisor log"), ("report", "write/show a durable report"), ("stop", "stop automatic work and preserve state"), ("resume", "resume the existing exact-thread goal")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--repo", default=None)
        if name == "status":
            cmd.add_argument("--json", action="store_true")
        if name == "log":
            cmd.add_argument("--tail", type=int, default=80)
        if name == "resume":
            cmd.add_argument("--no-inhibit", action="store_true", help="do not wrap the foreground supervisor in systemd-inhibit")

    doctor = sub.add_parser("doctor", help="check Linux, Codex, auth, quota, and local state support")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--repo", default=None)

    install = sub.add_parser("install", help="install a user-local launcher")
    install.add_argument("--service", action="store_true", help="also install a marked user-level systemd unit")
    install.add_argument("--repo", default=None, help="Git repository to bind when installing the user service")
    sub.add_parser("uninstall", help="remove only Nightwatch-owned user-local installation")
    recover = sub.add_parser("recover", help="record explicit recovery of an ambiguous exact-thread lease")
    recover.add_argument("--repo", default=None)
    recover.add_argument("--ack-ambiguous", action="store_true", help="acknowledge manual review before clearing an ambiguous claim")
    sub.add_parser("test", help="run non-destructive product checks")
    # `nightwatch test quota-soak` is intentionally informational: it never burns quota.
    sub.choices["test"].add_argument("test_name", nargs="?", default=None)
    return parser


def _root(value: str | None) -> Path:
    try:
        return repo_root(value or Path.cwd())
    except GitError as exc:
        raise SystemExit("nightwatch: command requires a Git repository") from exc


def _store(value: str | None) -> NightwatchStore:
    return NightwatchStore(_root(value))


def _maybe_inhibit(args: argparse.Namespace, root: Path) -> None:
    if getattr(args, "no_inhibit", False) or os.environ.get("NIGHTWATCH_INHIBITED") == "1":
        return
    binary = shutil.which("systemd-inhibit")
    if not binary:
        return
    try:
        probe = subprocess.run(
            [binary, "--what=sleep", "--mode=block", "--why=Nightwatch probe", "/bin/true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        probe = None
    if probe is None or probe.returncode != 0:
        print("nightwatch: systemd-inhibit is not usable here; continuing without a sleep lock", file=sys.stderr)
        return
    source_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["NIGHTWATCH_INHIBITED"] = "1"
    old_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_root + (os.pathsep + old_pythonpath if old_pythonpath else "")
    child_args = [sys.executable, "-m", "nightwatch.cli", args.command]
    if args.command == "run":
        child_args.append(args.goal)
        for command in args.verify:
            child_args.extend(["--verify", command])
    child_args.extend(["--repo", str(root), "--no-inhibit"])
    os.execvpe(binary, [binary, "--what=sleep", "--mode=block", "--why=Nightwatch supervisor", *child_args], env)


def _run(args: argparse.Namespace) -> int:
    root = _root(args.repo)
    store = NightwatchStore(root)
    if store.exists():
        try:
            state = store.load_state()
        except StateIntegrityError as exc:
            raise SystemExit(f"nightwatch: refusing to overwrite invalid durable state: {exc}") from exc
        raise SystemExit(f"nightwatch: a run already exists in {root} (state={state['state']}); use nightwatch resume")
    if store.legacy_directory.exists():
        print("nightwatch: legacy repo/.nightwatch state is ignored for schema 2; it is preserved for forensic review", file=sys.stderr)
    if args.service:
        _validate_install_targets(root)
    else:
        _maybe_inhibit(args, root)
    state = store.initialize(make_run_id(str(root)), args.goal, str(root), verify_commands=args.verify)
    if args.service:
        _install_user_files(root)
        try:
            _start_user_service()
        except RuntimeError as exc:
            print(
                "nightwatch: goal was saved as NEW but the user service was not started; "
                f"run `nightwatch resume --repo {root}` after fixing systemd: {exc}",
                file=sys.stderr,
            )
            return 1
        print(f"Nightwatch service started: run_id={state['run_id']} repo={root}")
        return 0
    supervisor = Supervisor(store)
    _install_signal_handlers(supervisor)
    final = supervisor.execute(start=True)
    print(f"Nightwatch {final['state']}: run_id={final['run_id']} thread_id={final.get('thread_id') or '(not captured)'}")
    return _exit_code(final["state"])


def _resume(args: argparse.Namespace) -> int:
    root = _root(args.repo)
    _maybe_inhibit(args, root)
    store = NightwatchStore(root)
    supervisor = Supervisor(store)
    _install_signal_handlers(supervisor)
    final = supervisor.execute(start=False)
    print(f"Nightwatch {final['state']}: run_id={final['run_id']} thread_id={final.get('thread_id') or '(not captured)'}")
    return _exit_code(final["state"])


def _exit_code(state: str) -> int:
    return {
        State.DONE.value: 0,
        State.AWAITING_ACCEPTANCE.value: 0,
        State.BLOCKED.value: 10,
        State.STOPPED.value: 11,
        State.FAILED.value: 12,
    }.get(state, 20)


def _recover(args: argparse.Namespace) -> int:
    store = _store(args.repo)
    state = store.load_state()
    if not state.get("resume_claim"):
        print("nightwatch: no ambiguous resume claim is present")
        return 0
    if not args.ack_ambiguous:
        print("nightwatch: refusing to clear an ambiguous claim without --ack-ambiguous", file=sys.stderr)
        return 10
    store.mutate("human_override", "user acknowledged ambiguous exact-thread recovery after manual review", lambda item: {**item, "resume_claim": None, "last_error": "human acknowledged ambiguous lease"})
    if store.load_state()["state"] == State.BLOCKED.value:
        store.transition(State.RECOVERING, "ambiguous_claim_acknowledged", "manual acknowledgement permits an exact-thread resume")
    print("nightwatch: ambiguous claim acknowledged; run `nightwatch resume` to issue one exact-thread continuation")
    return 0


def _install_signal_handlers(supervisor: Supervisor) -> None:
    def handle(_signum, _frame):
        supervisor.request_stop()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


def _status(args: argparse.Namespace) -> int:
    try:
        store = _store(args.repo)
        state = store.load_state()
        plan = store.load_plan()
    except (SystemExit, StateIntegrityError) as exc:
        if args.json:
            print(json.dumps({"state": "NO_RUN", "error": str(exc)}, indent=2))
            return 0
        print(f"Nightwatch: no trusted run ({exc})")
        return 0
    progress = plan_progress(plan)
    value = {"state": state, "plan": plan, "progress": progress}
    if args.json:
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    print("Nightwatch")
    print(f"STATE          {state['state']}")
    print(f"THREAD         {state.get('thread_id') or '(not captured)'}")
    print(f"RUN_ID         {state['run_id']}")
    print(f"GENERATION     {state['generation']}")
    print(f"QUOTA SOURCE   {state.get('quota_source') or '(none)'}")
    quality = "LIVE_APP_SERVER" if state.get("quota_source") == "live_app_server" else "GUARDED_PROBE_ONLY" if state.get("quota_source") in {"rollout_schedule_only", "unavailable"} else "(unknown)"
    print(f"RECOVERY MODE  {quality}")
    owner = state.get("supervisor_owner") or {}
    print(f"SUPERVISOR     {owner.get('pid') if isinstance(owner, dict) else '(none)'}")
    print(f"RESET          {state.get('next_resume_at') or '(none)'}")
    print(f"VERIFIED       {progress['verified_count']} / {progress['total_count']} milestones ({progress['verified_percent']}%)")
    print(f"IMPLEMENTED    {progress['implemented_count']} / {progress['total_count']} milestones ({progress['implemented_percent']}%)")
    blocked = sum(1 for item in plan["milestones"] if item.get("status") == "blocked")
    print(f"BLOCKED        {blocked}")
    if state.get("last_error"):
        print(f"LAST ERROR     {state['last_error']}")
    current = next((item for item in plan["milestones"] if item.get("status") in {"working", "implemented", "pending"}), None)
    if current:
        print(f"CURRENT        {current['id']} — {current['title']}")
    return 0


def _log(args: argparse.Namespace) -> int:
    store = _store(args.repo)
    if not store.log_path.exists():
        print("Nightwatch: no supervisor log")
        return 0
    lines = store.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-max(1, args.tail):]:
        print(line)
    return 0


def _report(args: argparse.Namespace) -> int:
    store = _store(args.repo)
    state = store.load_state()
    verification = state.get("last_verification")
    text = build_report(store, state, verification)
    path = store.write_report(text)
    print(text, end="")
    print(f"Report written: {path}", file=sys.stderr)
    return 0


def _stop(args: argparse.Namespace) -> int:
    store = _store(args.repo)
    state = store.load_state()
    if state["state"] in {State.DONE.value, State.STOPPED.value, State.AWAITING_ACCEPTANCE.value}:
        print(f"Nightwatch {state['state']}")
        return 0
    supervisor = Supervisor(store)
    supervisor.request_stop()
    supervisor.store.transition(State.STOPPED, "manual_stop", "user requested stop; no automatic recovery will continue")
    print("Nightwatch STOPPED; durable state preserved")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    binary = os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
    inhibitor = shutil.which("systemd-inhibit")
    inhibit_ok = False
    if inhibitor:
        try:
            probe = subprocess.run(
                [inhibitor, "--what=sleep", "--mode=block", "--why=Nightwatch probe", "/bin/true"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
            inhibit_ok = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            inhibit_ok = False
    report: dict[str, Any] = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "linux_first": sys.platform.startswith("linux"),
        "codex_binary": shutil.which(binary) or (binary if Path(binary).is_file() else None),
        "systemd_inhibit": inhibit_ok,
        "auth": {"status": "unknown"},
        "quota": {"status": "unknown"},
    }
    try:
        result = subprocess.run([binary, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=8, check=False)
        report["codex_version"] = result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        report["codex_version"] = None
    try:
        result = subprocess.run([binary, "login", "status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
        report["auth"] = {"status": "ok" if result.returncode == 0 else "fail", "credentials_read": False}
    except (OSError, subprocess.TimeoutExpired):
        report["auth"] = {"status": "fail", "credentials_read": False}
    try:
        quota = make_quota_provider().read()
        report["quota"] = {"status": "ok", "authority": "LIVE_APP_SERVER" if quota.source == "live_app_server" else "ROLLOUT_SCHEDULE_ONLY", "recovery_capability": "LIVE_REVALIDATION" if quota.source == "live_app_server" else "GUARDED_PROBE_ONLY", **quota.to_dict()}
    except Exception as exc:
        report["quota"] = {"status": "unavailable", "source": "none", "error": redact(str(exc)) or type(exc).__name__}
    try:
        root = _root(args.repo)
        report["git"] = snapshot(root).to_dict()
        report["nightwatch_state"] = (NightwatchStore(root).load_state().get("state") if NightwatchStore(root).exists() else "NO_RUN")
    except (SystemExit, StateIntegrityError, GitError) as exc:
        report["git"] = {"status": "unavailable", "error": str(exc)}
    report["status"] = "ok" if report["codex_binary"] and report["auth"]["status"] == "ok" else "fail"
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Nightwatch doctor: {report['status']}")
        print(f"Codex: {report.get('codex_version') or '(missing)'}")
        print(f"Auth: {report['auth']['status']}")
        print(f"Quota authority: {report['quota'].get('authority', 'unavailable')} ({report['quota'].get('source', 'none')})")
        if report["quota"].get("primary"):
            print(f"5h: {report['quota']['primary'].get('used_percent')}% used, reset={report['quota']['primary'].get('resets_at')}")
        if report["quota"].get("secondary"):
            print(f"weekly: {report['quota']['secondary'].get('used_percent')}% used, reset={report['quota']['secondary'].get('resets_at')}")
        print(f"systemd-inhibit: {'available' if report['systemd_inhibit'] else 'unavailable'}")
    return 0 if report["status"] == "ok" else 1


def _install_paths() -> tuple[Path, Path]:
    return (
        Path.home() / ".local" / "bin" / "nightwatch",
        Path.home() / ".config" / "systemd" / "user" / "nightwatch.service",
    )


def _service_text(service_root: Path) -> str:
    source_root = Path(__file__).resolve().parents[1]
    template = (source_root / "systemd" / "nightwatch.service").read_text(encoding="utf-8")
    # systemd parses ExecStart itself (not through a shell), so quote the repo
    # there as one argument. WorkingDirectory is a unit path setting: quotes
    # are literal for that setting, therefore it must remain an absolute raw
    # path.
    unit_root = _systemd_quote(str(service_root))
    directory_root = str(service_root).replace("%", "%%")
    rendered = template.replace(
        "ExecStart=%h/.local/bin/nightwatch resume",
        f"WorkingDirectory={directory_root}\nExecStart=%h/.local/bin/nightwatch resume --repo {unit_root}",
    )
    return "# nightwatch-install\n" + rendered


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _validate_install_targets(service_root: Path | None = None) -> None:
    launcher, service = _install_paths()
    if launcher.exists() or launcher.is_symlink():
        try:
            existing = launcher.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if "nightwatch-install:" not in existing:
            raise SystemExit(f"nightwatch: refusing to overwrite existing {launcher}")
    if service_root is None or not service.exists():
        return
    existing = service.read_text(encoding="utf-8", errors="replace")
    if "# nightwatch-install" not in existing:
        raise SystemExit(f"nightwatch: refusing to overwrite existing {service}")
    existing_root = next((line.removeprefix("WorkingDirectory=").strip('"') for line in existing.splitlines() if line.startswith("WorkingDirectory=")), None)
    if existing_root is not None and Path(existing_root.replace("%%", "%")).resolve() != service_root.resolve():
        raise SystemExit(
            f"nightwatch: existing user service is bound to {existing_root}; refusing to repoint it to {service_root}"
        )


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _backup_marked_install(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if "nightwatch-install" not in text:
        raise SystemExit(f"nightwatch: refusing to overwrite existing {path}")
    backup_root = Path.home() / ".local" / "state" / "codex-nightwatch" / "install-backups" / now_iso().replace(":", "-").replace(".", "-")
    backup_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(backup_root, 0o700)
    backup = backup_root / path.name
    _atomic_write(backup, text, 0o600)


def _install_user_files(service_root: Path | None = None) -> tuple[Path, Path | None]:
    _validate_install_targets(service_root)
    source_root = Path(__file__).resolve().parents[1]
    launcher, service = _install_paths()
    launcher_text = f"#!/bin/sh\n# nightwatch-install: {source_root}\nexec python3 {source_root / 'bin' / 'nightwatch'} \"$@\"\n"
    _backup_marked_install(launcher)
    _atomic_write(launcher, launcher_text, 0o755)
    if service_root is not None:
        _backup_marked_install(service)
        _atomic_write(service, _service_text(service_root), 0o600)
        return launcher, service
    return launcher, None


def _start_user_service() -> None:
    binary = shutil.which("systemctl")
    if not binary:
        raise RuntimeError("systemctl is not installed")
    for command in (
        [binary, "--user", "daemon-reload"],
        [binary, "--user", "enable", "--now", "nightwatch.service"],
    ):
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("systemctl --user is unavailable") from exc
        if result.returncode != 0:
            raise RuntimeError("systemctl --user could not contact or start the user service")


def _install(args: argparse.Namespace) -> int:
    service_root = _root(args.repo) if args.service else None
    launcher, service = _install_user_files(service_root)
    print(f"Installed {launcher}")
    if service is not None:
        print(f"Installed {service}")
        print("Enable it with: systemctl --user daemon-reload && systemctl --user enable --now nightwatch.service")
    return 0


def _uninstall(_args: argparse.Namespace) -> int:
    removed = []
    for path in _install_paths():
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "nightwatch-install" not in text:
            print(f"Preserved unrelated file: {path}", file=sys.stderr)
            continue
        path.unlink()
        removed.append(str(path))
    print("Uninstalled: " + (", ".join(removed) if removed else "nothing"))
    return 0


def _test(args: argparse.Namespace) -> int:
    if args.test_name == "app-server":
        try:
            quota = AppServerQuotaProvider().read()
            if quota.source != "live_app_server":
                print(f"REAL_APP_SERVER_RATE_LIMITS = FAIL ({quota.source})")
                return 20
            print("REAL_APP_SERVER_RATE_LIMITS = PASS")
            print(f"5h={quota.primary.used_percent if quota.primary else None}; weekly={quota.secondary.used_percent if quota.secondary else None}")
            return 0
        except Exception as exc:
            print(f"REAL_APP_SERVER_RATE_LIMITS = FAIL ({type(exc).__name__})")
            return 20
    if args.test_name != "quota-soak":
        print("Available non-destructive tests: nightwatch test quota-soak | nightwatch test app-server")
        return 0
    print("REAL_QUOTA_SOAK = PENDING_REAL_QUOTA_SOAK")
    print("No quota is intentionally consumed. A natural provider limit cycle can be recorded later.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return {
            "run": _run,
            "resume": _resume,
            "recover": _recover,
            "status": _status,
            "log": _log,
            "report": _report,
            "stop": _stop,
            "doctor": _doctor,
            "install": _install,
            "uninstall": _uninstall,
            "test": _test,
        }[args.command](args)
    except SupervisorAlreadyRunning:
        print("nightwatch: already supervised by another process; state was not changed", file=sys.stderr)
        return 0
    except StateIntegrityError as exc:
        print(f"nightwatch: FAIL CLOSED: {exc}", file=sys.stderr)
        return 20
    except SystemExit:
        raise
    except Exception as exc:
        print(f"nightwatch: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
