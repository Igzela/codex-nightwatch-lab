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

from nightwatch.models import QuotaSnapshot, QuotaWindow, State  # noqa: E402
from nightwatch.quota import QuotaError  # noqa: E402
from nightwatch.storage import NightwatchStore  # noqa: E402
from nightwatch.supervisor import Supervisor  # noqa: E402


FAKE = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"


class ScriptedQuota:
    def __init__(self):
        self.calls = 0

    def read(self):
        self.calls += 1
        return QuotaSnapshot(
            "fake-provider", "now",
            QuotaWindow("5h", 0, 300, int(time.time()) + 100),
            QuotaWindow("weekly", 0, 10080, int(time.time()) + 100),
        )


def git_run(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def fixture() -> tuple[tempfile.TemporaryDirectory, Path, Path, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-e2e-")
    root = Path(temporary.name)
    git_run(root, "init", "-q")
    git_run(root, "config", "user.email", "nightwatch@example.invalid")
    git_run(root, "config", "user.name", "Nightwatch Test")
    (root / "README.md").write_text("fixture\n")
    git_run(root, "add", "README.md")
    git_run(root, "commit", "-qm", "fixture")
    plan_file = root / "plan-source.json"
    plan_file.write_text(json.dumps({"required_verification_commands": ["git diff --check"], "milestones": [{"id": "M1", "title": "implement fixture", "weight": 1, "required": True, "verification_commands": ["test -f fake-implemented.txt"]}]}))
    progress_file = root / "progress-source.json"
    progress_file.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}))
    return temporary, root, plan_file, progress_file


class FakeCodexE2E(unittest.TestCase):
    def env(self, plan: Path, progress: Path, scenario: str = "normal"):
        return patch.dict(os.environ, {
            "NIGHTWATCH_CODEX_BIN": str(FAKE),
            "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
            "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
            "NIGHTWATCH_WAIT_POLL_SECONDS": "0.05",
            "FAKE_CODEX_SCENARIO": scenario,
            "FAKE_CODEX_PLAN_FILE": str(plan),
            "FAKE_CODEX_PROGRESS_FILE": str(progress),
        }, clear=False)

    def test_normal_completion_exact_thread_and_done_guard(self):
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-normal", "implement fixture", str(root))
            with self.env(plan, progress):
                final = Supervisor(store, ScriptedQuota()).execute(start=True)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            self.assertTrue(final["final_verification_passed"])
            self.assertEqual(store.load_plan()["milestones"][0]["status"], "verified")
            events = "\n".join(path.read_text() for path in store.runs_path.glob("*.events.jsonl"))
            self.assertIn("TEST-001", events)
            self.assertNotIn("--last", events)
            self.assertTrue((store.reports_path / "latest.md").exists())
        finally:
            temporary.cleanup()

    def test_service_bootstrap_can_start_a_new_durable_goal(self):
        """The unit executes `resume`; NEW state must start, not create a new thread."""
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-service", "implement fixture", str(root))
            with self.env(plan, progress):
                final = Supervisor(store, ScriptedQuota()).execute(start=False)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            events = [json.loads(line)["event"] for line in store.events_path.read_text().splitlines()]
            self.assertIn("preflight_started", events)
            self.assertIn("thread_started", events)
        finally:
            temporary.cleanup()

    def test_quota_waits_revalidates_then_resumes_same_thread(self):
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-quota", "implement fixture", str(root))
            quota = ScriptedQuota()
            with self.env(plan, progress, "quota_then_success"), patch.dict(os.environ, {"FAKE_CODEX_RESET_SECONDS": "1"}):
                final = Supervisor(store, quota).execute(start=True)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            self.assertEqual(final["generation"], 2)
            self.assertEqual(final["recoveries"], 1)
            self.assertGreaterEqual(quota.calls, 2)
            self.assertEqual(json.loads((root / ".fake-codex-state.json").read_text())["resumes"], 1)
            events = [json.loads(line) for line in store.events_path.read_text().splitlines()]
            names = [event["event"] for event in events]
            self.assertIn("quota_exhausted", names)
            self.assertIn("quota_revalidated", names)
            self.assertIn("resume_claimed", names)
            self.assertIn("resume_started", names)
            run_events = "\n".join(path.read_text() for path in store.runs_path.glob("*.events.jsonl"))
            self.assertIn('"action": "resume"', run_events)
            self.assertIn("TEST-001", run_events)
            self.assertNotIn("--last", run_events)
        finally:
            temporary.cleanup()

    def test_auth_failure_never_loops(self):
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-auth", "goal", str(root))
            with self.env(plan, progress, "auth"):
                final = Supervisor(store, ScriptedQuota()).execute(start=True)
            self.assertEqual(final["state"], State.FAILED.value)
            self.assertEqual(final["error_kind"], "auth_error")
        finally:
            temporary.cleanup()

    def test_blocker_is_not_done(self):
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-blocker", "goal", str(root))
            with self.env(plan, progress, "blocker"):
                final = Supervisor(store, ScriptedQuota()).execute(start=True)
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertNotEqual(final["state"], State.DONE.value)
        finally:
            temporary.cleanup()

    def test_done_but_verification_failure_cannot_become_done(self):
        temporary, root, _plan, progress = fixture()
        bad_plan = root / "bad-plan.json"
        bad_plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "bad check", "weight": 1, "required": True, "verification_commands": ["false"]}]}))
        try:
            store = NightwatchStore(root)
            store.initialize("run-bad-check", "goal", str(root))
            with self.env(bad_plan, progress, "done_but_fails"):
                final = Supervisor(store, ScriptedQuota()).execute(start=True)
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertFalse(final["final_verification_passed"])
        finally:
            temporary.cleanup()

    def test_weekly_limit_is_distinguished(self):
        temporary, root, plan, progress = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-weekly", "goal", str(root))
            with self.env(plan, progress, "weekly"):
                first = Supervisor(store, ScriptedQuota())
                # Run only the provider turn so the state remains inspectable in WAIT_QUOTA.
                first._preflight()
                result = first._run_turn()
            self.assertEqual(result["state"], State.WAIT_QUOTA.value)
            self.assertEqual(result["error_kind"], "usage_limit_weekly")
            self.assertEqual(result["quota_windows"][0]["name"], "weekly")
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
