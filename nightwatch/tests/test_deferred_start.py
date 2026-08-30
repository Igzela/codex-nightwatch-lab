from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

if not os.environ.get("NIGHTWATCH_STATE_HOME"):
    TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-deferred-tests-")
    os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from nightwatch import cli  # noqa: E402
from nightwatch.models import QuotaSnapshot, QuotaWindow, State  # noqa: E402
from nightwatch.storage import NightwatchStore  # noqa: E402
from nightwatch.supervisor import Supervisor  # noqa: E402
from nightwatch.tui import RunCatalog, _agent_summary, _next_action, render_dashboard, status_run  # noqa: E402

FAKE = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"


def git_run(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def repo_fixture() -> tuple[tempfile.TemporaryDirectory, Path, Path, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-deferred-repo-")
    root = Path(temporary.name) / "repo"
    root.mkdir(parents=True, exist_ok=True)
    git_run(root, "init", "-q")
    git_run(root, "config", "user.email", "nightwatch@example.invalid")
    git_run(root, "config", "user.name", "Nightwatch Test")
    (root / "README.md").write_text("fixture\n")
    git_run(root, "add", "README.md")
    git_run(root, "commit", "-qm", "fixture")
    plan = root / "plan.json"
    plan.write_text(json.dumps({"milestones": [{"id": "M1", "title": "fixture", "weight": 1}]}))
    progress = root / "progress.json"
    progress.write_text(json.dumps({"milestones": [{"id": "M1", "status": "implemented"}]}))
    return temporary, root, plan, progress


class ScriptedQuota:
    def __init__(self, snapshots: list[QuotaSnapshot] | QuotaSnapshot):
        self.snapshots = list(snapshots) if isinstance(snapshots, list) else [snapshots]
        self.calls = 0

    def read(self) -> QuotaSnapshot:
        idx = min(self.calls, len(self.snapshots) - 1)
        self.calls += 1
        return self.snapshots[idx]


class DeferredStartTests(unittest.TestCase):
    def env(self, plan: Path, progress: Path, scenario: str = "normal", **extra):
        values = {
            "NIGHTWATCH_CODEX_BIN": str(FAKE),
            "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
            "NIGHTWATCH_QUOTA_BUFFER_SECONDS": "0",
            "NIGHTWATCH_WAIT_POLL_SECONDS": "0.05",
            "FAKE_CODEX_SCENARIO": scenario,
            "FAKE_CODEX_PLAN_FILE": str(plan),
            "FAKE_CODEX_PROGRESS_FILE": str(progress),
        }
        values.update(extra)
        return patch.dict(os.environ, values, clear=False)

    def test_initial_exhausted_quota_defers_first_codex_spawn(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-1", "test deferred goal", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
            quota = ScriptedQuota(
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time()) + 3600),
                    QuotaWindow("weekly", 20.0, 10080, int(time.time()) + 86400),
                )
            )
            supervisor = Supervisor(store, quota)
            with self.env(plan, progress):
                self.assertTrue(supervisor._preflight())
            state = store.load_state()
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            self.assertFalse((root / ".fake-codex-state.json").exists())
            self.assertEqual(state["generation"], 1)
            self.assertIsNone(state["thread_id"])
            self.assertIsNone(state["active_process"])
        finally:
            temporary.cleanup()

    def test_deferred_first_start_has_no_thread_before_recovery(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-2", "test no thread", str(root), verify_commands=["git diff --check"])
            quota = ScriptedQuota(
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time()) + 3600),
                )
            )
            supervisor = Supervisor(store, quota)
            with self.env(plan, progress):
                supervisor._preflight()
            state = store.load_state()
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            self.assertIsNone(state.get("thread_id"))
            events = store.load_events()
            event_names = [e["event"] for e in events]
            self.assertIn("quota_exhausted", event_names)
            self.assertNotIn("thread_started", event_names)
            self.assertNotIn("provider_started", event_names)
        finally:
            temporary.cleanup()

    def test_deferred_start_records_authoritative_reset(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-3", "test reset record", str(root), verify_commands=["git diff --check"])
            expected_reset = 1788111358
            quota = ScriptedQuota(
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, expected_reset),
                    QuotaWindow("weekly", 37.0, 10080, expected_reset + 500000),
                )
            )
            supervisor = Supervisor(store, quota)
            with self.env(plan, progress):
                supervisor._preflight()
            state = store.load_state()
            self.assertEqual(state["quota_source"], "live_app_server")
            self.assertIsNotNone(state.get("next_resume_at"))
            self.assertEqual(state["error_kind"], "usage_limit_5h")
            self.assertEqual(len(state["quota_windows"]), 2)
            primary_window = next(w for w in state["quota_windows"] if w["name"] == "5h")
            self.assertEqual(primary_window["resets_at"], expected_reset)
            self.assertEqual(primary_window["used_percent"], 100.0)
        finally:
            temporary.cleanup()

    def test_deferred_start_survives_supervisor_restart(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-4", "test restart survival", str(root), verify_commands=["git diff --check"])
            quota = ScriptedQuota(
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time()) + 3600),
                )
            )
            supervisor1 = Supervisor(store, quota)
            with self.env(plan, progress):
                supervisor1._preflight()
            state1 = store.load_state()
            self.assertEqual(state1["state"], State.WAIT_QUOTA.value)

            supervisor2 = Supervisor(store, quota)
            self.assertTrue(supervisor2._recover_supervisor_restart())
            state2 = store.load_state()
            self.assertEqual(state2["state"], State.WAIT_QUOTA.value)
            self.assertIsNone(state2.get("thread_id"))
            self.assertEqual(state2["generation"], 1)
        finally:
            temporary.cleanup()

    def test_recovered_quota_starts_first_provider_turn_once(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-5", "test recovery start once", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
            quota = ScriptedQuota([
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time())),
                    QuotaWindow("weekly", 10.0, 10080, int(time.time()) + 86400),
                ),
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:01Z",
                    QuotaWindow("5h", 0.0, 300, int(time.time()) + 18000),
                    QuotaWindow("weekly", 10.0, 10080, int(time.time()) + 86400),
                ),
            ])
            with self.env(plan, progress):
                final = Supervisor(store, quota).execute(start=True)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            fake_state = json.loads((root / ".fake-codex-state.json").read_text())
            self.assertEqual(fake_state["starts"], 1)
            self.assertEqual(fake_state["resumes"], 0)
        finally:
            temporary.cleanup()

    def test_first_thread_is_captured_after_deferred_recovery(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-6", "test thread capture", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
            quota = ScriptedQuota([
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time())),
                ),
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:01Z",
                    QuotaWindow("5h", 0.0, 300, int(time.time()) + 18000),
                ),
            ])
            with self.env(plan, progress, FAKE_CODEX_THREAD_ID="TEST-DEFERRED-EXACT-999"):
                final = Supervisor(store, quota).execute(start=True)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-DEFERRED-EXACT-999")
            events = store.load_events()
            thread_event = next(e for e in events if e["event"] == "thread_started")
            self.assertEqual(thread_event["state"], State.RUNNING.value)
        finally:
            temporary.cleanup()

    def test_unrecovered_authoritative_quota_does_not_spawn(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-7", "test unrecovered wait", str(root), verify_commands=["git diff --check"])
            quota = ScriptedQuota([
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time())),
                ),
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:01Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time()) + 7200),
                ),
            ])
            supervisor = Supervisor(store, quota)
            with self.env(plan, progress):
                supervisor._preflight()
                revalidated = supervisor._wait_and_revalidate_quota()
            self.assertTrue(revalidated)
            state = store.load_state()
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            self.assertFalse((root / ".fake-codex-state.json").exists())
            self.assertIsNone(state.get("thread_id"))
            events = store.load_events()
            names = [e["event"] for e in events]
            self.assertIn("quota_still_exhausted", names)
            self.assertNotIn("provider_started", names)
        finally:
            temporary.cleanup()

    def test_weekly_exhaustion_also_defers_first_start(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-deferred-8", "test weekly exhaustion", str(root), verify_commands=["git diff --check"])
            quota = ScriptedQuota(
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 10.0, 300, int(time.time()) + 18000),
                    QuotaWindow("weekly", 100.0, 10080, int(time.time()) + 500000),
                )
            )
            supervisor = Supervisor(store, quota)
            with self.env(plan, progress):
                supervisor._preflight()
            state = store.load_state()
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            self.assertEqual(state["error_kind"], "usage_limit_weekly")
            self.assertFalse((root / ".fake-codex-state.json").exists())
            self.assertIsNone(state.get("thread_id"))
        finally:
            temporary.cleanup()

    def test_existing_exact_thread_quota_recovery_behavior_unchanged(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize(
                "run-deferred-9",
                "test existing thread quota recovery",
                str(root),
                thread_id="PRE-EXISTING-EXACT-THREAD",
                verify_commands=["test -f fake-implemented.txt", "git diff --check"],
            )
            quota = ScriptedQuota([
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 0.0, 300, int(time.time()) + 18000),
                ),
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:01Z",
                    QuotaWindow("5h", 0.0, 300, int(time.time()) + 18000),
                ),
            ])
            with self.env(plan, progress, scenario="quota_again", FAKE_CODEX_THREAD_ID="PRE-EXISTING-EXACT-THREAD"), patch.dict(os.environ, {"FAKE_CODEX_RESET_SECONDS": "1"}):
                final = Supervisor(store, quota).execute(start=False)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "PRE-EXISTING-EXACT-THREAD")
            fake_state = json.loads((root / ".fake-codex-state.json").read_text())
            self.assertEqual(fake_state["resumes"], 2)
            self.assertEqual(fake_state["starts"], 0)
        finally:
            temporary.cleanup()

    def test_deferred_start_status_is_human_readable(self):
        temporary, root, plan, progress = repo_fixture()
        state_home = Path(temporary.name) / "state"
        state_home.mkdir(parents=True, exist_ok=True)
        try:
            store = NightwatchStore(root, state_home=state_home)
            store.initialize(
                "run-deferred-10",
                "test human readability",
                str(root),
                verify_commands=["git diff --check"],
            )
            quota = ScriptedQuota(
                QuotaSnapshot(
                    "live_app_server",
                    "2026-08-31T00:00:00Z",
                    QuotaWindow("5h", 100.0, 300, int(time.time()) + 3600),
                )
            )
            supervisor = Supervisor(store, quota)
            with self.env(plan, progress):
                supervisor._preflight()
            state = store.load_state()
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)

            args = cli._parser().parse_args(["status", "--repo", str(root)])
            output = io.StringIO()
            with patch.dict(os.environ, {"NIGHTWATCH_STATE_HOME": str(state_home)}), redirect_stdout(output):
                self.assertEqual(cli._status(args), 0)
            rendered = output.getvalue()
            self.assertIn("STATE          WAIT_QUOTA", rendered)
            self.assertIn("AGENT          WAITING_QUOTA (first launch deferred)", rendered)
            self.assertIn("THREAD         (not captured — first launch deferred)", rendered)

            self.assertEqual(_next_action(state), "revalidate quota at reset, then start first thread")
            self.assertIn("WAITING_QUOTA (first launch deferred)", _agent_summary(state))

            run = RunCatalog(state_home).discover()[0]
            dashboard = render_dashboard([run], selected=0, width=120)
            self.assertIn("First launch deferred", dashboard)
            status_text = status_run(run)
            self.assertIn("First launch deferred — no Codex thread created yet", status_text)
            self.assertIn("revalidate quota at reset, then start first thread", status_text)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
