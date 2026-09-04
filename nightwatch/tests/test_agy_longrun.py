from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from nightwatch.models import ErrorKind, ProviderResult, QuotaSnapshot, QuotaWindow, State
from nightwatch.storage import NightwatchStore
from nightwatch.supervisor import Supervisor, MAX_RECOVERY_FAILURES


def _make_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True, exist_ok=True)
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email test@example.com")
    os.system(f"git -C {repo} config user.name Test")
    (repo / "README.md").write_text("initial\n")
    os.system(f"git -C {repo} add README.md && git -C {repo} commit -qm init")
    return repo


class AgyLongRunQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.parent = Path(self.td)
        self.repo = _make_repo(self.parent, "repo-longrun")
        self.store = NightwatchStore(self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def test_agy_100_normal_quota_cycles_do_not_trip_failure_breaker(self) -> None:
        """Verify 100 consecutive normal quota exhaustion and recovery cycles run indefinitely."""
        self.store.initialize("run-100-cycles", "Long run soak", str(self.repo), provider="agy", thread_id="conv-soak-1")
        self.store.transition(State.PREFLIGHT, "preflight", "checking")
        self.store.transition(State.RUNNING, "run_started", "ready")
        supervisor = Supervisor(self.store)
        now_ts = int(datetime.now(timezone.utc).timestamp())

        for cycle in range(100):
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

            # Revalidate with clean recovery
            recovered_snap = QuotaSnapshot("AGY_CLI", "now", primary=QuotaWindow("5h", 10.0, 300, reset_ts))
            with patch.object(supervisor, "_get_quota_snapshot", return_value=recovered_snap), \
                 patch("nightwatch.supervisor._sleep_until"):
                reval = supervisor._wait_and_revalidate_quota()
                self.assertTrue(reval)
                self.assertEqual(supervisor.store.load_state()["state"], State.RECOVERING.value)
                self.store.transition(State.RUNNING, "recovered", "test ready")
                self.assertEqual(supervisor.store.load_state()["state"], State.RUNNING.value)

        final_state = self.store.load_state()
        self.assertEqual(final_state["quota_cycles"], 100)
        self.assertEqual(final_state["recovery_failures"], 0)
        self.assertNotEqual(final_state["state"], State.FAILED.value)
        self.assertNotEqual(final_state["state"], State.BLOCKED.value)

    def test_repeated_authoritative_probe_failure_increments_recovery_failures_and_trips_breaker(self) -> None:
        """Abnormal recovery failures (e.g. authoritative quota error) increment recovery_failures and trip breaker."""
        self.store.initialize("run-abnormal", "Abnormal failure test", str(self.repo), provider="agy", thread_id="conv-abnormal-1")
        self.store.transition(State.PREFLIGHT, "preflight", "checking")
        self.store.transition(State.RUNNING, "run_started", "ready")
        supervisor = Supervisor(self.store)
        now_ts = int(datetime.now(timezone.utc).timestamp())

        # First normal quota wait
        result = ProviderResult(
            exit_code=1,
            signal=None,
            thread_id="conv-abnormal-1",
            event_count=1,
            malformed_count=0,
            error_kind=ErrorKind.QUOTA_5H,
            error_detail="resource exhausted",
            reset_at=now_ts + 60,
            reset_source="agy_usage_probe",
            quota_windows=[QuotaWindow("5h", 100.0, 300, now_ts + 60)],
        )
        state = supervisor._handle_result(result)
        self.assertEqual(state["state"], State.WAIT_QUOTA.value)
        self.assertEqual(state["recovery_failures"], 0)

        # Repeated abnormal quota probe errors
        err_snap = QuotaSnapshot("AGY_CLI", "now", error="server 503 unavailable")
        with patch.object(supervisor, "_get_quota_snapshot", return_value=err_snap), \
             patch("nightwatch.supervisor._sleep_until"):
            # Attempt 1 abnormal failure
            supervisor._wait_and_revalidate_quota()
            self.assertEqual(supervisor.store.load_state()["recovery_failures"], 1)

            # Attempt 2 abnormal failure
            supervisor._wait_and_revalidate_quota()
            self.assertEqual(supervisor.store.load_state()["recovery_failures"], 2)

            # Attempt 3 trips circuit breaker (MAX_RECOVERY_FAILURES = 3)
            res = supervisor._wait_and_revalidate_quota()
            self.assertFalse(res)
            final_state = supervisor.store.load_state()
            self.assertEqual(final_state["state"], State.FAILED.value)
            self.assertIn("circuit breaker reached", final_state["last_error"])


if __name__ == "__main__":
    unittest.main()
