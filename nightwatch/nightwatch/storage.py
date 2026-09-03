from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import fcntl

from . import __version__
from .models import State, empty_state, validate_plan, validate_state
from .testing import crash_hook


class StateIntegrityError(RuntimeError):
    """The durable record cannot be trusted; callers must fail closed."""


class SupervisorAlreadyRunning(RuntimeError):
    """Another process owns the lifetime supervisor lease."""


MAX_EVENT_BYTES = 1_000_000
DEFAULT_MAX_SEGMENT_BYTES = 500_000
DEFAULT_MAX_SEGMENT_EVENTS = 2_000



@dataclass(frozen=True)
class TrustedRunHome:
    path: Path
    root: Path
    repo_dir_name: str
    runtime_identity: tuple[int, int]
    home_identity: tuple[int, int]

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __truediv__(self, other: Any) -> Path:
        return self.path / other

    def verify(self) -> None:
        """Verify the entire directory chain using O_NOFOLLOW descriptors without mutating paths."""
        verify_trusted_codex_home_chain(
            self.root,
            self.repo_dir_name,
            expected_runtime_identity=self.runtime_identity,
            expected_home_identity=self.home_identity,
        )


def _open_dir_descriptor(
    name_or_path: str | Path,
    dir_fd: int | None = None,
    mode: int = 0o700,
    create: bool = False,
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

    try:
        if dir_fd is not None:
            st_pre = os.stat(name_or_path, dir_fd=dir_fd, follow_symlinks=False)
        else:
            st_pre = os.lstat(name_or_path)
    except FileNotFoundError:
        st_pre = None
    except OSError as exc:
        raise StateIntegrityError(f"cannot inspect entry {name_or_path}") from exc

    if st_pre is not None:
        if stat.S_ISLNK(st_pre.st_mode):
            raise StateIntegrityError(f"entry {name_or_path} must be a real directory and not a symlink")
        if not stat.S_ISDIR(st_pre.st_mode):
            raise StateIntegrityError(f"entry {name_or_path} must be a directory")

    if create and st_pre is None:
        try:
            if dir_fd is not None:
                os.mkdir(name_or_path, mode=mode, dir_fd=dir_fd)
            else:
                target_path = Path(name_or_path)
                if not target_path.parent.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True, mode=mode)
                os.mkdir(name_or_path, mode=mode)
        except FileExistsError:
            pass
        except OSError as exc:
            raise StateIntegrityError(f"cannot create directory {name_or_path}") from exc

    try:
        if dir_fd is not None:
            fd = os.open(name_or_path, flags, dir_fd=dir_fd)
        else:
            fd = os.open(name_or_path, flags)
    except OSError as exc:
        raise StateIntegrityError(f"cannot securely open directory {name_or_path}: must be real directory and not a symlink") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            os.close(fd)
            raise StateIntegrityError(f"descriptor {name_or_path} is not a directory")
        if create:
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
        return fd, (st.st_dev, st.st_ino)
    except Exception:
        os.close(fd)
        raise


