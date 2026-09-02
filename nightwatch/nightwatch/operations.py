from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git import GitError, repo_root, snapshot
from .account_broker import AccountBrokerError, AccountRecord, CodexAuthAdapter
from .models import (
    State,
    TERMINAL_STATES,
    validate_model_name,
    validate_reasoning_effort,
)
from .quota import make_quota_provider
from .storage import (
    NightwatchStore,
    StateIntegrityError,
    make_run_id,
    now_iso,
    redact,
    repo_identity,
)
from .supervisor import Supervisor, find_repo_codex_processes

MAX_GOAL_CHARS = 4_000
MAX_INSTRUCTION_CHARS = 4_000
MAX_VERIFY_COMMANDS = 16


def validate_human_text(value: str, name: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    clean = value.strip()
    if not clean:
        raise ValueError(f"{name} cannot be empty")
    if len(clean) > max_chars:
        raise ValueError(f"{name} exceeds maximum length ({len(clean)} > {max_chars})")
    if any(ord(char) < 32 and char not in "\n\t\r" for char in clean):
        raise ValueError(f"{name} contains forbidden control characters")
    return clean


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class RunSpec:
    repo: Path
    goal: str
    model: str | None = None
    reasoning_effort: str | None = None
    verify_commands: tuple[str, ...] = ()
    thread_id: str | None = None
    service: bool = True
    account_mode: str = "current-only"
    account_selectors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo", Path(self.repo).expanduser().resolve())
        object.__setattr__(self, "goal", validate_human_text(self.goal, "goal", MAX_INSTRUCTION_CHARS))
        if self.model is not None:
            object.__setattr__(self, "model", validate_model_name(self.model))
        if self.reasoning_effort is not None:
            object.__setattr__(self, "reasoning_effort", validate_reasoning_effort(self.reasoning_effort))
        commands = tuple(validate_human_text(item, "verification command", MAX_INSTRUCTION_CHARS) for item in self.verify_commands)
        object.__setattr__(self, "verify_commands", commands)
        if self.thread_id is not None:
            thread = validate_human_text(self.thread_id, "thread ID", 256)
            if "\n" in thread or "\t" in thread:
                raise ValueError("thread ID must be a single line")
            object.__setattr__(self, "thread_id", thread)
        mode = self.account_mode.replace("-", "_").upper()
        if mode not in {"CURRENT_ONLY", "AUTO_POOL"}:
            raise ValueError("account mode must be current-only or auto-pool")
        if mode == "AUTO_POOL" and not self.account_selectors:
            raise ValueError("auto-pool requires at least one explicitly selected account")
        if mode == "CURRENT_ONLY" and self.account_selectors:
            raise ValueError("--account selectors require --account-mode auto-pool")
        object.__setattr__(self, "account_mode", mode)
        selectors = tuple(validate_human_text(item, "account selector", 512) for item in self.account_selectors)
        if len(selectors) != len(set(selectors)):
            raise ValueError("account selectors must be unique")
        object.__setattr__(self, "account_selectors", selectors)


def service_name(service_root: Path) -> str:
    return f"nightwatch-{repo_identity(service_root)}.service"


def install_paths(service_root: Path | None = None) -> tuple[Path, Path]:
    return (
        Path.home() / ".local" / "bin" / "nightwatch",
        Path.home() / ".config" / "systemd" / "user" / (service_name(service_root) if service_root else "nightwatch.service"),
    )


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def service_text(service_root: Path) -> str:
    template = Path(__file__).with_name("nightwatch.service").read_text(encoding="utf-8")
    # systemd parses ExecStart itself (not through a shell), so quote the repo
    # there as one argument. WorkingDirectory is a unit path setting: quotes
    # are literal for that setting, therefore it must remain an absolute raw
    # path.
    unit_root = systemd_quote(str(service_root))
    directory_root = str(service_root).replace("%", "%%")
    rendered = template.replace(
        "ExecStart=%h/.local/bin/nightwatch resume",
        f"WorkingDirectory={directory_root}\nExecStart=%h/.local/bin/nightwatch resume --repo {unit_root}",
    )
    return "# nightwatch-install\n" + rendered


def launcher_is_reusable(text: str) -> bool:
    """Keep an existing nightwatch launcher: install.sh marker or pip console script."""
    return "nightwatch-install:" in text or "from nightwatch.cli import main" in text


def validate_install_targets(service_root: Path | None = None) -> None:
    launcher, service = install_paths(service_root)
    if launcher.exists() or launcher.is_symlink():
        try:
            existing = launcher.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if not launcher_is_reusable(existing):
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


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def backup_marked_install(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if "nightwatch-install" not in text:
        raise SystemExit(f"nightwatch: refusing to overwrite existing {path}")
    backup_root = Path.home() / ".local" / "state" / "codex-nightwatch" / "install-backups" / now_iso().replace(":", "-").replace(".", "-")
    backup_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(backup_root, 0o700)
    backup = backup_root / path.name
    atomic_write(backup, text, 0o600)


def install_user_files(service_root: Path | None = None) -> tuple[Path, Path | None]:
    validate_install_targets(service_root)
    source_root = Path(__file__).resolve().parents[1]
    launcher, service = install_paths(service_root)
    launcher_text = f"#!/bin/sh\n# nightwatch-install: {source_root}\nexec python3 {source_root / 'bin' / 'nightwatch'} \"$@\"\n"
    existing_launcher = ""
    if launcher.exists() or launcher.is_symlink():
        existing_launcher = launcher.read_text(encoding="utf-8", errors="replace")
    if existing_launcher and launcher_is_reusable(existing_launcher):
        pass
    else:
        if existing_launcher:
            backup_marked_install(launcher)
        atomic_write(launcher, launcher_text, 0o755)
    if service_root is not None:
        if service.exists() and "nightwatch-install" in service.read_text(encoding="utf-8", errors="replace"):
            backup_marked_install(service)
        elif service.exists():
            raise SystemExit(f"nightwatch: refusing to overwrite existing {service}")
        atomic_write(service, service_text(service_root), 0o600)
        return launcher, service
    return launcher, None


def start_user_service(service_unit_name: str = "nightwatch.service") -> None:
    binary = shutil.which("systemctl")
    if not binary:
        raise RuntimeError("systemctl is not installed")
    for command in (
        [binary, "--user", "daemon-reload"],
        [binary, "--user", "enable", "--now", service_unit_name],
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


def resume_service(repo: Path) -> ActionResult:
    root = repo_root(repo)
    live = find_repo_codex_processes(root)
    if live:
        pids = ", ".join(str(item["pid"]) for item in live)
        return ActionResult(False, f"Interactive Codex still running (PID {pids}); close it before /resume")
    try:
        validate_install_targets(root)
        _launcher, service = install_user_files(root)
        name = service_name(root)
        start_user_service(name)
        return ActionResult(True, f"Resume service started: {service.name if service else name}")
    except (RuntimeError, SystemExit, OSError) as exc:
        return ActionResult(False, f"Resume service was not started: {exc}")


def adopt_run(spec: RunSpec) -> ActionResult:
    bound = RunSpec(
        spec.repo,
        spec.goal,
        spec.model,
        spec.reasoning_effort,
        spec.verify_commands,
        spec.thread_id,
        service=False,
        account_mode=spec.account_mode,
        account_selectors=spec.account_selectors,
    )
    result = start_run(bound, run_in_service=False)
    if not result.ok:
        return result
    live = find_repo_codex_processes(bound.repo)
    suffix = " Use /resume to start unattended supervision."
    if live:
        pids = ", ".join(str(item["pid"]) for item in live)
        suffix = f" Interactive Codex still running (PID {pids}); /resume after it exits."
    return ActionResult(True, f"Adopted thread {bound.thread_id} as NEW.{suffix}")


def stop_run(repo: Path) -> ActionResult:
    root = repo_root(repo)
    store = NightwatchStore(root)
    if not store.exists():
        return ActionResult(False, f"No Nightwatch run found in {root}")
    state = store.load_state()
    if state["state"] in {State.DONE.value, State.STOPPED.value, State.AWAITING_ACCEPTANCE.value}:
        return ActionResult(True, f"Nightwatch {state['state']}")
    supervisor = Supervisor(store)
    supervisor.request_stop()
    supervisor.store.transition(State.STOPPED, "manual_stop", "user requested stop; no automatic recovery will continue")
    return ActionResult(True, "Nightwatch STOPPED; durable state preserved")


def queue_steer(store: NightwatchStore, instruction: str) -> ActionResult:
    text = validate_human_text(instruction, "instruction", MAX_INSTRUCTION_CHARS)
    state = store.load_state()
    current_state = state.get("state")
    if current_state in {item.value for item in TERMINAL_STATES}:
        return ActionResult(
            False,
            "Instruction was NOT queued because this Nightwatch run is terminal.\n"
            "Use /resume or start a new supervised run before steering.",
        )
    thread = state.get("thread_id")
    if not isinstance(thread, str) or not thread:
        return ActionResult(False, "No exact thread has been captured; steering was not sent.")
    binary = os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
    try:
        result = subprocess.run(
            [binary, "queue", "--thread", thread, "--message", text],
            cwd=store.repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ActionResult(False, "Codex queue transport was unavailable; no instruction was recorded as delivered.")
    if result.returncode != 0:
        return ActionResult(False, "Codex did not accept the queued instruction; inspect the exact thread state.")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    store.append_event(
        "user_instruction_queued",
        "confirmed user instruction queued to exact thread",
        {"instruction_sha256": digest, "instruction_chars": len(text)},
    )
    return ActionResult(True, f"Instruction queued to exact thread {thread}.")


_WORKTREE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def create_worktree(source_repo: str | Path, label: str) -> Path:
    source = repo_root(source_repo)
    if not _WORKTREE_LABEL.fullmatch(label):
        raise ValueError("worktree label must contain only letters, digits, '.', '_', or '-'")
    worktrees_root = source.parent / ".worktrees"
    layout = worktrees_root / source.name
    for candidate in (worktrees_root, layout):
        if candidate.is_symlink():
            raise ValueError(f"worktree layout must not contain symlinks: {candidate}")
    target = layout / label
    if target.exists() or target.is_symlink():
        raise ValueError(f"worktree target already exists: {target}")
    layout.mkdir(parents=True, exist_ok=True)
    if layout.resolve() != layout:
        raise ValueError("worktree layout resolved outside its confirmed path")
    branch = f"nightwatch/{label}"
    try:
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(target), "HEAD"],
            cwd=source,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError("git worktree add failed") from exc
    if result.returncode != 0:
        raise GitError("git worktree add failed; branch or target may already exist")
    return target.resolve()


def start_run(spec: RunSpec, run_in_service: bool = True) -> ActionResult:
    root = repo_root(spec.repo)
    store = NightwatchStore(root)
    if store.exists():
        try:
            state = store.load_state()
        except StateIntegrityError as exc:
            return ActionResult(False, f"Refusing to overwrite invalid durable state: {exc}")
        return ActionResult(False, f"A run already exists in {root} (state={state['state']}); use /resume")

    try:
        authorized = resolve_authorized_accounts(spec, root)
    except (AccountBrokerError, ValueError) as exc:
        return ActionResult(False, f"Account configuration rejected: {exc}")

    state = store.initialize(
        make_run_id(str(root)),
        spec.goal,
        str(root),
        verify_commands=list(spec.verify_commands),
        thread_id=spec.thread_id,
        model=spec.model,
        reasoning_effort=spec.reasoning_effort,
        account_mode=spec.account_mode,
        authorized_accounts=authorized,
    )
    if run_in_service:
        try:
            validate_install_targets(root)
            _launcher, service = install_user_files(root)
            start_user_service(service_name(root))
            return ActionResult(True, f"Nightwatch service started: run_id={state['run_id']} repo={root}")
        except (RuntimeError, SystemExit, OSError) as exc:
            return ActionResult(
                False,
                f"Goal saved as NEW, but user service could not be started; run `nightwatch resume --repo {root}`: {exc}",
            )
    return ActionResult(True, f"Nightwatch goal initialized: run_id={state['run_id']}")


def resolve_authorized_accounts(spec: RunSpec, root: Path) -> list[str]:
    """Resolve explicit human selectors to stable codex-auth account keys."""
    if spec.account_mode == "CURRENT_ONLY":
        return []
    adapter = CodexAuthAdapter()
    accounts = adapter.list_accounts()
    selected: list[str] = []
    for selector in spec.account_selectors:
        exact = [item for item in accounts if item.account_key == selector]
        matches = exact or [
            item for item in accounts
            if selector.casefold() in {value.casefold() for value in (item.alias, item.account_name) if value}
        ]
        if len(matches) != 1:
            raise ValueError(f"account selector {selector!r} did not resolve to exactly one stored account")
        key = matches[0].account_key
        if key not in selected:
            selected.append(key)
    if not selected:
        raise ValueError("auto-pool requires at least one explicitly selected account")
    return selected


def list_account_choices() -> list[AccountRecord]:
    """Return local account choices for human confirmation surfaces."""
    return CodexAuthAdapter().list_accounts()


def list_models() -> list[dict[str, Any]]:
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


def doctor_snapshot(repo: Path | None = None) -> dict[str, Any]:
    binary = os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
    inhibitor = shutil.which("systemd-inhibit")
    inhibit_ok = False
    if inhibitor:
        try:
            probe = subprocess.run(
                [inhibitor, "--what=sleep", "--mode=block", "--why=Nightwatch probe", "/bin/true"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
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
        "account_pool": {"status": "optional_unavailable", "count": 0},
    }
    try:
        accounts = CodexAuthAdapter().list_accounts()
        report["account_pool"] = {"status": "available", "count": len(accounts), "fingerprints": [item.fingerprint for item in accounts]}
    except AccountBrokerError as exc:
        report["account_pool"] = {"status": "optional_unavailable", "count": 0, "error": type(exc).__name__}
    try:
        result = subprocess.run(
            [binary, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
            check=False,
        )
        report["codex_version"] = result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        report["codex_version"] = None
    try:
        result = subprocess.run(
            [binary, "login", "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        report["auth"] = {"status": "ok" if result.returncode == 0 else "fail", "credentials_read": False}
    except (OSError, subprocess.TimeoutExpired):
        report["auth"] = {"status": "fail", "credentials_read": False}
    try:
        quota = make_quota_provider().read()
        report["quota"] = {
            "status": "ok",
            "authority": "LIVE_APP_SERVER" if quota.source == "live_app_server" else "ROLLOUT_SCHEDULE_ONLY",
            "recovery_capability": "LIVE_REVALIDATION" if quota.source == "live_app_server" else "GUARDED_PROBE_ONLY",
            **quota.to_dict(),
        }
    except Exception as exc:
        report["quota"] = {"status": "unavailable", "source": "none", "error": redact(str(exc)) or type(exc).__name__}
    if repo is not None:
        try:
            root = repo_root(repo)
            report["git"] = snapshot(root).to_dict()
            report["nightwatch_state"] = (
                NightwatchStore(root).load_state().get("state") if NightwatchStore(root).exists() else "NO_RUN"
            )
        except (SystemExit, StateIntegrityError, GitError) as exc:
            report["git"] = {"status": "unavailable", "error": str(exc)}
    report["status"] = "ok" if report["codex_binary"] and report["auth"]["status"] == "ok" else "fail"
    return report
