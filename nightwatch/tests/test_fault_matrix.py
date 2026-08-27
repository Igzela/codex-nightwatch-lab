from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from nightwatch.codex import classify_failure  # noqa: E402
from nightwatch.models import ErrorKind, ProviderResult, QuotaSnapshot, QuotaWindow, State  # noqa: E402
from nightwatch.storage import NightwatchStore  # noqa: E402
from nightwatch.supervisor import Supervisor  # noqa: E402


FAKE = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"


def git_run(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check,
    )


def fixture() -> tuple[tempfile.TemporaryDirectory, Path, Path, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-fault-")
    root = Path(temporary.name)
    git_run(root, "init", "-q")
    git_run(root, "config", "user.email", "nightwatch@example.invalid")
    git_run(root, "config", "user.name", "Nightwatch Test")
    (root / "README.md").write_text("fixture\n")
    git_run(root, "add", "README.md")
    git_run(root, "commit", "-qm", "fixture")
    plan = root / "plan.json"
    plan.write_text(json.dumps({
        "required_verification_commands": ["git diff --check"],
        "milestones": [{
            "id": "M1", "title": "fixture", "weight": 1, "required": True,
            "verification_commands": ["test -f fake-implemented.txt"],
        }],
    }))
    progress = root / "progress.json"
    progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}))
    return temporary, root, plan, progress


class RecoveredQuota:
    def read(self) -> QuotaSnapshot:
        return QuotaSnapshot(
            "fake-provider", "now",
            QuotaWindow("5h", 0, 300, int(time.time()) + 1),
            QuotaWindow("weekly", 0, 10080, int(time.time()) + 1),
        )


class ExhaustedQuota:
    def __init__(self) -> None:
        self.calls = 0

    def read(self) -> QuotaSnapshot:
        self.calls += 1
        return QuotaSnapshot(
            "fake-provider", "now",
            QuotaWindow("5h", 100, 300, int(time.time()) + 2),
            QuotaWindow("weekly", 0, 10080, int(time.time()) + 2),
        )