def establish_trusted_codex_home(
    root: Path,
    repo_directory_name: str,
    create: bool = True,
    expected_runtime_identity: tuple[int, int] | None = None,
    expected_home_identity: tuple[int, int] | None = None,
) -> tuple[Path, tuple[int, int], tuple[int, int]]:
    root_fd: int | None = None
    dir_fd: int | None = None
    runtime_fd: int | None = None
    home_fd: int | None = None
    try:
        root_fd, root_id = _open_dir_descriptor(root, create=create)
        dir_fd, dir_id = _open_dir_descriptor(repo_directory_name, dir_fd=root_fd, create=create)
        runtime_fd, runtime_id = _open_dir_descriptor("codex-runtime", dir_fd=dir_fd, create=create)
        if expected_runtime_identity is not None and runtime_id != expected_runtime_identity:
            raise StateIntegrityError("codex-runtime directory inode was replaced")
        home_fd, home_id = _open_dir_descriptor("codex-home", dir_fd=runtime_fd, create=create)
        if expected_home_identity is not None and home_id != expected_home_identity:
            raise StateIntegrityError("codex-home directory inode was replaced")
        home_path = root / repo_directory_name / "codex-runtime" / "codex-home"
        return home_path, runtime_id, home_id
    finally:
        for fd in (home_fd, runtime_fd, dir_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def verify_trusted_codex_home_chain(
    root: Path,
    repo_directory_name: str,
    expected_runtime_identity: tuple[int, int],
    expected_home_identity: tuple[int, int],
) -> tuple[Path, tuple[int, int], tuple[int, int]]:
    return establish_trusted_codex_home(
        root,
        repo_directory_name,
        create=False,
        expected_runtime_identity=expected_runtime_identity,
        expected_home_identity=expected_home_identity,
    )



ALLOWED_TRANSITIONS: dict[State, set[State]] = {
    State.NEW: {State.NEW, State.PREFLIGHT, State.STOPPED, State.FAILED, State.BLOCKED},
    State.PREFLIGHT: {State.PREFLIGHT, State.RUNNING, State.WAIT_QUOTA, State.FAILED, State.BLOCKED, State.STOPPED},
    State.RUNNING: {State.RUNNING, State.WAIT_QUOTA, State.RETRY_BACKOFF, State.RECOVERING, State.VERIFYING, State.BLOCKED, State.FAILED, State.STOPPED},
    State.WAIT_QUOTA: {State.WAIT_QUOTA, State.RECOVERING, State.BLOCKED, State.STOPPED, State.FAILED},
    State.RETRY_BACKOFF: {State.RETRY_BACKOFF, State.RECOVERING, State.RUNNING, State.BLOCKED, State.FAILED, State.STOPPED},
    State.RECOVERING: {State.RECOVERING, State.RUNNING, State.WAIT_QUOTA, State.RETRY_BACKOFF, State.VERIFYING, State.BLOCKED, State.FAILED, State.STOPPED},
    State.VERIFYING: {State.VERIFYING, State.DONE, State.AWAITING_ACCEPTANCE, State.RUNNING, State.BLOCKED, State.FAILED, State.STOPPED},
    State.AWAITING_ACCEPTANCE: {State.AWAITING_ACCEPTANCE, State.RECOVERING, State.STOPPED},
    State.BLOCKED: {State.BLOCKED, State.RECOVERING, State.STOPPED},
    State.STOPPED: {State.STOPPED, State.RECOVERING, State.RUNNING},
    State.DONE: {State.DONE},
    State.FAILED: {State.FAILED, State.RECOVERING, State.RUNNING},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact(value: Any) -> Any:
    """Redact common credentials before anything reaches a log or event."""
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if re.search(r"(?:token|secret|password|api[_-]?key|authorization|cookie|credential)", str(key), re.I) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        text = re.sub(r"(?i)\b(?:sk|sess|chatcmpl|ghp|github_pat|xox[baprs])[-_A-Za-z0-9.]{12,}", "[REDACTED]", text)
        text = re.sub(r"(?i)(?:aws_access_key_id|aws_secret_access_key|api[_-]?key|access[_-]?token|authorization|cookie)\s*[=:]\s*\S+", "[REDACTED]", text)
        text = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
        return text[:4000]
    return value


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "nightwatch"


def control_plane_root() -> Path:
    base = os.environ.get("NIGHTWATCH_STATE_HOME")
    if base:
        return Path(base).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state") / "codex-nightwatch"


def _git_identity(repo: Path) -> str:
    try:
        result = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def repo_identity(repo: str | Path) -> str:
    resolved = Path(repo).resolve()
    digest = hashlib.sha256((str(resolved) + "\0" + _git_identity(resolved)).encode("utf-8")).hexdigest()[:16]
    return f"{safe_slug(resolved.name)}-{digest}"


def policy_hash(policy: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def sufficient_verification_policy(commands: list[str]) -> bool:
    """A diff-only check cannot accept an arbitrary natural-language goal."""
    return any(command.strip() != "git diff --check" for command in commands)


class NightwatchStore:
    """Trusted control plane. The repository mailbox is deliberately separate."""

    MAILBOX_RUN_OUTPUTS = ("proposed-plan.json", "progress.json", "blocker.json")

    def __init__(self, repo: str | Path, state_home: str | Path | None = None):
        self.repo = Path(repo).resolve()
        self.repo_id = repo_identity(self.repo)
        requested_root = Path(state_home).expanduser() if state_home else control_plane_root()
        self.root = requested_root.resolve()
        self._assert_control_plane_outside_workspace()
        self.directory = self.root / self.repo_id
        if self.directory.resolve() != self.directory:
            raise StateIntegrityError("trusted control plane directory must not be a symlink")
        self._assert_path_outside_workspace(self.directory.resolve())
        self.legacy_directory = self.repo / ".nightwatch"
        self.mailbox_directory = self.repo / ".nightwatch-agent"
        self.state_path = self.directory / "state.json"
        self.goal_path = self.directory / "goal.md"
        self.plan_path = self.directory / "plan.json"
        self.policy_path = self.directory / "verification-policy.json"
        self.acceptance_path = self.directory / "acceptance.json"
        self.metadata_path = self.directory / "metadata.json"
        self.checkpoint_path = self.directory / "checkpoint.md"
        self.legacy_events_path = self.directory / "events.jsonl"
        self.events_dir = self.directory / "events"
        self.events_manifest_path = self.events_dir / "manifest.json"
        self.log_path = self.directory / "supervisor.log"
        self.runs_path = self.directory / "runs"
        self.reports_path = self.directory / "reports"
        self.lock_path = self.directory / "state.lock"
        self.supervisor_lock_path = self.directory / "supervisor.lock"
        self.codex_runtime_path = self.directory / "codex-runtime"
        self.codex_home_path = self.codex_runtime_path / "codex-home"
        self._codex_runtime_identity: tuple[int, int] | None = None
        self._codex_home_identity: tuple[int, int] | None = None

    @property
    def events_path(self) -> Path:
        if self._has_legacy_events_only():
            return self.legacy_events_path
        return self._active_segment_path()

    @property
    def codex_home(self) -> Path:
        return self.codex_home_path

    @property
    def trusted_codex_home(self) -> TrustedRunHome:
        if self._codex_runtime_identity is None or self._codex_home_identity is None:
            self.ensure_codex_home()
        return TrustedRunHome(
            self.codex_home_path,
            self.root,
            self.directory.name,
            self._codex_runtime_identity,
            self._codex_home_identity,
        )

    def ensure_codex_home(self) -> Path:
        self._assert_control_plane_outside_workspace()
        self._assert_path_outside_workspace(self.directory)
        expected_runtime_id: tuple[int, int] | None = self._codex_runtime_identity
        expected_home_id: tuple[int, int] | None = self._codex_home_identity
        if (expected_runtime_id is None or expected_home_id is None) and self.exists():
            state = self.load_state()
            raw_runtime = state.get("codex_runtime_identity")
            raw_home = state.get("codex_home_identity")
            if raw_runtime is not None or raw_home is not None:
                if raw_runtime is None or raw_home is None:
                    raise StateIntegrityError("both codex_runtime_identity and codex_home_identity must be present in trusted state")
                if (
                    not isinstance(raw_runtime, list)
                    or len(raw_runtime) != 2
                    or type(raw_runtime[0]) is not int
                    or type(raw_runtime[1]) is not int
                    or isinstance(raw_runtime[0], bool)
                    or isinstance(raw_runtime[1], bool)
                    or raw_runtime[0] <= 0
                    or raw_runtime[1] <= 0
                    or not isinstance(raw_home, list)
                    or len(raw_home) != 2
                    or type(raw_home[0]) is not int
                    or type(raw_home[1]) is not int
                    or isinstance(raw_home[0], bool)
                    or isinstance(raw_home[1], bool)
                    or raw_home[0] <= 0
                    or raw_home[1] <= 0
                ):
                    raise StateIntegrityError("malformed codex home identity in trusted state")
                expected_runtime_id = (raw_runtime[0], raw_runtime[1])
                expected_home_id = (raw_home[0], raw_home[1])
            else:
                if self.codex_runtime_path.exists() or self.codex_home_path.exists():
                    if state.get("legacy_pre_identity_migration") is True:
                        pass
                    else:
                        raise StateIntegrityError(
                            "persistent codex-home exists but durable identity is absent in trusted state; failing closed"
                        )
        _, runtime_id, home_id = establish_trusted_codex_home(
            self.root,
            self.directory.name,
            create=True,
            expected_runtime_identity=expected_runtime_id,
            expected_home_identity=expected_home_id,
        )
        self._codex_runtime_identity = runtime_id
        self._codex_home_identity = home_id
        return self.codex_home_path

    def verify_codex_home(self) -> None:
        self._assert_control_plane_outside_workspace()
        self._assert_path_outside_workspace(self.directory)
        expected_runtime_id = self._codex_runtime_identity
        expected_home_id = self._codex_home_identity
        if (expected_runtime_id is None or expected_home_id is None) and self.exists():
            state = self.load_state()
            raw_runtime = state.get("codex_runtime_identity")
            raw_home = state.get("codex_home_identity")
            if raw_runtime is None or raw_home is None:
                raise StateIntegrityError("cannot verify codex home: missing durable identity in trusted state")
            if (
                not isinstance(raw_runtime, list)
                or len(raw_runtime) != 2
                or type(raw_runtime[0]) is not int
                or type(raw_runtime[1]) is not int
                or isinstance(raw_runtime[0], bool)
                or isinstance(raw_runtime[1], bool)
                or raw_runtime[0] <= 0
                or raw_runtime[1] <= 0
                or not isinstance(raw_home, list)
                or len(raw_home) != 2
                or type(raw_home[0]) is not int
                or type(raw_home[1]) is not int
                or isinstance(raw_home[0], bool)
                or isinstance(raw_home[1], bool)
                or raw_home[0] <= 0
                or raw_home[1] <= 0
            ):
                raise StateIntegrityError("malformed codex home identity in trusted state")
            expected_runtime_id = (raw_runtime[0], raw_runtime[1])
            expected_home_id = (raw_home[0], raw_home[1])
        if expected_runtime_id is None or expected_home_id is None:
            raise StateIntegrityError("cannot verify codex home without established identities")
        verify_trusted_codex_home_chain(
            self.root,
            self.directory.name,
            expected_runtime_identity=expected_runtime_id,
            expected_home_identity=expected_home_id,
        )

    def exists(self) -> bool:
        return self.state_path.exists()

    @contextmanager
    def locked(self) -> Iterator[None]:
        self._ensure_directory()
        handle = self.lock_path.open("a+")
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextmanager
    def supervisor_lease(self) -> Iterator[None]:
        self._ensure_directory()
        handle = self.supervisor_lock_path.open("a+")
        try:
            os.chmod(self.supervisor_lock_path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SupervisorAlreadyRunning("another Nightwatch supervisor owns this run") from exc
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def initialize(
        self,
        run_id: str,
        goal: str,
        repo: str,
        timestamp: str | None = None,
        verify_commands: list[str] | None = None,
        thread_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        account_mode: str = "CURRENT_ONLY",
        authorized_accounts: list[str] | None = None,
        provider: str = "codex",
    ) -> dict[str, Any]:
        timestamp = timestamp or now_iso()
        commands = list(verify_commands or [])
        policy_core = {"schema_version": 1, "source": "cli", "final_commands": commands}
        policy = {**policy_core, "policy_hash": policy_hash(policy_core)}
        acceptance = {"schema_version": 1, "goal_hash": hashlib.sha256(goal.encode("utf-8")).hexdigest(), "verification_policy_hash": policy["policy_hash"], "required_final_checks": commands, "plan_minimum": {"milestones": 1}, "baseline_repo": str(self.repo), "created_at": timestamp}
        state = empty_state(
            run_id,
            goal,
            str(Path(repo).resolve()),
            self.repo_id,
            timestamp,
            model=model,
            reasoning_effort=reasoning_effort,
            provider=provider,
        )
        state["acceptance_ready"] = sufficient_verification_policy(commands)
        if account_mode not in {"CURRENT_ONLY", "AUTO_POOL"}:
            raise StateIntegrityError(f"unsupported account mode: {account_mode}")
        if provider == "agy" and account_mode == "AUTO_POOL":
            raise StateIntegrityError("AGY provider does not support AUTO_POOL")
        state["account_mode"] = account_mode
        state["authorized_accounts"] = list(authorized_accounts or [])
        if account_mode == "AUTO_POOL":
            from .models import cross_account_thread_mode_for_version
            thread_mode = cross_account_thread_mode_for_version(None)
            state["cross_account_thread_mode"] = thread_mode
            state["cross_account_thread_capability"] = {
                "codex_version": None,
                "mode": thread_mode,
            }
        if thread_id:
            state["thread_id"] = thread_id
        profile = "default" if commands else "none"
        plan = {"schema_version": 2, "authority": "nightwatch", "policy_hash": policy["policy_hash"], "milestones": [{"id": "M1", "title": "Complete the goal", "weight": 100, "required": True, "status": "pending", "verification_profile": profile, "evidence": []}]}
        metadata = {"schema_version": 2, "repo_id": self.repo_id, "repo_path": str(self.repo), "repo_remote": _git_identity(self.repo), "created_at": timestamp}
        validate_state(state)
        validate_plan(plan)
        with self.locked():
            if self.state_path.exists():
                raise StateIntegrityError("a Nightwatch run already exists in this repository")
            mailbox_fd = self._open_mailbox_directory(create=True)
            try:
                self.runs_path.mkdir(mode=0o700)
                self.reports_path.mkdir(mode=0o700)
                self.events_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if provider == "codex":
                    self.ensure_codex_home()
                    if self._codex_runtime_identity is not None:
                        state["codex_runtime_identity"] = list(self._codex_runtime_identity)
                    if self._codex_home_identity is not None:
                        state["codex_home_identity"] = list(self._codex_home_identity)
                state["initialized"] = True
                validate_state(state)
                self._clear_mailbox_run_outputs_at(mailbox_fd)
                self._write_json_at(mailbox_fd, "context.json", {"goal_hash": acceptance["goal_hash"], "mailbox_contract": "untrusted-input-only"})
            finally:
                os.close(mailbox_fd)
            self._write_text(self.goal_path, goal.rstrip() + "\n")
            self._write_json(self.policy_path, policy)
            self._write_json(self.acceptance_path, acceptance)
            self._write_json(self.metadata_path, metadata)
            self._write_json(self.plan_path, plan)
            self._write_text(self.checkpoint_path, f"# Nightwatch checkpoint\n\nCreated: {timestamp}\n")
            self._write_json(self.state_path, state)
            self._append_event_unlocked(state, "run_created", "trusted control plane initialized")
            self._log_unlocked("NEW: trusted control plane initialized")
        return state

    def load_state(self) -> dict[str, Any]:
        """
        Load state with bounded event-frontier validation.

        Complexity contract:
        - Manifest & segment structural check: O(number_of_segments) metadata validations via os.lstat.
        - Historical segment content reads: strictly O(1); sealed segment bodies are never parsed.
        - Active segment tail read: reads only the final record (or single-event orphan during crash recovery).
        """
        try:
            state = self._read_json_regular(self.state_path)
            if "provider" not in state:
                state["provider"] = "codex"
            validate_state(state)
            if state.get("repo") != str(self.repo) or state.get("repo_id") != self.repo_id:
                raise StateIntegrityError(
                    f"trusted state does not belong to this repository identity: "
                    f"state_repo={state.get('repo')!r} self_repo={str(self.repo)!r} "
                    f"state_repo_id={state.get('repo_id')!r} self_repo_id={self.repo_id!r}"
                )
            self._validate_event_frontier()
            return state
        except FileNotFoundError as exc:
            raise StateIntegrityError("trusted state.json is missing") from exc
        except StateIntegrityError:
            raise
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise StateIntegrityError("trusted state.json is unreadable or invalid") from exc

    def load_plan(self) -> dict[str, Any]:
        try:
            plan = self._read_json_regular(self.plan_path)
            validate_plan(plan)
            if plan.get("policy_hash") != self.load_policy().get("policy_hash"):
                raise StateIntegrityError("plan is not bound to the frozen verification policy")
            return plan
        except FileNotFoundError as exc:
            raise StateIntegrityError("trusted plan.json is missing") from exc
        except StateIntegrityError:
            raise
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise StateIntegrityError("trusted plan.json is unreadable or invalid") from exc

    def load_policy(self) -> dict[str, Any]:
        try:
            policy = self._read_json_regular(self.policy_path)
            core = {"schema_version": policy.get("schema_version"), "source": policy.get("source"), "final_commands": policy.get("final_commands")}
            if policy.get("schema_version") != 1 or not isinstance(policy.get("final_commands"), list) or not all(isinstance(item, str) and item.strip() for item in policy["final_commands"]) or policy.get("policy_hash") != policy_hash(core):
                raise StateIntegrityError("frozen verification policy is invalid")
            return policy
        except FileNotFoundError as exc:
            raise StateIntegrityError("frozen verification policy is missing") from exc
        except StateIntegrityError:
            raise
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise StateIntegrityError("frozen verification policy is unreadable") from exc

    def load_acceptance(self) -> dict[str, Any]:
        try:
            value = self._read_json_regular(self.acceptance_path)
            if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("verification_policy_hash") != self.load_policy().get("policy_hash"):
                raise StateIntegrityError("trusted acceptance policy is invalid")
            return value
        except FileNotFoundError as exc:
            raise StateIntegrityError("trusted acceptance policy is missing") from exc

    def save_plan(self, plan: dict[str, Any]) -> None:
        validate_plan(plan)
        if plan.get("policy_hash") != self.load_policy().get("policy_hash"):
            raise StateIntegrityError("cannot save a plan not bound to frozen policy")
        with self.locked():
            self._write_json(self.plan_path, plan)

    def transition(self, target: State | str, event: str, reason: str, changes: dict[str, Any] | None = None, timestamp: str | None = None) -> dict[str, Any]:
        target_state = State(target)
        timestamp = timestamp or now_iso()
        with self.locked():
            current = self.load_state()
            current_state = State(current["state"])
            if target_state not in ALLOWED_TRANSITIONS[current_state]:
                raise StateIntegrityError(f"invalid state transition {current_state.value} -> {target_state.value}")
            next_state = dict(current)
            next_state.update(changes or {})
            next_state.update({"state": target_state.value, "updated_at": timestamp, "last_event": event})
            validate_state(next_state)
            self._write_json(self.state_path, next_state)
            crash_hook("AFTER_STATE_WRITE")
            self._append_event_unlocked(next_state, event, reason)
            self._log_unlocked(f"{target_state.value}: {reason}")
            return next_state

    def mutate(self, event: str, reason: str, mutator, timestamp: str | None = None) -> dict[str, Any]:
        timestamp = timestamp or now_iso()
        with self.locked():
            state = self.load_state()
            next_state = dict(mutator(dict(state)))
            next_state.update({"updated_at": timestamp, "last_event": event})
            validate_state(next_state)
            self._write_json(self.state_path, next_state)
            crash_hook("AFTER_STATE_WRITE")
            self._append_event_unlocked(next_state, event, reason)
            self._log_unlocked(f"{next_state['state']}: {reason}")
            return next_state

    def append_event(self, event: str, reason: str, metadata: dict[str, Any] | None = None) -> None:
        with self.locked():
            state = self.load_state()
            self._append_event_unlocked(state, event, reason, metadata)
            self._log_unlocked(f"{state['state']}: {reason}")

    def load_events(self) -> list[dict[str, Any]]:
        """Return the sequence-validated trusted lifecycle timeline."""
        self._validate_event_frontier()
        if self._has_legacy_events_only():
            return self._legacy_event_items()
        if not self.events_manifest_path.exists():
            return []
        manifest = self._read_json_regular(self.events_manifest_path)
        events: list[dict[str, Any]] = []
        for seg in manifest.get("segments", []):
            path = self.legacy_events_path if seg.get("is_legacy_root") else self.events_dir / seg["name"]
            raw = self._read_segment_body(path)
            if seg.get("sha256") is not None:
                digest = hashlib.sha256(raw).hexdigest()
                if digest != seg["sha256"]:
                    raise StateIntegrityError(f"trusted event segment {seg['name']} digest mismatch")
            for line in raw.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise StateIntegrityError(f"trusted event segment {seg['name']} is corrupt") from exc
        previous = 0
        for item in events:
            seq = item.get("seq") if isinstance(item, dict) else None
            if not isinstance(seq, int) or seq != previous + 1:
                raise StateIntegrityError("trusted event log sequence is corrupt")
            previous = seq
        return events

    def log(self, message: str) -> None:
        with self.locked():
            self._log_unlocked(message)

    def write_run_event(self, generation: int, value: dict[str, Any]) -> Path:
        self.runs_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.runs_path / f"generation-{generation}.events.jsonl"
        self._append_file(path, json.dumps(redact(value), sort_keys=True, ensure_ascii=False) + "\n")
        return path

    def write_report(self, text: str, timestamp: str | None = None) -> Path:
        stamp = (timestamp or now_iso()).replace(":", "-").replace(".", "-")
        path = self.reports_path / f"report-{stamp}.md"
        self._write_text(path, text)
        self._write_text(self.reports_path / "latest.md", text)
        return path

    def _ensure_directory(self) -> None:
        self._assert_control_plane_outside_workspace()
        self._assert_path_outside_workspace(self.directory)
        root_fd: int | None = None
        dir_fd: int | None = None
        try:
            root_fd, _ = _open_dir_descriptor(self.root, create=True)
            dir_fd, _ = _open_dir_descriptor(self.directory.name, dir_fd=root_fd, create=True)
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
            if root_fd is not None:
                try:
                    os.close(root_fd)
                except OSError:
                    pass

    def _assert_control_plane_outside_workspace(self) -> None:
        self._assert_path_outside_workspace(self.root)

    def _assert_path_outside_workspace(self, path: Path) -> None:
        try:
            inside = path == self.repo or path.is_relative_to(self.repo)
        except ValueError:
            inside = False
        if inside:
            raise StateIntegrityError("trusted control plane must be outside the Codex workspace")

    def _open_mailbox_directory(self, create: bool = False) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if create:
            try:
                os.mkdir(self.mailbox_directory, 0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise StateIntegrityError("cannot create the Nightwatch mailbox directory") from exc
        try:
            info = os.lstat(self.mailbox_directory)
        except FileNotFoundError as exc:
            raise StateIntegrityError("Nightwatch mailbox directory is missing") from exc
        except OSError as exc:
            raise StateIntegrityError("Nightwatch mailbox directory cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise StateIntegrityError("Nightwatch mailbox directory must be a real directory")
        try:
            descriptor = os.open(self.mailbox_directory, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(descriptor)
                raise StateIntegrityError("Nightwatch mailbox directory is not a directory")
            return descriptor
        except StateIntegrityError:
            raise
        except OSError as exc:
            raise StateIntegrityError("Nightwatch mailbox directory is unsafe") from exc

    @staticmethod
    def _mailbox_name(name: str) -> str:
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise StateIntegrityError("invalid Nightwatch mailbox filename")
        return name

    @classmethod
    def _clear_mailbox_run_outputs_at(cls, directory_fd: int) -> None:
        for name in cls.MAILBOX_RUN_OUTPUTS:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise StateIntegrityError(f"cannot clear stale Nightwatch mailbox output: {name}") from exc
        os.fsync(directory_fd)

    def read_mailbox_file(self, name: str) -> bytes | None:
        name = self._mailbox_name(name)
        try:
            directory_fd = self._open_mailbox_directory()
        except StateIntegrityError as exc:
            if "is missing" in str(exc):
                return None
            raise
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                return None
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_EVENT_BYTES:
                    raise StateIntegrityError("unsafe Nightwatch mailbox file")
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = -1
                    return handle.read(MAX_EVENT_BYTES + 1)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except StateIntegrityError:
            raise
        except OSError as exc:
            raise StateIntegrityError("Nightwatch mailbox file is unsafe") from exc
        finally:
            os.close(directory_fd)

    @classmethod
    def _write_json_at(cls, directory_fd: int, name: str, value: Any) -> None:
        cls._write_text_at(directory_fd, name, json.dumps(redact(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_text_at(directory_fd: int, name: str, text: str) -> None:
        temporary = f".{name}.{secrets.token_hex(12)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = -1
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def _has_legacy_events_only(self) -> bool:
        return self.legacy_events_path.exists() and not self.events_manifest_path.exists()

    def _active_segment_name(self) -> str:
        if self.events_manifest_path.exists():
            manifest = self._read_json_regular(self.events_manifest_path)
            if not isinstance(manifest, dict):
                raise StateIntegrityError("trusted events manifest is invalid")
            curr = manifest.get("current_segment")
            if isinstance(curr, str) and curr:
                return curr
            raise StateIntegrityError("trusted events manifest current_segment is invalid")
        return "segment-000001.jsonl"

    def _active_segment_path(self) -> Path:
        return self.events_dir / self._active_segment_name()

    def _max_segment_bytes(self) -> int:
        return int(os.environ.get("NIGHTWATCH_MAX_SEGMENT_BYTES", DEFAULT_MAX_SEGMENT_BYTES))

    def _read_segment_body(self, path: Path) -> bytes:
        return path.read_bytes()

    @staticmethod
    def _create_empty_segment(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)
        os.close(fd)

    @staticmethod
    def _read_last_json_line(path: Path, file_size: int) -> dict[str, Any] | None:
        read_size = min(file_size, 4096)
        with open(path, "rb") as f:
            f.seek(file_size - read_size)
            chunk = f.read(read_size)
        lines = chunk.splitlines()
        for line in reversed(lines):
            line_str = line.strip().decode("utf-8", errors="replace")
            if line_str:
                try:
                    return json.loads(line_str)
                except json.JSONDecodeError as exc:
                    raise StateIntegrityError("active segment tail is malformed JSON") from exc
        return None

    def _get_or_init_manifest_unlocked(self) -> dict[str, Any]:
        if self.events_manifest_path.exists():
            return self._read_json_regular(self.events_manifest_path)
        return {
            "schema_version": 1,
            "last_seq": 0,
            "current_segment": "segment-000001.jsonl",
            "segments": [],
        }

    def _migrate_legacy_events_unlocked(self) -> None:
        stat_res = os.lstat(self.legacy_events_path)
        if not stat.S_ISREG(stat_res.st_mode) or stat.S_ISLNK(stat_res.st_mode):
            raise StateIntegrityError("legacy events.jsonl is not a regular file")
        if stat_res.st_size > MAX_EVENT_BYTES:
            raise StateIntegrityError("legacy events.jsonl exceeds maximum allowed size")
        events = []
        with self.legacy_events_path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(json.loads(line))
        previous = 0
        for item in events:
            seq = item.get("seq") if isinstance(item, dict) else None
            if not isinstance(seq, int) or seq != previous + 1:
                raise StateIntegrityError("legacy events.jsonl sequence is corrupt")
            previous = seq
        k = len(events)
        legacy_bytes = self._read_segment_body(self.legacy_events_path)
        legacy_sha = hashlib.sha256(legacy_bytes).hexdigest()
        self.events_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        legacy_seg = {
            "name": "events.jsonl",
            "is_legacy_root": True,
            "seq_start": 1,
            "seq_end": k,
            "event_count": k,
            "byte_size": stat_res.st_size,
            "sha256": legacy_sha,
            "prev_sha256": None,
        }
        manifest = {
            "schema_version": 1,
            "last_seq": k,
            "current_segment": "events.jsonl",
            "segments": [legacy_seg],
        }
        self._write_json(self.events_manifest_path, manifest)

    def _append_event_unlocked(self, state: dict[str, Any], event: str, reason: str, metadata: dict[str, Any] | None = None) -> None:
        self.events_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._has_legacy_events_only():
            self._migrate_legacy_events_unlocked()

        manifest = self._get_or_init_manifest_unlocked()
        next_seq = manifest["last_seq"] + 1

        item = {
            "seq": next_seq,
            "ts": state["updated_at"],
            "event": event,
            "reason": reason,
            "run_id": state["run_id"],
            "state": state["state"],
            "generation": state.get("generation"),
            "thread_id": state.get("thread_id"),
            "repo": state.get("repo"),
            "git_head": state.get("last_git_head"),
        }
        if metadata:
            item["metadata"] = metadata
        line_text = json.dumps(redact(item), sort_keys=True, ensure_ascii=False) + "\n"
        line_bytes = line_text.encode("utf-8")

        segments = manifest["segments"]
        active_seg = segments[-1] if segments else None
        need_rotation = False

        if active_seg is None or active_seg.get("is_legacy_root"):
            need_rotation = True
        else:
            active_path = self.events_dir / active_seg["name"]
            curr_size = os.lstat(active_path).st_size if active_path.exists() else 0
            max_seg_bytes = self._max_segment_bytes()
            max_seg_events = int(os.environ.get("NIGHTWATCH_MAX_SEGMENT_EVENTS", DEFAULT_MAX_SEGMENT_EVENTS))
            if curr_size + len(line_bytes) > max_seg_bytes or active_seg.get("event_count", 0) >= max_seg_events:
                need_rotation = True

        if need_rotation:
            prev_hash = None
            if active_seg is not None:
                if active_seg.get("is_legacy_root"):
                    prev_hash = active_seg.get("sha256")
                else:
                    active_path = self.events_dir / active_seg["name"]
                    active_bytes = self._read_segment_body(active_path)
                    prev_hash = hashlib.sha256(active_bytes).hexdigest()
                    active_seg["sha256"] = prev_hash
                    active_seg["byte_size"] = os.lstat(active_path).st_size

            seg_num = sum(1 for s in segments if not s.get("is_legacy_root")) + 1
            new_name = f"segment-{seg_num:06d}.jsonl"
            new_seg = {
                "name": new_name,
                "seq_start": next_seq,
                "seq_end": next_seq,
                "event_count": 0,
                "byte_size": 0,
                "sha256": None,
                "prev_sha256": prev_hash,
            }
            segments.append(new_seg)
            manifest["current_segment"] = new_name
            active_seg = new_seg
            active_path = self.events_dir / active_seg["name"]
            self._create_empty_segment(active_path)
            crash_hook("AFTER_SEGMENT_CREATE")

        active_path = self.events_dir / active_seg["name"]
        self._append_file(active_path, line_text)
        crash_hook("AFTER_EVENT_APPEND")

        active_seg["seq_end"] = next_seq
        active_seg["event_count"] = active_seg.get("event_count", 0) + 1
        active_seg["byte_size"] = os.lstat(active_path).st_size
        manifest["last_seq"] = next_seq
        self._write_json(self.events_manifest_path, manifest)
        if need_rotation:
            crash_hook("AFTER_ROTATION_MANIFEST_COMMIT")

    def _reconcile_orphan_segment_unlocked(self, manifest: dict[str, Any], unexpected_entries: set[str]) -> None:
        """Reconcile an uncommitted next segment orphan left by a crash during rotation.

        Acceptable orphan candidate must satisfy ALL:
        - exactly one unexpected non-dot entry;
        - regular file;
        - no symlink;
        - exact expected next segment name;
        - no path escape;
        - previous manifest is structurally valid;
        - previous active segment is intact;
        - no ambiguity about sequence frontier.
        """
        # CASE 6: two or more unknown orphan segments => FAIL CLOSED
        if len(unexpected_entries) != 1:
            raise StateIntegrityError("events directory entries do not match manifest: multiple unexpected entries")

        orphan_name = next(iter(unexpected_entries))
        if not isinstance(orphan_name, str) or Path(orphan_name).name != orphan_name or "/" in orphan_name or "\\" in orphan_name or orphan_name.startswith("."):
            raise StateIntegrityError("events directory entries do not match manifest: invalid orphan entry name")

        segments = manifest.get("segments")
        if not isinstance(segments, list) or not segments:
            raise StateIntegrityError("events directory entries do not match manifest: manifest segments invalid")

        # CASE 7: exact expected next segment name check
        next_seg_num = sum(1 for s in segments if not s.get("is_legacy_root")) + 1
        expected_next_name = f"segment-{next_seg_num:06d}.jsonl"
        if orphan_name != expected_next_name:
            raise StateIntegrityError(f"events directory entries do not match manifest: unexpected entry {orphan_name}")

        orphan_path = self.events_dir / orphan_name
        try:
            orphan_st = os.lstat(orphan_path)
        except OSError as exc:
            raise StateIntegrityError(f"cannot inspect orphan entry {orphan_name}") from exc

        # CASE 7: symlink / directory => FAIL CLOSED
        if stat.S_ISLNK(orphan_st.st_mode):
            raise StateIntegrityError(f"orphan segment {orphan_name} is a symlink")
        if not stat.S_ISREG(orphan_st.st_mode):
            raise StateIntegrityError(f"orphan segment {orphan_name} must be a regular file")

        # Verify previous active segment is intact
        prev_seg = segments[-1]
        prev_path = self.legacy_events_path if prev_seg.get("is_legacy_root") else self.events_dir / prev_seg["name"]
        try:
            prev_st = os.lstat(prev_path)
        except OSError as exc:
            raise StateIntegrityError(f"cannot inspect previous active segment {prev_seg['name']}") from exc
        if stat.S_ISLNK(prev_st.st_mode) or not stat.S_ISREG(prev_st.st_mode):
            raise StateIntegrityError(f"previous active segment {prev_seg['name']} must be an intact regular file")
        if prev_st.st_size != prev_seg["byte_size"]:
            raise StateIntegrityError("previous active segment size modified")
        if prev_seg["byte_size"] > 0:
            last_line = self._read_last_json_line(prev_path, prev_seg["byte_size"])
            if not isinstance(last_line, dict) or last_line.get("seq") != manifest.get("last_seq"):
                raise StateIntegrityError("previous active segment tail does not match manifest last_seq")
        if prev_seg.get("sha256") is not None:
            prev_bytes = self._read_segment_body(prev_path)
            if hashlib.sha256(prev_bytes).hexdigest() != prev_seg["sha256"]:
                raise StateIntegrityError("previous active segment digest mismatch")

        orphan_size = orphan_st.st_size
        max_allowed = self._max_segment_bytes() + 65_536
        if orphan_size > max_allowed:
            raise StateIntegrityError("orphan segment exceeds maximum segment limit")

        # CASE 1: orphan next segment exists and is empty
        if orphan_size == 0:
            try:
                os.unlink(orphan_path)
            except OSError as exc:
                raise StateIntegrityError(f"cannot remove empty orphan segment {orphan_name}") from exc
            return

        try:
            raw = self._read_segment_body(orphan_path)
        except OSError as exc:
            raise StateIntegrityError(f"cannot read orphan segment {orphan_name}") from exc

        if not raw.strip():
            try:
                os.unlink(orphan_path)
            except OSError as exc:
                raise StateIntegrityError(f"cannot remove empty orphan segment {orphan_name}") from exc
            return

        if not raw.endswith(b"\n"):
            # CASE 2 vs CASE 4: Incomplete append vs malformed non-JSON
            if b"\n" in raw:
                raise StateIntegrityError("orphan segment contains unexpected multiple records or trailing data")
            if not raw.strip().startswith(b"{"):
                raise StateIntegrityError("orphan segment contains malformed non-JSON data")
            # Incomplete atomic append
            try:
                os.unlink(orphan_path)
            except OSError as exc:
                raise StateIntegrityError(f"cannot discard uncommitted orphan segment {orphan_name}") from exc
            return

        lines = [l for l in raw.splitlines() if l.strip()]
        if len(lines) != 1:
            raise StateIntegrityError("orphan segment contains multiple records")

        try:
            item = json.loads(lines[0].decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # CASE 4: Complete line with malformed JSON => FAIL CLOSED
            raise StateIntegrityError("orphan segment contains malformed JSON record") from exc

        if not isinstance(item, dict):
            raise StateIntegrityError("orphan segment event must be a JSON object")

        event_seq = item.get("seq")
        expected_seq = manifest["last_seq"] + 1
        if not isinstance(event_seq, int) or event_seq != expected_seq:
            # CASE 5: orphan event seq skips/repeats => FAIL CLOSED
            raise StateIntegrityError(f"orphan segment seq {event_seq} does not match expected {expected_seq}")

        if not isinstance(item.get("event"), str) or not item.get("event"):
            raise StateIntegrityError("orphan segment event missing event type")

        # CASE 3: Valid single event with seq == manifest.last_seq + 1
        if prev_seg.get("sha256") is None:
            prev_bytes = self._read_segment_body(prev_path)
            prev_hash = hashlib.sha256(prev_bytes).hexdigest()
            prev_seg["sha256"] = prev_hash
            prev_seg["byte_size"] = prev_st.st_size
        else:
            prev_hash = prev_seg["sha256"]

        new_seg = {
            "name": orphan_name,
            "seq_start": event_seq,
            "seq_end": event_seq,
            "event_count": 1,
            "byte_size": orphan_size,
            "sha256": None,
            "prev_sha256": prev_hash,
        }
        manifest["segments"].append(new_seg)
        manifest["current_segment"] = orphan_name
        manifest["last_seq"] = event_seq

        self._write_json(self.events_manifest_path, manifest)
        crash_hook("AFTER_ROTATION_MANIFEST_COMMIT")

    def _validate_event_frontier(self) -> None:
        """Validate event log frontier and reconcile uncommitted crash records.

        Complexity:
        - O(number_of_segments) metadata checks (manifest entries, lstat on sealed segments)
        - O(1) historical body reads (only active segment tail or orphan candidate read)
        """
        if self._has_legacy_events_only():
            self._validate_legacy_events()
            return
        if not self.events_manifest_path.exists():
            if self.exists():
                raise StateIntegrityError("trusted event log is missing")
            return

        try:
            stat_manifest = os.lstat(self.events_manifest_path)
            if not stat.S_ISREG(stat_manifest.st_mode) or stat.S_ISLNK(stat_manifest.st_mode):
                raise StateIntegrityError("manifest.json is unsafe")
            manifest = self._read_json_regular(self.events_manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateIntegrityError("trusted events manifest is unreadable or corrupt") from exc

        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise StateIntegrityError("unsupported event manifest schema")
        segments = manifest.get("segments")
        if not isinstance(segments, list) or not segments:
            raise StateIntegrityError("manifest segments must be a non-empty list")
        last_seq = manifest.get("last_seq")
        if not isinstance(last_seq, int) or last_seq < 1:
            raise StateIntegrityError("invalid manifest last_seq")
        current_segment = manifest.get("current_segment")
        if current_segment != segments[-1].get("name"):
            raise StateIntegrityError("manifest current_segment mismatch")

        expected_seq = 1
        prev_hash = None
        for seg in segments:
            if not isinstance(seg, dict):
                raise StateIntegrityError("invalid segment entry in manifest")
            name = seg.get("name")
            s_start = seg.get("seq_start")
            s_end = seg.get("seq_end")
            count = seg.get("event_count")
            byte_size = seg.get("byte_size")
            if not isinstance(name, str) or not name:
                raise StateIntegrityError("invalid segment name in manifest")
            if not isinstance(s_start, int) or s_start != expected_seq:
                raise StateIntegrityError("segment seq_start broken")
            if not isinstance(s_end, int) or s_end < s_start:
                raise StateIntegrityError("segment seq_end broken")
            if not isinstance(count, int) or count < 0:
                raise StateIntegrityError("segment event_count invalid")
            if not isinstance(byte_size, int) or byte_size < 0:
                raise StateIntegrityError("segment byte_size invalid")
            if seg.get("prev_sha256") != prev_hash:
                raise StateIntegrityError("segment digest chain broken")
            prev_hash = seg.get("sha256")
            expected_seq = s_end + 1

        if last_seq != segments[-1]["seq_end"]:
            raise StateIntegrityError("manifest last_seq does not match last segment seq_end")

        for seg in segments[:-1]:
            path = self.legacy_events_path if seg.get("is_legacy_root") else self.events_dir / seg["name"]
            try:
                st = os.lstat(path)
            except OSError as exc:
                raise StateIntegrityError(f"cannot inspect sealed segment {seg['name']}") from exc
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                raise StateIntegrityError(f"sealed segment {seg['name']} must be a regular file")
            if st.st_size != seg["byte_size"]:
                raise StateIntegrityError(f"sealed segment {seg['name']} size modified")

        try:
            entries = {f for f in os.listdir(self.events_dir) if not f.startswith(".")}
        except OSError as exc:
            raise StateIntegrityError("cannot inspect events directory") from exc
        expected_entries = {"manifest.json"} | {s["name"] for s in segments if not s.get("is_legacy_root")}

        if entries != expected_entries:
            missing_entries = expected_entries - entries
            if missing_entries:
                raise StateIntegrityError(f"events directory entries do not match manifest: missing {missing_entries}")
            unexpected_entries = entries - expected_entries
            self._reconcile_orphan_segment_unlocked(manifest, unexpected_entries)
            try:
                entries = {f for f in os.listdir(self.events_dir) if not f.startswith(".")}
            except OSError as exc:
                raise StateIntegrityError("cannot inspect events directory after reconciliation") from exc
            expected_entries = {"manifest.json"} | {s["name"] for s in manifest["segments"] if not s.get("is_legacy_root")}
            if entries != expected_entries:
                raise StateIntegrityError("events directory entries do not match manifest after reconciliation")
            last_seq = manifest["last_seq"]
            segments = manifest["segments"]

        active_seg = segments[-1]
        active_path = self.legacy_events_path if active_seg.get("is_legacy_root") else self.events_dir / active_seg["name"]
        try:
            st = os.lstat(active_path)
        except OSError as exc:
            raise StateIntegrityError(f"cannot inspect active segment {active_seg['name']}") from exc
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise StateIntegrityError(f"active segment {active_seg['name']} must be a regular file")
        max_allowed = self._max_segment_bytes() + 65_536
        if st.st_size > max_allowed:
            raise StateIntegrityError("active segment exceeds maximum segment limit")

        if st.st_size > active_seg["byte_size"]:
            trailing_bytes = active_path.read_bytes()[active_seg["byte_size"]:]
            if trailing_bytes.endswith(b"\n"):
                try:
                    trailing_item = json.loads(trailing_bytes.decode("utf-8").strip())
                except json.JSONDecodeError:
                    raise StateIntegrityError("trusted event log has corrupt trailing record")
                if isinstance(trailing_item, dict):
                    t_seq = trailing_item.get("seq")
                    if t_seq == last_seq + 1:
                        active_seg["seq_end"] = t_seq
                        active_seg["event_count"] = active_seg.get("event_count", 0) + 1
                        active_seg["byte_size"] = st.st_size
                        manifest["last_seq"] = t_seq
                        self._write_json(self.events_manifest_path, manifest)
                        last_seq = t_seq
                    else:
                        raise StateIntegrityError("trusted event log sequence is corrupt")
                else:
                    raise StateIntegrityError("trusted event log sequence is corrupt")
            else:
                try:
                    with open(active_path, "r+b") as handle:
                        handle.truncate(active_seg["byte_size"])
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError as exc:
                    raise StateIntegrityError("cannot reconcile active segment frontier") from exc
        elif st.st_size < active_seg["byte_size"]:
            raise StateIntegrityError("active segment truncated unexpectedly")

        if active_seg["byte_size"] > 0:
            last_line = self._read_last_json_line(active_path, active_seg["byte_size"])
            if not isinstance(last_line, dict) or last_line.get("seq") != last_seq:
                raise StateIntegrityError("active segment frontier tail does not match manifest last_seq")

    def _validate_legacy_events(self) -> None:
        events = self._legacy_event_items()
        previous = 0
        for item in events:
            seq = item.get("seq") if isinstance(item, dict) else None
            if not isinstance(seq, int) or seq != previous + 1:
                raise StateIntegrityError("trusted event log sequence is corrupt")
            previous = seq

    def _legacy_event_items(self) -> list[dict[str, Any]]:
        try:
            stat_res = os.lstat(self.legacy_events_path)
            if not os.path.isfile(self.legacy_events_path) or os.path.islink(self.legacy_events_path) or stat_res.st_size > MAX_EVENT_BYTES:
                raise StateIntegrityError("trusted event log is unsafe")
            raw = self._read_segment_body(self.legacy_events_path)
            events = []
            for line in raw.decode("utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
            return events
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise StateIntegrityError("trusted event log is unreadable") from exc

    def _event_items(self) -> list[dict[str, Any]]:
        return self.load_events()

    def _next_event_seq(self) -> int:
        if self._has_legacy_events_only():
            events = self._legacy_event_items()
            return int(events[-1]["seq"]) + 1 if events else 1
        if self.events_manifest_path.exists():
            manifest = self._read_json_regular(self.events_manifest_path)
            return int(manifest.get("last_seq", 0)) + 1
        return 1

    def _validate_event_sequence(self) -> None:
        self._validate_event_frontier()

    def _log_unlocked(self, message: str) -> None:
        self._append_file(self.log_path, f"{now_iso()} nightwatch/{__version__} {redact(message)}\n")

    @staticmethod
    def _read_json_regular(path: Path) -> Any:
        stat = os.lstat(path)
        if not os.path.isfile(path) or os.path.islink(path) or stat.st_size > MAX_EVENT_BYTES:
            raise StateIntegrityError(f"unsafe trusted file: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _append_file(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @classmethod
    def _write_json(cls, path: Path, value: Any) -> None:
        cls._write_text(path, json.dumps(redact(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def make_run_id(repo: str, timestamp: str | None = None) -> str:
    stamp = (timestamp or now_iso()).replace("-", "").replace(":", "").replace(".", "")[:15]
    return f"{safe_slug(Path(repo).name)}-{stamp}"
