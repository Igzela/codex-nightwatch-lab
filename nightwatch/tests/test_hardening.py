from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))
TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-hardened-state-")
os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

from nightwatch.app_server import AppServerClient, AppServerProtocolError  # noqa: E402
from nightwatch.milestones import adopt_proposed_plan, read_mailbox_json, trusted_environment  # noqa: E402
from nightwatch.models import State  # noqa: E402
from nightwatch.quota import QuotaError, parse_quota_result  # noqa: E402
from nightwatch.storage import NightwatchStore, StateIntegrityError, SupervisorAlreadyRunning  # noqa: E402
from nightwatch.supervisor import process_identity, process_matches, resume_prompt, start_prompt  # noqa: E402


FAKE_APP = PRODUCT.parent / "test-artifacts" / "fake-app-server" / "fake_app_server.py"


def fixture() -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-hardening-")
    root = Path(temporary.name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "fixture"], cwd=root, check=True)
    return temporary, root


class TrustedControlPlaneTests(unittest.TestCase):
    def test_new_run_clears_stale_mailbox_outputs_but_preserves_unrelated_files(self):
        temporary, root = fixture()
        try:
            mailbox = root / ".nightwatch-agent"
            mailbox.mkdir()
            for name in ("proposed-plan.json", "progress.json", "blocker.json"):
                (mailbox / name).write_text('{"stale": true}\n')
            (mailbox / "user-note.txt").write_text("preserve\n")

            store = NightwatchStore(root)
            store.initialize("fresh-run", "fresh goal", str(root), verify_commands=["pytest -q"])

            for name in ("proposed-plan.json", "progress.json", "blocker.json"):
                self.assertFalse((mailbox / name).exists())
            self.assertEqual((mailbox / "user-note.txt").read_text(), "preserve\n")
            context = json.loads((mailbox / "context.json").read_text())
            self.assertEqual(context["goal_hash"], store.load_acceptance()["goal_hash"])
        finally:
            temporary.cleanup()

    def test_new_run_unlinks_stale_mailbox_symlink_without_touching_target(self):
        temporary, root = fixture()
        outside_temporary = tempfile.TemporaryDirectory(prefix="nightwatch-stale-mailbox-target-")
        try:
            outside = Path(outside_temporary.name) / "outside.json"
            outside.write_text('{"preserve": true}\n')
            mailbox = root / ".nightwatch-agent"
            mailbox.mkdir()
            (mailbox / "progress.json").symlink_to(outside)

            NightwatchStore(root).initialize("fresh-run", "fresh goal", str(root), verify_commands=["pytest -q"])

            self.assertFalse((mailbox / "progress.json").exists())
            self.assertEqual(outside.read_text(), '{"preserve": true}\n')
        finally:
            outside_temporary.cleanup()
            temporary.cleanup()

    def test_symlinked_mailbox_root_is_rejected(self):
        temporary, root = fixture()
        outside_temporary = tempfile.TemporaryDirectory(prefix="nightwatch-mailbox-escape-")
        outside = Path(outside_temporary.name)
        try:
            (root / ".nightwatch-agent").symlink_to(outside, target_is_directory=True)
            store = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store.initialize("mailbox-root-symlink", "goal", str(root), verify_commands=["pytest -q"])
            self.assertEqual(list(outside.iterdir()), [])
            self.assertTrue((root / ".nightwatch-agent").is_symlink())
        finally:
            outside_temporary.cleanup()
            temporary.cleanup()

    def test_state_home_inside_repo_is_rejected(self):
        temporary, root = fixture()
        try:
            inside = root / ".state"
            with patch.dict(os.environ, {"NIGHTWATCH_STATE_HOME": str(inside)}, clear=False):
                with self.assertRaisesRegex(StateIntegrityError, "outside the Codex workspace"):
                    NightwatchStore(root)
            self.assertFalse(inside.exists())
        finally:
            temporary.cleanup()

    def test_state_home_symlink_resolving_inside_repo_is_rejected(self):
        temporary, root = fixture()
        try:
            target = root / ".state-target"
            target.mkdir()
            link = root / ".state-link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(StateIntegrityError, "outside the Codex workspace"):
                NightwatchStore(root, state_home=link)
            self.assertEqual(list(target.iterdir()), [])
        finally:
            temporary.cleanup()

    def test_xdg_state_home_inside_repo_is_rejected(self):
        temporary, root = fixture()
        try:
            inside = root / ".xdg-state"
            clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(Path.home()))}
            with patch.dict(os.environ, {**clean_env, "XDG_STATE_HOME": str(inside)}, clear=True):
                with self.assertRaisesRegex(StateIntegrityError, "outside the Codex workspace"):
                    NightwatchStore(root)
            self.assertFalse(inside.exists())
        finally:
            temporary.cleanup()

    def test_external_state_home_remains_valid(self):
        temporary, root = fixture()
        state_temporary = tempfile.TemporaryDirectory(prefix="nightwatch-external-state-")
        try:
            external = Path(state_temporary.name) / "state"
            with patch.dict(os.environ, {"NIGHTWATCH_STATE_HOME": str(external)}, clear=False):
                store = NightwatchStore(root)
                state = store.initialize("external-state", "goal", str(root), verify_commands=["pytest -q"])
            self.assertTrue(store.directory.is_relative_to(external))
            self.assertFalse(store.directory.is_relative_to(root))
            self.assertEqual(store.load_state()["run_id"], state["run_id"])
        finally:
            state_temporary.cleanup()
            temporary.cleanup()

    def test_state_is_outside_workspace_and_legacy_state_is_ignored(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root, state_home=TEST_STATE_HOME)
            state = store.initialize("control", "goal", str(root), verify_commands=["git diff --check"])
            self.assertTrue(store.state_path.is_relative_to(Path(TEST_STATE_HOME)))
            self.assertFalse(store.state_path.is_relative_to(root))
            self.assertEqual(stat.S_IMODE(store.directory.stat().st_mode), 0o700)
            legacy = root / ".nightwatch"
            legacy.mkdir()
            (legacy / "state.json").write_text(json.dumps({"thread_id": "ATTACKER"}))
            self.assertEqual(store.load_state()["thread_id"], state["thread_id"])
            self.assertTrue(store.mailbox_directory.is_relative_to(root))
        finally:
            temporary.cleanup()

    def test_mailbox_command_authority_and_symlink_are_rejected(self):
        temporary, root = fixture()
        escaped = Path(tempfile.gettempdir()) / f"NIGHTWATCH_ESCAPED_{os.getpid()}"
        try:
            store = NightwatchStore(root)
            store.initialize("mailbox", "goal", str(root), verify_commands=["git diff --check"])
            context = json.loads((store.mailbox_directory / "context.json").read_text())
            (store.mailbox_directory / "proposed-plan.json").write_text(json.dumps({"goal_hash": context["goal_hash"], "milestones": [{"id": "M1", "title": "attack", "verification_commands": [f"touch {escaped}"]}]}))
            self.assertFalse(adopt_proposed_plan(store))
            self.assertFalse(escaped.exists())
            (store.mailbox_directory / "progress.json").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                read_mailbox_json(store, "progress.json")
        finally:
            if escaped.exists():
                escaped.unlink()
            temporary.cleanup()

    def test_mailbox_root_replacement_is_rejected_on_later_read(self):
        temporary, root = fixture()
        outside_temporary = tempfile.TemporaryDirectory(prefix="nightwatch-mailbox-replacement-")
        outside = Path(outside_temporary.name)
        try:
            store = NightwatchStore(root)
            store.initialize("mailbox-replacement", "goal", str(root), verify_commands=["pytest -q"])
            original = root / ".nightwatch-agent-original"
            store.mailbox_directory.rename(original)
            store.mailbox_directory.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                read_mailbox_json(store, "context.json")
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            outside_temporary.cleanup()
            temporary.cleanup()

    def test_frozen_policy_is_not_changed_by_workspace_file(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("policy", "goal", str(root), verify_commands=["git diff --check"])
            before = store.load_policy()
            (root / ".nightwatch-policy.toml").write_text('[verification]\nfinal = ["touch /tmp/nope"]\n')
            self.assertEqual(store.load_policy(), before)
            self.assertEqual(store.load_acceptance()["verification_policy_hash"], before["policy_hash"])
        finally:
            temporary.cleanup()

    def test_diff_only_policy_cannot_authorize_natural_language_done(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            state = store.initialize("weak-policy", "implement a production feature", str(root), verify_commands=["git diff --check"])
            self.assertFalse(state["acceptance_ready"])
        finally:
            temporary.cleanup()

    def test_event_sequence_and_secret_redaction_fail_closed(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("events", "goal", str(root), verify_commands=["git diff --check"])
            with store.events_path.open("a") as handle:
                handle.write('{"seq":99}\n')
            with self.assertRaises(StateIntegrityError):
                store.load_state()
        finally:
            temporary.cleanup()

    def test_verification_environment_excludes_secrets(self):
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "secret", "GITHUB_TOKEN": "secret", "PATH": "/bin"}, clear=False):
            env = trusted_environment()
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["PATH"], "/bin")

    def test_prompts_expose_frozen_checks_without_granting_authority(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            state = store.initialize("prompt-policy", "implement calculator", str(root), verify_commands=["python3 -m unittest discover -s tests -v", "git diff --check"])
            initial = start_prompt(store, state["goal"])
            resumed = resume_prompt(store, store.load_state())
            for prompt in (initial, resumed):
                self.assertIn("python3 -m unittest discover -s tests -v", prompt)
            self.assertIn("cannot authorize host commands", initial)
            self.assertIn("not the model, decides verified/DONE", resumed)
            self.assertIn(store.load_acceptance()["goal_hash"], resumed)
            self.assertIn("If `.nightwatch-agent/proposed-plan.json` is absent, create it", resumed)
        finally:
            temporary.cleanup()


class AppServerProtocolTests(unittest.TestCase):
    def test_full_handshake_ignores_noise_and_parses_milliseconds(self):
        for scenario in ("milliseconds", "malformed"):
            with self.subTest(scenario=scenario), patch.dict(os.environ, {"FAKE_APP_SERVER_SCENARIO": scenario}, clear=False):
                client = AppServerClient(str(FAKE_APP), timeout=3)
                value = client.rate_limits()
            quota = parse_quota_result(value, "live_app_server")
            self.assertEqual(quota.primary.used_percent, 99.9)
            self.assertEqual(quota.primary.resets_at, 1787859341)
            self.assertEqual([item.get("method") for item in client.trace if item["direction"] == "send"], ["initialize", "initialized", "account/rateLimits/read"])
            self.assertIn({"direction": "recv", "kind": "other_response", "id": 999}, client.trace)
            if scenario == "malformed":
                self.assertIn({"direction": "recv", "kind": "malformed"}, client.trace)

    def test_protocol_error_and_timeout_are_not_silently_accepted(self):
        for scenario in ("error", "timeout", "exit"):
            with self.subTest(scenario=scenario), patch.dict(os.environ, {"FAKE_APP_SERVER_SCENARIO": scenario}, clear=False):
                with self.assertRaises(AppServerProtocolError):
                    AppServerClient(str(FAKE_APP), timeout=0.2).rate_limits()
        with patch.dict(os.environ, {"FAKE_APP_SERVER_SCENARIO": "missing_rate_limits"}, clear=False):
            with self.assertRaises(QuotaError):
                parse_quota_result(AppServerClient(str(FAKE_APP), timeout=1).rate_limits())

    def test_quota_parser_edges(self):
        snapshot = parse_quota_result({"rateLimits": {"primary": {"usedPercent": 101, "windowDurationMins": 300, "resetsAt": None}, "secondary": {"usedPercent": 99.9, "windowDurationMins": 10080, "resetsAt": 1787859341}}})
        self.assertTrue(snapshot.primary.exhausted)
        self.assertIsNone(snapshot.primary.resets_at)
        self.assertFalse(snapshot.secondary.exhausted)
        only_primary = parse_quota_result({"rateLimits": {"primary": {"usedPercent": None, "windowDurationMins": 300, "resetsAt": 1787859341}}})
        self.assertIsNone(only_primary.primary.used_percent)
        self.assertIsNone(only_primary.secondary)
        only_secondary = parse_quota_result({"rate_limits": {"secondary": {"used_percent": 100, "window_duration_mins": 10080, "resets_at": 1787859341}}})
        self.assertIsNone(only_secondary.primary)
        self.assertTrue(only_secondary.secondary.exhausted)


class ProcessAndLockTests(unittest.TestCase):
    def test_two_resume_processes_allow_one_supervisor_only(self):
        temporary, root = fixture()
        first = None
        child_pid = None
        try:
            store = NightwatchStore(root)
            store.initialize("two-resume", "goal", str(root), verify_commands=["git diff --check"])
            fake = PRODUCT.parent / "test-artifacts" / "fake-codex" / "fake_codex.py"
            env = dict(os.environ)
            env.update({"PYTHONPATH": str(PRODUCT), "NIGHTWATCH_CODEX_BIN": str(fake), "NIGHTWATCH_SKIP_AUTH_CHECK": "1", "FAKE_CODEX_SCENARIO": "slow"})
            command = [sys.executable, str(PRODUCT / "bin" / "nightwatch"), "resume", "--repo", str(root), "--no-inhibit"]
            first = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.time() + 8
            while time.time() < deadline:
                current = store.load_state()
                if current.get("supervisor_owner") and current.get("active_process"):
                    child_pid = current["active_process"]["pid"]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child_pid)
            second = subprocess.run(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5, check=False)
            self.assertEqual(second.returncode, 0)
            self.assertIn("already supervised", second.stderr)
            self.assertEqual(store.load_state()["active_process"]["pid"], child_pid)
        finally:
            if first and first.poll() is None:
                first.kill()
                first.wait()
            if child_pid:
                try:
                    os.kill(child_pid, 9)
                except OSError:
                    pass
            if first:
                for stream in (first.stdout, first.stderr):
                    if stream is not None:
                        stream.close()
            temporary.cleanup()

    def test_lifetime_lock_rejects_second_owner_without_mutating_state(self):
        temporary, root = fixture()
        child = None
        try:
            store = NightwatchStore(root)
            store.initialize("lock", "goal", str(root), verify_commands=["git diff --check"])
            code = "from nightwatch.storage import NightwatchStore; import sys,time; s=NightwatchStore(sys.argv[1]);\nwith s.supervisor_lease():\n print('locked', flush=True); time.sleep(30)"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(PRODUCT)
            child = subprocess.Popen([sys.executable, "-c", code, str(root)], stdout=subprocess.PIPE, text=True, env=env)
            self.assertEqual(child.stdout.readline().strip(), "locked")
            before = store.load_state()
            with self.assertRaises(SupervisorAlreadyRunning):
                with store.supervisor_lease():
                    pass
            self.assertEqual(store.load_state(), before)
        finally:
            if child and child.poll() is None:
                child.kill()
                child.wait()
            if child and child.stdout is not None:
                child.stdout.close()
            temporary.cleanup()

    def test_pid_identity_rejects_forged_starttime(self):
        process = subprocess.Popen(["/bin/sleep", "5"])
        try:
            identity = process_identity(process.pid)
            self.assertIsNotNone(identity)
            assert identity is not None
            self.assertTrue(process_matches(identity))
            self.assertFalse(process_matches({**identity, "starttime": "forged"}))
        finally:
            process.terminate()
            process.wait()


if __name__ == "__main__":
    unittest.main()
