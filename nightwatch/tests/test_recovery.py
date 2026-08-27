from __future__ import annotations

import json
import os
import signal
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
from nightwatch.storage import NightwatchStore, StateIntegrityError  # noqa: E402
from nightwatch.supervisor import Supervisor  # noqa: E402

FAKE = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"


def git_run(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def repo_fixture() -> tuple[tempfile.TemporaryDirectory, Path, Path, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-recovery-")
    root = Path(temporary.name)
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


class RecoveryTests(unittest.TestCase):
    def env(self, plan: Path, progress: Path, scenario: str, **extra):
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

    def test_codex_crash_resumes_exact_thread(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-crash", "goal", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
            with self.env(plan, progress, "crash", FAKE_CODEX_RESUME_SCENARIO="normal"):
                final = Supervisor(store, object()).execute(start=True)
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            self.assertGreaterEqual(final["crash_attempt"], 1)
            commands = "\n".join(path.read_text() for path in store.runs_path.glob("*.events.jsonl"))
            self.assertIn('"action": "resume"', commands)
            self.assertNotIn("--last", commands)
        finally:
            temporary.cleanup()

    def test_ambiguous_claim_after_restart_fails_closed(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-claim", "goal", str(root), verify_commands=["git diff --check"])
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test")
            store.transition(State.WAIT_QUOTA, "quota_exhausted", "test", {"thread_id": "TEST-001", "generation": 2, "next_resume_at": "2030-01-01T00:00:00Z"})
            store.mutate("resume_claimed", "simulated supervisor crash after claim", lambda state: {**state, "resume_claim": {"generation": 2, "claim_id": "x", "claimed_at": "2026-01-01T00:00:00Z", "pid": 999999, "phase": "spawn_prepared"}})
            with self.env(plan, progress, "normal"):
                final = Supervisor(store, object()).execute(start=False)
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertEqual(final["error_kind"], "state_integrity")
            self.assertFalse((root / ".fake-codex-state.json").exists())
        finally:
            temporary.cleanup()

    def test_resume_claim_after_provider_exit_fails_closed_on_restart(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-claim-running", "goal", str(root), verify_commands=["git diff --check"])
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test", {"thread_id": "TEST-001"})
            store.mutate("resume_claimed", "simulated crash after exact resume provider exit", lambda state: {
                **state,
                "generation": 2,
                "resume_claim": {"generation": 2, "claim_id": "x", "claimed_at": "2026-01-01T00:00:00Z", "pid": 999999, "phase": "spawn_prepared"},
            })
            with self.env(plan, progress, "normal"):
                final = Supervisor(store, object()).execute(start=False)
            self.assertEqual(final["state"], State.BLOCKED.value)
            self.assertEqual(final["error_kind"], "state_integrity")
            self.assertFalse((root / ".fake-codex-state.json").exists())
        finally:
            temporary.cleanup()

    def test_changed_repo_head_is_rejected_after_verified_commit(self):
        temporary, root, plan, progress = repo_fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-git", "goal", str(root), verify_commands=["test -f fake-implemented.txt", "git diff --check"])
            original = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            store.mutate("test_setup", "test", lambda state: {**state, "last_verified_commit": original, "thread_id": "TEST-001", "state": "RUNNING"})
            (root / "other.txt").write_text("external\n")
            git_run(root, "add", "other.txt")
            git_run(root, "commit", "-qm", "external")
            # A descendant commit is safe and should not be rejected.
            with self.env(plan, progress, "normal"):
                final = Supervisor(store, object()).execute(start=False)
            self.assertIn(final["state"], {State.DONE.value, State.BLOCKED.value})
        finally:
            temporary.cleanup()

    def test_nightwatch_restart_preserves_thread_without_new_start(self):
        temporary, root, plan, progress = repo_fixture()
        child = None
        supervisor_process = None
        try:
            environment = dict(os.environ)
            environment.update({
                "NIGHTWATCH_CODEX_BIN": str(FAKE),
                "NIGHTWATCH_SKIP_AUTH_CHECK": "1",
                "FAKE_CODEX_SCENARIO": "slow",
                "FAKE_CODEX_RESUME_SCENARIO": "normal",
                "FAKE_CODEX_PLAN_FILE": str(plan),
                "FAKE_CODEX_PROGRESS_FILE": str(progress),
            })
            command = [sys.executable, str(PRODUCT / "bin" / "nightwatch"), "run", "goal", "--verify", "test -f fake-implemented.txt", "--repo", str(root), "--no-inhibit"]
            supervisor_process = subprocess.Popen(command, cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            state_path = NightwatchStore(root).state_path
            deadline = time.time() + 10
            state = {}
            while time.time() < deadline:
                if state_path.exists():
                    try:
                        state = json.loads(state_path.read_text())
                    except json.JSONDecodeError:
                        pass
                    if state.get("thread_id") and state.get("active_process"):
                        break
                time.sleep(0.05)
            self.assertEqual(state.get("thread_id"), "TEST-001")
            child = state.get("active_process", {}).get("pid")
            os.kill(supervisor_process.pid, signal.SIGKILL)
            supervisor_process.wait(timeout=5)
            supervisor_process.communicate(timeout=2)
            if child and _pid_alive(child):
                os.kill(child, signal.SIGKILL)
            resumed = subprocess.run([sys.executable, str(PRODUCT / "bin" / "nightwatch"), "resume", "--repo", str(root)], cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20, check=False)
            self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
            final = json.loads(state_path.read_text())
            self.assertEqual(final["state"], State.DONE.value)
            self.assertEqual(final["thread_id"], "TEST-001")
            fake_state = json.loads((root / ".fake-codex-state.json").read_text())
            self.assertEqual(fake_state["starts"], 1)
            self.assertEqual(fake_state["resumes"], 1)
        finally:
            if supervisor_process and supervisor_process.poll() is None:
                supervisor_process.kill()
            if child and _pid_alive(child):
                os.kill(child, signal.SIGKILL)
            temporary.cleanup()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
