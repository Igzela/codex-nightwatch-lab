from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nightwatch.models import ErrorKind
from nightwatch.providers import get_provider_adapter
from nightwatch.storage import NightwatchStore

FAKE_AGY = Path(__file__).resolve().parents[2] / "test-artifacts" / "fake-agy" / "fake_agy.py"


def _make_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True, exist_ok=True)
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email test@example.com")
    os.system(f"git -C {repo} config user.name Test")
    (repo / "README.md").write_text("initial\n")
    os.system(f"git -C {repo} add README.md && git -C {repo} commit -qm init")
    return repo


class AgyStreamContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.parent = Path(self.td)
        self.repo = _make_repo(self.parent, "repo-stream")
        self.store = NightwatchStore(self.repo)
        self.adapter = get_provider_adapter("agy")
        self.env_patch = patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY)})
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def test_start_init_x_step_x_result_x_passes(self) -> None:
        """init X, step X, result X -> PASS."""
        self.store.initialize("run-pass", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_CONV_ID": "conv-exact-100"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertIsNone(res.error_kind)
            self.assertEqual(res.thread_id, "conv-exact-100")
            self.assertEqual(res.exit_code, 0)

    def test_start_init_x_step_y_fails_state(self) -> None:
        """init X, step Y, result X -> FAIL STATE."""
        self.store.initialize("run-step-mismatch", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "mismatched_step"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIsNone(res.thread_id)

    def test_start_init_x_result_y_fails_state(self) -> None:
        """init X, step X, result Y -> FAIL STATE."""
        self.store.initialize("run-res-mismatch", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "mismatched_result"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIsNone(res.thread_id)

    def test_start_without_init_fails_state(self) -> None:
        """Start without authoritative init event -> FAIL STATE."""
        self.store.initialize("run-no-init", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "no_init"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIn("missing authoritative init event", res.error_detail)

    def test_resume_init_x_step_x_result_x_passes(self) -> None:
        """resume X, init X, step X, result X -> PASS."""
        self.store.initialize("run-resume-pass", "goal", str(self.repo), provider="agy", thread_id="conv-resume-555")
        with patch.dict(os.environ, {"FAKE_AGY_CONV_ID": "conv-resume-555"}):
            res = self.adapter.run_turn(self.store, 1, "hello", thread_id="conv-resume-555")
            self.assertIsNone(res.error_kind)
            self.assertEqual(res.thread_id, "conv-resume-555")

    def test_resume_init_y_fails_state(self) -> None:
        """resume X, init Y -> FAIL STATE."""
        self.store.initialize("run-resume-mismatch", "goal", str(self.repo), provider="agy", thread_id="conv-resume-expected")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "mismatch"}):
            res = self.adapter.run_turn(self.store, 1, "hello", thread_id="conv-resume-expected")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIsNone(res.thread_id)

    def test_resume_stderr_not_found_fails_state(self) -> None:
        """resume X, stderr not-found -> FAIL STATE."""
        self.store.initialize("run-resume-notfound", "goal", str(self.repo), provider="agy", thread_id="conv-not-found-id")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "not_found"}):
            res = self.adapter.run_turn(self.store, 1, "hello", thread_id="conv-not-found-id")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIn("not found", res.error_detail.lower())

    def test_terminal_status_canceled_sets_aborted(self) -> None:
        """Result status CANCELED sets aborted=True, kind=None."""
        self.store.initialize("run-canceled", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "canceled_result"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertTrue(res.aborted)
            self.assertIsNone(res.error_kind)

    def test_terminal_status_non_terminal_fails_state(self) -> None:
        """Result status RUNNING appearing as final result -> FAIL STATE."""
        self.store.initialize("run-running-status", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "non_terminal_result"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIn("non-terminal result status", res.error_detail)

    def test_terminal_status_unknown_fails_state(self) -> None:
        """Unknown result status -> FAIL STATE."""
        self.store.initialize("run-unknown-status", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "unknown_result_status"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIn("unknown result status", res.error_detail)

    def test_no_result_event_with_code_0_fails_state(self) -> None:
        """Exit 0 without emitting result event -> FAIL STATE."""
        self.store.initialize("run-no-res", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "no_result_event"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.STATE)
            self.assertIn("without emitting a terminal result event", res.error_detail)

    def test_quota_precedence_over_generic_crash(self) -> None:
        """Result ERROR + quota text classifies as QUOTA, not generic CRASH."""
        self.store.initialize("run-quota-err", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "error_result_with_quota"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.QUOTA_5H)
            self.assertIn("quota limit reached", res.error_detail)

    def test_auth_failure_precedence_over_generic_crash(self) -> None:
        """Authentication failure text classifies as AUTH, not generic CRASH."""
        self.store.initialize("run-auth-err", "goal", str(self.repo), provider="agy")
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "auth_failure"}):
            res = self.adapter.run_turn(self.store, 1, "hello")
            self.assertEqual(res.error_kind, ErrorKind.AUTH)
            self.assertIn("authentication failure", res.error_detail)


if __name__ == "__main__":
    unittest.main()
