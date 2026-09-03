from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import os
import re
import shutil
import subprocess
from typing import Any


_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_REASONING_EFFORT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
ACCOUNT_MODES = {"CURRENT_ONLY", "AUTO_POOL"}
PROVEN_CROSS_ACCOUNT_CODEX_VERSIONS = frozenset({"0.152.1"})


def installed_codex_version(binary: str | None = None) -> str | None:
    bin_path = binary or os.environ.get("NIGHTWATCH_CODEX_BIN") or shutil.which("codex") or "codex"
    try:
        result = subprocess.run(
            [bin_path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def cross_account_thread_mode_for_version(version: str | None) -> str:
    # Explicit internal test seam for simulating proven behavior in unit fixtures:
    if os.environ.get("NIGHTWATCH_UNSAFE_FORCE_CROSS_ACCOUNT_PROVEN") == "1":
        return "PROVEN"

    base_mode = "INCONCLUSIVE"
    if version:
        clean_version = version.split()[-1].strip()
        if clean_version in PROVEN_CROSS_ACCOUNT_CODEX_VERSIONS or version in PROVEN_CROSS_ACCOUNT_CODEX_VERSIONS:
            base_mode = "PROVEN"

    override = os.environ.get("NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE")
    if override:
        # A normal environment override may only downgrade capability (e.g. PROVEN -> UNSUPPORTED / INCONCLUSIVE)
        # It may not upgrade an unvalidated Codex version to PROVEN in production.
        if override in {"UNSUPPORTED", "INCONCLUSIVE", "UNKNOWN"}:
            return override
        if override == "PROVEN":
            return "PROVEN" if base_mode == "PROVEN" else "INCONCLUSIVE"

    return base_mode


def validate_model_name(value: str) -> str:
    if not _MODEL_NAME.fullmatch(value):
        raise ValueError("model must be a visible model slug (letters, digits, '.', '_', ':', '/', or '-')")
    return value


def validate_reasoning_effort(value: str) -> str:
    if not _REASONING_EFFORT.fullmatch(value):
        raise ValueError("reasoning effort must be a visible level name")
    return value


class State(StrEnum):
    NEW = "NEW"
    PREFLIGHT = "PREFLIGHT"
    RUNNING = "RUNNING"
    WAIT_QUOTA = "WAIT_QUOTA"
    RETRY_BACKOFF = "RETRY_BACKOFF"
    RECOVERING = "RECOVERING"
    VERIFYING = "VERIFYING"
    AWAITING_ACCEPTANCE = "AWAITING_ACCEPTANCE"
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
    account_fingerprint: str | None = None

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
            "account_fingerprint": self.account_fingerprint,
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


TERMINAL_STATES = {State.DONE, State.FAILED, State.BLOCKED, State.STOPPED, State.AWAITING_ACCEPTANCE}


def empty_state(
    run_id: str,
    goal: str,
    repo: str,
    repo_id: str,
    now: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "goal": goal,
        "repo": repo,
        "repo_id": repo_id,
        "thread_id": None,
        "model": model,
        "reasoning_effort": reasoning_effort,
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
        "active_process": None,
        "supervisor_owner": None,
        "resume_claim": None,
        "retry_attempt": 0,
        "crash_attempt": 0,
        "recoveries": 0,
        "quota_cycles": 0,
        "recovery_failures": 0,
        "pool_probe_failures": 0,
        "last_event": "run_created",
        "last_provider_exit": None,
        "last_provider_signal": None,
        "last_git_head": None,
        "final_verification_passed": False,
        "plan_ready": False,
        "verification_attempts": 0,
        "last_verification": None,
        "inhibit_requested": False,
        "acceptance_ready": False,
        "account_mode": "CURRENT_ONLY",
        "authorized_accounts": [],
        "current_account_key": None,
        "current_account_fingerprint": None,
        "active_account_fingerprint": None,
        "account_generation": 0,
        "account_lease": None,
        "account_claim": None,
        "account_reselect": False,
        "account_snapshots": {},
        "account_reset_times": {},
        "account_errors": {},
        "last_switch_reason": None,
        "cross_account_thread_mode": "INCONCLUSIVE",
        "cross_account_thread_capability": None,
        "thread_handoff": None,
    }


def validate_state(state: dict[str, Any]) -> None:
    required = {
        "schema_version", "run_id", "goal", "repo", "repo_id", "thread_id", "state",
        "generation", "created_at", "updated_at", "next_resume_at", "quota_source",
        "last_verified_commit", "last_error", "last_event",
    }
    missing = sorted(required - set(state))
    if missing:
        raise ValueError(f"state missing fields: {', '.join(missing)}")
    if state.get("schema_version") != 2:
        raise ValueError(f"unsupported state schema: {state.get('schema_version')!r}")
    if state.get("state") not in {item.value for item in State}:
        raise ValueError(f"unknown state: {state.get('state')!r}")
    if not isinstance(state.get("generation"), int) or state["generation"] < 1:
        raise ValueError("state.generation must be a positive integer")
    for field_name in ("recoveries", "quota_cycles", "recovery_failures", "pool_probe_failures"):
        value = state.get(field_name, 0)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"state.{field_name} must be a non-negative integer")
    if state.get("thread_id") is not None and not isinstance(state["thread_id"], str):
        raise ValueError("state.thread_id must be a string or null")
    if state.get("model") is not None:
        validate_model_name(state["model"])
    if state.get("reasoning_effort") is not None:
        validate_reasoning_effort(state["reasoning_effort"])
    if state.get("account_mode", "CURRENT_ONLY") not in ACCOUNT_MODES:
        raise ValueError("state.account_mode is invalid")
    authorized = state.get("authorized_accounts", [])
    if not isinstance(authorized, list) or len(authorized) != len(set(authorized)) or not all(isinstance(item, str) and item.strip() for item in authorized):
        raise ValueError("state.authorized_accounts must contain unique stable account keys")
    current_key = state.get("current_account_key")
    if current_key is not None and (not isinstance(current_key, str) or not current_key.strip()):
        raise ValueError("state.current_account_key must be a string or null")
    for field_name in ("current_account_fingerprint", "active_account_fingerprint"):
        value = state.get(field_name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"state.{field_name} must be a string or null")
    capability = state.get("cross_account_thread_mode", "INCONCLUSIVE")
    if capability not in {"PROVEN", "UNSUPPORTED", "INCONCLUSIVE"}:
        raise ValueError("state.cross_account_thread_mode is invalid")
    if state.get("thread_handoff") is not None and not isinstance(state["thread_handoff"], dict):
        raise ValueError("state.thread_handoff must be an object or null")
    if state.get("account_lease") is not None and not isinstance(state["account_lease"], dict):
        raise ValueError("state.account_lease must be an object or null")
    if state.get("account_claim") is not None and not isinstance(state["account_claim"], dict):
        raise ValueError("state.account_claim must be an object or null")
    if state.get("resume_claim") is not None:
        claim = state["resume_claim"]
        if not isinstance(claim, dict) or not isinstance(claim.get("generation"), int):
            raise ValueError("invalid resume_claim")
    active = state.get("active_process")
    if active is not None:
        if not isinstance(active, dict) or not isinstance(active.get("pid"), int) or not isinstance(active.get("starttime"), str):
            raise ValueError("invalid active_process")


def validate_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("milestones"), list):
        raise ValueError("plan must contain a milestones array")
    if not plan["milestones"]:
        raise ValueError("plan must contain at least one milestone")
    seen: set[str] = set()
    total = 0.0
    if plan.get("schema_version") != 2 or plan.get("authority") != "nightwatch":
        raise ValueError("plan is not a trusted schema 2 Nightwatch plan")
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
        profile = item.get("verification_profile")
        if profile not in {"default", "none"}:
            raise ValueError(f"milestone {ident!r} has invalid verification_profile")
        if "verification_commands" in item:
            raise ValueError("model-controlled verification_commands are forbidden")
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
