import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from nightwatch.cli import main
from nightwatch.models import State
from nightwatch.operations import queue_steer
from nightwatch.providers import get_provider_adapter
from nightwatch.storage import NightwatchStore
from nightwatch.tui import RunCatalog, TuiController


def git_repo(root: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


class TestSteerProviderAwareness(unittest.TestCase):

    def test_provider_adapter_steering_capabilities(self):
        codex_adapter = get_provider_adapter("codex")
        agy_adapter = get_provider_adapter("agy")

        self.assertTrue(codex_adapter.supports_live_steering())
        self.assertFalse(agy_adapter.supports_live_steering())

        ok, msg = agy_adapter.steer("/tmp", "some-conversation-uuid", "any instruction")
        self.assertFalse(ok)
        self.assertIn("AGY live steering is not supported by the current upstream CLI; no instruction was sent.", msg)

    def test_codex_steer_success_preserves_codex_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("codex-run", "goal", str(root), thread_id="THREAD-CODEX", provider="codex")
            store.transition(State.PREFLIGHT, "preflight", "preflight")
            store.transition(State.RUNNING, "running", "running")

            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch("nightwatch.operations.subprocess.run", return_value=completed) as mock_run:
                result = queue_steer(store, "stop editing the config")

            self.assertTrue(result.ok)
            self.assertIn("Instruction queued to exact thread THREAD-CODEX", result.message)
            mock_run.assert_called_once()
            args = mock_run.call_args.args[0]
            self.assertEqual(args[:4], ["codex", "queue", "--thread", "THREAD-CODEX"])
            self.assertEqual(args[args.index("--message") + 1], "stop editing the config")

            events = store.load_events()
            steer_events = [e for e in events if e.get("event") == "user_instruction_queued"]
            self.assertEqual(len(steer_events), 1)

    def test_agy_steer_explicit_unsupported_and_never_calls_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("agy-run", "goal", str(root), thread_id="80519578-b13f-421e-967a-7a43636c6245", provider="agy")
            store.transition(State.PREFLIGHT, "preflight", "preflight")
            store.transition(State.RUNNING, "running", "running")

            with patch("nightwatch.operations.subprocess.run") as mock_run, \
                 patch("subprocess.run") as mock_subproc_run:
                result = queue_steer(store, "update prompt on agy")
                mock_run.assert_not_called()
                mock_subproc_run.assert_not_called()

            self.assertFalse(result.ok)
            self.assertEqual(
                result.message,
                "AGY live steering is not supported by the current upstream CLI; no instruction was sent.",
            )

            # Confirm no event side effects were written
            events = store.load_events()
            steer_events = [e for e in events if e.get("event") == "user_instruction_queued"]
            self.assertEqual(len(steer_events), 0)

    def test_agy_steer_without_thread_id_also_returns_unsupported_without_codex_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("agy-run-no-thread", "goal", str(root), thread_id=None, provider="agy")
            store.transition(State.PREFLIGHT, "preflight", "preflight")
            store.transition(State.RUNNING, "running", "running")

            with patch("nightwatch.operations.subprocess.run") as mock_run:
                result = queue_steer(store, "instruction")
                mock_run.assert_not_called()

            self.assertFalse(result.ok)
            self.assertEqual(
                result.message,
                "AGY live steering is not supported by the current upstream CLI; no instruction was sent.",
            )

    def test_terminal_run_rejects_steer_before_provider_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("agy-term", "goal", str(root), thread_id="AGY-123", provider="agy")
            store.transition(State.STOPPED, "test_stop", "stopped")

            result = queue_steer(store, "try steering stopped")
            self.assertFalse(result.ok)
            self.assertIn("Instruction was NOT queued because this Nightwatch run is terminal", result.message)

    def test_tui_steer_rejection_for_agy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            state_home = Path(temporary) / "state"
            store = NightwatchStore(root, state_home=state_home)
            store.initialize("agy-run", "goal", str(root), thread_id="AGY-UUID-999", provider="agy")
            store.transition(State.PREFLIGHT, "preflight", "preflight")
            store.transition(State.RUNNING, "running", "running")

            catalog = RunCatalog(state_home)
            runs = catalog.discover()
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].provider, "agy")

            app = TuiController(repo=root, runs=runs)
            app._open_steer_confirm("test instruction")

            self.assertEqual(
                app.message,
                "AGY live steering is not supported by the current upstream CLI; no instruction was sent.",
            )
            self.assertIsNone(app.overlay)
            self.assertIsNone(app.pending)
            self.assertIsNone(app.awaiting)

    def test_cli_steer_for_agy_and_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            state_home = Path(temporary) / "state"

            # 1. Test AGY CLI steer
            with patch.dict(os.environ, {"NIGHTWATCH_STATE_HOME": str(state_home)}):
                store_agy = NightwatchStore(root, state_home=state_home)
                store_agy.initialize("run-agy", "goal", str(root), thread_id="AGY-CLI-1", provider="agy")
                store_agy.transition(State.PREFLIGHT, "preflight", "preflight")
                store_agy.transition(State.RUNNING, "running", "running")

                err_buf = io.StringIO()
                with patch("sys.stderr", err_buf):
                    rc = main(["steer", "steer command", "--repo", str(root)])
                self.assertEqual(rc, 1)
                self.assertIn("AGY live steering is not supported by the current upstream CLI", err_buf.getvalue())

            # 2. Test Codex CLI steer
            with tempfile.TemporaryDirectory() as temporary2:
                root2 = Path(temporary2) / "repo"
                root2.mkdir()
                git_repo(root2)
                state_home2 = Path(temporary2) / "state"

                with patch.dict(os.environ, {"NIGHTWATCH_STATE_HOME": str(state_home2)}):
                    store_codex = NightwatchStore(root2, state_home=state_home2)
                    store_codex.initialize("run-codex", "goal", str(root2), thread_id="CODEX-CLI-1", provider="codex")
                    store_codex.transition(State.PREFLIGHT, "preflight", "preflight")
                    store_codex.transition(State.RUNNING, "running", "running")

                    orig_run = subprocess.run

                    def mock_run(*args, **kwargs):
                        cmd = args[0] if args else kwargs.get("args", [])
                        if cmd and cmd[0] == "codex":
                            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                        return orig_run(*args, **kwargs)

                    out_buf = io.StringIO()
                    with patch("subprocess.run", side_effect=mock_run), \
                         patch("sys.stdout", out_buf):
                        rc = main(["steer", "steer command", "--repo", str(root2)])
                    self.assertEqual(rc, 0)
                    self.assertIn("Instruction queued to exact thread CODEX-CLI-1", out_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
