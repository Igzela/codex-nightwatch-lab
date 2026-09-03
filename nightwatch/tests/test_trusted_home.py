from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))
TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-home-state-")
os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

from nightwatch.models import validate_state  # noqa: E402
from nightwatch.storage import NightwatchStore, StateIntegrityError  # noqa: E402


def fixture() -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="nightwatch-home-")
    root = Path(temporary.name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "fixture"], cwd=root, check=True)
    return temporary, root


class TrustedHomeTests(unittest.TestCase):
    def test_fresh_initialization_records_trusted_home_identity(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            state = store.initialize("run-init", "goal", str(root), account_mode="AUTO_POOL")

            runtime_id = state.get("codex_runtime_identity")
            home_id = state.get("codex_home_identity")

            self.assertIsInstance(runtime_id, list)
            self.assertEqual(len(runtime_id), 2)
            self.assertIsInstance(home_id, list)
            self.assertEqual(len(home_id), 2)

            runtime_stat = os.lstat(store.codex_runtime_path)
            home_stat = os.lstat(store.codex_home_path)

            self.assertEqual(runtime_id, [runtime_stat.st_dev, runtime_stat.st_ino])
            self.assertEqual(home_id, [home_stat.st_dev, home_stat.st_ino])
        finally:
            temporary.cleanup()

    def test_matching_persisted_identity_survives_restart(self):
        temporary, root = fixture()
        try:
            store1 = NightwatchStore(root)
            store1.initialize("run-restart", "goal", str(root))

            # Simulate clean process restart by constructing a new store instance
            store2 = NightwatchStore(root)
            state2 = store2.load_state()

            self.assertIsNotNone(state2.get("codex_runtime_identity"))
            self.assertIsNotNone(state2.get("codex_home_identity"))

            # Calling verify and ensure succeeds
            store2.verify_codex_home()
            path = store2.ensure_codex_home()
            self.assertEqual(path, store2.codex_home_path)
        finally:
            temporary.cleanup()

    def test_home_inode_replacement_after_restart_fails_closed(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-home-replace", "goal", str(root))

            # Replace codex-home directory with a new guaranteed distinct inode
            old_home = store.codex_home_path
            new_home = Path(tempfile.mkdtemp(dir=store.codex_runtime_path))
            shutil.rmtree(old_home)
            os.replace(new_home, old_home)

            store2 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store2.verify_codex_home()

            store3 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store3.ensure_codex_home()
        finally:
            temporary.cleanup()

    def test_runtime_inode_replacement_after_restart_fails_closed(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-runtime-replace", "goal", str(root))

            # Replace codex-runtime directory with a new guaranteed distinct inode
            runtime_dir = store.codex_runtime_path
            new_runtime = Path(tempfile.mkdtemp(dir=store.directory))
            (new_runtime / "codex-home").mkdir(mode=0o700)
            shutil.rmtree(runtime_dir)
            os.replace(new_runtime, runtime_dir)

            store2 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store2.verify_codex_home()

            store3 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store3.ensure_codex_home()
        finally:
            temporary.cleanup()

    def test_existing_home_without_expected_identity_is_not_retrusted(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-untrusted", "goal", str(root))

            # Strip identities from state.json
            state_data = json.loads(store.state_path.read_text(encoding="utf-8"))
            state_data["codex_runtime_identity"] = None
            state_data["codex_home_identity"] = None
            state_data["legacy_pre_identity_migration"] = False
            store.state_path.write_text(json.dumps(state_data), encoding="utf-8")

            store2 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store2.ensure_codex_home()

            store3 = NightwatchStore(root)
            with self.assertRaises(StateIntegrityError):
                store3.verify_codex_home()
        finally:
            temporary.cleanup()

    def test_malformed_codex_home_identity_fails_state_validation(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            state = store.initialize("run-malformed", "goal", str(root))

            # Boolean as int
            bad_state = dict(state)
            bad_state["codex_runtime_identity"] = [True, 123]
            with self.assertRaises(ValueError):
                validate_state(bad_state)

            # Not a list
            bad_state["codex_runtime_identity"] = "bad"
            with self.assertRaises(ValueError):
                validate_state(bad_state)

            # Negative int
            bad_state["codex_runtime_identity"] = [-1, 2]
            with self.assertRaises(ValueError):
                validate_state(bad_state)

            # Wrong length
            bad_state["codex_runtime_identity"] = [1]
            with self.assertRaises(ValueError):
                validate_state(bad_state)
        finally:
            temporary.cleanup()

    def test_only_one_identity_field_fails_closed(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            state = store.initialize("run-one-field", "goal", str(root))

            state_runtime_only = dict(state)
            state_runtime_only["codex_runtime_identity"] = [100, 200]
            state_runtime_only["codex_home_identity"] = None
            with self.assertRaises(ValueError):
                validate_state(state_runtime_only)

            state_home_only = dict(state)
            state_home_only["codex_runtime_identity"] = None
            state_home_only["codex_home_identity"] = [100, 200]
            with self.assertRaises(ValueError):
                validate_state(state_home_only)
        finally:
            temporary.cleanup()

    def test_legacy_state_identity_migration_is_explicit(self):
        temporary, root = fixture()
        try:
            store = NightwatchStore(root)
            store.initialize("run-legacy-mig", "goal", str(root))

            # Set explicit legacy migration flag with missing identities
            state_data = json.loads(store.state_path.read_text(encoding="utf-8"))
            state_data["codex_runtime_identity"] = None
            state_data["codex_home_identity"] = None
            state_data["legacy_pre_identity_migration"] = True
            store.state_path.write_text(json.dumps(state_data), encoding="utf-8")

            store2 = NightwatchStore(root)
            # With explicit migration flag, ensure_codex_home establishes and updates
            path = store2.ensure_codex_home()
            self.assertEqual(path, store2.codex_home_path)
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
