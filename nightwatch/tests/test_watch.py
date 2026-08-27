from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nightwatch.storage import NightwatchStore
from nightwatch.supervisor import (
    PassiveWatcher,
    Supervisor,
    find_active_threads_for_repo,
    find_repo_codex_processes,
)


class WatchAndAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="nightwatch-watch-tests-")
        self.repo = Path(self.tmp_dir) / "test-repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        os.system(f"git -C {self.repo} init -q")
        os.system(f"git -C {self.repo} config user.email 'test@example.com'")
        os.system(f"git -C {self.repo} config user.name 'Test'")
        test_file = self.repo / "test.txt"
        test_file.write_text("hello\n")
        os.system(f"git -C {self.repo} add test.txt && git -C {self.repo} commit -qm 'initial'")
        
        self.state_home = Path(self.tmp_dir) / "state"
        self.store = NightwatchStore(self.repo, state_home=self.state_home)

    def tearDown(self) -> None:
        os.system(f"rm -rf {self.tmp_dir}")

    def test_adopt_thread_initializes_with_exact_thread_id(self) -> None:
        state = self.store.initialize(
            "run-adopt",
            "Adopt test thread",
            str(self.repo),
            verify_commands=["git diff --check"],
            thread_id="01a04416-c7aa-7271-9ede-7fe2d40cf950",
        )
        self.assertEqual(state["thread_id"], "01a04416-c7aa-7271-9ede-7fe2d40cf950")
        loaded = self.store.load_state()
        self.assertEqual(loaded["thread_id"], "01a04416-c7aa-7271-9ede-7fe2d40cf950")

    def test_preflight_blocks_when_external_codex_process_is_running(self) -> None:
        self.store.initialize(
            "run-collision",
            "Goal",
            str(self.repo),
            verify_commands=["git diff --check"],
        )
        fake_processes = [{"pid": 999999, "executable": "/bin/codex", "cwd": str(self.repo)}]
        with patch("nightwatch.supervisor.find_repo_codex_processes", return_value=fake_processes):
            supervisor = Supervisor(self.store)
            result = supervisor.execute(start=True)
            self.assertEqual(result["state"], "BLOCKED")
            self.assertIn("another Codex process", result["last_error"])

    def test_passive_watcher_parses_rollout_telemetry(self) -> None:
        rollout_file = self.repo / "fake-rollout.jsonl"
        lines = [
            json.dumps({"type": "session_meta", "payload": {"id": "01a04416-c7aa-7271-9ede-7fe2d40cf950", "cwd": str(self.repo)}}),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 50000, "input_tokens": 45000, "output_tokens": 5000}},
                    "rate_limits": {
                        "primary": {"used_percent": 15.0, "resets_at": 1787866896},
                        "secondary": {"used_percent": 2.0, "resets_at": 1788453696},
                    },
                },
            }),
        ]
        rollout_file.write_text("\n".join(lines) + "\n")

        watcher = PassiveWatcher(self.store)
        with patch.object(watcher, "discover_active_session", return_value={
            "pid": 12345,
            "processes": [{"pid": 12345}],
            "thread": {"id": "01a04416-c7aa-7271-9ede-7fe2d40cf950", "model": "gpt-5.6-luna", "git_branch": "main", "first_user_message": "Test goal"},
            "thread_id": "01a04416-c7aa-7271-9ede-7fe2d40cf950",
            "rollout_path": str(rollout_file),
        }):
            snapshot = watcher.inspect_live_snapshot()
            self.assertTrue(snapshot["active"])
            self.assertEqual(snapshot["thread_id"], "01a04416-c7aa-7271-9ede-7fe2d40cf950")
            self.assertEqual(snapshot["rate_limits"]["primary"]["used_percent"], 15.0)
            self.assertEqual(snapshot["tokens"]["total_tokens"], 50000)