class FaultMatrixTests(unittest.TestCase):
    def env(self, plan: Path, progress: Path, scenario: str) -> patch:
        return patch.dict(os.environ, {
            "NIGHTWATCH_CODEX_BIN": str(FAKE),
            "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
            "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
            "NIGHTWATCH_WAIT_POLL_SECONDS": "0.05",
            "FAKE_CODEX_SCENARIO": scenario,
            "FAKE_CODEX_PLAN_FILE": str(plan),
            "FAKE_CODEX_PROGRESS_FILE": str(progress),
        }, clear=False)

    def test_malformed_jsonl_and_missing_thread_fail_closed(self):
        for scenario in ("malformed", "missing_thread"):
            temporary, root, plan, progress = fixture()
            try:
                store = NightwatchStore(root)
                store.initialize(f"run-{scenario}", "goal", str(root))
                with self.env(plan, progress, scenario):
                    final = Supervisor(store, RecoveredQuota()).execute(start=True)
                self.assertEqual(final["state"], State.BLOCKED.value, scenario)
                self.assertNotEqual(final["state"], State.DONE.value)
                self.assertIsNone(final.get("active_pid"))
            finally:
                temporary.cleanup()

    def test_transient_errors_are_bounded_backoff_not_immediate_retry(self):
        for kind in (ErrorKind.TEMPORARY_429, ErrorKind.CAPACITY, ErrorKind.NETWORK):
            temporary, root, _plan, _progress = fixture()
            try:
                store = NightwatchStore(root)
                store.initialize(f"run-{kind.value}", "goal", str(root))
                store.transition(State.PREFLIGHT, "preflight_started", "test")
                store.transition(State.RUNNING, "provider_launch_ready", "test", {"thread_id": "TEST-001"})
                supervisor = Supervisor(store, RecoveredQuota())
                result = supervisor._handle_result(ProviderResult(1, None, "TEST-001", 1, 0, error_kind=kind, error_detail=kind.value))
                self.assertEqual(result["state"], State.RETRY_BACKOFF.value, kind.value)
                self.assertGreater(result["next_resume_at"], result["updated_at"], kind.value)
                self.assertEqual(result["active_pid"], None)
            finally:
                temporary.cleanup()

    def test_quota_revalidation_still_exhausted_never_claims_or_resumes(self):
        temporary, root, _plan, _progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-still-quota", "goal", str(root))
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test", {"thread_id": "TEST-001"})
            supervisor = Supervisor(store, ExhaustedQuota())
            with patch.dict(os.environ, {"NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0", "NIGHTWATCH_WAIT_POLL_SECONDS": "0.01"}):
                supervisor._enter_quota_wait(ProviderResult(1, None, "TEST-001", 1, 0, error_kind=ErrorKind.QUOTA_5H, reset_at=int(time.time()), reset_source="provider_epoch"))
                self.assertTrue(supervisor._wait_and_revalidate_quota())
            state = store.load_state()
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            self.assertIsNone(state["resume_claim"])
            self.assertEqual(state["recoveries"], 1)
        finally:
            temporary.cleanup()

    def test_duplicate_quota_events_classify_once_and_lease_is_single_flight(self):
        event = {"type": "error", "error": {"code": "usage_limit_reached", "rateLimitReachedType": "5h", "resetsAt": int(time.time()) + 1}}
        kind, _detail, _reset, _source, _windows, _blocker = classify_failure([event, event], "", 1, None)
        self.assertEqual(kind, ErrorKind.QUOTA_5H)

        temporary, root, _plan, _progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-duplicate", "goal", str(root))
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test", {"thread_id": "TEST-001"})
            supervisor = Supervisor(store, RecoveredQuota())
            supervisor._enter_quota_wait(ProviderResult(1, None, "TEST-001", 2, 0, error_kind=kind, reset_at=int(time.time()), reset_source="provider_epoch"))
            self.assertTrue(supervisor._claim_resume())
            self.assertFalse(supervisor._claim_resume())
        finally:
            temporary.cleanup()

    def test_quota_resume_transient_failure_does_not_start_second_process(self):
        temporary, root, _plan, _progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-quota-transient", "goal", str(root))
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test", {"thread_id": "TEST-001"})
            supervisor = Supervisor(store, RecoveredQuota())
            supervisor._enter_quota_wait(ProviderResult(1, None, "TEST-001", 1, 0, error_kind=ErrorKind.QUOTA_5H, reset_at=int(time.time()), reset_source="provider_epoch"))
            self.assertTrue(supervisor._claim_resume())
            store.transition(State.RECOVERING, "resume_started", "test")
            final = supervisor._handle_result(ProviderResult(1, None, "TEST-001", 1, 0, error_kind=ErrorKind.NETWORK, error_detail="disconnect"))
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertIsNone(final["resume_claim"])
        finally:
            temporary.cleanup()

    def test_wrong_repo_and_git_conflict_are_blocked_before_provider(self):
        temporary, root, _plan, _progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-wrong-root", "goal", str(root / "wrong"))
            with self.env(root / "missing", root / "missing", "normal"):
                final = Supervisor(store, RecoveredQuota()).execute(start=True)
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertFalse((root / ".fake-codex-state.json").exists())
        finally:
            temporary.cleanup()

        temporary, root, _plan, _progress = fixture()
        try:
            (root / "conflict.txt").write_text("base\n")
            git_run(root, "add", "conflict.txt")
            git_run(root, "commit", "-qm", "base")
            branch = git_run(root, "branch", "--show-current").stdout.strip()
            git_run(root, "checkout", "-qb", "other")
            (root / "conflict.txt").write_text("other\n")
            git_run(root, "commit", "-qam", "other")
            git_run(root, "checkout", "-q", branch)
            (root / "conflict.txt").write_text("main\n")
            git_run(root, "commit", "-qam", "main")
            merge = git_run(root, "merge", "other", check=False)
            self.assertNotEqual(merge.returncode, 0)
            store = NightwatchStore(root)
            store.initialize("run-conflict", "goal", str(root))
            with self.env(root / "missing", root / "missing", "normal"):
                final = Supervisor(store, RecoveredQuota()).execute(start=True)
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertFalse((root / ".fake-codex-state.json").exists())
        finally:
            temporary.cleanup()

    def test_quota_hit_again_is_a_new_generation_and_eventually_reuses_thread(self):
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-quota-again", "goal", str(root))
            with self.env(plan, progress, "quota_again"), patch.dict(os.environ, {"FAKE_CODEX_RESET_SECONDS": "0"}):
                final = Supervisor(store, RecoveredQuota()).execute(start=True)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            self.assertEqual(final["generation"], 3)
            fake_state = json.loads((root / ".fake-codex-state.json").read_text())
            self.assertEqual(fake_state["starts"], 1)
            self.assertEqual(fake_state["resumes"], 2)
        finally:
            temporary.cleanup()

    def test_manual_resume_reopens_stopped_run_without_new_thread(self):
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-manual", "goal", str(root))
            with self.env(plan, progress, "auth"):
                first = Supervisor(store, RecoveredQuota()).execute(start=True)
            self.assertEqual(first["state"], State.FAILED.value)
            with self.env(plan, progress, "normal"):
                final = Supervisor(store, RecoveredQuota()).execute(start=False)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            fake_state = json.loads((root / ".fake-codex-state.json").read_text())
            self.assertEqual(fake_state["starts"], 1)
            self.assertEqual(fake_state["resumes"], 1)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
