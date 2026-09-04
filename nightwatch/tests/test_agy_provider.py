from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))


from nightwatch.models import (
    ErrorKind,
    ProviderResult,
    QuotaSnapshot,
    QuotaWindow,
    State,
    empty_state,
    validate_state,
)
from nightwatch.process_identity import pid_alive
from nightwatch.providers import AgyProviderAdapter, CodexProviderAdapter, get_provider_adapter
from nightwatch.storage import NightwatchStore, StateIntegrityError
from nightwatch.supervisor import Supervisor


FAKE_AGY = Path(__file__).resolve().parents[2] / "test-artifacts" / "fake-agy" / "fake_agy.py"


def _git_init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    os.system(f"git -C {repo} init -q")
    os.system(f"git -C {repo} config user.email 'test@example.com'")
    os.system(f"git -C {repo} config user.name 'Test'")
    (repo / "README.md").write_text("initial\n")
    os.system(f"git -C {repo} add README.md && git -C {repo} commit -qm 'init'")


class AgyProviderAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AgyProviderAdapter()

    def test_provider_name_and_registry(self) -> None:
        self.assertEqual(self.adapter.name, "agy")
        self.assertIsInstance(get_provider_adapter("agy"), AgyProviderAdapter)
        self.assertIsInstance(get_provider_adapter("codex"), CodexProviderAdapter)

    def test_build_command_start(self) -> None:
        args, action = self.adapter.build_command(
            repo="/tmp/test",
            thread_id=None,
            prompt="implement feature X",
            model="gemini-3.8-flash-high",
            reasoning_effort="high",
        )
        self.assertEqual(action, "start")
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--output-format", args)
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--model", args)
        self.assertEqual(args[args.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertIn("-p", args)
        self.assertEqual(args[args.index("-p") + 1], "implement feature X")
        self.assertNotIn("--conversation", args)
        self.assertNotIn("-c", args)
        self.assertNotIn("--continue", args)

    def test_build_command_exact_resume_never_uses_heuristic_continue(self) -> None:
        args, action = self.adapter.build_command(
            repo="/tmp/test",
            thread_id="exact-uuid-12345",
            prompt="continue task",
            model=None,
            reasoning_effort=None,
        )
        self.assertEqual(action, "resume")
        self.assertIn("--conversation", args)
        self.assertEqual(args[args.index("--conversation") + 1], "exact-uuid-12345")
        self.assertNotIn("-c", args)
        self.assertNotIn("--continue", args)

    def test_validate_reasoning_effort(self) -> None:
        self.assertEqual(self.adapter.validate_reasoning_effort("low"), "low")
        self.assertEqual(self.adapter.validate_reasoning_effort("medium"), "medium")
        self.assertEqual(self.adapter.validate_reasoning_effort("high"), "high")
        with self.assertRaises(ValueError):
            self.adapter.validate_reasoning_effort("ultra")

    def test_supports_auto_pool_is_false(self) -> None:
        self.assertFalse(self.adapter.supports_auto_pool())

    def test_state_compatibility_with_agy_provider(self) -> None:
        state = empty_state(
            run_id="test-run",
            goal="Test AGY integration",
            repo="/tmp/repo",
            repo_id="repo-123",
            now="2026-09-03T12:00:00Z",
            provider="agy",
        )
        self.assertEqual(state["provider"], "agy")
        validate_state(state)

        # Rejects AUTO_POOL with AGY
        state_pool = dict(state)
        state_pool["account_mode"] = "AUTO_POOL"
        with self.assertRaises(ValueError):
            validate_state(state_pool)

    def test_state_defaults_provider_to_codex_if_missing(self) -> None:
        state = empty_state(
            run_id="test-run",
            goal="Test backwards compat",
            repo="/tmp/repo",
            repo_id="repo-123",
            now="2026-09-03T12:00:00Z",
        )
        del state["provider"]
        validate_state(state)
        self.assertEqual(state.get("provider", "codex"), "codex")

    def test_probe_quota_parses_usage_stream_json(self) -> None:
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY)}):
            snap = self.adapter.probe_quota()
            self.assertEqual(snap.source, "AGY_CLI")
            self.assertIsNone(snap.error)
            self.assertIsNotNone(snap.primary)
            self.assertEqual(snap.primary.name, "5h")
            self.assertEqual(snap.primary.used_percent, 60.0)
            self.assertIsNotNone(snap.secondary)
            self.assertEqual(snap.secondary.name, "weekly")
            self.assertEqual(snap.secondary.used_percent, 35.0)

    def test_auth_sanity_verification(self) -> None:
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY)}):
            self.assertTrue(self.adapter.auth_sanity())

    def test_exact_conversation_mismatch_fails_closed(self) -> None:
        """If AGY creates a new conversation instead of resuming the requested ID, fail closed."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test mismatch", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch"}):
                result = self.adapter.run_turn(store, 1, "resume prompt", thread_id="EXPECTED-CONV-ID")
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIn("mismatch", result.error_detail.lower())

    def test_mismatch_immediate_abort_prevents_sentinel_side_effect(self) -> None:
        """Mandatory side-effect boundary test: mismatch on init immediately aborts provider before side effect."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test mismatch sentinel", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
            if sentinel.exists():
                sentinel.unlink()

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch_sentinel"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIsNone(result.thread_id)
                self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER MISMATCH ABORT")
                self.assertTrue(len(spawned_pids) == 1, "Expected exactly 1 spawned process")
                time.sleep(0.1)
                self.assertFalse(pid_alive(spawned_pids[0]), "Provider child must be killed and reaped immediately")

    def test_step_mismatch_immediate_abort_prevents_sentinel_side_effect(self) -> None:
        """Mandatory side-effect boundary test: mismatch on later step immediately aborts provider before side effect."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test step mismatch sentinel", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
            if sentinel.exists():
                sentinel.unlink()

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "step_mismatch_sentinel"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIsNone(result.thread_id)
                self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER STEP MISMATCH ABORT")
                self.assertTrue(len(spawned_pids) == 1)
                time.sleep(0.1)
                self.assertFalse(pid_alive(spawned_pids[0]), "Provider child must be killed and reaped immediately")

    def test_stderr_conversation_not_found_fails_closed(self) -> None:
        """If stderr reports warning: conversation \"...\" not found, fail closed."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test not found", str(repo), provider="agy", thread_id="NONEXISTENT-ID")

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "not_found"}):
                result = self.adapter.run_turn(store, 1, "resume prompt", thread_id="NONEXISTENT-ID")
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIn("not found", result.error_detail.lower())

    def test_stderr_not_found_immediate_abort_prevents_sentinel_side_effect(self) -> None:
        """Mandatory side-effect boundary test: stderr not-found immediately aborts provider before side effect."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test not found sentinel", str(repo), provider="agy", thread_id="NONEXISTENT-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
            if sentinel.exists():
                sentinel.unlink()

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "not_found_sentinel"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="NONEXISTENT-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER NOT_FOUND ABORT")
                self.assertTrue(len(spawned_pids) == 1)
                time.sleep(0.1)
                self.assertFalse(pid_alive(spawned_pids[0]), "Provider child must be killed and reaped immediately")

    def test_mismatch_aborts_entire_process_group_and_prevents_sentinel(self) -> None:
        """Prove that AGY initial identity mismatch aborts the entire process group, killing descendants."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test mismatch descendant", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "DESCENDANT_SHOULD_NOT_EXIST.txt"
            descendant_pid_file = repo / "descendant.pid"

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch_descendant"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIsNone(result.thread_id)
                self.assertTrue(len(spawned_pids) == 1, "Expected exactly 1 leader process spawned")
                leader_pid = spawned_pids[0]

                time.sleep(0.6)
                self.assertFalse(sentinel.exists(), "Descendant sentinel file MUST NOT exist after mismatch abort")
                self.assertFalse(pid_alive(leader_pid), "AGY leader process must be dead")
                if descendant_pid_file.exists():
                    desc_pid = int(descendant_pid_file.read_text().strip())
                    self.assertFalse(pid_alive(desc_pid), "Descendant process must be killed by process group abort")

    def test_later_stream_mismatch_aborts_descendants(self) -> None:
        """Prove that step_update mismatch aborts the entire process group and kills descendants."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test step mismatch descendant", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "DESCENDANT_SHOULD_NOT_EXIST.txt"
            descendant_pid_file = repo / "descendant.pid"

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "step_mismatch_descendant"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertIsNone(result.thread_id)
                self.assertTrue(len(spawned_pids) == 1)
                leader_pid = spawned_pids[0]

                time.sleep(0.6)
                self.assertFalse(sentinel.exists(), "Descendant sentinel MUST NOT exist after step mismatch abort")
                self.assertFalse(pid_alive(leader_pid), "AGY leader process must be dead")
                if descendant_pid_file.exists():
                    desc_pid = int(descendant_pid_file.read_text().strip())
                    self.assertFalse(pid_alive(desc_pid), "Descendant process must be killed by process group abort")

    def test_timeout_aborts_descendants(self) -> None:
        """Prove that watchdog print timeout aborts the entire process group and kills descendants."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test timeout descendant", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "DESCENDANT_SHOULD_NOT_EXIST.txt"
            descendant_pid_file = repo / "descendant.pid"

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "timeout_descendant"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                    timeout=0.15,
                )
                self.assertTrue(len(spawned_pids) == 1)
                leader_pid = spawned_pids[0]

                time.sleep(1.1)
                self.assertFalse(sentinel.exists(), "Descendant sentinel MUST NOT exist after timeout abort")
                self.assertFalse(pid_alive(leader_pid), "AGY leader process must be dead")
                if descendant_pid_file.exists():
                    desc_pid = int(descendant_pid_file.read_text().strip())
                    self.assertFalse(pid_alive(desc_pid), "Descendant process must be killed by timeout abort")

    def test_manual_stop_aborts_descendants(self) -> None:
        """Prove that manual stop request aborts the entire AGY process group and kills descendants."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test manual stop descendant", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")
            sup = Supervisor(store)

            sentinel = repo / "DESCENDANT_SHOULD_NOT_EXIST.txt"
            descendant_pid_file = repo / "descendant.pid"
            turn_done = threading.Event()

            def run_worker() -> None:
                with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "stop_descendant"}):
                    sup._run_turn()
                    turn_done.set()

            worker = threading.Thread(target=run_worker, daemon=True)
            worker.start()

            for _ in range(50):
                if descendant_pid_file.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(descendant_pid_file.exists(), "Descendant PID file was created")
            desc_pid = int(descendant_pid_file.read_text().strip())
            self.assertTrue(pid_alive(desc_pid), "Descendant process is running")

            sup.request_stop()
            worker.join(timeout=5.0)
            self.assertTrue(turn_done.is_set(), "Turn finished after stop request")

            time.sleep(1.1)
            self.assertFalse(sentinel.exists(), "Descendant sentinel MUST NOT exist after manual stop")
            self.assertFalse(pid_alive(desc_pid), "Descendant process must be killed by manual stop")

    def test_unrelated_process_group_survives_killpg(self) -> None:
        """Prove that killpg on AGY process group does not affect unrelated process groups."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test unrelated survival", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                start_new_session=True,
            )
            unrelated_pid = unrelated.pid
            self.assertTrue(pid_alive(unrelated_pid), "Unrelated process must be alive initially")

            try:
                with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch_descendant"}):
                    result = self.adapter.run_turn(
                        store,
                        1,
                        "resume prompt",
                        thread_id="EXPECTED-CONV-ID",
                    )
                    self.assertEqual(result.error_kind, ErrorKind.STATE)

                self.assertTrue(pid_alive(unrelated_pid), "Unrelated process must SURVIVE AGY process group abort")
            finally:
                try:
                    os.kill(unrelated_pid, signal.SIGKILL)
                    unrelated.wait(timeout=1.0)
                except OSError:
                    pass

    def test_stubborn_mismatch_escalates_to_sigkill_and_kills_descendant(self) -> None:
        """Prove that conversation mismatch escalates past leader death to SIGKILL for stubborn children."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test stubborn mismatch", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "STUBBORN_DESCENDANT_SENTINEL.txt"
            descendant_pid_file = repo / "stubborn_descendant.pid"

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "stubborn_mismatch"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertTrue(len(spawned_pids) == 1)
                leader_pid = spawned_pids[0]

                time.sleep(1.2)
                self.assertFalse(sentinel.exists(), "Stubborn descendant sentinel MUST NOT exist")
                self.assertFalse(pid_alive(leader_pid), "AGY leader process must be dead")
                self.assertTrue(descendant_pid_file.exists(), "Stubborn descendant PID file must exist")
                desc_pid = int(descendant_pid_file.read_text().strip())
                self.assertFalse(pid_alive(desc_pid), "Stubborn descendant must be killed by SIGKILL escalation")

    def test_stubborn_step_mismatch_escalates_to_sigkill(self) -> None:
        """Prove that step_update mismatch escalates to SIGKILL and terminates stubborn descendant."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test stubborn step mismatch", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "STUBBORN_DESCENDANT_SENTINEL.txt"
            descendant_pid_file = repo / "stubborn_descendant.pid"

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "stubborn_step_mismatch"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                )
                self.assertEqual(result.error_kind, ErrorKind.STATE)
                self.assertTrue(len(spawned_pids) == 1)
                leader_pid = spawned_pids[0]

                time.sleep(1.2)
                self.assertFalse(sentinel.exists(), "Stubborn descendant sentinel MUST NOT exist")
                self.assertFalse(pid_alive(leader_pid), "AGY leader process must be dead")
                self.assertTrue(descendant_pid_file.exists(), "Stubborn descendant PID file must exist")
                desc_pid = int(descendant_pid_file.read_text().strip())
                self.assertFalse(pid_alive(desc_pid), "Stubborn descendant must be dead")

    def test_manual_stop_stubborn_group_escalates_to_sigkill(self) -> None:
        """Prove that Supervisor.request_stop() fully escalates through SIGKILL when leader and descendant ignore SIGINT."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test stubborn stop", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")
            sup = Supervisor(store)

            sentinel = repo / "STUBBORN_DESCENDANT_SENTINEL.txt"
            descendant_pid_file = repo / "stubborn_descendant.pid"
            turn_done = threading.Event()

            def run_worker() -> None:
                with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "stubborn_stop"}):
                    sup._run_turn()
                    turn_done.set()

            worker = threading.Thread(target=run_worker, daemon=True)
            worker.start()

            for _ in range(50):
                if descendant_pid_file.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(descendant_pid_file.exists(), "Descendant PID file was created")
            desc_pid = int(descendant_pid_file.read_text().strip())
            self.assertTrue(pid_alive(desc_pid), "Descendant process is running")

            t_stop = time.monotonic()
            sup.request_stop()
            worker.join(timeout=6.0)
            elapsed = time.monotonic() - t_stop
            self.assertTrue(turn_done.is_set(), "Turn finished after stop request")
            self.assertLess(elapsed, 5.0, f"Manual stop must return in bounded time (took {elapsed:.2f}s)")

            time.sleep(1.6)
            self.assertFalse(sentinel.exists(), "Stubborn descendant sentinel MUST NOT exist after manual stop")
            self.assertFalse(pid_alive(desc_pid), "Descendant process must be killed by manual stop")

    def test_stubborn_timeout_escalates_to_sigkill(self) -> None:
        """Prove that watchdog timeout aborts stubborn descendant with SIGKILL and returns ErrorKind.CRASH."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test stubborn timeout", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            spawned_pids: list[int] = []
            sentinel = repo / "STUBBORN_DESCENDANT_SENTINEL.txt"
            descendant_pid_file = repo / "stubborn_descendant.pid"

            with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "stubborn_timeout"}):
                result = self.adapter.run_turn(
                    store,
                    1,
                    "resume prompt",
                    thread_id="EXPECTED-CONV-ID",
                    on_spawn=lambda pid, action: spawned_pids.append(pid),
                    timeout=0.15,
                )
                self.assertEqual(result.error_kind, ErrorKind.CRASH)
                self.assertTrue(len(spawned_pids) == 1)
                leader_pid = spawned_pids[0]

                time.sleep(1.6)
                self.assertFalse(sentinel.exists(), "Stubborn descendant sentinel MUST NOT exist after timeout abort")
                self.assertFalse(pid_alive(leader_pid), "AGY leader process must be dead")
                if descendant_pid_file.exists():
                    desc_pid = int(descendant_pid_file.read_text().strip())
                    self.assertFalse(pid_alive(desc_pid), "Stubborn descendant must be killed by timeout abort")

    def test_signal_escalation_order_sigint_sigterm_sigkill(self) -> None:
        """Prove that stubborn child causes signals in exact order: SIGINT -> SIGTERM -> SIGKILL to the same PGID."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test signal order", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            signals_sent: list[tuple[int, int]] = []
            real_killpg = os.killpg

            def spy_killpg(pgid: int, sig: int) -> None:
                signals_sent.append((pgid, sig))
                real_killpg(pgid, sig)

            with patch("os.killpg", side_effect=spy_killpg):
                with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "stubborn_mismatch"}):
                    result = self.adapter.run_turn(
                        store,
                        1,
                        "resume prompt",
                        thread_id="EXPECTED-CONV-ID",
                    )
                    self.assertEqual(result.error_kind, ErrorKind.STATE)

            escalation_signals = [(pgid, sig) for pgid, sig in signals_sent if sig != 0]
            self.assertGreaterEqual(len(escalation_signals), 3)
            pgids = [p for p, _ in escalation_signals]
            sigs = [s for _, s in escalation_signals]

            self.assertEqual(len(set(pgids)), 1, "All signals must target the same AGY PGID")
            self.assertEqual(
                sigs[:3],
                [signal.SIGINT, signal.SIGTERM, signal.SIGKILL],
                f"Signals must be sent in order SIGINT -> SIGTERM -> SIGKILL, got {sigs}",
            )

    def test_graceful_group_exits_without_sigkill(self) -> None:
        """Prove that normal AGY group that exits on SIGINT does not trigger SIGTERM or SIGKILL."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test graceful stop", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            signals_sent: list[tuple[int, int]] = []
            real_killpg = os.killpg

            def spy_killpg(pgid: int, sig: int) -> None:
                signals_sent.append((pgid, sig))
                real_killpg(pgid, sig)

            with patch("os.killpg", side_effect=spy_killpg):
                with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch_sentinel"}):
                    result = self.adapter.run_turn(
                        store,
                        1,
                        "resume prompt",
                        thread_id="EXPECTED-CONV-ID",
                    )
                    self.assertEqual(result.error_kind, ErrorKind.STATE)

            escalation_sigs = [s for _, s in signals_sent if s != 0]
            self.assertIn(signal.SIGINT, escalation_sigs)
            self.assertNotIn(signal.SIGTERM, escalation_sigs, "SIGTERM must not be sent if group exits on SIGINT")
            self.assertNotIn(signal.SIGKILL, escalation_sigs, "SIGKILL must not be sent if group exits on SIGINT")

    def test_unrelated_process_group_survives_stubborn_escalation(self) -> None:
        """Prove that stubborn escalation to SIGKILL on AGY process group does not affect unrelated process groups."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _git_init(repo)
            store = NightwatchStore(repo)
            store.initialize("run-1", "test unrelated survival stubborn", str(repo), provider="agy", thread_id="EXPECTED-CONV-ID")

            unrelated = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                start_new_session=True,
            )
            unrelated_pid = unrelated.pid
            self.assertTrue(pid_alive(unrelated_pid), "Unrelated process must be alive initially")

            try:
                with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "stubborn_mismatch"}):
                    result = self.adapter.run_turn(
                        store,
                        1,
                        "resume prompt",
                        thread_id="EXPECTED-CONV-ID",
                    )
                    self.assertEqual(result.error_kind, ErrorKind.STATE)

                self.assertTrue(pid_alive(unrelated_pid), "Unrelated process must SURVIVE stubborn AGY process group SIGKILL")
            finally:
                try:
                    os.kill(unrelated_pid, signal.SIGKILL)
                    unrelated.wait(timeout=1.0)
                except OSError:
                    pass


class ProcessGroupLivenessAndEscalationTests(unittest.TestCase):
    """Deterministic unit tests for process-group liveness and abort escalation edge cases."""

    def test_group_absent_before_abort_no_dangerous_signaling(self) -> None:
        """When group is absent before abort, no dangerous killpg signals are sent."""
        from nightwatch.providers import _abort_process_group
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("nightwatch.providers._process_group_alive", return_value=False):
            with patch("os.killpg") as mock_killpg:
                ret = _abort_process_group(99999, mock_proc)
                mock_killpg.assert_not_called()
                self.assertEqual(ret, 0)

    def test_group_disappears_after_sigint(self) -> None:
        """When group disappears after SIGINT, SIGTERM and SIGKILL are not sent."""
        from nightwatch.providers import _abort_process_group
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        alive_seq = [True, False, False]
        with patch("nightwatch.providers._process_group_alive", side_effect=alive_seq):
            with patch("os.killpg") as mock_killpg:
                ret = _abort_process_group(99999, mock_proc, grace_seconds=0.2)
                self.assertEqual(ret, 0)
                mock_killpg.assert_called_once_with(99999, signal.SIGINT)

    def test_group_survives_sigint_escalates_to_sigterm(self) -> None:
        """When group survives SIGINT, SIGTERM is sent; if it disappears, SIGKILL is not sent."""
        from nightwatch.providers import _abort_process_group
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        calls: list[int] = []

        def fake_alive(pgid: int) -> bool:
            if signal.SIGTERM in calls:
                return False
            return True

        def fake_killpg(pgid: int, sig: int) -> None:
            calls.append(sig)

        with patch("nightwatch.providers._process_group_alive", side_effect=fake_alive):
            with patch("os.killpg", side_effect=fake_killpg):
                ret = _abort_process_group(99999, mock_proc, grace_seconds=0.05)
                self.assertEqual(ret, 0)
                self.assertIn(signal.SIGINT, calls)
                self.assertIn(signal.SIGTERM, calls)
                self.assertNotIn(signal.SIGKILL, calls)

    def test_group_survives_sigterm_escalates_to_sigkill(self) -> None:
        """When group survives SIGTERM, SIGKILL is sent."""
        from nightwatch.providers import _abort_process_group
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 99999
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        calls: list[int] = []

        def fake_alive(pgid: int) -> bool:
            if signal.SIGKILL in calls:
                return False
            return True

        def fake_killpg(pgid: int, sig: int) -> None:
            calls.append(sig)

        with patch("nightwatch.providers._process_group_alive", side_effect=fake_alive):
            with patch("os.killpg", side_effect=fake_killpg):
                ret = _abort_process_group(99999, mock_proc, grace_seconds=0.05)
                self.assertEqual(ret, 0)
                self.assertEqual(calls, [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])

    def test_leader_dead_but_group_alive_escalation_continues(self) -> None:
        """When leader is dead (poll returns code) but group is alive, escalation continues to SIGKILL."""
        from nightwatch.providers import _abort_process_group
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 99999
        mock_proc.poll.return_value = -2
        mock_proc.returncode = -2

        calls: list[int] = []

        def fake_alive(pgid: int) -> bool:
            if signal.SIGKILL in calls:
                return False
            return True

        def fake_killpg(pgid: int, sig: int) -> None:
            calls.append(sig)

        with patch("nightwatch.providers._process_group_alive", side_effect=fake_alive):
            with patch("os.killpg", side_effect=fake_killpg):
                ret = _abort_process_group(99999, mock_proc, grace_seconds=0.05)
                self.assertIn(signal.SIGKILL, calls)
                self.assertEqual(ret, -2)

    def test_leader_alive_but_group_absent_safely_reconciled(self) -> None:
        """When leader is alive (poll returns None) but group is absent, safely reconciles leader directly without os.killpg."""
        from nightwatch.providers import _abort_process_group
        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 99999
        poll_responses = [None, -15, -15]
        mock_proc.poll.side_effect = poll_responses
        mock_proc.returncode = -15

        with patch("nightwatch.providers._process_group_alive", return_value=False):
            with patch("os.killpg") as mock_killpg:
                ret = _abort_process_group(99999, mock_proc, grace_seconds=0.05)
                mock_killpg.assert_not_called()
                mock_proc.send_signal.assert_called()
                self.assertEqual(ret, -15)

    def test_permission_error_from_killpg_zero_treated_as_alive(self) -> None:
        """PermissionError from killpg(pgid, 0) is treated as group alive / fail safely."""
        from nightwatch.process_identity import process_group_alive
        with patch("os.killpg", side_effect=PermissionError("Operation not permitted")):
            self.assertTrue(process_group_alive(88888))

    def test_invalid_pgid_never_signals_supervisor_group(self) -> None:
        """Invalid pgid (<=1, matching supervisor group, or mismatching leader pid) never calls killpg on supervisor."""
        from nightwatch.providers import _abort_process_group
        sup_pgid = os.getpgrp()

        mock_proc = MagicMock(spec=subprocess.Popen)
        mock_proc.pid = 77777
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with patch("os.killpg") as mock_killpg:
            _abort_process_group(sup_pgid, mock_proc)
            mock_killpg.assert_not_called()

            _abort_process_group(0, mock_proc)
            _abort_process_group(1, mock_proc)
            _abort_process_group(-1, mock_proc)
            mock_killpg.assert_not_called()

            _abort_process_group(12345, mock_proc)
            mock_killpg.assert_not_called()



class AgySupervisorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="nightwatch-agy-sup-")
        self.repo = Path(self.tmp) / "repo"
        _git_init(self.repo)
        self.store = NightwatchStore(self.repo)
        self.adapter = get_provider_adapter("agy")

    def tearDown(self) -> None:
        os.system(f"rm -rf {self.tmp}")

    def test_supervisor_initializes_and_runs_agy_turn(self) -> None:
        self.store.initialize(
            "run-agy",
            "Supervise AGY goal",
            str(self.repo),
            provider="agy",
            verify_commands=["git diff --check"],
        )
        loaded = self.store.load_state()
        self.assertEqual(loaded["provider"], "agy")

        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_CONV_ID": "agy-conv-777", "FAKE_AGY_SCENARIO": "normal"}):
            supervisor = Supervisor(self.store)
            result = supervisor.execute(start=True)
            self.assertEqual(result["thread_id"], "agy-conv-777")
            self.assertEqual(result["provider"], "agy")
            self.assertEqual(result["state"], State.AWAITING_ACCEPTANCE.value)

    def test_supervisor_quota_exhaustion_enters_wait_quota(self) -> None:
        """When AGY reports quota exhaustion, supervisor enters WAIT_QUOTA."""
        self.store.initialize(
            "run-agy-quota",
            "Supervise AGY quota goal",
            str(self.repo),
            provider="agy",
        )

        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "exhausted"}):
            supervisor = Supervisor(self.store)
            supervisor._preflight()
            result = supervisor._run_turn()
            self.assertEqual(result["state"], State.WAIT_QUOTA.value)
            self.assertEqual(result["provider"], "agy")
            self.assertIsNotNone(result.get("next_resume_at"))

    def test_synthetic_quota_recovery_cycles(self) -> None:
        """Verify synthetic quota recovery transitions increment quota_cycles without tripping failure breaker."""
        self.store.initialize("run-soak", "Quota soak", str(self.repo), provider="agy", thread_id="conv-soak-1")
        # Step through preflight to running
        self.store.transition(State.PREFLIGHT, "preflight", "checking")
        self.store.transition(State.RUNNING, "run_started", "test running")
        supervisor = Supervisor(self.store)
        now_ts = int(datetime.now(timezone.utc).timestamp())

        # Test 25 recovery cycles (surpassing old 20-cycle limit)
        for cycle in range(25):
            reset_ts = now_ts + 60
            result = ProviderResult(
                exit_code=1,
                signal=None,
                thread_id="conv-soak-1",
                event_count=1,
                malformed_count=0,
                error_kind=ErrorKind.QUOTA_5H,
                error_detail="resource exhausted",
                reset_at=reset_ts,
                reset_source="agy_usage_probe",
                quota_windows=[QuotaWindow("5h", 100.0, 300, reset_ts)],
            )
            state = supervisor._handle_result(result)
            self.assertEqual(state["state"], State.WAIT_QUOTA.value)
            self.assertEqual(state["quota_cycles"], cycle + 1)
            self.assertEqual(state["recovery_failures"], 0)

            recovered_snap = QuotaSnapshot(
                "AGY_CLI",
                "now",
                primary=QuotaWindow("5h", 10.0, 300, reset_ts),
            )
            with patch.object(supervisor, "_get_quota_snapshot", return_value=recovered_snap), \
                 patch("nightwatch.supervisor._sleep_until"):
                reval = supervisor._wait_and_revalidate_quota()
                self.assertTrue(reval)
                self.assertEqual(supervisor.store.load_state()["state"], State.RECOVERING.value)
                # Re-enter running for next cycle
                self.store.transition(State.RUNNING, "recovered", "test ready")
                self.assertEqual(supervisor.store.load_state()["state"], State.RUNNING.value)

        final_state = self.store.load_state()
        self.assertEqual(final_state["quota_cycles"], 25)
        self.assertEqual(final_state["recovery_failures"], 0)
        self.assertEqual(final_state["state"], State.RUNNING.value)

    def test_supervisor_mismatch_immediate_abort_e2e(self) -> None:
        """End-to-end supervisor verification: mismatch aborts immediately, blocks run, and sentinel is absent."""
        self.store.initialize(
            "run-mismatch-e2e",
            "Supervise AGY goal",
            str(self.repo),
            provider="agy",
            thread_id="EXPECTED-CONV-ID",
            verify_commands=["git diff --check"],
        )

        sentinel = self.repo / "SHOULD_NOT_EXIST_AFTER_MISMATCH.txt"
        if sentinel.exists():
            sentinel.unlink()

        supervisor = Supervisor(self.store)
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "mismatch_sentinel"}):
            final = supervisor.execute(start=True)

        self.assertEqual(final["state"], State.BLOCKED.value)
        self.assertFalse(sentinel.exists(), "SENTINEL FILE MUST NOT EXIST AFTER SUPERVISOR MISMATCH ABORT")
        self.assertIsNone(final.get("active_process"))
        run_events = self.store.load_run_events(1)
        mismatch_events = [e for e in run_events if e.get("type") == "thread_id_mismatch"]
        self.assertTrue(len(mismatch_events) >= 1, "Expected thread_id_mismatch event recorded in run events")




    def test_build_command_default_and_custom_print_timeout(self) -> None:
        """Verify default 60m print timeout and explicit print timeout in build_command."""
        args_def, _ = self.adapter.build_command(
            repo="/tmp/test",
            thread_id=None,
            prompt="do task",
        )
        self.assertIn("--print-timeout", args_def)
        self.assertEqual(args_def[args_def.index("--print-timeout") + 1], "60m")

        args_custom, _ = self.adapter.build_command(
            repo="/tmp/test",
            thread_id=None,
            prompt="do task",
            print_timeout="15m",
        )
        self.assertIn("--print-timeout", args_custom)
        self.assertEqual(args_custom[args_custom.index("--print-timeout") + 1], "15m")

        with self.assertRaises(ValueError):
            self.adapter.build_command(
                repo="/tmp/test",
                thread_id=None,
                prompt="do task",
                print_timeout="invalid",
            )

    def test_run_turn_passes_durable_print_timeout(self) -> None:
        """Verify run_turn passes the configured durable agy_print_timeout."""
        self.store.initialize(
            "run-print-timeout",
            "Test print timeout",
            str(self.repo),
            provider="agy",
            agy_print_timeout="45m",
        )
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "normal"}):
            result = self.adapter.run_turn(self.store, 1, "test prompt")
            self.assertIsNone(result.error_kind)

        run_events = self.store.load_run_events(1)
        cmd_events = [e for e in run_events if e.get("type") == "provider_command"]
        self.assertTrue(len(cmd_events) >= 1)
        argv = cmd_events[0].get("argv", [])
        self.assertIn("--print-timeout", argv)
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "45m")

    def test_watchdog_timeout_classified_as_crash_not_quota(self) -> None:
        """Watchdog timeout aborts process and classifies strictly as ErrorKind.CRASH, never quota."""
        self.store.initialize(
            "run-watchdog-crash",
            "Test watchdog crash",
            str(self.repo),
            provider="agy",
            agy_print_timeout="10s",
        )
        with patch.dict(os.environ, {"NIGHTWATCH_AGY_BIN": str(FAKE_AGY), "FAKE_AGY_SCENARIO": "hang"}):
            result = self.adapter.run_turn(self.store, 1, "hang test", timeout=0.2)

        self.assertEqual(result.error_kind, ErrorKind.CRASH)
        self.assertFalse(result.aborted)
        self.assertIsNotNone(result.error_detail)
        self.assertIn("timed out", result.error_detail.lower())

        run_events = self.store.load_run_events(1)
        timeout_events = [e for e in run_events if e.get("type") == "agy_watchdog_timeout"]
        self.assertTrue(len(timeout_events) >= 1)

    def test_status_and_report_include_print_timeout(self) -> None:
        """Verify build_report and _render_status include PROVIDER and PRINT TIMEOUT for AGY."""
        from io import StringIO
        from unittest.mock import patch as mock_patch
        from nightwatch.supervisor import build_report
        from nightwatch.cli import _render_status

        self.store.initialize(
            "run-report-status",
            "Test status display",
            str(self.repo),
            provider="agy",
            agy_print_timeout="2h",
        )
        state = self.store.load_state()

        # Check build_report
        report = build_report(self.store, state)
        self.assertIn("- PROVIDER: agy", report)
        self.assertIn("- PRINT TIMEOUT: 2h", report)

        from nightwatch.models import plan_progress

        plan = self.store.load_plan()
        out = StringIO()
        with mock_patch("sys.stdout", out):
            _render_status({
                "state": state,
                "plan": plan,
                "progress": plan_progress(plan),
                "agent": {"status": "IDLE", "pid": None, "action": None, "supervisor_pid": None},
            })
        status_output = out.getvalue()
        self.assertIn("PROVIDER       agy", status_output)
        self.assertIn("PRINT TIMEOUT  2h", status_output)


class AgyStateValidationAndMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        _git_init(self.repo)
        self.store = NightwatchStore(self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_agy_state_valid_timeout_passes(self) -> None:
        state = empty_state(
            run_id="run-valid-timeout",
            goal="Test valid timeout",
            repo=str(self.repo),
            repo_id="repo-123",
            now="2026-09-04T12:00:00Z",
            provider="agy",
            agy_print_timeout="30m",
        )
        validate_state(state)
        self.assertEqual(state["agy_print_timeout"], "30m")

    def test_agy_state_malformed_timeout_fails_closed(self) -> None:
        state = empty_state(
            run_id="run-malformed-timeout",
            goal="Test malformed timeout",
            repo=str(self.repo),
            repo_id="repo-123",
            now="2026-09-04T12:00:00Z",
            provider="agy",
        )
        for bad in ["not-a-timeout", "60", "60x", "m60", "", "   "]:
            state["agy_print_timeout"] = bad
            with self.assertRaises(ValueError):
                validate_state(state)

    def test_agy_state_timeout_bool_fails(self) -> None:
        state = empty_state(
            run_id="run-bool-timeout",
            goal="Test bool timeout",
            repo=str(self.repo),
            repo_id="repo-123",
            now="2026-09-04T12:00:00Z",
            provider="agy",
        )
        state["agy_print_timeout"] = True
        with self.assertRaises(ValueError):
            validate_state(state)
        state["agy_print_timeout"] = False
        with self.assertRaises(ValueError):
            validate_state(state)

    def test_agy_state_timeout_out_of_range_fails(self) -> None:
        state = empty_state(
            run_id="run-range-timeout",
            goal="Test out-of-range timeout",
            repo=str(self.repo),
            repo_id="repo-123",
            now="2026-09-04T12:00:00Z",
            provider="agy",
        )
        state["agy_print_timeout"] = "4s"  # < 5s
        with self.assertRaises(ValueError):
            validate_state(state)
        state["agy_print_timeout"] = "25h"  # > 24h
        with self.assertRaises(ValueError):
            validate_state(state)

    def test_agy_state_missing_or_none_timeout_fails(self) -> None:
        state = empty_state(
            run_id="run-missing-timeout",
            goal="Test missing timeout",
            repo=str(self.repo),
            repo_id="repo-123",
            now="2026-09-04T12:00:00Z",
            provider="agy",
        )
        state["agy_print_timeout"] = None
        with self.assertRaises(ValueError):
            validate_state(state)
        del state["agy_print_timeout"]
        with self.assertRaises(ValueError):
            validate_state(state)

    def test_codex_state_with_timeout_fails_validate_state(self) -> None:
        state = empty_state(
            run_id="run-codex-timeout",
            goal="Test codex with timeout",
            repo=str(self.repo),
            repo_id="repo-123",
            now="2026-09-04T12:00:00Z",
            provider="codex",
        )
        state["agy_print_timeout"] = "30m"
        with self.assertRaises(ValueError):
            validate_state(state)

    def test_legacy_agy_state_missing_timeout_migrates_to_60m(self) -> None:
        self.store.initialize(
            "run-legacy-agy",
            "Test legacy migration",
            str(self.repo),
            provider="agy",
        )
        # Manually remove agy_print_timeout from disk to simulate pre-0.5.2 AGY state
        state_data = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertIn("agy_print_timeout", state_data)
        del state_data["agy_print_timeout"]
        self.store.state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")

        loaded = self.store.load_state()
        self.assertEqual(loaded["agy_print_timeout"], "60m")

    def test_legacy_migration_persists_timeout(self) -> None:
        self.store.initialize(
            "run-legacy-persist",
            "Test legacy migration persistence",
            str(self.repo),
            provider="agy",
        )
        state_data = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        del state_data["agy_print_timeout"]
        self.store.state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")

        # Load should trigger migration and persist to disk
        self.store.load_state()

        persisted = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted.get("agy_print_timeout"), "60m")

    def test_present_malformed_timeout_is_not_migrated(self) -> None:
        self.store.initialize(
            "run-malformed-persist",
            "Test malformed not migrated",
            str(self.repo),
            provider="agy",
        )
        state_data = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        state_data["agy_print_timeout"] = "invalid-timeout"
        self.store.state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")

        with self.assertRaises(StateIntegrityError):
            self.store.load_state()

        persisted = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted.get("agy_print_timeout"), "invalid-timeout")

    def test_codex_legacy_state_without_timeout_still_loads(self) -> None:
        self.store.initialize(
            "run-codex-legacy",
            "Test codex legacy",
            str(self.repo),
            provider="codex",
        )
        state_data = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        if "agy_print_timeout" in state_data:
            del state_data["agy_print_timeout"]
        self.store.state_path.write_text(json.dumps(state_data, indent=2), encoding="utf-8")

        loaded = self.store.load_state()
        self.assertIsNone(loaded.get("agy_print_timeout"))


if __name__ == "__main__":
    unittest.main()
