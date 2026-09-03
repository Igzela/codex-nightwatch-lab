from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))
TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-dedup-state-")
os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

from nightwatch.milestones import verify_milestones  # noqa: E402
from nightwatch.models import cross_account_thread_mode_for_version  # noqa: E402
from nightwatch.operations import MAX_VERIFY_COMMANDS, RunSpec  # noqa: E402
from nightwatch.storage import NightwatchStore  # noqa: E402
from nightwatch.supervisor import Supervisor  # noqa: E402


def fixture() -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-dedup-")
    root = Path(temporary.name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "fixture"], cwd=root, check=True)
    return temporary, root


class VerificationDedupTests(unittest.TestCase):
    def test_verify_milestones_runs_shared_checks_once_for_all_eligible_milestones(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-1", "goal", str(root), verify_commands=["echo check1"])
            plan = store.load_plan()
            plan["milestones"] = [
                {"id": "M1", "title": "m1", "weight": 50, "required": True, "status": "implemented", "verification_profile": "default", "evidence": []},
                {"id": "M2", "title": "m2", "weight": 50, "required": True, "status": "working", "verification_profile": "default", "evidence": []},
            ]
            store.save_plan(plan)

            call_counts: list[str] = []

            def fake_runner(repo, cmd):
                call_counts.append(cmd)
                return {"command": cmd, "ok": True, "exit_code": 0, "stdout": "ok\n", "stderr": ""}

            with patch("nightwatch.milestones._run_trusted_command", side_effect=fake_runner):
                result = verify_milestones(store)

            self.assertTrue(result["all_milestones_verified"])
            self.assertTrue(result["all_final_checks_passed"])
            # Shared checks (1) + final check (1) = 2 total
            self.assertEqual(len(call_counts), 2)
            self.assertEqual(call_counts, ["echo check1", "echo check1"])

            updated_plan = store.load_plan()
            self.assertEqual(updated_plan["milestones"][0]["status"], "verified")
            self.assertEqual(updated_plan["milestones"][1]["status"], "verified")
            self.assertEqual(len(updated_plan["milestones"][0]["evidence"]), 1)
            self.assertEqual(len(updated_plan["milestones"][1]["evidence"]), 1)
        finally:
            temporary.cleanup()

    def test_verification_commands_total_sets_capped_at_two(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-2", "goal", str(root), verify_commands=["check_a", "check_b"])
            plan = store.load_plan()
            # 5 eligible milestones
            plan["milestones"] = [
                {"id": f"M{i}", "title": f"m{i}", "weight": 20, "required": True, "status": "implemented", "verification_profile": "default", "evidence": []}
                for i in range(1, 6)
            ]
            store.save_plan(plan)

            call_counts: list[str] = []

            def fake_runner(repo, cmd):
                call_counts.append(cmd)
                return {"command": cmd, "ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

            with patch("nightwatch.milestones._run_trusted_command", side_effect=fake_runner):
                result = verify_milestones(store)

            self.assertTrue(result["all_milestones_verified"])
            # 2 commands * 2 sets = 4 calls total (regardless of 5 milestones)
            self.assertEqual(len(call_counts), 4)
            self.assertEqual(call_counts, ["check_a", "check_b", "check_a", "check_b"])
        finally:
            temporary.cleanup()

    def test_verification_failure_marks_no_eligible_milestones_verified(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-3", "goal", str(root), verify_commands=["fail_check"])
            plan = store.load_plan()
            plan["milestones"] = [
                {"id": "M1", "title": "m1", "weight": 50, "required": True, "status": "implemented", "verification_profile": "default", "evidence": []},
                {"id": "M2", "title": "m2", "weight": 50, "required": True, "status": "implemented", "verification_profile": "default", "evidence": []},
            ]
            store.save_plan(plan)

            def fake_runner(repo, cmd):
                return {"command": cmd, "ok": False, "exit_code": 1, "stdout": "", "stderr": "error"}

            with patch("nightwatch.milestones._run_trusted_command", side_effect=fake_runner):
                result = verify_milestones(store)

            self.assertFalse(result["all_milestones_verified"])
            self.assertFalse(result["all_final_checks_passed"])
            updated_plan = store.load_plan()
            self.assertEqual(updated_plan["milestones"][0]["status"], "implemented")
            self.assertEqual(updated_plan["milestones"][1]["status"], "implemented")
        finally:
            temporary.cleanup()

    def test_verification_profile_none_runs_no_commands(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-4", "goal", str(root), verify_commands=["cmd"])
            plan = store.load_plan()
            plan["milestones"] = [
                {"id": "M1", "title": "m1", "weight": 100, "required": True, "status": "implemented", "verification_profile": "none", "evidence": []},
            ]
            store.save_plan(plan)

            call_counts: list[str] = []

            def fake_runner(repo, cmd):
                call_counts.append(cmd)
                return {"command": cmd, "ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

            with patch("nightwatch.milestones._run_trusted_command", side_effect=fake_runner):
                verify_milestones(store)

            # Only final check runs (1 call), milestone phase was skipped because profile != "default"
            self.assertEqual(len(call_counts), 1)
        finally:
            temporary.cleanup()

    def test_verify_milestones_does_not_leak_or_cache_across_calls(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-5", "goal", str(root), verify_commands=["cmd"])
            call_counts = [0]

            def fake_runner(repo, cmd):
                call_counts[0] += 1
                return {"command": cmd, "ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

            with patch("nightwatch.milestones._run_trusted_command", side_effect=fake_runner):
                verify_milestones(store)
                first_count = call_counts[0]
                verify_milestones(store)
                second_count = call_counts[0]

            # Second call executed independent fresh verification checks
            self.assertGreater(second_count, first_count)
        finally:
            temporary.cleanup()


class AuthSanityAndCapabilityTests(unittest.TestCase):
    def test_auth_check_runs_when_nightwatch_codex_bin_is_set(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("auth-test", "goal", str(root))
            supervisor = Supervisor(store)

            executed_commands: list[list[str]] = []

            def fake_subprocess_run(cmd, **kwargs):
                executed_commands.append(cmd)
                mock_res = MagicMock()
                mock_res.returncode = 0
                return mock_res

            with patch.dict(os.environ, {"NIGHTWATCH_CODEX_BIN": "/custom/codex"}, clear=False):
                os.environ.pop("NIGHTWATCH_SKIP_AUTH_CHECK", None)
                with patch("subprocess.run", side_effect=fake_subprocess_run):
                    res = supervisor._auth_sanity("/custom/codex")
                    self.assertTrue(res)
                    self.assertEqual(executed_commands, [["/custom/codex", "login", "status"]])
        finally:
            temporary.cleanup()

    def test_auth_check_bypassed_only_when_nightwatch_skip_auth_check_is_one(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("auth-skip", "goal", str(root))
            supervisor = Supervisor(store)

            with patch.dict(os.environ, {"NIGHTWATCH_SKIP_AUTH_CHECK": "1"}, clear=False):
                with patch("subprocess.run") as mock_run:
                    res = supervisor._auth_sanity("/bin/codex")
                    self.assertTrue(res)
                    mock_run.assert_not_called()
        finally:
            temporary.cleanup()

    def test_cross_account_capability_mode_enum_rejects_unknown(self):
        with patch.dict(os.environ, {"NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE": "UNKNOWN"}, clear=False):
            # When override is "UNKNOWN", it is rejected and does not return "UNKNOWN"
            mode = cross_account_thread_mode_for_version("codex 0.152.1")
            self.assertIn(mode, {"PROVEN", "UNSUPPORTED", "INCONCLUSIVE"})
            self.assertNotEqual(mode, "UNKNOWN")


class VerifyLimitTests(unittest.TestCase):
    def test_verify_commands_at_limit_accepted(self):
        temporary, root = fixture()
        try:
            cmds = tuple(f"echo {i}" for i in range(MAX_VERIFY_COMMANDS))
            self.assertEqual(len(cmds), 16)
            spec = RunSpec(root, "goal", verify_commands=cmds)
            self.assertEqual(len(spec.verify_commands), 16)
        finally:
            temporary.cleanup()

    def test_verify_commands_over_limit_rejected(self):
        temporary, root = fixture()
        try:
            cmds = tuple(f"echo {i}" for i in range(MAX_VERIFY_COMMANDS + 1))
            self.assertEqual(len(cmds), 17)
            with self.assertRaises(ValueError) as ctx:
                RunSpec(root, "goal", verify_commands=cmds)
            self.assertIn("too many verification commands", str(ctx.exception))
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
