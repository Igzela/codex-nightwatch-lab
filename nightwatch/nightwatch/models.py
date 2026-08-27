from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class State(StrEnum):
    NEW = "NEW"
    PREFLIGHT = "PREFLIGHT"
    RUNNING = "RUNNING"
    WAIT_QUOTA = "WAIT_QUOTA"
    RETRY_BACKOFF = "RETRY_BACKOFF"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"
    DONE = "DONE"
    FAILED = "FAILED"


class ErrorKind(StrEnum):
    QUOTA_5H = "usage_limit_5h"
    QUOTA_WEEKLY = "usage_limit_weekly"
    TEMPORARY_429 = "temporary_429"
    CAPACITY = "capacity_overload"
    NETWORK = "network_error"
    AUTH = "auth_error"
    CRASH = "codex_crash"
    BLOCKER = "task_blocker"
    MALFORMED = "malformed_jsonl"
    STATE = "state_integrity"
    GIT = "git_conflict"
    UNKNOWN = "unknown_provider_error"


@dataclass(frozen=True)
class QuotaWindow:
    name: str
    used_percent: float | None = None
    window_duration_mins: int | None = None
    resets_at: int | None = None

    @property
    def exhausted(self) -> bool:
        return self.used_percent is not None and self.used_percent >= 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "used_percent": self.used_percent,
            "window_duration_mins": self.window_duration_mins,
            "resets_at": self.resets_at,
        }


@dataclass(frozen=True)
class QuotaSnapshot:
    source: str
    read_at: str
    primary: QuotaWindow | None = None
    secondary: QuotaWindow | None = None
    plan_type: str | None = None
    error: str | None = None

    def windows(self) -> list[QuotaWindow]:
        return [window for window in (self.primary, self.secondary) if window]

    def exhausted_windows(self) -> list[QuotaWindow]:
        return [window for window in self.windows() if window.exhausted]

    def recovered(self, governing_names: set[str] | None = None) -> bool:
        windows = self.windows()
        if governing_names:
            windows = [window for window in windows if window.name in governing_names]
        return bool(windows) and all(not window.exhausted for window in windows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "read_at": self.read_at,
            "primary": self.primary.to_dict() if self.primary else None,
            "secondary": self.secondary.to_dict() if self.secondary else None,
            "plan_type": self.plan_type,
            "error": self.error,
        }


@dataclass
class ProviderResult:
    exit_code: int | None
    signal: str | None
    thread_id: str | None
    event_count: int
    malformed_count: int
    event_types: list[str] = field(default_factory=list)
    error_kind: ErrorKind | None = None
    error_detail: str | None = None
    reset_at: int | None = None
    reset_source: str | None = None
    quota_windows: list[QuotaWindow] = field(default_factory=list)
    blocker: str | None = None
    aborted: bool = False
    run_log: str | None = None


TERMINAL_STATES = {State.DONE, State.FAILED, State.BLOCKED, State.STOPPED}


def empty_state(run_id: str, goal: str, repo: str, now: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "goal": goal,
        "repo": repo,
        "thread_id": None,
        "state": State.NEW.value,
        "generation": 1,
        "created_at": now,
        "updated_at": now,
        "next_resume_at": None,
        "quota_source": None,
        "quota": None,
        "quota_windows": [],
        "last_verified_commit": None,
        "last_error": None,
        "error_kind": None,
        "blocker": None,
        "active_pid": None,
        "active_started_at": None,
        "active_action": None,
        "resume_claim": None,
        "retry_attempt": 0,
        "crash_attempt": 0,
        "recoveries": 0,
        "last_event": "run_created",
        "last_provider_exit": None,
        "last_provider_signal": None,
        "last_git_head": None,
        "final_verification_passed": False,
        "plan_ready": False,
        "verification_attempts": 0,
        "last_verification": None,
        "inhibit_requested": False,
    }


def validate_state(state: dict[str, Any]) -> None:
    required = {
        "schema_version", "run_id", "goal", "repo", "thread_id", "state",
        "generation", "created_at", "updated_at", "next_resume_at", "quota_source",
        "last_verified_commit", "last_error", "last_event",
    }
    missing = sorted(required - set(state))
    if missing:
        raise ValueError(f"state missing fields: {', '.join(missing)}")
    if state.get("schema_version") != 1:
        raise ValueError(f"unsupported state schema: {state.get('schema_version')!r}")
    if state.get("state") not in {item.value for item in State}:
        raise ValueError(f"unknown state: {state.get('state')!r}")
    if not isinstance(state.get("generation"), int) or state["generation"] < 1:
        raise ValueError("state.generation must be a positive integer")
    if state.get("thread_id") is not None and not isinstance(state["thread_id"], str):
        raise ValueError("state.thread_id must be a string or null")
    if state.get("resume_claim") is not None:
        claim = state["resume_claim"]
        if not isinstance(claim, dict) or not isinstance(claim.get("generation"), int):
            raise ValueError("invalid resume_claim")


def validate_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("milestones"), list):
        raise ValueError("plan must contain a milestones array")
    if not plan["milestones"]:
        raise ValueError("plan must contain at least one milestone")
    seen: set[str] = set()
    total = 0.0
    allowed = {"pending", "working", "implemented", "verified", "blocked"}
    for item in plan["milestones"]:
        if not isinstance(item, dict):
            raise ValueError("milestone must be an object")
        ident = item.get("id")
        title = item.get("title")
        weight = item.get("weight")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError("milestone ids must be unique non-empty strings")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"milestone {ident!r} has no title")
        if not isinstance(weight, (int, float)) or weight <= 0:
            raise ValueError(f"milestone {ident!r} has invalid weight")
        if item.get("status", "pending") not in allowed:
            raise ValueError(f"milestone {ident!r} has invalid status")
        commands = item.get("verification_commands", [])
        if not isinstance(commands, list) or not all(isinstance(command, str) and command.strip() for command in commands):
            raise ValueError(f"milestone {ident!r} has invalid verification_commands")
        if item.get("required", True) and not commands:
            raise ValueError(f"required milestone {ident!r} needs a verification command")
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError(f"milestone {ident!r} has invalid evidence")
        seen.add(ident)
        total += float(weight)
    if total <= 0:
        raise ValueError("plan weight must be positive")


def plan_progress(plan: dict[str, Any]) -> dict[str, Any]:
    validate_plan(plan)
    milestones = plan["milestones"]
    implemented = [item for item in milestones if item.get("status") in {"implemented", "verified"}]
    verified = [item for item in milestones if item.get("status") == "verified"]
    total = sum(float(item["weight"]) for item in milestones)
    implemented_weight = sum(float(item["weight"]) for item in implemented)
    verified_weight = sum(float(item["weight"]) for item in verified)
    return {
        "implemented_count": len(implemented),
        "verified_count": len(verified),
        "total_count": len(milestones),
        "implemented_weight": implemented_weight,
        "verified_weight": verified_weight,
        "total_weight": total,
        "implemented_percent": round(implemented_weight / total * 100, 2),
        "verified_percent": round(verified_weight / total * 100, 2),
    }
