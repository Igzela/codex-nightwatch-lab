from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))


from nightwatch.models import (
    ErrorKind,
    ProviderResult,
    QuotaSnapshot,
    QuotaWindow,
    State,
    empty_state,
    validate_state,
)
from nightwatch.process_identity import pid_alive
from nightwatch.providers import AgyProviderAdapter, CodexProviderAdapter, get_provider_adapter
from nightwatch.storage import NightwatchStore
from nightwatch.supervisor import Supervisor


FAKE_AGY = Path(__file__).resolve().parents[2] / "test-artifacts" / "fake-agy" / "fake_agy.py"


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email 'test@example.com'")
    os.system(f"git -C {repo} config user.name 'Test'")
    (repo / "README.md").write_text("initial\n")
    os.system(f"git -C {repo} add README.md && git -C {repo} commit -qm 'init'")


class AgyProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AgyProviderAdapter()

    def test_provider_name_and_registry(self) -> None:
        self.assertEqual(self.adapter.name, "agy")
        self.assertIsInstance(get_provider_adapter("agy"), AgyProviderAdapter)
        self.assertIsInstance(get_provider_adapter("codex"), CodexProviderAdapter)

    def test_build_command_start(self) -> None:
        args, action = self.adapter.build_command(
            repo="/tmp/test",
            thread_id=None,
            prompt="implement feature X",
            model="gemini-3.8-flash-high",
            reasoning_effort="high",
        )
        self.assertEqual(action, "start")
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--output-format", args)
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--model", args)
        self.assertEqual(args[args.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertIn("-p", args)
        self.assertEqual(args[args.index("-p") + 1], "implement feature X")
        self.assertNotIn("--conversation", args)
        self.assertNotIn("-c", args)
        self.assertNotIn("--continue", args)

    def test_build_command_exact_resume_never_uses_heuristic_continue(self) -> None:
        args, action = self.adapter.build_command(
            repo="/tmp/test",
            thread_id="exact-uuid-12345",
            prompt="continue task",
            model=None,
            reasoning_effort=None,
        )
        self.assertEqual(action, "resume")
        self.assertIn("--conversation", args)
        self.assertEqual(args[args.index("--conversation") + 1], "exact-uuid-12345")
        self.assertNotIn("-c", args)
        self.assertNotIn("--continue", args)

    def test_validate_reasoning_effort(self) -> None:
        self.assertEqual(self.adapter.validate_reasoning_effort("low"), "low")
        self.assertEqual(self.adapter.validate_reasoning_effort("medium"), "medium")
        self.assertEqual(self.adapter.validate_reasoning_effort("high"), "high")
        with self.assertRaises(ValueError):
            self.adapter.validate_reasoning_effort("ultra")

    def test_supports_auto_pool_is_false(self) -> None:
        self.assertFalse(self.adapter.supports_auto_pool())

    def test_state_compatibility_with_agy_provider(self) -> None:
        state = empty_state(
            run_id="test-run",
            goal="Test AGY integration",
            repo="/tmp/repo",
            repo_id="repo-123",
            now="2026-09-03T12:00:00Z",
            provider="agy",
        )
        self.assertEqual(state["provider"], "agy")
        validate_state(state)

        # Rejects AUTO_POOL with AGY
        state_pool = dict(state)
        state_pool["account_mode"] = "AUTO_POOL"
        with self.assertRaises(ValueError):
            validate_state(state_pool)

    def test_state_defaults_provider_to_codex_if_missing(self) -> None:
        state = empty_state(
            run_id="test-run",
            goal="Test backwards compat",
            repo="/tmp/repo",
            repo_id="repo-123",
            now="2026-09-03T12:00:00Z",
        )
        del state["provider"]
        validate_state(state)
        self.assertEqual(state.get("provider", "codex"), "codex")

    def test_probe_quota_parses_usage_stream_json(self) -> None:
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY)}):
            snap = self.adapter.probe_quota()
            self.assertEqual(snap.source, "AGY_CLI")
            self.assertIsNone(snap.error)
            self.assertIsNotNone(snap.primary)
            self.assertEqual(snap.primary.name, "5h")
            self.assertEqual(snap.primary.used_percent, 60.0)
            self.assertIsNotNone(snap.secondary)
            self.assertEqual(snap.secondary.name, "weekly")
            self.assertEqual(snap.secondary.used_percent, 35.0)

    def test_auth_sanity_verification(self) -> None:
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY)}):
            self.assertTrue(self.adapter.auth_sanity())

    def test_exact_conversation_mismatch_fails_closed(self) -> None:
        """If AGY creates a new conversation instead of resuming the requested ID, fail closed."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test mismatch", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch"}):
                result = self.adapter.run_turn(store, 1, "resume prompt", thread_id="EXPECTED-CONV-ID")
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIn("mismatch", result.error_detail.lower())

    def test_mismatch_immediate_abort_prevents_sentinel_side_effect(self) -> None:
        """Mandatory side-effect boundary test: mismatch on init immediately aborts provider before side effect."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test mismatch sentinel", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
            if sentinel.exists():
                sentinel.unlink()

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch_sentinel"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIsNone(result.thread_id)
                self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER MISMATCH ABORT")
                self.assertTrue(len(spawned_pids) == 1, "Expected exactly 1 spawned process")
                time.sleep(0.1)
                self.assertFalse(pid_alive(spawned_pids[0]), "Provider child must be killed and reaped immediately")

    def test_step_mismatch_immediate_abort_prevents_sentinel_side_effect(self) -> None:
        """Mandatory side-effect boundary test: mismatch on later step immediately aborts provider before side effect."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test step mismatch sentinel", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
            if sentinel.exists():
                sentinel.unlink()

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "step_mismatch_sentinel"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIsNone(result.thread_id)
                self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER STEP MISMATCH ABORT")
                self.assertTrue(len(spawned_pids) == 1)
                time.sleep(0.1)
                self.assertFalse(pid_alive(spawned_pids[0]), "Provider child must be killed and reaped immediately")

    def test_stderr_conversation_not_found_fails_closed(self) -> None:
        """If stderr reports warning: conversation \"...\" not found, fail closed."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test not found", str(repo), provider="agy", thread_id="NONEXISTENT-ID")

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "not_found"}):
                result = self.adapter.run_turn(store, 1, "resume prompt", thread_id="NONEXISTENT-ID")
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIn("not found", result.error_detail.lower())

    def test_stderr_not_found_immediate_abort_prevents_sentinel_side_effect(self) -> None:
        """Mandatory side-effect boundary test: stderr not-found immediately aborts provider before side effect."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test not found sentinel", str(repo), provider="agy", thread_id="NONEXISTENT-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
            if sentinel.exists():
                sentinel.unlink()

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "not_found_sentinel"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="NONEXISTENT-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER NOT_FOUND ABORT")
                self.assertTrue(len(spawned_pids) == 1)
                time.sleep(0.1)
                self.assertFalse(pid_alive(spawned_pids[0]), "Provider child must be killed and reaped immediately")



class AgySupervisorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="nightwatch-agy-sup-")
        self.repo = Path(self.tmp) / "repo"
        _git_init(self.repo)
        self.store = NightwatchStore(self.repo)

    def tearDown(self) -> None:
        os.system(f"rm -rf {self.tmp}")

    def test_supervisor_initializes_and_runs_agy_turn(self) -> None:
        self.store.initialize(
            "run-agy",
            "Supervise AGY goal",
            str(self.repo),
            provider="agy",
            verify_commands=["git diff --check"],
        )
        loaded = self.store.load_state()
        self.assertEqual(loaded["provider"], "agy")

        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_CONV_ID": "agy-conv-777", "FAKE_AGY_SCENARIO": "normal"}):
            supervisor = Supervisor(self.store)
            result = supervisor.execute(start=True)
            self.assertEqual(result["thread_id"], "agy-conv-777")
            self.assertEqual(result["provider"], "agy")
            self.assertEqual(result["state"], State.AWAITING_ACCEPTANCE.value)

    def test_supervisor_quota_exhaustion_enters_wait_quota(self) -> None:
        """When AGY reports quota exhaustion, supervisor enters WAIT_QUOTA."""
        self.store.initialize(
            "run-agy-quota",
            "Supervise AGY quota goal",
            str(self.repo),
            provider="agy",
        )

        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "exhausted"}):
            supervisor = Supervisor(self.store)
            supervisor._preflight()
            result = supervisor._run_turn()
            self.assertEqual(result["state"], State.WAIT_QUOTA.value)
            self.assertEqual(result["provider"], "agy")
            self.assertIsNotNone(result.get("next_resume_at"))

    def test_synthetic_quota_recovery_cycles(self) -> None:
        """Verify synthetic quota recovery transitions increment quota_cycles without tripping failure breaker."""
        self.store.initialize("run-soak", "Quota soak", str(self.repo), provider="agy", thread_id="conv-soak-1")
        # Step through preflight to running
        self.store.transition(State.PREFLIGHT, "preflight", "checking")
        self.store.transition(State.RUNNING, "run_started", "test running")
        supervisor = Supervisor(self.store)
        now_ts = int(datetime.now(timezone.utc).timestamp())

        # Test 25 recovery cycles (surpassing old 20-cycle limit)
        for cycle in range(25):
            reset_ts = now_ts + 60
            result = ProviderResult(
                exit_code=1,
                signal=None,
                thread_id="conv-soak-1",
                event_count=1,
                malformed_count=0,
                error_kind=ErrorKind.QUOTA_5H,
                error_detail="resource exhausted",
                reset_at=reset_ts,
                reset_source="agy_usage_probe",
                quota_windows=[QuotaWindow("5h", 100.0, 300, reset_ts)],
            )
            state = supervisor._handle_result(result)
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            self.assertEqual(state["quota_cycles"], cycle + 1)
            self.assertEqual(state["recovery_failures"], 0)

            recovered_snap = QuotaSnapshot(
                "AGY_CLI",
                "now",
                primary=QuotaWindow("5h", 10.0, 300, reset_ts),
            )
            with patch.object(supervisor, "_get_quota_snapshot", return_value=recovered_snap), \
                 patch("nightwatch.supervisor._sleep_until"):
                reval = supervisor._wait_and_revalidate_quota()
                self.assertTrue(reval)
                self.assertEqual(supervisor.store.load_state()["state"], State.RECOVERING.value)
                # Re-enter running for next cycle
                self.store.transition(State.RUNNING, "recovered", "test ready")
                self.assertEqual(supervisor.store.load_state()["state"], State.RUNNING.value)

        final_state = self.store.load_state()
        self.assertEqual(final_state["quota_cycles"], 25)
        self.assertEqual(final_state["recovery_failures"], 0)
        self.assertEqual(final_state["state"], State.RUNNING.value)

    def test_supervisor_mismatch_immediate_abort_e2e(self) -> None:
        """End-to-end supervisor verification: mismatch aborts immediately, blocks run, and sentinel is absent."""
        self.store.initialize(
            "run-mismatch-e2e",
            "Supervise AGY goal",
            str(self.repo),
            provider="agy",
            thread_id="EXPECTED-CONV-ID",
            verify_commands=["git diff --check"],
        )

        sentinel = self.repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
        if sentinel.exists():
            sentinel.unlink()

        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch_sentinel"}):
            final = supervisor.execute(start=True)

        self.assertEqual(final["state"], State.BLOCKED.value)
        self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER SUPERVISOR MISMATCH ABORT")
        self.assertIsNone(final.get("active_process"))
        run_events = self.store.load_run_events(1)
        mismatch_events = [e for e in run_events if e.get("type") == "thread_id_mismatch"]
        self.assertTrue(len(mismatch_events) >= 1, "Expected thread_id_mismatch event recorded in run events")




if __name__ == "__main__":

    unittest.main()
