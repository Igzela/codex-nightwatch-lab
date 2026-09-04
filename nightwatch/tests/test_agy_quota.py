from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nightwatch.models import State, QuotaSnapshot, QuotaWindow, ErrorKind
from nightwatch.providers import get_provider_adapter, agy_model_family
from nightwatch.storage import NightwatchStore
from nightwatch.supervisor import Supervisor

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


if __name__ == "__main__":
    unittest.main()
