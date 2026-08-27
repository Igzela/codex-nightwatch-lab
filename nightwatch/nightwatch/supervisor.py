from __future__ import annotations

import json
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .codex import run_codex
from .git import GitError, GitSnapshot, is_ancestor, repo_root, snapshot
from .milestones import adopt_proposed_plan, current_milestone, ingest_progress, verify_milestones
from .models import ErrorKind, ProviderResult, QuotaSnapshot, QuotaWindow, State, TERMINAL_STATES
from .quota import QuotaError, make_quota_provider, quota_recovered
from .storage import NightwatchStore, StateIntegrityError, SupervisorAlreadyRunning, now_iso
from .testing import crash_hook


MAX_TRANSIENT_RETRIES = 3
MAX_CRASH_RETRIES = 3
MAX_QUOTA_RECOVERIES = 20
MAX_QUOTA_REVALIDATION_FAILURES = 3
QUOTA_POLL_SECONDS = 30
QUOTA_BUFFER_SECONDS = 60
TRANSIENT_BACKOFF_SECONDS = (2, 5, 15)
CRASH_BACKOFF_SECONDS = (1, 3, 10)


def _dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _sleep_until(target: int, poll_seconds: float = 30.0) -> None:
    while True:
        remaining = target - int(time.time())
        if remaining <= 0:
            return
        time.sleep(min(remaining, poll_seconds))


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        if sys_platform_linux():
            try:
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
                state = stat.rsplit(")", 1)[-1].strip().split(maxsplit=1)[0]
                if state == "Z":
                    return False
            except OSError:
                return False
        return True
    except OSError:
        return False


def process_identity(pid: int) -> dict[str, Any] | None:
    """Linux PID identity resistant to PID reuse."""
    if not sys_platform_linux() or not pid_alive(pid):
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = stat_text.rsplit(")", 1)[-1].strip().split()
        starttime = fields[19]
        executable = os.readlink(f"/proc/{pid}/exe")
    except (OSError, IndexError):
        return None
    return {"pid": pid, "starttime": starttime, "executable": executable}


def process_matches(record: dict[str, Any]) -> bool:
    observed = process_identity(record.get("pid")) if isinstance(record.get("pid"), int) else None
    return bool(observed and observed.get("starttime") == record.get("starttime") and observed.get("executable") == record.get("executable"))


def sys_platform_linux() -> bool:
    return sys.platform.startswith("linux")


def find_repo_codex_processes(repo: str | Path, exclude_pid: int | None = None) -> list[dict[str, Any]]:
    if not sys_platform_linux():
        return []
    target = Path(repo).resolve()
    found: list[dict[str, Any]] = []
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return []
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if exclude_pid and pid == exclude_pid:
            continue
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
            if cwd == target:
                exe = os.readlink(f"/proc/{pid}/exe")
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").lower()
                is_codex = ("codex" in exe.lower() or "codex" in cmdline) and "nightwatch" not in exe.lower() and "nightwatch" not in cmdline and "pytest" not in cmdline and "unittest" not in cmdline
                if is_codex:
                    found.append({"pid": pid, "executable": exe, "cmdline": cmdline, "cwd": str(cwd)})
        except (OSError, ValueError):
            continue
    return found


def find_active_threads_for_repo(repo: str | Path, codex_home: str | Path | None = None) -> list[dict[str, Any]]:
    target = str(Path(repo).resolve())
    db_path = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "state_5.sqlite"
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, rollout_path, updated_at_ms, model, git_branch, thread_source, first_user_message, title "
                "FROM threads WHERE cwd = ? ORDER BY updated_at_ms DESC LIMIT 10",
                (target,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": row[0],
                    "rollout_path": row[1],
                    "updated_at_ms": row[2],
                    "model": row[3],
                    "git_branch": row[4],
                    "thread_source": row[5],
                    "first_user_message": row[6],
                    "title": row[7],
                }
                for row in rows
            ]
    except Exception:
        return []


def start_prompt(store: NightwatchStore, goal: str) -> str:
    goal_hash = store.load_acceptance()["goal_hash"]
    commands = store.load_policy()["final_commands"]
    verification = "\n".join(f"- {command}" for command in commands) or "- No trusted automatic verification is configured."
    return f"""You are working under Nightwatch supervision in this Git repository.

Read Git status/diff and the untrusted mailbox `.nightwatch-agent/context.json` first. Nightwatch's authoritative state, acceptance policy, quota evidence, thread ID, and verification policy are outside the workspace and unavailable to you.
Before making implementation changes, create `.nightwatch-agent/proposed-plan.json` with this exact shape:
{{"goal_hash":"{goal_hash}","milestones":[{{"id":"M1","title":"...","weight":1}}]}}
Do not add verification commands or policy to the proposal: models may suggest task structure but cannot authorize host commands. After each milestone is implemented, update `.nightwatch-agent/progress.json` with `{{"milestones":[{{"id":"M1","status":"implemented"}}]}}`.

Goal:
{goal}

The frozen, user-authorized verification commands are:
{verification}
Arrange the repository so these exact commands pass.  You may run them yourself, but you may not change their authority.

Work only on unfinished work. If a real blocker prevents progress, write `.nightwatch-agent/blocker.json` with a concise reason and explain it. Never use dangerous approval or sandbox bypass flags."""


