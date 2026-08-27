from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import fcntl

from . import __version__
from .models import State, empty_state, validate_plan, validate_state


class StateIntegrityError(RuntimeError):
    """The durable record cannot be trusted; callers must fail closed."""


ALLOWED_TRANSITIONS: dict[State, set[State]] = {
    State.NEW: {State.NEW, State.PREFLIGHT, State.STOPPED, State.FAILED, State.BLOCKED},
    State.PREFLIGHT: {State.PREFLIGHT, State.RUNNING, State.FAILED, State.BLOCKED, State.STOPPED},
    State.RUNNING: {
        State.RUNNING, State.WAIT_QUOTA, State.RETRY_BACKOFF, State.RECOVERING,
        State.VERIFYING, State.BLOCKED, State.FAILED, State.STOPPED,
    },
    State.WAIT_QUOTA: {State.WAIT_QUOTA, State.RECOVERING, State.BLOCKED, State.STOPPED, State.FAILED},
    State.RETRY_BACKOFF: {State.RETRY_BACKOFF, State.RECOVERING, State.RUNNING, State.BLOCKED, State.FAILED, State.STOPPED},
    State.RECOVERING: {State.RECOVERING, State.RUNNING, State.WAIT_QUOTA, State.RETRY_BACKOFF, State.VERIFYING, State.BLOCKED, State.FAILED, State.STOPPED},
    State.VERIFYING: {State.VERIFYING, State.DONE, State.RUNNING, State.BLOCKED, State.FAILED, State.STOPPED},
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
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if re.search(r"(?:token|secret|password|api[_-]?key|authorization|cookie|credential)", key_text, re.I):
                clean[key_text] = "[REDACTED]"
            else:
                clean[key_text] = redact(item)
        return clean
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
        text = re.sub(r"(?i)\b(?:sk|sess|chatcmpl)-[A-Za-z0-9._-]{16,}", "[REDACTED]", text)
        text = re.sub(r"(?i)(?:api[_-]?key|access[_-]?token)\s*[=:]\s*\S+", "[REDACTED]", text)
        return text[:2000]
    return value


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")[:80] or "nightwatch"


class NightwatchStore:
    def __init__(self, repo: str | Path):
        self.repo = Path(repo).resolve()
        self.directory = self.repo / ".nightwatch"
        self.state_path = self.directory / "state.json"
        self.goal_path = self.directory / "goal.md"
        self.plan_path = self.directory / "plan.json"
        self.checkpoint_path = self.directory / "checkpoint.md"
        self.events_path = self.directory / "events.jsonl"
        self.log_path = self.directory / "supervisor.log"
        self.runs_path = self.directory / "runs"
        self.reports_path = self.directory / "reports"
        self.lock_path = self.directory / ".lock"

    def exists(self) -> bool:
        return self.state_path.exists()

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = self.lock_path.open("a+")
        try:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def initialize(self, run_id: str, goal: str, repo: str, timestamp: str | None = None) -> dict[str, Any]:
        timestamp = timestamp or now_iso()
        state = empty_state(run_id, goal, repo, timestamp)
        plan = {
            "schema_version": 1,
            "authority": "nightwatch-bootstrap",
            "required_verification_commands": ["git diff --check"],
            "milestones": [{
                "id": "M1",
                "title": "Complete the goal and pass final verification",
                "weight": 100,
                "required": True,
                "status": "pending",
                "verification_commands": ["git diff --check"],
                "evidence": [],
            }],
        }
        validate_state(state)
        validate_plan(plan)
        with self.locked():
            if self.state_path.exists():
                raise StateIntegrityError("a Nightwatch run already exists in this repository")
            self.runs_path.mkdir(mode=0o700)
            self.reports_path.mkdir(mode=0o700)
            self._write_text(self.goal_path, goal.rstrip() + "\n")
            self._write_json(self.plan_path, plan)
            self._write_text(self.checkpoint_path, f"# Nightwatch checkpoint\n\nCreated: {timestamp}\n")
            self._write_json(self.state_path, state)
            self._append_event_unlocked(state, "run_created", "goal initialized")
            self._log_unlocked("NEW: goal initialized")
        return state

    def load_state(self) -> dict[str, Any]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            validate_state(state)
            return state
        except FileNotFoundError as exc:
            raise StateIntegrityError(".nightwatch/state.json is missing") from exc
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise StateIntegrityError(".nightwatch/state.json is unreadable or invalid") from exc

    def load_plan(self) -> dict[str, Any]:
        try:
            plan = json.loads(self.plan_path.read_text(encoding="utf-8"))
            validate_plan(plan)
            return plan
        except FileNotFoundError as exc:
            raise StateIntegrityError(".nightwatch/plan.json is missing") from exc
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise StateIntegrityError(".nightwatch/plan.json is unreadable or invalid") from exc

    def save_plan(self, plan: dict[str, Any]) -> None:
        validate_plan(plan)
        with self.locked():
            self._write_json(self.plan_path, plan)

    def transition(
        self,
        target: State | str,
        event: str,
        reason: str,
        changes: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        target_state = State(target)
        timestamp = timestamp or now_iso()
        with self.locked():
            current = self.load_state()
            current_state = State(current["state"])
            if target_state not in ALLOWED_TRANSITIONS[current_state]:
                raise StateIntegrityError(f"invalid state transition {current_state.value} -> {target_state.value}")
            next_state = dict(current)
            next_state.update(changes or {})
            next_state["state"] = target_state.value
            next_state["updated_at"] = timestamp
            next_state["last_event"] = event
            validate_state(next_state)
            self._write_json(self.state_path, next_state)
            self._append_event_unlocked(next_state, event, reason)
            self._log_unlocked(f"{target_state.value}: {reason}")
            return next_state

    def mutate(
        self,
        event: str,
        reason: str,
        mutator,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        timestamp = timestamp or now_iso()
        with self.locked():
            state = self.load_state()
            next_state = dict(mutator(dict(state)))
            next_state["updated_at"] = timestamp
            next_state["last_event"] = event
            validate_state(next_state)
            self._write_json(self.state_path, next_state)
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
        clean = redact(value)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(clean, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        return path

    def write_report(self, text: str, timestamp: str | None = None) -> Path:
        self.reports_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = (timestamp or now_iso()).replace(":", "-").replace(".", "-")
        path = self.reports_path / f"report-{stamp}.md"
        self._write_text(path, text)
        self._write_text(self.reports_path / "latest.md", text)
        return path

    def _append_event_unlocked(
        self,
        state: dict[str, Any],
        event: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        item = {
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
        self.events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(redact(item), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.events_path, 0o600)

    def _log_unlocked(self, message: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{now_iso()} nightwatch/{__version__} {redact(message)}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(self.log_path, 0o600)

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
