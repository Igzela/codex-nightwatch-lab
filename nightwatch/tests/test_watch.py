from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from nightwatch.models import State
from nightwatch.storage import NightwatchStore
from nightwatch.supervisor import (
    PassiveWatcher,
    Supervisor,
    extract_rollout_meta,
    find_active_threads_for_repo,
    find_proven_codex_sessions,
    find_repo_codex_processes,
    process_matches,
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

    def _create_rollout(self, thread_id: str, cwd: str | None = None, primary_pct: float = 10.0) -> Path:
        rollout_file = Path(self.tmp_dir) / f"rollout-{thread_id}.jsonl"
        lines = [
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "session_id": thread_id,
                    "cwd": cwd or str(self.repo),
                    "thread_source": "user",
                },
            }),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 1000, "input_tokens": 900, "output_tokens": 100}},
                    "rate_limits": {
                        "primary": {"used_percent": primary_pct, "resets_at": 1787866896},
                        "secondary": {"used_percent": 1.0, "resets_at": 1788453696},
                    },
                },
            }),
        ]
        rollout_file.write_text("\n".join(lines) + "\n")
        return rollout_file

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
        rollout_file = self._create_rollout("01a04416-c7aa-7271-9ede-7fe2d40cf950", primary_pct=15.0)
        watcher = PassiveWatcher(self.store)
        with patch.object(watcher, "discover_active_session", return_value={
            "status": "OK",
            "active": True,
            "pid": 12345,
            "pid_identity": {"pid": 12345, "starttime": "100", "executable": "/bin/codex"},
            "thread_id": "01a04416-c7aa-7271-9ede-7fe2d40cf950",
            "model": "gpt-5.6-luna",
            "branch": "main",
            "title": "Test goal",
            "rollout_path": str(rollout_file),
        }):
            snapshot = watcher.inspect_live_snapshot()
            self.assertEqual(snapshot["status"], "OK")
            self.assertTrue(snapshot["active"])
            self.assertEqual(snapshot["thread_id"], "01a04416-c7aa-7271-9ede-7fe2d40cf950")
            self.assertEqual(snapshot["rate_limits"]["primary"]["used_percent"], 15.0)
            self.assertEqual(snapshot["tokens"]["total_tokens"], 1000)

    def test_watch_binds_pid_to_thread_from_its_own_rollout(self) -> None:
        rollout_a = self._create_rollout("THREAD-A")
        rollout_b = self._create_rollout("THREAD-B")

        fake_procs = [
            {"pid": 1001, "executable": "/bin/codex", "cwd": str(self.repo)},
            {"pid": 1002, "executable": "/bin/codex", "cwd": str(self.repo)},
        ]
        # SQLite returns THREAD-B as newest
        fake_sqlite = [
            {"id": "THREAD-B", "title": "Session B", "model": "gpt-5.6-terra"},
            {"id": "THREAD-A", "title": "Session A", "model": "gpt-5.6-luna"},
        ]

        def fake_readlink(path_str: str) -> str:
            if "1001" in path_str:
                return str(rollout_a)
            if "1002" in path_str:
                return str(rollout_b)
            return ""

        def fake_listdir(path_str: str) -> list[str]:
            if "/proc/" in path_str and "/fd" in path_str:
                return ["3"]
            return []

        with patch("nightwatch.supervisor.sys_platform_linux", return_value=True), \
             patch("nightwatch.supervisor.find_repo_codex_processes", return_value=fake_procs), \
             patch("nightwatch.supervisor.process_identity", side_effect=lambda p: {"pid": p, "starttime": str(p), "executable": "/bin/codex"}), \
             patch("nightwatch.supervisor.process_matches", return_value=True), \
             patch("nightwatch.supervisor.find_active_threads_for_repo", return_value=fake_sqlite), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("os.listdir", side_effect=fake_listdir), \
             patch("os.readlink", side_effect=fake_readlink):

            sessions = find_proven_codex_sessions(self.repo)
            self.assertEqual(len(sessions), 2)
            session_1001 = [s for s in sessions if s["pid"] == 1001][0]
            session_1002 = [s for s in sessions if s["pid"] == 1002][0]

            # Primary proof chain: PID 1001 MUST bind to THREAD-A, not SQLite's newest THREAD-B
            self.assertEqual(session_1001["thread_id"], "THREAD-A")
            self.assertEqual(session_1001["rollout_path"], str(rollout_a))
            self.assertEqual(session_1002["thread_id"], "THREAD-B")
            self.assertEqual(session_1002["rollout_path"], str(rollout_b))

    def test_multiple_live_sessions_fail_closed_without_explicit_thread(self) -> None:
        fake_proven = [
            {"pid": 1001, "thread_id": "THREAD-A", "pid_identity": {"pid": 1001}, "rollout_path": "/r/a.jsonl"},
            {"pid": 1002, "thread_id": "THREAD-B", "pid_identity": {"pid": 1002}, "rollout_path": "/r/b.jsonl"},
        ]
        watcher = PassiveWatcher(self.store, explicit_thread=None)
        with patch("nightwatch.supervisor.find_proven_codex_sessions", return_value=fake_proven):
            disc = watcher.discover_active_session()
            self.assertEqual(disc["status"], "AMBIGUOUS_ACTIVE_SESSIONS")
            self.assertFalse(disc["active"])
            self.assertEqual(len(disc["sessions"]), 2)

    def test_explicit_thread_selects_only_matching_live_session(self) -> None:
        rollout_a = self._create_rollout("THREAD-A")
        rollout_b = self._create_rollout("THREAD-B")
        fake_procs = [
            {"pid": 1001, "executable": "/bin/codex", "cwd": str(self.repo)},
            {"pid": 1002, "executable": "/bin/codex", "cwd": str(self.repo)},
        ]

        def fake_readlink(path_str: str) -> str:
            if "1001" in path_str:
                return str(rollout_a)
            if "1002" in path_str:
                return str(rollout_b)
            return ""

        def fake_listdir(path_str: str) -> list[str]:
            if "/proc/" in path_str and "/fd" in path_str:
                return ["3"]
            return []

        with patch("nightwatch.supervisor.sys_platform_linux", return_value=True), \
             patch("nightwatch.supervisor.find_repo_codex_processes", return_value=fake_procs), \
             patch("nightwatch.supervisor.process_identity", side_effect=lambda p: {"pid": p, "starttime": str(p), "executable": "/bin/codex"}), \
             patch("nightwatch.supervisor.process_matches", return_value=True), \
             patch("nightwatch.supervisor.find_active_threads_for_repo", return_value=[]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("os.listdir", side_effect=fake_listdir), \
             patch("os.readlink", side_effect=fake_readlink):

            watcher = PassiveWatcher(self.store, explicit_thread="THREAD-B")
            disc = watcher.discover_active_session()
            self.assertEqual(disc["status"], "OK")
            self.assertEqual(disc["thread_id"], "THREAD-B")
            self.assertEqual(disc["pid"], 1002)

    def test_rollout_thread_mismatch_never_uses_recent_sqlite_thread(self) -> None:
        rollout_a = self._create_rollout("THREAD-A")
        fake_procs = [{"pid": 1001, "executable": "/bin/codex", "cwd": str(self.repo)}]
        fake_sqlite = [{"id": "THREAD-B", "title": "Irrelevant thread"}]

        with patch("nightwatch.supervisor.sys_platform_linux", return_value=True), \
             patch("nightwatch.supervisor.find_repo_codex_processes", return_value=fake_procs), \
             patch("nightwatch.supervisor.process_identity", return_value={"pid": 1001, "starttime": "1001", "executable": "/bin/codex"}), \
             patch("nightwatch.supervisor.process_matches", return_value=True), \
             patch("nightwatch.supervisor.find_active_threads_for_repo", return_value=fake_sqlite), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("os.listdir", return_value=["3"]), \
             patch("os.readlink", return_value=str(rollout_a)):

            watcher = PassiveWatcher(self.store)
            disc = watcher.discover_active_session()
            self.assertEqual(disc["status"], "OK")
            # Must strictly use rollout thread A, never SQLite thread B
            self.assertEqual(disc["thread_id"], "THREAD-A")

    def test_quota_hit_does_not_start_supervisor_while_watched_pid_alive(self) -> None:
        rollout_a = self._create_rollout("THREAD-A", primary_pct=100.0)
        watcher = PassiveWatcher(self.store)
        fake_session = {
            "status": "OK",
            "active": True,
            "pid": 1001,
            "pid_identity": {"pid": 1001, "starttime": "1001", "executable": "/bin/codex"},
            "thread_id": "THREAD-A",
            "rollout_path": str(rollout_a),
        }

        with patch.object(watcher, "discover_active_session", return_value=fake_session), \
             patch("nightwatch.supervisor.process_matches", return_value=True), \
             patch.object(Supervisor, "execute") as mock_exec:

            def fake_sleep(duration: float) -> None:
                watcher.request_stop()

            with patch("time.sleep", side_effect=fake_sleep):
                snap = watcher.watch(auto_takeover=True)

            self.assertEqual(snap["status"], "TAKEOVER_PENDING")
            self.assertTrue(snap["takeover_pending"])
            self.assertFalse(self.store.exists())
            mock_exec.assert_not_called()

    def test_takeover_starts_only_after_same_pid_identity_exits(self) -> None:
        rollout_a = self._create_rollout("THREAD-A", primary_pct=100.0)
        watcher = PassiveWatcher(self.store)
        fake_session = {
            "status": "OK",
            "active": True,
            "pid": 1001,
            "pid_identity": {"pid": 1001, "starttime": "1001", "executable": "/bin/codex"},
            "thread_id": "THREAD-A",
            "rollout_path": str(rollout_a),
            "title": "Goal A",
        }

        alive_seq = [True, True, False]
        def fake_matches(ident: dict) -> bool:
            return alive_seq.pop(0) if alive_seq else False

        mock_supervisor_execute = MagicMock(return_value={"state": State.WAIT_QUOTA.value, "thread_id": "THREAD-A"})

        with patch.object(watcher, "discover_active_session", return_value=fake_session), \
             patch("nightwatch.supervisor.process_matches", side_effect=fake_matches), \
             patch("nightwatch.supervisor.find_repo_codex_processes", return_value=[]), \
             patch.object(Supervisor, "execute", mock_supervisor_execute), \
             patch("time.sleep", return_value=None):

            result = watcher.watch(auto_takeover=True)
            self.assertTrue(self.store.exists())
            state = self.store.load_state()
            self.assertEqual(state["thread_id"], "THREAD-A")
            mock_supervisor_execute.assert_called_once_with(start=False)
            self.assertEqual(result["state"], State.WAIT_QUOTA.value)

    def test_pid_reuse_does_not_count_as_original_session_alive(self) -> None:
        fake_identity = {"pid": 1001, "starttime": "12345", "executable": "/bin/codex"}
        reused_stat = "1001 (codex) S 1 1001 1001 0 -1 4194304 100 0 0 0 0 0 0 0 20 0 1 0 67890 0 0"

        with patch("nightwatch.supervisor.sys_platform_linux", return_value=True), \
             patch("nightwatch.supervisor.pid_alive", return_value=True), \
             patch("pathlib.Path.read_text", return_value=reused_stat), \
             patch("os.readlink", return_value="/bin/codex"):

            self.assertFalse(process_matches(fake_identity))

    def test_process_exit_uses_frozen_proven_thread_not_newest_sqlite_thread(self) -> None:
        rollout_a = self._create_rollout("THREAD-A", primary_pct=10.0)
        watcher = PassiveWatcher(self.store)
        fake_session = {
            "status": "OK",
            "active": True,
            "pid": 1001,
            "pid_identity": {"pid": 1001, "starttime": "1001", "executable": "/bin/codex"},
            "thread_id": "THREAD-A",
            "rollout_path": str(rollout_a),
            "title": "Goal A",
        }

        mock_supervisor_execute = MagicMock(return_value={"state": State.DONE.value, "thread_id": "THREAD-A"})
        fake_sqlite_after = [{"id": "THREAD-B", "title": "Newest thread"}]

        with patch.object(watcher, "discover_active_session", return_value=fake_session), \
             patch("nightwatch.supervisor.process_matches", return_value=False), \
             patch("nightwatch.supervisor.find_repo_codex_processes", return_value=[]), \
             patch("nightwatch.supervisor.find_active_threads_for_repo", return_value=fake_sqlite_after), \
             patch.object(Supervisor, "execute", mock_supervisor_execute):

            watcher.watch(auto_takeover=True)
            self.assertTrue(self.store.exists())
            self.assertEqual(self.store.load_state()["thread_id"], "THREAD-A")


class FakeE2EWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="nightwatch-fake-e2e-")
        self.repo = Path(self.tmp_dir) / "test-repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        os.system(f"git -C {self.repo} init -q")
        os.system(f"git -C {self.repo} config user.email 'test@example.com'")
        os.system(f"git -C {self.repo} config user.name 'Test'")
        (self.repo / "test.txt").write_text("hello\n")
        os.system(f"git -C {self.repo} add test.txt && git -C {self.repo} commit -qm 'initial'")
        self.store = NightwatchStore(self.repo, state_home=Path(self.tmp_dir) / "state")

    def tearDown(self) -> None:
        os.system(f"rm -rf {self.tmp_dir}")

    def _make_rollout(self, thread_id: str, pct: float = 10.0) -> Path:
        p = Path(self.tmp_dir) / f"rollout-{thread_id}.jsonl"
        lines = [
            json.dumps({"type": "session_meta", "payload": {"id": thread_id, "session_id": thread_id, "cwd": str(self.repo)}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": pct, "resets_at": 1787866896}}}}),
        ]
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_fake_e2e_multi_session_ambiguity_and_explicit_selection(self) -> None:
        """E2E A - MULTI SESSION: Default watch is ambiguous, explicit --thread selects strictly without cross-binding."""
        rollout_a = self._make_rollout("THREAD-A")
        rollout_b = self._make_rollout("THREAD-B")

        fake_procs = [
            {"pid": 2001, "executable": "/bin/codex", "cwd": str(self.repo)},
            {"pid": 2002, "executable": "/bin/codex", "cwd": str(self.repo)},
        ]

        def fake_readlink(p: str) -> str:
            if "2001" in str(p):
                return str(rollout_a)
            if "2002" in str(p):
                return str(rollout_b)
            return ""

        def fake_listdir(p: str) -> list[str]:
            if "/proc/" in str(p) and "/fd" in str(p):
                return ["3"]
            return []

        with patch("nightwatch.supervisor.sys_platform_linux", return_value=True), \
             patch("nightwatch.supervisor.find_repo_codex_processes", return_value=fake_procs), \
             patch("nightwatch.supervisor.process_identity", side_effect=lambda pid: {"pid": pid, "starttime": str(pid), "executable": "/bin/codex"}), \
             patch("nightwatch.supervisor.process_matches", return_value=True), \
             patch("nightwatch.supervisor.find_active_threads_for_repo", return_value=[]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("os.listdir", side_effect=fake_listdir), \
             patch("os.readlink", side_effect=fake_readlink):

            # 1. Default watch without --thread -> AMBIGUOUS
            watcher_default = PassiveWatcher(self.store, explicit_thread=None)
            snap_default = watcher_default.inspect_live_snapshot()
            self.assertEqual(snap_default["status"], "AMBIGUOUS_ACTIVE_SESSIONS")
            self.assertFalse(snap_default["active"])

            # 2. Watch with --thread THREAD-A -> Strictly THREAD-A (PID 2001)
            watcher_a = PassiveWatcher(self.store, explicit_thread="THREAD-A")
            snap_a = watcher_a.inspect_live_snapshot()
            self.assertEqual(snap_a["status"], "OK")
            self.assertEqual(snap_a["thread_id"], "THREAD-A")
            self.assertEqual(snap_a["pid"], 2001)

            # 3. Watch with --thread THREAD-B -> Strictly THREAD-B (PID 2002)
            watcher_b = PassiveWatcher(self.store, explicit_thread="THREAD-B")
            snap_b = watcher_b.inspect_live_snapshot()
            self.assertEqual(snap_b["status"], "OK")
            self.assertEqual(snap_b["thread_id"], "THREAD-B")
            self.assertEqual(snap_b["pid"], 2002)

    def test_fake_e2e_quota_takeover_timeline(self) -> None:
        """E2E B - QUOTA TAKEOVER: T0 (98%) -> T1 (100% alive, TAKEOVER_PENDING) -> T2 (alive, pending) -> T3 (exited -> takeover)."""
        rollout_a = self._make_rollout("THREAD-A", pct=98.0)
        watcher = PassiveWatcher(self.store)

        fake_session = {
            "status": "OK",
            "active": True,
            "pid": 3001,
            "pid_identity": {"pid": 3001, "starttime": "3001", "executable": "/bin/codex"},
            "thread_id": "THREAD-A",
            "rollout_path": str(rollout_a),
            "title": "Goal E2E",
        }

        timeline = [
            {"pct": 98.0, "alive": True},
            {"pct": 100.0, "alive": True},
            {"pct": 100.0, "alive": True},
            {"pct": 100.0, "alive": False},
        ]

        def step_poll(duration: float) -> None:
            if timeline:
                cur = timeline.pop(0)
                self._make_rollout("THREAD-A", pct=cur["pct"])

        def is_alive(ident: dict) -> bool:
            if timeline:
                return timeline[0]["alive"]
            return False

        mock_supervisor_execute = MagicMock(return_value={"state": State.WAIT_QUOTA.value, "thread_id": "THREAD-A"})

        with patch.object(watcher, "discover_active_session", return_value=fake_session), \
             patch("nightwatch.supervisor.process_matches", side_effect=is_alive), \
             patch("nightwatch.supervisor.find_repo_codex_processes", return_value=[]), \
             patch.object(Supervisor, "execute", mock_supervisor_execute), \
             patch("time.sleep", side_effect=step_poll):

            result = watcher.watch(auto_takeover=True)
            self.assertTrue(self.store.exists())
            self.assertEqual(self.store.load_state()["thread_id"], "THREAD-A")
            mock_supervisor_execute.assert_called_once_with(start=False)
            self.assertEqual(result["state"], State.WAIT_QUOTA.value)