def resume_prompt(store: NightwatchStore, state: dict[str, Any]) -> str:
    commands = store.load_policy()["final_commands"]
    verification = "\n".join(f"- {command}" for command in commands) or "- No trusted automatic verification is configured."
    return f"""Resume the same exact Nightwatch task thread. Read `.nightwatch-agent/context.json`, `.nightwatch-agent/proposed-plan.json`, `.nightwatch-agent/progress.json` if present, Git status, Git diff, and recent commits. Trusted Nightwatch control-plane state is outside the workspace. The durable task state says generation={state['generation']} and thread_id={state.get('thread_id')}.

Determine which milestones are actually verified from available repository evidence. Do not repeat completed work. Continue only unfinished work. Update `.nightwatch-agent/progress.json` only with implemented/working/blocked facts. Nightwatch, not the model, decides verified/DONE and only runs frozen user-authorized checks. The frozen user-authorized verification commands are:
{verification}
Repair unfinished work until those exact commands pass; do not edit policy or claim verification authority. If blocked, write `.nightwatch-agent/blocker.json` and explain the missing human decision or external prerequisite."""


class Supervisor:
    def __init__(self, store: NightwatchStore, quota_provider=None):
        self.store = store
        self.quota_provider = quota_provider or make_quota_provider()
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True
        try:
            state = self.store.load_state()
        except StateIntegrityError:
            return
        active = state.get("active_process")
        if isinstance(active, dict) and process_matches(active):
            try:
                os.kill(active["pid"], signal.SIGINT)
            except OSError:
                pass

    def execute(self, start: bool = False) -> dict[str, Any]:
        try:
            with self.store.supervisor_lease():
                owner = {"pid": os.getpid(), "acquired_at": now_iso()}
                self.store.mutate("supervisor_lease_acquired", "supervisor lifetime lease acquired", lambda item: {**item, "supervisor_owner": owner})
                try:
                    return self._execute(start)
                finally:
                    try:
                        self.store.mutate("supervisor_lease_released", "supervisor lifetime lease released", lambda item: {**item, "supervisor_owner": None if item.get("supervisor_owner") == owner else item.get("supervisor_owner")})
                    except StateIntegrityError:
                        pass
        except SupervisorAlreadyRunning:
            raise

    def _execute(self, start: bool = False) -> dict[str, Any]:
        state = self.store.load_state()
        if state["state"] == State.DONE.value:
            return state
        if not start and state["state"] in {State.BLOCKED.value, State.STOPPED.value, State.FAILED.value}:
            if not state.get("thread_id"):
                return self._fail_closed("cannot manually resume without an exact durable thread_id", ErrorKind.STATE)
            self.store.transition(State.RECOVERING, "manual_resume_requested", "explicit resume requested for a terminal run; exact thread preserved")
        if start and state["state"] == State.NEW.value:
            if not self._preflight():
                return self.store.load_state()
        elif state["state"] == State.NEW.value:
            self.store.transition(State.PREFLIGHT, "preflight_started", "resume requested for a new run")
            if not self._preflight_checks():
                return self.store.load_state()
        if not self._recover_supervisor_restart():
            return self.store.load_state()
        try:
            while not self._stop_requested:
                state = self.store.load_state()
                current = State(state["state"])
                if current in TERMINAL_STATES:
                    return state
                if current == State.WAIT_QUOTA:
                    if not self._wait_and_revalidate_quota():
                        return self.store.load_state()
                    continue
                if current == State.RETRY_BACKOFF:
                    self._sleep_backoff(state)
                    self.store.transition(State.RECOVERING, "transient_retry_ready", "bounded transient backoff elapsed")
                    continue
                if current in {State.RECOVERING, State.RUNNING, State.PREFLIGHT, State.STOPPED, State.FAILED}:
                    if current == State.PREFLIGHT:
                        self.store.transition(State.RUNNING, "provider_launch_ready", "preflight passed")
                    elif current in {State.STOPPED, State.FAILED}:
                        if not state.get("thread_id"):
                            return self._fail_closed("cannot resume without an exact durable thread_id", ErrorKind.STATE)
                        self.store.transition(State.RECOVERING, "manual_resume_requested", "resume command requested exact thread")
                    self._run_turn()
                    continue
                if current == State.VERIFYING:
                    if self._finish_verification():
                        return self.store.load_state()
                    continue
                return self._fail_closed(f"unsupported state {current.value}", ErrorKind.STATE)
        except KeyboardInterrupt:
            self.request_stop()
            self.store.transition(State.STOPPED, "supervisor_interrupted", "Ctrl-C preserved durable state")
            return self.store.load_state()
        if self._stop_requested:
            self.store.transition(State.STOPPED, "supervisor_stopped", "stop requested; state preserved")
        return self.store.load_state()

    def _preflight(self) -> bool:
        self.store.transition(State.PREFLIGHT, "preflight_started", "repo, Codex, auth, and quota sanity checks")
        return self._preflight_checks()

    def _preflight_checks(self) -> bool:
        state = self.store.load_state()
        try:
            current_root = repo_root(self.store.repo)
            if str(current_root) != str(Path(state["repo"]).resolve()):
                self._fail_closed("repository root changed", ErrorKind.GIT)
                return False
            git = snapshot(current_root)
            if git.conflicts:
                self._fail_closed("repository has unresolved merge conflicts", ErrorKind.GIT)
                return False
        except GitError:
            self._fail_closed("Git preflight failed", ErrorKind.GIT)
            return False
        if os.environ.get("NIGHTWATCH_IGNORE_CONCURRENT_CODEX") != "1":
            running_codex = find_repo_codex_processes(current_root, exclude_pid=os.getpid())
            current_active = state.get("active_process")
            if isinstance(current_active, dict) and current_active.get("pid"):
                running_codex = [p for p in running_codex if p["pid"] != current_active["pid"]]
            if running_codex:
                pids = ", ".join(str(p["pid"]) for p in running_codex)
                self._fail_closed(
                    f"another Codex process (PID {pids}) is active in this repository; "
                    "use `nightwatch watch` to monitor it passively or wait until it exits",
                    ErrorKind.STATE,
                )
                return False
        binary = os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
        if not (Path(binary).is_file() and os.access(binary, os.X_OK)) and not shutil.which(binary):
            self._fail_closed("Codex CLI was not found", ErrorKind.UNKNOWN)
            return False
        auth_ok = self._auth_sanity(binary)
        if not auth_ok:
            self._fail_closed("Codex authentication sanity check failed", ErrorKind.AUTH)
            return False
        try:
            quota = self.quota_provider.read()
            self.store.mutate("quota_sanity_ok", "quota provider returned a validated snapshot", lambda item: {**item, "quota": quota.to_dict(), "quota_source": quota.source})
        except Exception as exc:
            # A first run can still start: quota may be healthy but its optional
            # read may be network-blocked. Automatic recovery remains fail closed.
            self.store.append_event("quota_sanity_unavailable", "quota source unavailable during preflight", {"error": type(exc).__name__})
        return True

    def _auth_sanity(self, binary: str) -> bool:
        if os.environ.get("NIGHTWATCH_SKIP_AUTH_CHECK") == "1" or os.environ.get("NIGHTWATCH_CODEX_BIN"):
            return True
        try:
            result = subprocess.run([binary, "login", "status"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _recover_supervisor_restart(self) -> bool:
        state = self.store.load_state()
        active = state.get("active_process")
        if isinstance(active, dict):
            if pid_alive(active.get("pid")) and not process_matches(active):
                self._fail_closed("active provider PID identity cannot be proven after restart", ErrorKind.STATE)
                return False
            self.store.transition(State.RECOVERING, "active_process_reconciled", "supervisor restart found a prior Codex child", {"active_process": active})
            deadline = time.monotonic() + 60
            while process_matches(active) and time.monotonic() < deadline:
                time.sleep(0.25)
            if process_matches(active):
                self._fail_closed("prior Codex process is still alive but its output is unobserved", ErrorKind.STATE)
                return False
            state = self.store.mutate("active_process_exited", "prior Codex child is no longer alive", lambda item: {**item, "active_process": None})
            if not state.get("thread_id"):
                self._fail_closed("supervisor restarted before exact thread_id was durable", ErrorKind.STATE)
                return False
            if state.get("resume_claim"):
                self._fail_closed("prior exact-thread resume result is ambiguous after supervisor restart", ErrorKind.STATE)
                return False
            return True
        if state["state"] == State.RUNNING.value and state.get("thread_id"):
            self.store.transition(State.RECOVERING, "supervisor_restart_recovery", "running state with exact thread recovered from disk")
        if state.get("resume_claim"):
            claim = state["resume_claim"]
            if claim.get("phase") == "claimed":
                self.store.mutate("resume_claim_proven_not_sent", "claim existed before provider spawn preparation; safe exact-thread retry", lambda item: {**item, "resume_claim": None})
                return True
            self._fail_closed("ambiguous resume claim after supervisor restart; run `nightwatch recover --ack-ambiguous` after review", ErrorKind.STATE)
            return False
        return True

    def _run_turn(self) -> dict[str, Any]:
        state = self.store.load_state()
        thread = state.get("thread_id")
        if state["state"] == State.PREFLIGHT.value and not thread:
            state = self.store.transition(State.RUNNING, "provider_launch_ready", "preflight passed")
        if state["state"] in {State.RUNNING.value, State.PREFLIGHT.value} and not thread:
            action = "starting new exact-thread goal"
        else:
            if not thread:
                return self._fail_closed("resume path has no exact thread_id", ErrorKind.STATE)
            action = "resuming exact thread"
        prompt = start_prompt(self.store, state["goal"]) if not thread else resume_prompt(self.store, state)
        self.store.mutate("provider_launch_prepared", action, lambda item: {**item, "resume_claim": ({**item["resume_claim"], "phase": "spawn_prepared"} if item.get("resume_claim") else None), "last_error": None})

        def on_spawn(pid: int, child_action: str) -> None:
            identity = process_identity(pid)
            if identity is None:
                raise StateIntegrityError("cannot persist Linux provider process identity")
            self.store.mutate("provider_started", "Codex child process started", lambda item: {**item, "active_process": {**identity, "action": child_action, "thread_id": item.get("thread_id"), "started_at": now_iso()}, "resume_claim": ({**item["resume_claim"], "phase": "spawned"} if item.get("resume_claim") else None)})
            crash_hook("AFTER_PROVIDER_SPAWN")

        def on_thread(candidate: str) -> None:
            self.store.mutate("thread_started", "exact thread ID captured from Codex JSONL", lambda item: {**item, "thread_id": candidate})
            crash_hook("AFTER_THREAD_CAPTURE")

        crash_hook("BEFORE_PROVIDER_SPAWN")
        result = run_codex(self.store, state["generation"], prompt, thread, on_spawn, on_thread)
        self.store.mutate("provider_finished", "Codex child process finished", lambda item: {**item, "active_process": None, "last_provider_exit": result.exit_code, "last_provider_signal": result.signal})
        crash_hook("AFTER_PROVIDER_EXIT")
        self.store.append_event("provider_result", "classified Codex result", {"error_kind": result.error_kind.value if result.error_kind else None, "exit_code": result.exit_code, "signal": result.signal, "event_count": result.event_count, "malformed_count": result.malformed_count})
        return self._handle_result(result)

    def _handle_result(self, result: ProviderResult) -> dict[str, Any]:
        if result.aborted or self._stop_requested:
            return self.store.transition(State.STOPPED, "provider_interrupted", "provider interrupted by user; state preserved", {"last_error": result.error_detail, "resume_claim": None})
        if result.error_kind in {ErrorKind.QUOTA_5H, ErrorKind.QUOTA_WEEKLY}:
            return self._enter_quota_wait(result)
        if result.error_kind in {ErrorKind.TEMPORARY_429, ErrorKind.CAPACITY, ErrorKind.NETWORK}:
            if self.store.load_state().get("resume_claim"):
                return self.store.transition(State.BLOCKED, "quota_resume_not_retried", "quota recovery provider turn failed after its single-flight lease; refusing a second resume for the same generation", {"last_error": result.error_detail, "error_kind": result.error_kind.value, "resume_claim": None})
            return self._handle_transient(result)
        if result.error_kind == ErrorKind.CRASH:
            return self._handle_crash(result)
        if result.error_kind == ErrorKind.BLOCKER:
            return self.store.transition(State.BLOCKED, "task_blocked", result.blocker or result.error_detail or "Codex reported a task blocker", {"blocker": result.blocker or result.error_detail, "last_error": result.error_detail, "error_kind": result.error_kind.value, "resume_claim": None})
        if result.error_kind in {ErrorKind.AUTH, ErrorKind.STATE, ErrorKind.MALFORMED, ErrorKind.GIT, ErrorKind.UNKNOWN}:
            target = State.BLOCKED if result.error_kind in {ErrorKind.STATE, ErrorKind.GIT, ErrorKind.MALFORMED} else State.FAILED
            return self.store.transition(target, "provider_failed", result.error_detail or result.error_kind.value, {"last_error": result.error_detail, "error_kind": result.error_kind.value, "resume_claim": None})
        return self._after_success()

    def _enter_quota_wait(self, result: ProviderResult) -> dict[str, Any]:
        state = self.store.load_state()
        if state["recoveries"] >= MAX_QUOTA_RECOVERIES:
            return self._fail_closed("quota recovery circuit breaker reached", ErrorKind.QUOTA_5H)
        windows = result.quota_windows or [QuotaWindow("weekly" if result.error_kind == ErrorKind.QUOTA_WEEKLY else "5h", 100, 10080 if result.error_kind == ErrorKind.QUOTA_WEEKLY else 300, result.reset_at)]
        reset = result.reset_at or max((window.resets_at or 0 for window in windows), default=0)
        source = result.reset_source or "bounded_fallback"
        if not reset:
            reset = int(time.time()) + (7 * 86400 if result.error_kind == ErrorKind.QUOTA_WEEKLY else 5 * 3600)
            source = "bounded_fallback"
        buffer_seconds = int(os.environ.get("NIGHTWATCH_QUOTA_BUFFER_SECONDS", QUOTA_BUFFER_SECONDS))
        next_at = reset + max(0, buffer_seconds)
        generation = state["generation"] + 1
        waiting = self.store.transition(State.WAIT_QUOTA, "quota_exhausted", result.error_detail or result.error_kind.value, {
            "generation": generation,
            "next_resume_at": datetime.fromtimestamp(next_at, timezone.utc).isoformat().replace("+00:00", "Z"),
            "quota_source": source,
            "quota_windows": [window.to_dict() for window in windows],
            "resume_claim": None,
            "retry_attempt": 0,
            "crash_attempt": 0,
            "recoveries": state["recoveries"] + 1,
            "last_error": result.error_detail,
            "error_kind": result.error_kind.value,
        })
        crash_hook("AFTER_QUOTA_DETECT")
        return waiting

    def _wait_and_revalidate_quota(self) -> bool:
        state = self.store.load_state()
        target = _dt(state.get("next_resume_at"))
        if target is None:
            self._fail_closed("WAIT_QUOTA has no valid next_resume_at", ErrorKind.STATE)
            return False
        _sleep_until(int(target.timestamp()), float(os.environ.get("NIGHTWATCH_WAIT_POLL_SECONDS", QUOTA_POLL_SECONDS)))
        governing = {window.get("name") for window in state.get("quota_windows", []) if isinstance(window, dict) and window.get("name")}
        if self._stop_requested:
            self.store.transition(State.STOPPED, "supervisor_stopped", "stop requested during quota wait")
            return False
        try:
            quota = self.quota_provider.read()
            self.store.mutate("quota_revalidated", "quota authority queried after reset", lambda item: {**item, "quota": quota.to_dict(), "quota_source": quota.source})
            if quota.source == "live_app_server":
                if not quota_recovered(quota, governing):
                    later = max((window.resets_at or int(time.time()) + QUOTA_POLL_SECONDS for window in quota.windows() if window.name in governing), default=int(time.time()) + QUOTA_POLL_SECONDS)
                    buffer_seconds = int(os.environ.get("NIGHTWATCH_QUOTA_BUFFER_SECONDS", QUOTA_BUFFER_SECONDS))
                    self.store.transition(State.WAIT_QUOTA, "quota_still_exhausted", "live quota authority is still exhausted; no resume sent", {"next_resume_at": datetime.fromtimestamp(later + max(0, buffer_seconds), timezone.utc).isoformat().replace("+00:00", "Z"), "resume_claim": None})
                    return True
                return self._start_quota_recovery("live quota authority confirmed recovery")
            return self._guarded_quota_probe("live quota authority unavailable; rollout is schedule-only")
        except Exception as exc:
            self.store.mutate("quota_revalidation_failed", "live quota authority unavailable after reset", lambda item: {**item, "last_error": f"quota authority unavailable: {type(exc).__name__}", "quota_source": "unavailable"})
            return self._guarded_quota_probe("live quota authority unavailable after provider-declared reset")

    def _start_quota_recovery(self, reason: str) -> bool:
        if not self._claim_resume():
            return False
        self.store.transition(State.RECOVERING, "resume_started", reason)
        return True

    def _guarded_quota_probe(self, reason: str) -> bool:
        state = self.store.load_state()
        if state.get("resume_claim"):
            self._fail_closed("quota generation already used its guarded provider availability attempt", ErrorKind.STATE)
            return False
        if not self._claim_resume():
            return False
        self.store.transition(State.RECOVERING, "quota_guarded_probe_started", reason)
        return True

    def _claim_resume(self) -> bool:
        claimed = False
        claim_id = f"{self.store.load_state()['run_id']}:{self.store.load_state()['generation']}"

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            if state["state"] != State.WAIT_QUOTA.value or state.get("thread_id") is None:
                return state
            existing = state.get("resume_claim")
            if existing and existing.get("generation") == state["generation"]:
                return state
            state["resume_claim"] = {"generation": state["generation"], "claim_id": claim_id, "claimed_at": now_iso(), "pid": os.getpid(), "phase": "claimed"}
            state["last_error"] = None
            claimed = True
            return state

        self.store.mutate("resume_claimed", "single-flight lease claimed for quota generation", mutate)
        if claimed:
            crash_hook("AFTER_RESUME_CLAIM")
        return claimed

    def _handle_transient(self, result: ProviderResult) -> dict[str, Any]:
        state = self.store.load_state()
        attempt = int(state.get("retry_attempt", 0)) + 1
        if attempt > MAX_TRANSIENT_RETRIES:
            return self.store.transition(State.BLOCKED, "transient_retry_exhausted", "transient failure retry budget exhausted", {"last_error": result.error_detail, "error_kind": result.error_kind.value if result.error_kind else None})
        delay = TRANSIENT_BACKOFF_SECONDS[min(attempt - 1, len(TRANSIENT_BACKOFF_SECONDS) - 1)]
        return self.store.transition(State.RETRY_BACKOFF, "transient_error", f"bounded retry {attempt}/{MAX_TRANSIENT_RETRIES} after {result.error_kind.value if result.error_kind else 'transient'}", {"retry_attempt": attempt, "next_resume_at": datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat().replace("+00:00", "Z"), "last_error": result.error_detail, "error_kind": result.error_kind.value if result.error_kind else None, "retry_delay_seconds": delay, "resume_claim": None})

    def _handle_crash(self, result: ProviderResult) -> dict[str, Any]:
        state = self.store.load_state()
        if state.get("resume_claim"):
            return self.store.transition(State.BLOCKED, "quota_resume_crashed", "quota recovery provider turn crashed after its single-flight lease; manual review required", {"last_error": result.error_detail, "error_kind": ErrorKind.CRASH.value, "resume_claim": None})
        attempt = int(state.get("crash_attempt", 0)) + 1
        if not state.get("thread_id") or attempt > MAX_CRASH_RETRIES:
            return self.store.transition(State.FAILED, "codex_crash_unrecoverable", "Codex crashed without a safe bounded recovery", {"last_error": result.error_detail, "error_kind": ErrorKind.CRASH.value, "crash_attempt": attempt, "resume_claim": None})
        delay = CRASH_BACKOFF_SECONDS[min(attempt - 1, len(CRASH_BACKOFF_SECONDS) - 1)]
        return self.store.transition(State.RETRY_BACKOFF, "codex_crash", f"exact-thread crash recovery {attempt}/{MAX_CRASH_RETRIES}", {"retry_attempt": 0, "crash_attempt": attempt, "last_error": result.error_detail, "error_kind": ErrorKind.CRASH.value, "next_resume_at": datetime.fromtimestamp(time.time() + delay, timezone.utc).isoformat().replace("+00:00", "Z"), "retry_delay_seconds": delay, "resume_claim": None})

    def _sleep_backoff(self, state: dict[str, Any]) -> None:
        target = _dt(state.get("next_resume_at"))
        if target:
            _sleep_until(int(target.timestamp()), 0.25)

    def _after_success(self) -> dict[str, Any]:
        if self.store.load_state().get("resume_claim"):
            crash_hook("BEFORE_CLAIM_CLEAR")
            self.store.mutate("resume_completed", "exact-thread provider result consumed by Nightwatch", lambda item: {**item, "resume_claim": None})
        self._check_git_recovery()
        adopted = adopt_proposed_plan(self.store)
        if not self.store.load_state().get("plan_ready") and not adopted:
            return self._fail_closed("Codex completed without a valid durable milestone plan", ErrorKind.STATE)
        ingest_progress(self.store)
        self.store.transition(State.VERIFYING, "verification_started", "Codex turn ended; Nightwatch is running milestone and final checks")
        return self._finish_verification()

    def _finish_verification(self) -> dict[str, Any]:
        state = self.store.load_state()
        try:
            git = self._check_git_recovery()
            result = verify_milestones(self.store, git)
        except (GitError, StateIntegrityError) as exc:
            return self._fail_closed(f"verification precondition failed: {type(exc).__name__}", ErrorKind.GIT)
        attempts = int(state.get("verification_attempts", 0)) + 1
        all_ok = result["all_milestones_verified"] and result["all_final_checks_passed"] and state.get("plan_ready", False) and state.get("acceptance_ready", False) and not result["git"]["conflicts"]
        self.store.mutate("verification_completed", "mechanical milestone/final verification completed", lambda item: {**item, "verification_attempts": attempts, "last_verification": result, "last_verified_commit": result["git"].get("head") if all_ok else item.get("last_verified_commit"), "final_verification_passed": all_ok, "last_git_head": result["git"].get("head")})
        if all_ok:
            return self._write_done_report(result)
        if not state.get("acceptance_ready", False):
            return self.store.transition(State.AWAITING_ACCEPTANCE, "awaiting_trusted_acceptance", "implementation may be complete but no frozen user verification policy can authorize DONE", {"last_error": "no trusted --verify policy was supplied"})
        if attempts >= MAX_CRASH_RETRIES:
            return self.store.transition(State.BLOCKED, "verification_failed", "verification did not pass within the bounded correction budget", {"last_error": "one or more milestone/final checks failed", "error_kind": ErrorKind.BLOCKER.value})
        return self.store.transition(State.RUNNING, "verification_failed_continue", "verification failed; exact thread must correct the repository")

    def _check_git_recovery(self) -> GitSnapshot:
        state = self.store.load_state()
        git = snapshot(self.store.repo)
        if git.conflicts:
            self._fail_closed("Git contains unresolved conflicts", ErrorKind.GIT)
            raise GitError("Git contains unresolved conflicts")
        previous = state.get("last_verified_commit")
        if previous and git.head and git.head != previous and not is_ancestor(previous, git.head, self.store.repo):
            self._fail_closed("repository HEAD is not a descendant of the last verified commit", ErrorKind.GIT)
            raise GitError("repository HEAD is not a descendant of the last verified commit")
        self.store.mutate("git_observed", "Git reality captured for recovery", lambda item: {**item, "last_git_head": git.head})
        return git

    def _fail_closed(self, reason: str, kind: ErrorKind) -> dict[str, Any]:
        current = self.store.load_state()
        target = State.BLOCKED if kind in {ErrorKind.STATE, ErrorKind.GIT, ErrorKind.MALFORMED, ErrorKind.BLOCKER} else State.FAILED
        return self.store.transition(target, "fail_closed", reason, {"last_error": reason, "error_kind": kind.value, "blocker": reason if target == State.BLOCKED else current.get("blocker")})

    def _write_done_report(self, verification: dict[str, Any]) -> dict[str, Any]:
        crash_hook("BEFORE_DONE_WRITE")
        state = self.store.transition(State.DONE, "done_guard_passed", "all required milestones and final verification passed", {"final_verification_passed": True, "last_verified_commit": verification["git"].get("head")})
        self.store.write_report(build_report(self.store, state, verification))
        self.store.append_event("final_report_written", "final report generated after DONE guard")
        return self.store.load_state()


def build_report(store: NightwatchStore, state: dict[str, Any], verification: dict[str, Any] | None = None) -> str:
    plan = store.load_plan()
    verification = verification or state.get("last_verification") or {}
    progress = verification.get("progress", {})
    quota = state.get("quota") or {}
    quota_lines = []
    for label, key in (("5h", "primary"), ("weekly", "secondary")):
        window = quota.get(key) if isinstance(quota, dict) else None
        if isinstance(window, dict):
            quota_lines.append(f"- {label}: {window.get('used_percent')}% used; duration={window.get('window_duration_mins')}m; reset={window.get('resets_at')}")
    if not quota_lines:
        quota_lines = ["- (no validated snapshot persisted for this run)"]
    return "\n".join([
        "# Nightwatch report",
        "",
        "## GOAL",
        state["goal"],
        "",
        f"- RESULT: {state['state']}",
        f"- RUNTIME: {state['created_at']} → {state['updated_at']}",
        f"- THREAD_ID: {state.get('thread_id') or '(none)' }",
        f"- QUOTA SOURCE: {state.get('quota_source') or '(none)'}",
        f"- RECOVERIES: {state.get('recoveries', 0)}",
        f"- FINAL HEAD: {state.get('last_verified_commit') or state.get('last_git_head') or '(unknown)'}",
        "",
        "## QUOTA WINDOWS",
        *quota_lines,
        "",
        "## MILESTONES",
        f"- implemented: {progress.get('implemented_count', 0)} / {progress.get('total_count', len(plan['milestones']))}",
        f"- verified: {progress.get('verified_count', 0)} / {progress.get('total_count', len(plan['milestones']))}",
        "",
        "## VERIFICATION",
        *[f"- {check.get('command')}: {'PASS' if check.get('ok') else 'FAIL'}" for check in verification.get("final_checks", [])],
        "",
        "## ERRORS/BLOCKERS",
        f"- {state.get('last_error') or state.get('blocker') or '(none)'}",
        "",
        "## COMMITS",
        f"- last verified commit: {state.get('last_verified_commit') or '(none)'}",
        "",
    ])


class PassiveWatcher:
    """Non-invasive observer and automatic takeover supervisor for active Codex sessions."""

    def __init__(self, store: NightwatchStore, codex_home: str | Path | None = None):
        self.store = store
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def discover_active_session(self) -> dict[str, Any] | None:
        processes = find_repo_codex_processes(self.store.repo, exclude_pid=os.getpid())
        threads = find_active_threads_for_repo(self.store.repo, self.codex_home)
        user_threads = [t for t in threads if t.get("thread_source") == "user"]
        primary = user_threads[0] if user_threads else (threads[0] if threads else None)
        active_pid = processes[0]["pid"] if processes else None
        rollout_path = None
        if active_pid:
            fd_dir = Path(f"/proc/{active_pid}/fd")
            if fd_dir.exists():
                for entry in fd_dir.iterdir():
                    try:
                        target = os.readlink(str(entry))
                        if "rollout-" in target and target.endswith(".jsonl"):
                            if primary and primary["id"] in target:
                                rollout_path = Path(target)
                                break
                            elif rollout_path is None:
                                rollout_path = Path(target)
                    except OSError:
                        continue
        if not rollout_path and primary and primary.get("rollout_path"):
            cand = Path(primary["rollout_path"])
            if cand.exists():
                rollout_path = cand
        if not primary and not rollout_path and not active_pid:
            return None
        return {
            "pid": active_pid,
            "processes": processes,
            "thread": primary,
            "thread_id": primary["id"] if primary else None,
            "rollout_path": str(rollout_path) if rollout_path else None,
        }

    def inspect_live_snapshot(self) -> dict[str, Any]:
        info = self.discover_active_session()
        if not info or not info.get("rollout_path"):
            return {
                "active": bool(info and info.get("processes")),
                "pid": info.get("pid") if info else None,
                "processes": info.get("processes", []) if info else [],
                "thread_id": info.get("thread_id") if info else None,
            }
        path = Path(info["rollout_path"])
        rate_limits = None
        tokens = None
        subagents = []
        last_turn_type = None
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        t = data.get("type")
                        payload = data.get("payload", {})
                        if isinstance(payload, dict):
                            if "rate_limits" in payload:
                                rate_limits = payload["rate_limits"]
                            if "info" in payload and "total_token_usage" in payload["info"]:
                                tokens = payload["info"]["total_token_usage"]
                        if "<subagents>" in line:
                            for sub in re.findall(r"-\s*([0-9a-fA-F-]{36}):\s*(\w+)", line):
                                if sub not in subagents:
                                    subagents.append(sub)
                        last_turn_type = t
                    except Exception:
                        pass
        primary_thread = info.get("thread") or {}
        return {
            "active": True,
            "pid": info.get("pid"),
            "pid_alive": pid_alive(info.get("pid")) if info.get("pid") else False,
            "thread_id": info.get("thread_id"),
            "model": primary_thread.get("model"),
            "branch": primary_thread.get("git_branch"),
            "title": primary_thread.get("title") or primary_thread.get("first_user_message"),
            "rate_limits": rate_limits,
            "tokens": tokens,
            "subagents": subagents,
            "last_turn_type": last_turn_type,
            "rollout_path": info.get("rollout_path"),
        }

    def watch(
        self,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        poll_interval: float = 2.0,
        auto_takeover: bool = False,
        goal: str | None = None,
        verify_commands: list[str] | None = None,
    ) -> dict[str, Any]:
        info = self.discover_active_session()
        if not info or not info.get("rollout_path"):
            snap = {"active": False, "pid": info.get("pid") if info else None}
            if on_update:
                on_update(snap)
            return snap

        last_status_str = ""
        while not self._stop_requested:
            snap = self.inspect_live_snapshot()
            status_str = f"{snap.get('pid')}:{snap.get('pid_alive')}:{snap.get('rate_limits')}:{snap.get('tokens')}"
            if status_str != last_status_str:
                last_status_str = status_str
                if on_update:
                    on_update(snap)

            limits = snap.get("rate_limits") or {}
            primary_exhausted = (limits.get("primary", {}).get("used_percent") or 0) >= 100
            secondary_exhausted = (limits.get("secondary", {}).get("used_percent") or 0) >= 100
            quota_hit = primary_exhausted or secondary_exhausted

            if quota_hit and auto_takeover:
                thread_id = snap.get("thread_id")
                if not thread_id:
                    break
                if not self.store.exists():
                    self.store.initialize(
                        make_run_id(str(self.store.repo)),
                        goal or snap.get("title") or "Auto-takeover after quota limit",
                        str(self.store.repo),
                        verify_commands=verify_commands or [],
                        thread_id=thread_id,
                    )
                supervisor = Supervisor(self.store)
                return supervisor.execute(start=False)

            if not snap.get("pid_alive"):
                if auto_takeover:
                    thread_id = snap.get("thread_id")
                    if not self.store.exists() and thread_id:
                        self.store.initialize(
                            make_run_id(str(self.store.repo)),
                            goal or snap.get("title") or "Supervised session continuation",
                            str(self.store.repo),
                            verify_commands=verify_commands or [],
                            thread_id=thread_id,
                        )
                    if self.store.exists():
                        supervisor = Supervisor(self.store)
                        return supervisor.execute(start=False)
                break

            time.sleep(poll_interval)

        return self.inspect_live_snapshot()
