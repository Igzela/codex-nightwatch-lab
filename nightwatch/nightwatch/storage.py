from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
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


ALLOWED_TRANSITIONS: dict[State, set[State]] = {
    State.NEW: {State.NEW, State.PREFLIGHT, State.STOPPED, State.FAILED, State.BLOCKED},
    State.PREFLIGHT: {State.PREFLIGHT, State.RUNNING, State.FAILED, State.BLOCKED, State.STOPPED},
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

    def __init__(self, repo: str | Path, state_home: str | Path | None = None):
        self.repo = Path(repo).resolve()
        self.repo_id = repo_identity(self.repo)
        self.root = Path(state_home).resolve() if state_home else control_plane_root()
        self.directory = self.root / self.repo_id
        self.legacy_directory = self.repo / ".nightwatch"
        self.mailbox_directory = self.repo / ".nightwatch-agent"
        self.state_path = self.directory / "state.json"
        self.goal_path = self.directory / "goal.md"
        self.plan_path = self.directory / "plan.json"
        self.policy_path = self.directory / "verification-policy.json"
        self.acceptance_path = self.directory / "acceptance.json"
        self.metadata_path = self.directory / "metadata.json"
        self.checkpoint_path = self.directory / "checkpoint.md"
        self.events_path = self.directory / "events.jsonl"
        self.log_path = self.directory / "supervisor.log"
        self.runs_path = self.directory / "runs"
        self.reports_path = self.directory / "reports"
        self.lock_path = self.directory / "state.lock"
        self.supervisor_lock_path = self.directory / "supervisor.lock"

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

    def initialize(self, run_id: str, goal: str, repo: str, timestamp: str | None = None, verify_commands: list[str] | None = None) -> dict[str, Any]:
        timestamp = timestamp or now_iso()
        commands = list(verify_commands or [])
        policy_core = {"schema_version": 1, "source": "cli", "final_commands": commands}
        policy = {**policy_core, "policy_hash": policy_hash(policy_core)}
        acceptance = {"schema_version": 1, "goal_hash": hashlib.sha256(goal.encode("utf-8")).hexdigest(), "verification_policy_hash": policy["policy_hash"], "required_final_checks": commands, "plan_minimum": {"milestones": 1}, "baseline_repo": str(self.repo), "created_at": timestamp}
        state = empty_state(run_id, goal, repo, self.repo_id, timestamp)
        state["acceptance_ready"] = sufficient_verification_policy(commands)
        profile = "default" if commands else "none"
        plan = {"schema_version": 2, "authority": "nightwatch", "policy_hash": policy["policy_hash"], "milestones": [{"id": "M1", "title": "Complete the goal", "weight": 100, "required": True, "status": "pending", "verification_profile": profile, "evidence": []}]}
        metadata = {"schema_version": 2, "repo_id": self.repo_id, "repo_path": str(self.repo), "repo_remote": _git_identity(self.repo), "created_at": timestamp}
        validate_state(state)
        validate_plan(plan)
        with self.locked():
            if self.state_path.exists():
                raise StateIntegrityError("a Nightwatch run already exists in this repository")
            self.runs_path.mkdir(mode=0o700)
            self.reports_path.mkdir(mode=0o700)
            self.mailbox_directory.mkdir(mode=0o700, exist_ok=True)
            self._write_json(self.mailbox_directory / "context.json", {"goal_hash": acceptance["goal_hash"], "mailbox_contract": "untrusted-input-only"})
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
        try:
            state = self._read_json_regular(self.state_path)
            validate_state(state)
            if state.get("repo") != str(self.repo) or state.get("repo_id") != self.repo_id:
                raise StateIntegrityError("trusted state does not belong to this repository identity")
            self._validate_event_sequence()
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
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)

    def _append_event_unlocked(self, state: dict[str, Any], event: str, reason: str, metadata: dict[str, Any] | None = None) -> None:
        item = {"seq": self._next_event_seq(), "ts": state["updated_at"], "event": event, "reason": reason, "run_id": state["run_id"], "state": state["state"], "generation": state.get("generation"), "thread_id": state.get("thread_id"), "repo": state.get("repo"), "git_head": state.get("last_git_head")}
        if metadata:
            item["metadata"] = metadata
        self._append_file(self.events_path, json.dumps(redact(item), sort_keys=True, ensure_ascii=False) + "\n")

    def _next_event_seq(self) -> int:
        events = self._event_items()
        return int(events[-1]["seq"]) + 1 if events else 1

    def _validate_event_sequence(self) -> None:
        previous = 0
        for item in self._event_items():
            seq = item.get("seq") if isinstance(item, dict) else None
            if not isinstance(seq, int) or seq != previous + 1:
                raise StateIntegrityError("trusted event log sequence is corrupt")
            previous = seq

    def _event_items(self) -> list[dict[str, Any]]:
        try:
            stat = os.lstat(self.events_path)
            if not os.path.isfile(self.events_path) or os.path.islink(self.events_path) or stat.st_size > MAX_EVENT_BYTES:
                raise StateIntegrityError("trusted event log is unsafe")
            with self.events_path.open(encoding="utf-8") as handle:
                return [json.loads(line) for line in handle]
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise StateIntegrityError("trusted event log is unreadable") from exc

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
