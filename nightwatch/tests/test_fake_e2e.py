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

TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-trusted-tests-")
os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

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
            "live_app_server", "now",
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
    plan_file.write_text(json.dumps({"milestones": [{"id": "M1", "title": "implement fixture", "weight": 1}]}))
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
            store.initialize("run-normal", "implement fixture", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
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
            store.initialize("run-service", "implement fixture", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
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
            store.initialize("run-quota", "implement fixture", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
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
            store.initialize("run-auth", "goal", str(root), verify_commands=["git diff --check"])
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
            store.initialize("run-blocker", "goal", str(root), verify_commands=["git diff --check"])
            with self.env(plan, progress, "blocker"):
                final = Supervisor(store, ScriptedQuota()).execute(start=True)
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertNotEqual(final["state"], State.DONE.value)
        finally:
            temporary.cleanup()

    def test_done_but_verification_failure_cannot_become_done(self):
        temporary, root, _plan, progress = fixture()
        bad_plan = root / "bad-plan.json"
        bad_plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "bad check", "weight": 1}]}))
        try:
            store = NightwatchStore(root)
            store.initialize("run-bad-check", "goal", str(root), verify_commands=["false"])
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
            store.initialize("run-weekly", "goal", str(root), verify_commands=["git diff --check"])
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

    def test_e2e_multi_repo_concurrency_and_isolation(self):
        """Multi-repo isolation: separate stores, distinct services, independent lifecycle."""
        temp_a, root_a, plan_a, prog_a = fixture()
        temp_b, root_b, plan_b, prog_b = fixture()
        try:
            from nightwatch.operations import service_name, stop_run

            service_a = service_name(root_a)
            service_b = service_name(root_b)
            self.assertNotEqual(service_a, service_b)

            store_a = NightwatchStore(root_a)
            store_a.initialize("run-repo-a", "goal A", str(root_a), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
            store_b = NightwatchStore(root_b)
            store_b.initialize("run-repo-b", "goal B", str(root_b), verify_commands=["test -f fake-implemented.txt", "git diff --check"])

            # Stopping run A while in NEW transitions it to STOPPED without affecting run B
            stop_res = stop_run(root_a)
            self.assertIn("STOPPED", stop_res.message)
            self.assertEqual(store_a.load_state()["state"], State.STOPPED.value)
            self.assertEqual(store_b.load_state()["state"], State.NEW.value)

            with self.env(plan_b, prog_b):
                final_b = Supervisor(store_b, ScriptedQuota()).execute(start=True)
            self.assertEqual(final_b["state"], State.DONE.value)
            self.assertEqual(store_b.load_state()["state"], State.DONE.value)
            self.assertEqual(store_a.load_state()["state"], State.STOPPED.value)
        finally:
            temp_a.cleanup()
            temp_b.cleanup()

    def test_e2e_same_repo_worktree_concurrency(self):
        """Same-repo concurrency via isolated Git worktrees."""
        temp_source, root_source, plan, prog = fixture()
        try:
            from nightwatch.operations import create_worktree, service_name

            wt_a = create_worktree(root_source, "worker-a")
            wt_b = create_worktree(root_source, "worker-b")

            self.assertNotEqual(wt_a, wt_b)
            self.assertNotEqual(wt_a, root_source)
            self.assertNotEqual(wt_b, root_source)

            service_main = service_name(root_source)
            service_a = service_name(wt_a)
            service_b = service_name(wt_b)

            self.assertEqual(len({service_main, service_a, service_b}), 3)

            store_a = NightwatchStore(wt_a)
            store_a.initialize("run-wt-a", "worktree A goal", str(wt_a), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
            store_b = NightwatchStore(wt_b)
            store_b.initialize("run-wt-b", "worktree B goal", str(wt_b), verify_commands=["test -f fake-implemented.txt", "git diff --check"])

            self.assertNotEqual(store_a.directory, store_b.directory)

            plan_a = wt_a / "plan-source.json"
            prog_a = wt_a / "progress-source.json"
            plan_a.write_text(json.dumps({"milestones": [{"id": "M1", "title": "worktree A", "weight": 1}]}))
            prog_a.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}))

            with self.env(plan_a, prog_a):
                final_a = Supervisor(store_a, ScriptedQuota()).execute(start=True)
            self.assertEqual(final_a["state"], State.DONE.value)

            self.assertEqual(store_b.load_state()["state"], State.NEW.value)
        finally:
            temp_source.cleanup()

    def test_e2e_same_workspace_second_writer_fails_closed(self):
        """Attempting to run a second supervisor on the exact same workspace fails closed."""
        temp, root, plan, prog = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-first", "first goal", str(root), verify_commands=["git diff --check"])

            from nightwatch.storage import SupervisorAlreadyRunning
            with store.supervisor_lease():
                with self.assertRaises(SupervisorAlreadyRunning):
                    with store.supervisor_lease():
                        pass
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
