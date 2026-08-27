from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .codex import run_codex
from .git import GitError, GitSnapshot, is_ancestor, repo_root, snapshot
from .milestones import adopt_proposed_plan, current_milestone, ingest_progress, verify_milestones
from .models import ErrorKind, ProviderResult, QuotaSnapshot, QuotaWindow, State, TERMINAL_STATES
from .quota import QuotaError, make_quota_provider, quota_recovered
from .storage import NightwatchStore, StateIntegrityError, now_iso


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


def sys_platform_linux() -> bool:
    return sys.platform.startswith("linux")


def start_prompt(goal: str) -> str:
    return f"""You are working under Nightwatch supervision in this Git repository.

Read `.nightwatch/goal.md`, Git status/diff, and any existing `.nightwatch/checkpoint.md` first.
Before making implementation changes, create `.nightwatch/proposed-plan.json` with this exact shape:
{{"milestones":[{{"id":"M1","title":"...","weight":1,"required":true,"verification_commands":["..."]}}],"required_verification_commands":["..."]}}
Use concrete milestone verification commands that are safe and relevant to this repository. Do not claim a milestone is verified; Nightwatch runs commands and owns verified status.
After each milestone is implemented, update `.nightwatch/progress.json` with `{{"milestones":[{{"id":"M1","status":"implemented"}}]}}`.

Goal:
{goal}

Work only on unfinished work. Run the relevant verification commands before reporting completion. If a real blocker prevents progress, write `.nightwatch/blocker.json` with a concise reason and explain it. Never use dangerous approval or sandbox bypass flags."""


def resume_prompt(store: NightwatchStore, state: dict[str, Any]) -> str:
    return f"""Resume the same exact Nightwatch task thread. Read `.nightwatch/goal.md`, `.nightwatch/plan.json`, `.nightwatch/checkpoint.md`, `.nightwatch/progress.json` if present, Git status, Git diff, and recent commits. The durable task state says generation={state['generation']} and thread_id={state.get('thread_id')}.

Determine which milestones are actually verified from Nightwatch evidence. Do not repeat completed work. Continue only unfinished work. Update `.nightwatch/progress.json` only with implemented/working/blocked facts. Run each required verification command before considering a milestone complete. Nightwatch, not the model, decides verified/DONE. If blocked, write `.nightwatch/blocker.json` and explain the missing human decision or external prerequisite."""


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
        pid = state.get("active_pid")
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                pass

    def execute(self, start: bool = False) -> dict[str, Any]:
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
        active_pid = state.get("active_pid")
        if active_pid:
            self.store.transition(State.RECOVERING, "active_process_reconciled", "supervisor restart found a prior Codex child", {"active_pid": active_pid})
            deadline = time.monotonic() + 60
            while pid_alive(active_pid) and time.monotonic() < deadline:
                time.sleep(0.25)
            if pid_alive(active_pid):
                self._fail_closed("prior Codex process is still alive but its output is unobserved", ErrorKind.STATE)
                return False
            state = self.store.mutate("active_process_exited", "prior Codex child is no longer alive", lambda item: {**item, "active_pid": None, "active_action": None})
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
            self._fail_closed("ambiguous resume claim after supervisor restart; refusing duplicate resume", ErrorKind.STATE)
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
        prompt = start_prompt(state["goal"]) if not thread else resume_prompt(self.store, state)
        self.store.mutate("provider_launch_prepared", action, lambda item: {**item, "active_action": "resume" if thread else "start", "last_error": None})

        def on_spawn(pid: int, child_action: str) -> None:
            self.store.mutate("provider_started", "Codex child process started", lambda item: {**item, "active_pid": pid, "active_action": child_action, "active_started_at": now_iso()})

        def on_thread(candidate: str) -> None:
            self.store.mutate("thread_started", "exact thread ID captured from Codex JSONL", lambda item: {**item, "thread_id": candidate})

        result = run_codex(self.store, state["generation"], prompt, thread, on_spawn, on_thread)
        self.store.mutate("provider_finished", "Codex child process finished", lambda item: {**item, "active_pid": None, "active_action": None, "last_provider_exit": result.exit_code, "last_provider_signal": result.signal})
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
        return self.store.transition(State.WAIT_QUOTA, "quota_exhausted", result.error_detail or result.error_kind.value, {
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

    def _wait_and_revalidate_quota(self) -> bool:
        state = self.store.load_state()
        target = _dt(state.get("next_resume_at"))
        if target is None:
            self._fail_closed("WAIT_QUOTA has no valid next_resume_at", ErrorKind.STATE)
            return False
        _sleep_until(int(target.timestamp()), float(os.environ.get("NIGHTWATCH_WAIT_POLL_SECONDS", QUOTA_POLL_SECONDS)))
        failures = 0
        governing = {window.get("name") for window in state.get("quota_windows", []) if isinstance(window, dict) and window.get("name")}
        while not self._stop_requested:
            try:
                quota = self.quota_provider.read()
                failures = 0
                self.store.mutate("quota_revalidated", "fresh quota read completed before any resume", lambda item: {**item, "quota": quota.to_dict(), "quota_source": quota.source})
                if not quota_recovered(quota, governing):
                    later = max((window.resets_at or int(time.time()) + QUOTA_POLL_SECONDS for window in quota.windows() if window.name in governing), default=int(time.time()) + QUOTA_POLL_SECONDS)
                    buffer_seconds = int(os.environ.get("NIGHTWATCH_QUOTA_BUFFER_SECONDS", QUOTA_BUFFER_SECONDS))
                    self.store.transition(State.WAIT_QUOTA, "quota_still_exhausted", "quota revalidation is still exhausted; no resume sent", {"next_resume_at": datetime.fromtimestamp(later + max(0, buffer_seconds), timezone.utc).isoformat().replace("+00:00", "Z"), "resume_claim": None})
                    return True
                if not self._claim_resume():
                    return False
                self.store.transition(State.RECOVERING, "resume_started", "single-flight exact-thread resume claimed after quota revalidation")
                return True
            except Exception as exc:
                failures += 1
                self.store.mutate("quota_revalidation_failed", "quota recovery could not be freshly confirmed", lambda item: {**item, "last_error": f"quota revalidation failed: {type(exc).__name__}", "quota_source": "unavailable"})
                if failures >= MAX_QUOTA_REVALIDATION_FAILURES:
                    self._fail_closed("quota recovery cannot be safely confirmed", ErrorKind.STATE)
                    return False
                time.sleep(min(QUOTA_POLL_SECONDS, 2 * failures))
        self.store.transition(State.STOPPED, "supervisor_stopped", "stop requested during quota wait")
        return False

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
            state["resume_claim"] = {"generation": state["generation"], "claim_id": claim_id, "claimed_at": now_iso(), "pid": os.getpid()}
            state["last_error"] = None
            claimed = True
            return state

        self.store.mutate("resume_claimed", "single-flight lease claimed for quota generation", mutate)
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
        all_ok = result["all_milestones_verified"] and result["all_final_checks_passed"] and state.get("plan_ready", False) and not result["git"]["conflicts"]
        self.store.mutate("verification_completed", "mechanical milestone/final verification completed", lambda item: {**item, "verification_attempts": attempts, "last_verification": result, "last_verified_commit": result["git"].get("head") if all_ok else item.get("last_verified_commit"), "final_verification_passed": all_ok, "last_git_head": result["git"].get("head")})
        if all_ok:
            return self._write_done_report(result)
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
