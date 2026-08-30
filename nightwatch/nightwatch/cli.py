from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .git import GitError, repo_root, snapshot
from .models import TERMINAL_STATES, State, plan_progress, validate_model_name, validate_reasoning_effort
from .quota import AppServerQuotaProvider, QuotaError, make_quota_provider
from .storage import NightwatchStore, StateIntegrityError, SupervisorAlreadyRunning, make_run_id, now_iso, redact
from .supervisor import PassiveWatcher, Supervisor, build_report, pid_alive, process_matches


def _model_arg(value: str) -> str:
    try:
        return validate_model_name(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _reasoning_arg(value: str) -> str:
    try:
        return validate_reasoning_effort(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _interval_arg(value: str) -> float:
    try:
        interval = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a number") from exc
    if not 0.2 <= interval <= 60:
        raise argparse.ArgumentTypeError("interval must be between 0.2 and 60 seconds")
    return interval


def _add_model_options(parser: argparse.ArgumentParser, *, takeover: bool = False) -> None:
    suffix = " for auto-takeover" if takeover else ""
    parser.add_argument("--model", type=_model_arg, default=None, help=f"Codex model slug{suffix}; use `nightwatch models` to list")
    parser.add_argument("--reasoning-effort", type=_reasoning_arg, default=None, help=f"Codex reasoning level{suffix}, such as low, medium, high, or xhigh")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nightwatch", description="Fail-closed unattended supervisor for one OpenAI Codex thread")
    parser.add_argument("--version", action="version", version=f"nightwatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a new supervised goal")
    run.add_argument("goal")
    run.add_argument("--repo", default=None)
    run.add_argument("--thread", default=None, help="adopt and bind an existing exact thread ID")
    run.add_argument("--no-inhibit", action="store_true", help="do not wrap the foreground supervisor in systemd-inhibit")
    run.add_argument("--service", action="store_true", help="persist the new goal, then start the repo-bound user systemd service")
    run.add_argument("--verify", action="append", default=[], metavar="COMMAND", help="trusted final verification command; frozen before Codex starts (repeatable)")
    _add_model_options(run)

    watch = sub.add_parser("watch", help="passively monitor an active Codex session in this repo")
    watch.add_argument("--repo", default=None)
    watch.add_argument("--thread", default=None, help="exact thread ID to select and watch")
    watch.add_argument("--json", action="store_true", help="output live snapshot as JSON")
    watch.add_argument("--once", action="store_true", help="inspect snapshot once and exit")
    watch.add_argument("--auto-takeover", action="store_true", help="automatically take over and supervise thread when quota runs out or session exits")
    watch.add_argument("--goal", default=None, help="goal description for auto-takeover")
    watch.add_argument("--verify", action="append", default=[], metavar="COMMAND", help="verification commands for auto-takeover")
    _add_model_options(watch, takeover=True)

    adopt = sub.add_parser("adopt", help="bind an existing exact thread into Nightwatch control plane")
    adopt.add_argument("--thread", required=True, help="exact thread ID to adopt")
    adopt.add_argument("goal", nargs="?", default="Supervise adopted conversation", help="goal description")
    adopt.add_argument("--repo", default=None)
    adopt.add_argument("--verify", action="append", default=[], metavar="COMMAND", help="trusted verification commands")
    _add_model_options(adopt)

    for name, help_text in (("status", "show current durable status"), ("log", "show human-readable supervisor log"), ("report", "write/show a durable report"), ("stop", "stop automatic work and preserve state"), ("resume", "resume the existing exact-thread goal")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--repo", default=None)
        if name == "status":
            cmd.add_argument("--json", action="store_true")
            cmd.add_argument("--watch", action="store_true", help="refresh until the run reaches a terminal state")
            cmd.add_argument("--interval", type=_interval_arg, default=2.0, metavar="SECONDS")
        if name == "log":
            cmd.add_argument("--tail", type=int, default=80)
        if name == "resume":
            cmd.add_argument("--no-inhibit", action="store_true", help="do not wrap the foreground supervisor in systemd-inhibit")

    doctor = sub.add_parser("doctor", help="check Linux, Codex, auth, quota, and local state support")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--repo", default=None)

    models = sub.add_parser("models", help="show the installed Codex model catalog and reasoning levels")
    models.add_argument("--json", action="store_true")

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
        if args.thread:
            child_args.extend(["--thread", args.thread])
        if args.model:
            child_args.extend(["--model", args.model])
        if args.reasoning_effort:
            child_args.extend(["--reasoning-effort", args.reasoning_effort])
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
    state = store.initialize(
        make_run_id(str(root)),
        args.goal,
        str(root),
        verify_commands=args.verify,
        thread_id=getattr(args, "thread", None),
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
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


def _last_provider_event(store: NightwatchStore, generation: int) -> dict[str, Any] | None:
    path = store.runs_path / f"generation-{generation}.events.jsonl"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        offset = max(0, info.st_size - 65_536)
        os.lseek(descriptor, offset, os.SEEK_SET)
        chunk = os.read(descriptor, 65_536).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)
    lines = chunk.splitlines()
    if offset and lines:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _agent_runtime(state: dict[str, Any]) -> dict[str, Any]:
    active = state.get("active_process")
    owner = state.get("supervisor_owner")
    owner_alive = (
        process_matches(owner)
        if isinstance(owner, dict) and owner.get("starttime") and owner.get("executable")
        else isinstance(owner, dict) and pid_alive(owner.get("pid"))
    )
    if isinstance(active, dict) and process_matches(active):
        return {"status": "RUNNING", "pid": active["pid"], "action": active.get("action")}
    if state["state"] == State.WAIT_QUOTA.value:
        status = "WAITING_QUOTA"
    elif state["state"] in {item.value for item in TERMINAL_STATES}:
        status = state["state"]
    elif owner_alive:
        status = f"SUPERVISOR_{state['state']}"
    else:
        status = "IDLE"
    return {
        "status": status,
        "pid": None,
        "action": None,
        "supervisor_pid": owner.get("pid") if owner_alive else None,
    }


def _status_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    try:
        store = _store(args.repo)
        state = store.load_state()
        plan = store.load_plan()
    except (SystemExit, StateIntegrityError) as exc:
        return {"state": "NO_RUN", "error": str(exc), "terminal": True}
    progress = plan_progress(plan)
    agent = _agent_runtime(state)
    return {
        "state": state,
        "agent": agent,
        "plan": plan,
        "progress": progress,
        "last_provider_event": _last_provider_event(store, state["generation"]),
        "terminal": state["state"] in {item.value for item in TERMINAL_STATES},
    }


def _progress_bar(percent: float, width: int = 24) -> str:
    filled = min(width, max(0, round(percent / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _render_status(value: dict[str, Any]) -> None:
    if value.get("state") == "NO_RUN":
        print(f"Nightwatch: no trusted run ({value.get('error')})")
        return
    state = value["state"]
    plan = value["plan"]
    progress = value["progress"]
    agent = value["agent"]
    print("Nightwatch")
    print(f"STATE          {state['state']}")
    agent_detail = f" pid={agent['pid']} action={agent.get('action') or '(unknown)'}" if agent.get("pid") else ""
    print(f"AGENT          {agent['status']}{agent_detail}")
    print(f"THREAD         {state.get('thread_id') or '(not captured)'}")
    print(f"MODEL          {state.get('model') or '(Codex default)'}")
    print(f"REASONING      {state.get('reasoning_effort') or '(Codex default)'}")
    print(f"RUN_ID         {state['run_id']}")
    print(f"GENERATION     {state['generation']}")
    print(f"QUOTA SOURCE   {state.get('quota_source') or '(none)'}")
    quality = "LIVE_APP_SERVER" if state.get("quota_source") == "live_app_server" else "GUARDED_PROBE_ONLY" if state.get("quota_source") in {"rollout_schedule_only", "unavailable"} else "(unknown)"
    print(f"RECOVERY MODE  {quality}")
    quota = state.get("quota") or {}
    for key, fallback in (("primary", "5h"), ("secondary", "weekly")):
        window = quota.get(key) if isinstance(quota, dict) else None
        if isinstance(window, dict):
            label = str(window.get("name") or fallback).upper()
            print(f"QUOTA {label:<7} {window.get('used_percent', '(unknown)')}% used; reset={window.get('resets_at') or '(unknown)'}")
    print(f"SUPERVISOR     {agent.get('supervisor_pid') or '(none)'}")
    print(f"RESET          {state.get('next_resume_at') or '(none)'}")
    print(f"PROGRESS       {_progress_bar(progress['verified_percent'])} {progress['verified_percent']}% verified")
    print(f"VERIFIED       {progress['verified_count']} / {progress['total_count']} milestones")
    print(f"IMPLEMENTED    {progress['implemented_count']} / {progress['total_count']} milestones ({progress['implemented_percent']}%)")
    blocked = sum(1 for item in plan["milestones"] if item.get("status") == "blocked")
    print(f"BLOCKED        {blocked}")
    if state.get("last_error"):
        print(f"LAST ERROR     {state['last_error']}")
    current = next((item for item in plan["milestones"] if item.get("status") in {"working", "implemented", "pending"}), None)
    if current:
        print(f"CURRENT        {current['id']} — {current['title']}")
    print("MILESTONES")
    markers = {"pending": " ", "working": ">", "implemented": "+", "verified": "x", "blocked": "!"}
    for item in plan["milestones"]:
        status_value = item.get("status", "pending")
        print(f"  [{markers.get(status_value, '?')}] {item['id']} {item['title']} ({status_value})")
    event = value.get("last_provider_event") or {}
    if event:
        print(f"LAST EVENT     {event.get('type', '(unknown)')} ({event.get('action') or event.get('status') or 'provider'})")
    print(f"UPDATED        {state.get('updated_at') or '(unknown)'}")


def _status(args: argparse.Namespace) -> int:
    first = True
    try:
        while True:
            value = _status_snapshot(args)
            if args.json:
                if args.watch:
                    print(json.dumps(value, ensure_ascii=False), flush=True)
                else:
                    print(json.dumps(value, indent=2, ensure_ascii=False))
            else:
                if args.watch and not first and sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                _render_status(value)
            if not args.watch or value.get("terminal"):
                return 0
            first = False
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.json:
            print("\nNightwatch status watch stopped.")
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


def _model_catalog() -> list[dict[str, Any]]:
    binary = os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
    try:
        result = subprocess.run(
            [binary, "debug", "models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("installed Codex model catalog is unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("installed Codex model catalog command failed")
    try:
        raw_models = json.loads(result.stdout).get("models")
    except (AttributeError, json.JSONDecodeError) as exc:
        raise RuntimeError("installed Codex returned an invalid model catalog") from exc
    if not isinstance(raw_models, list):
        raise RuntimeError("installed Codex returned an invalid model catalog")
    catalog: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict) or item.get("visibility") not in {None, "list"}:
            continue
        try:
            slug = validate_model_name(item.get("slug"))
        except (TypeError, ValueError):
            continue
        default = item.get("default_reasoning_level")
        try:
            default = validate_reasoning_effort(default) if default is not None else None
        except (TypeError, ValueError):
            default = None
        levels: list[str] = []
        for level in item.get("supported_reasoning_levels") or []:
            candidate = level.get("effort") if isinstance(level, dict) else None
            try:
                candidate = validate_reasoning_effort(candidate)
            except (TypeError, ValueError):
                continue
            if candidate not in levels:
                levels.append(candidate)
        display_name = item.get("display_name")
        if not isinstance(display_name, str):
            display_name = slug
        display_name = " ".join(display_name.split())[:80] or slug
        catalog.append(
            {
                "slug": slug,
                "display_name": display_name,
                "default_reasoning_level": default,
                "supported_reasoning_levels": levels,
            }
        )
    if not catalog:
        raise RuntimeError("installed Codex model catalog contains no visible models")
    return catalog


def _models(args: argparse.Namespace) -> int:
    catalog = _model_catalog()
    if args.json:
        print(json.dumps({"models": catalog}, indent=2, ensure_ascii=False))
        return 0
    print("Installed Codex models (live local catalog)")
    for item in catalog:
        levels = ",".join(item["supported_reasoning_levels"]) or "(not reported)"
        default = item["default_reasoning_level"] or "(not reported)"
        print(f"{item['slug']:<24} default={default:<8} levels={levels}")
    return 0


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


def _adopt(args: argparse.Namespace) -> int:
    root = _root(args.repo)
    store = NightwatchStore(root)
    if store.exists():
        state = store.load_state()
        raise SystemExit(f"nightwatch: a run already exists in {root} (state={state['state']}, thread={state.get('thread_id')})")
    state = store.initialize(
        make_run_id(str(root)),
        args.goal or "Adopted conversation",
        str(root),
        verify_commands=args.verify,
        thread_id=args.thread,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    print(f"Nightwatch: adopted thread {args.thread} for repo {root} (run_id={state['run_id']})")
    print("Run `nightwatch resume` to start unattended supervision.")
    return 0


def _print_ambiguous_sessions(sessions: list[dict[str, Any]], root: Path) -> None:
    print("=" * 60)
    print("AMBIGUOUS_ACTIVE_SESSIONS")
    print(f"Multiple active Codex sessions found for {root}:")
    for s in sessions:
        title = str(s.get("title") or "")[:40]
        title_str = f" | Title: {title}" if title else ""
        print(f"  - PID {s.get('pid')} | Thread: {s.get('thread_id')} | Model: {s.get('model') or '(unknown)'} | Branch: {s.get('branch') or '(unknown)'}{title_str}")
    print("Please specify --thread <EXACT_THREAD_ID> to select one session.")
    print("=" * 60)


def _print_watch_snapshot(snap: dict[str, Any], root: Path) -> None:
    print("=" * 60)
    print(f"REPO         {root}")
    print(f"THREAD ID    {snap.get('thread_id') or '(none)'}")
    print(f"PROCESS      PID {snap.get('pid') or '(none)'} ({'ALIVE' if snap.get('pid_alive') else 'NOT RUNNING / EXITED'})")
    if snap.get("takeover_pending"):
        print(f"TAKEOVER     TAKEOVER_PENDING (waiting for interactive process to exit)")
    print(f"MODEL        {snap.get('model') or '(unknown)'} [branch: {snap.get('branch') or '(unknown)'}]")
    if snap.get("title"):
        print(f"GOAL/TITLE   {str(snap['title'])[:80]}")
    limits = snap.get("rate_limits") or {}
    p = limits.get("primary") or {}
    s = limits.get("secondary") or {}
    if p:
        p_pct = p.get('used_percent', 0)
        p_reset = p.get('resets_at')
        reset_str = f", reset={p_reset}" if p_reset else ""
        print(f"QUOTA 5H     {p_pct}% used{reset_str}")
    if s:
        s_pct = s.get('used_percent', 0)
        print(f"QUOTA WEEKLY {s_pct}% used")
    tokens = snap.get("tokens") or {}
    if tokens:
        print(f"TOKENS       total={tokens.get('total_tokens', 0):,}, input={tokens.get('input_tokens', 0):,}, output={tokens.get('output_tokens', 0):,}")
    subs = snap.get("subagents") or []
    if subs:
        sub_str = ", ".join(f"{name} ({sid[:8]}...)" for sid, name in subs)
        print(f"SUBAGENTS    {sub_str}")
    print("=" * 60)


def _watch(args: argparse.Namespace) -> int:
    root = _root(args.repo)
    store = NightwatchStore(root)
    watcher = PassiveWatcher(store, explicit_thread=args.thread)

    if args.once:
        snapshot = watcher.inspect_live_snapshot()
        if args.json:
            print(json.dumps(snapshot, indent=2, ensure_ascii=False))
            return 0 if snapshot.get("status") == "OK" else 1
        if snapshot.get("status") == "AMBIGUOUS_ACTIVE_SESSIONS":
            _print_ambiguous_sessions(snapshot.get("sessions", []), root)
            return 1
        if not snapshot.get("active"):
            print(f"Nightwatch watch: no active Codex session found for {root}")
            if snapshot.get("pid"):
                print(f"PID {snapshot['pid']} running but no proven rollout file located")
            return 1
        _print_watch_snapshot(snapshot, root)
        return 0

    init_snapshot = watcher.inspect_live_snapshot()
    if init_snapshot.get("status") == "AMBIGUOUS_ACTIVE_SESSIONS":
        if args.json:
            print(json.dumps(init_snapshot, ensure_ascii=False))
        else:
            _print_ambiguous_sessions(init_snapshot.get("sessions", []), root)
        return 1

    print(f"Nightwatch watch: passively monitoring {root} (Ctrl+C to stop)...")
    try:
        def on_update(snap: dict[str, Any]) -> None:
            if args.json:
                print(json.dumps(snap, ensure_ascii=False))
            elif snap.get("status") == "AMBIGUOUS_ACTIVE_SESSIONS":
                _print_ambiguous_sessions(snap.get("sessions", []), root)
            else:
                _print_watch_snapshot(snap, root)

        watcher.watch(
            on_update=on_update,
            poll_interval=2.0,
            auto_takeover=args.auto_takeover,
            goal=args.goal,
            verify_commands=args.verify,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
    except KeyboardInterrupt:
        print("\nNightwatch watch stopped.")
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
            "models": _models,
            "install": _install,
            "uninstall": _uninstall,
            "test": _test,
            "watch": _watch,
            "adopt": _adopt,
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
