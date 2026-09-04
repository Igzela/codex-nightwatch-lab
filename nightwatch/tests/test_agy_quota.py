from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[1]
if str(PRODUCT) not in sys.path:
    sys.path.insert(0, str(PRODUCT))

from nightwatch.models import State, QuotaSnapshot, QuotaWindow, ErrorKind, ProviderResult
from nightwatch.providers import get_provider_adapter, agy_model_family
from nightwatch.storage import NightwatchStore
from nightwatch.supervisor import Supervisor

FAKE_AGY = Path(__file__).resolve().parents[2] / "test-artifacts" / "fake-agy" / "fake_agy.py"
FAKE_CODEX = Path(__file__).resolve().parents[2] / "test-artifacts" / "fake-codex" / "fake_codex.py"


def _make_repo(parent: Path, name: str) -> Path:
    repo = parent / name
    repo.mkdir(parents=True, exist_ok=True)
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email test@example.com")
    os.system(f"git -C {repo} config user.name Test")
    (repo / "README.md").write_text("initial\n")
    os.system(f"git -C {repo} add README.md && git -C {repo} commit -qm init")
    return repo


class AgyModelFamilyMappingTests(unittest.TestCase):
    def test_gemini_family_mapping(self) -> None:
        self.assertEqual(agy_model_family("gemini-3.8-flash-high"), "gemini")
        self.assertEqual(agy_model_family("gemini-3.7-flash-medium"), "gemini")
        self.assertEqual(agy_model_family("gemini-3.1-pro-low"), "gemini")

    def test_third_party_family_mapping(self) -> None:
        self.assertEqual(agy_model_family("claude-sonnet-4-6"), "3p")
        self.assertEqual(agy_model_family("claude-opus-4-6-thinking"), "3p")
        self.assertEqual(agy_model_family("gpt-oss-120b-medium"), "3p")
        self.assertEqual(agy_model_family("gpt-4.5-turbo"), "3p")

    def test_unknown_family_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            agy_model_family("unknown-llama-999")
        with self.assertRaises(ValueError):
            agy_model_family("")


class AgyQuotaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.parent = Path(self.td)
        self.repo = _make_repo(self.parent, "repo-main")
        self.store = NightwatchStore(self.repo)
        self.adapter = get_provider_adapter("agy")
        self.env_patch = patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY)})
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def test_gemini_usable_and_3p_exhausted(self) -> None:
        """When 3P is exhausted but Gemini is usable: Gemini runs, Claude enters WAIT_QUOTA."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "exhausted_3p"}):
            # Gemini probe is usable
            gemini_snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNone(gemini_snap.error)
            self.assertFalse(gemini_snap.primary.exhausted)

            # Claude probe is exhausted
            claude_snap = self.adapter.probe_quota(model="claude-sonnet-4-6")
            self.assertIsNone(claude_snap.error)
            self.assertTrue(claude_snap.primary.exhausted)

            # Supervisor preflight with Gemini model succeeds
            self.store.initialize("run-gemini", "Gemini task", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
            supervisor = Supervisor(self.store)
            preflight_ok = supervisor._preflight()
            self.assertTrue(preflight_ok)

            # Supervisor preflight with Claude model defers to WAIT_QUOTA
            repo_claude = _make_repo(self.parent, "repo-claude")
            store_claude = NightwatchStore(repo_claude)
            store_claude.initialize("run-claude", "Claude task", str(repo_claude), provider="agy", model="claude-sonnet-4-6")
            supervisor_claude = Supervisor(store_claude)
            supervisor_claude._preflight()
            self.assertEqual(store_claude.load_state()["state"], State.WAIT_QUOTA.value)

    def test_gemini_exhausted_and_3p_usable(self) -> None:
        """When Gemini is exhausted but 3P is usable: Claude runs, Gemini enters WAIT_QUOTA."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "exhausted_gemini"}):
            # Gemini probe is exhausted
            gemini_snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNone(gemini_snap.error)
            self.assertTrue(gemini_snap.primary.exhausted)

            # Claude probe is usable
            claude_snap = self.adapter.probe_quota(model="claude-sonnet-4-6")
            self.assertIsNone(claude_snap.error)
            self.assertFalse(claude_snap.primary.exhausted)

            # Supervisor preflight with Claude model succeeds
            self.store.initialize("run-claude-ok", "Claude task", str(self.repo), provider="agy", model="claude-sonnet-4-6")
            supervisor_claude = Supervisor(self.store)
            preflight_ok = supervisor_claude._preflight()
            self.assertTrue(preflight_ok)

            # Supervisor preflight with Gemini model defers to WAIT_QUOTA
            repo_gemini = _make_repo(self.parent, "repo-gemini")
            store_gemini = NightwatchStore(repo_gemini)
            store_gemini.initialize("run-gemini-wait", "Gemini task", str(repo_gemini), provider="agy", model="gemini-3.8-flash-high")
            supervisor_gemini = Supervisor(store_gemini)
            supervisor_gemini._preflight()
            self.assertEqual(store_gemini.load_state()["state"], State.WAIT_QUOTA.value)

    def test_gpt_oss_uses_3p_group(self) -> None:
        """GPT-OSS model uses 3P quota group."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "exhausted_3p"}):
            snap = self.adapter.probe_quota(model="gpt-oss-120b-medium")
            self.assertIsNone(snap.error)
            self.assertTrue(snap.primary.exhausted)
            self.assertIn("3p", snap.plan_type.lower())

    def test_unknown_model_family_fails_closed(self) -> None:
        """Unknown model family fails closed with error in QuotaSnapshot."""
        snap = self.adapter.probe_quota(model="custom-finetuned-llama")
        self.assertIsNotNone(snap.error)
        self.assertIn("unknown agy model family", snap.error.lower())

    def test_missing_selected_family_5h_bucket_fails_closed(self) -> None:
        """Missing 5h bucket for selected family returns error in snapshot."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "missing_gemini_5h"}):
            snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNotNone(snap.error)
            self.assertIn("missing selected-family 5h bucket", snap.error)

    def test_missing_selected_family_weekly_bucket_fails_closed(self) -> None:
        """Missing weekly bucket for selected family returns error in snapshot."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "missing_gemini_weekly"}):
            snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNotNone(snap.error)
            self.assertIn("missing selected-family weekly bucket", snap.error)

    def test_malformed_remaining_fraction_fails_closed(self) -> None:
        """Malformed remaining_fraction fails closed."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "malformed_fraction"}):
            snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNotNone(snap.error)
            self.assertIn("malformed remaining_fraction", snap.error)

    def test_invalid_reset_timestamp_classified_safely(self) -> None:
        """Invalid reset timestamp falls back safely without unhandled exception."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "invalid_reset_time"}):
            snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNone(snap.error)
            self.assertIsNone(snap.primary.resets_at)

    def test_usage_malformed_json_returns_error_snapshot(self) -> None:
        """Malformed /usage output returns error snapshot."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "usage_malformed_json"}):
            snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNotNone(snap.error)
            self.assertIn("unexpected response format", snap.error)

    def test_usage_exec_failure_returns_error_snapshot(self) -> None:
        """Executable failure of /usage returns error snapshot with exit code and stderr."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "usage_exec_failure"}):
            snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNotNone(snap.error)
            self.assertIn("failed with code 1", snap.error)
            self.assertIn("executable failure", snap.error)

    def test_usage_auth_failure_returns_error_snapshot(self) -> None:
        """Authentication failure during /usage is identified as auth failure."""
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "usage_auth_failure"}):
            snap = self.adapter.probe_quota(model="gemini-3.8-flash-high")
            self.assertIsNotNone(snap.error)
            self.assertIn("authentication failure", snap.error.lower())


class AgyPreflightAuthoritativeQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp()
        self.parent = Path(self.td)
        self.repo = _make_repo(self.parent, "repo-preflight")
        self.store = NightwatchStore(self.repo)
        self.adapter = get_provider_adapter("agy")
        self.env_patch = patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY)})
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        shutil.rmtree(self.td, ignore_errors=True)

    def test_unknown_model_preflight_fails_closed_before_spawn(self) -> None:
        self.store.initialize("run-unk", "Unknown model", str(self.repo), provider="agy", model="unknown-llama-999")
        supervisor = Supervisor(self.store)
        with patch.object(self.adapter, "run_turn") as mock_run:
            supervisor._execute(start=True)
            state = self.store.load_state()
            self.assertEqual(state["state"], State.BLOCKED.value)
            self.assertEqual(state["error_kind"], ErrorKind.STATE.value)
            self.assertIn("unknown agy model", state["last_error"].lower())
            mock_run.assert_not_called()

    def test_missing_selected_family_5h_bucket_fails_closed_before_spawn(self) -> None:
        self.store.initialize("run-5h", "Missing 5h", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "missing_gemini_5h"}), patch.object(self.adapter, "run_turn") as mock_run:
            supervisor._execute(start=True)
            state = self.store.load_state()
            self.assertEqual(state["state"], State.BLOCKED.value)
            self.assertEqual(state["error_kind"], ErrorKind.STATE.value)
            self.assertIn("missing selected-family 5h bucket", state["last_error"])
            mock_run.assert_not_called()

    def test_missing_selected_family_weekly_bucket_fails_closed_before_spawn(self) -> None:
        self.store.initialize("run-weekly", "Missing weekly", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "missing_gemini_weekly"}), patch.object(self.adapter, "run_turn") as mock_run:
            supervisor._execute(start=True)
            state = self.store.load_state()
            self.assertEqual(state["state"], State.BLOCKED.value)
            self.assertEqual(state["error_kind"], ErrorKind.STATE.value)
            self.assertIn("missing selected-family weekly bucket", state["last_error"])
            mock_run.assert_not_called()

    def test_malformed_usage_response_fails_closed_before_spawn(self) -> None:
        self.store.initialize("run-malformed", "Malformed", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "usage_malformed_json"}), patch.object(self.adapter, "run_turn") as mock_run:
            supervisor._execute(start=True)
            state = self.store.load_state()
            self.assertEqual(state["state"], State.BLOCKED.value)
            self.assertEqual(state["error_kind"], ErrorKind.STATE.value)
            self.assertIn("unexpected response format", state["last_error"])
            mock_run.assert_not_called()

    def test_usage_exec_failure_fails_closed_before_spawn(self) -> None:
        self.store.initialize("run-exec-fail", "Exec fail", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "usage_exec_failure"}), patch.object(self.adapter, "run_turn") as mock_run:
            supervisor._execute(start=True)
            state = self.store.load_state()
            self.assertEqual(state["state"], State.BLOCKED.value)
            self.assertEqual(state["error_kind"], ErrorKind.STATE.value)
            self.assertIn("failed with code 1", state["last_error"])
            mock_run.assert_not_called()

    def test_usage_auth_failure_fails_closed_with_auth_error_before_spawn(self) -> None:
        self.store.initialize("run-auth-fail", "Auth fail", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "usage_auth_failure"}), patch.object(self.adapter, "run_turn") as mock_run:
            supervisor._execute(start=True)
            state = self.store.load_state()
            self.assertEqual(state["state"], State.FAILED.value)
            self.assertEqual(state["error_kind"], ErrorKind.AUTH.value)
            self.assertIn("authentication failure", state["last_error"].lower())
            mock_run.assert_not_called()

    def test_exhausted_quota_enters_wait_quota_before_spawn(self) -> None:
        self.store.initialize("run-exhausted", "Exhausted", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "exhausted"}), patch.object(self.adapter, "run_turn") as mock_run:
            preflight_ok = supervisor._preflight()
            self.assertTrue(preflight_ok)
            state = self.store.load_state()
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            mock_run.assert_not_called()

    def test_healthy_quota_spawns_provider(self) -> None:
        self.store.initialize("run-healthy", "Healthy", str(self.repo), provider="agy", model="gemini-3.8-flash-high")
        supervisor = Supervisor(self.store)
        def stop_after_turn(*args, **kwargs):
            supervisor._stop_requested = True
            return ProviderResult(exit_code=0, signal=None, thread_id="conv-1", event_count=1, malformed_count=0)
        with patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "normal"}), patch.object(self.adapter, "run_turn", side_effect=stop_after_turn) as mock_run:
            supervisor._execute(start=True)
            self.assertEqual(mock_run.call_count, 1)

    def test_codex_historical_preflight_preserved_when_quota_unavailable(self) -> None:
        self.store.initialize("run-codex", "Codex run", str(self.repo), provider="codex", model="gpt-5")
        supervisor = Supervisor(self.store)
        def stop_after_turn(*args, **kwargs):
            supervisor._stop_requested = True
            return ProviderResult(exit_code=0, signal=None, thread_id="thread-codex", event_count=1, malformed_count=0)
        with patch.dict(os.environ, {"NIGHTWATCH_CODEX_BIN": str(FAKE_CODEX)}), \
             patch.object(supervisor, "_get_quota_snapshot", side_effect=RuntimeError("quota network down")), \
             patch.object(supervisor, "_auth_sanity", return_value=True), \
             patch("nightwatch.supervisor.run_codex", side_effect=stop_after_turn) as mock_run:
            supervisor._execute(start=True)
            self.assertEqual(mock_run.call_count, 1)
            events = self.store.load_events()
            self.assertTrue(any(e.get("event") == "quota_sanity_unavailable" for e in events))


if __name__ == "__main__":
    unittest.main()
