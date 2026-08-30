from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-tui-tests-")
os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT))

from nightwatch import cli  # noqa: E402
from nightwatch.models import State  # noqa: E402
from nightwatch.storage import NightwatchStore  # noqa: E402
from nightwatch.supervisor import build_report  # noqa: E402
from nightwatch.tui import (  # noqa: E402
    RunCatalog,
    RunSpec,
    create_worktree,
    explain_run,
    palette_prefix,
    queue_steer,
    recap_run,
    render_dashboard,
    route_input,
    slash_commands,
    status_run,
    terminal_safe,
)


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


class TuiTests(unittest.TestCase):
    def test_slash_palette_is_discoverable_and_described(self):
        commands = slash_commands()
        names = {item.name for item in commands}
        self.assertTrue({"/run", "/multi", "/status", "/steer", "/explain", "/recap", "/report", "/quit"} <= names)
        self.assertTrue(all(item.summary.strip() for item in commands))
        self.assertEqual([item.name for item in slash_commands("sta")], ["/status"])
        self.assertEqual(palette_prefix("/"), "")
        self.assertEqual(palette_prefix("/status now"), "status")

    def test_natural_language_routes_by_context_and_never_executes_directly(self):
        new_goal = route_input("实现支付重试并通过测试", has_active_run=False)
        self.assertEqual((new_goal.kind, new_goal.argument, new_goal.requires_confirmation), ("run", "实现支付重试并通过测试", True))
        steer = route_input("不要修改数据库 schema", has_active_run=True)
        self.assertEqual((steer.kind, steer.argument, steer.requires_confirmation), ("steer", "不要修改数据库 schema", True))
        self.assertEqual(route_input("/", has_active_run=False).kind, "palette")
        self.assertEqual(route_input("/status", has_active_run=True).kind, "status")
        self.assertEqual(route_input("/run 新目标", has_active_run=True).kind, "run")

    def test_run_spec_rejects_oversized_or_control_character_input(self):
        with self.assertRaises(ValueError):
            RunSpec(repo=Path("/repo"), goal="bad\x00goal")
        with self.assertRaises(ValueError):
            RunSpec(repo=Path("/repo"), goal="x" * 4001)
        with self.assertRaises(ValueError):
            RunSpec(repo=Path("/repo"), goal="goal", verify_commands=("x" * 4001,))
        self.assertNotIn("\x1b", terminal_safe("safe\x1b[31mspoof"))
        self.assertNotIn("\u202e", terminal_safe("safe\u202espoof"))

    def test_catalog_discovers_multiple_trusted_runs_and_dashboard_shows_threads(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state_home = base / "state"
            repos = [base / "repo-a", base / "repo-b"]
            for index, root in enumerate(repos, start=1):
                root.mkdir()
                git_repo(root)
                store = NightwatchStore(root, state_home=state_home)
                store.initialize(
                    f"run-{index}",
                    f"goal {index}",
                    str(root),
                    thread_id=f"THREAD-{index}",
                    model="gpt-test",
                    reasoning_effort="high",
                    verify_commands=["git status --short"],
                )
                if index == 2:
                    store.transition(State.STOPPED, "test_stop", "stopped for dashboard test")

            catalog = RunCatalog(state_home)
            runs = catalog.discover()
            self.assertEqual([item.thread_id for item in runs], ["THREAD-1", "THREAD-2"])
            rendered = render_dashboard(runs, selected=0, width=110)
            self.assertIn("MULTI-THREAD CONTROL", rendered)
            self.assertIn("THREAD-1", rendered)
            self.assertIn("THREAD-2", rendered)
            self.assertIn("gpt-test · high", rendered)
            self.assertIn("Source: trusted state + sequence-validated events", rendered)
            status = status_run(runs[0])
            self.assertIn("STATUS · NEW", status)
            self.assertIn("Thread      THREAD-1", status)
            self.assertIn("Provenance  trusted state", status)

    def test_explain_and_recap_are_grounded_in_trusted_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize(
                "recap-run",
                "implement fixture",
                str(root),
                thread_id="THREAD-RECAP",
                verify_commands=["git status --short"],
            )
            store.transition(State.PREFLIGHT, "test_preflight", "preflight for quota explanation")
            store.transition(State.RUNNING, "test_running", "running for quota explanation")
            store.transition(
                State.WAIT_QUOTA,
                "quota_exhausted",
                "validated 5h quota window is exhausted",
                {"next_resume_at": "2030-01-01T00:00:00Z", "quota_source": "live_app_server"},
            )
            run = RunCatalog(Path(temporary) / "state").discover()[0]
            explanation = explain_run(run)
            self.assertIn("WAIT_QUOTA", explanation)
            self.assertIn("validated 5h quota window is exhausted", explanation)
            self.assertIn("live_app_server", explanation)
            self.assertIn("2030-01-01T00:00:00Z", explanation)
            recap = recap_run(run)
            self.assertIn("THREAD-RECAP", recap)
            self.assertIn("0/1 verified", recap)
            self.assertIn("git status --short", recap)
            self.assertIn("trusted verification", recap)
            report = build_report(store, store.load_state())
            self.assertIn("- GENERATION: 1", report)
            self.assertIn("## TRUSTED TIMELINE", report)
            self.assertIn("quota_exhausted", report)
            self.assertIn("Model-authored narrative", report)

    def test_queue_steer_uses_exact_thread_argv_without_shell_and_audits_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("steer-run", "goal", str(root), thread_id="THREAD-STEER")
            store.transition(State.PREFLIGHT, "test_preflight", "preflight")
            store.transition(State.RUNNING, "test_running", "active for steer test")
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch("nightwatch.operations.subprocess.run", return_value=completed) as run:
                result = queue_steer(store, "不要修改数据库 schema")
            self.assertTrue(result.ok)
            args = run.call_args.args[0]
            self.assertEqual(args[:4], ["codex", "queue", "--thread", "THREAD-STEER"])
            self.assertEqual(args[args.index("--message") + 1], "不要修改数据库 schema")
            self.assertNotIn("shell", run.call_args.kwargs)
            events = store.events_path.read_text()
            self.assertIn("user_instruction_queued", events)
            self.assertNotIn("不要修改数据库", events)
            with self.assertRaises(ValueError):
                queue_steer(store, "bad\x00instruction")

    def test_terminal_run_rejects_steer(self):
        for terminal_state in (State.DONE, State.FAILED, State.BLOCKED, State.STOPPED, State.AWAITING_ACCEPTANCE):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repo"
                root.mkdir()
                git_repo(root)
                store = NightwatchStore(root, state_home=Path(temporary) / "state")
                store.initialize("term-run", "goal", str(root), thread_id="THREAD-TERM")
                if terminal_state in (State.STOPPED, State.FAILED, State.BLOCKED):
                    store.transition(terminal_state, "test_transition", "testing terminal steer rejection")
                elif terminal_state == State.DONE:
                    store.transition(State.PREFLIGHT, "test", "test")
                    store.transition(State.RUNNING, "test", "test")
                    store.transition(State.VERIFYING, "test", "test")
                    store.transition(State.DONE, "test", "test")
                elif terminal_state == State.AWAITING_ACCEPTANCE:
                    store.transition(State.PREFLIGHT, "test", "test")
                    store.transition(State.RUNNING, "test", "test")
                    store.transition(State.VERIFYING, "test", "test")
                    store.transition(State.AWAITING_ACCEPTANCE, "test", "test")
                catalog = RunCatalog(Path(temporary) / "state")
                run = catalog.discover()[0]
                self.assertTrue(run.terminal)
                self.assertFalse(run.active)
                result = queue_steer(store, "test steering terminal run")
                self.assertFalse(result.ok)
                self.assertIn("Instruction was NOT queued because this Nightwatch run is terminal", result.message)
                self.assertIn("Use /resume or start a new supervised run before steering", result.message)

    def test_done_run_does_not_call_codex_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("done-run", "goal", str(root), thread_id="THREAD-DONE")
            store.transition(State.PREFLIGHT, "test", "test")
            store.transition(State.RUNNING, "test", "test")
            store.transition(State.VERIFYING, "test", "test")
            store.transition(State.DONE, "test_done", "goal completed")
            with patch("nightwatch.operations.subprocess.run") as mock_run:
                result = queue_steer(store, "new instructions after done")
                mock_run.assert_not_called()
            self.assertFalse(result.ok)

    def test_active_running_run_can_steer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("active-run", "goal", str(root), thread_id="THREAD-ACT")
            store.transition(State.PREFLIGHT, "test_preflight", "preflight")
            store.transition(State.RUNNING, "test_running", "active worker")
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch("nightwatch.operations.subprocess.run", return_value=completed) as mock_run:
                result = queue_steer(store, "keep going with task B")
                mock_run.assert_called_once()
            self.assertTrue(result.ok)
            self.assertIn("Instruction queued to exact thread THREAD-ACT", result.message)

    def test_direct_queue_steer_defense_in_depth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("defense-run", "goal", str(root), thread_id="THREAD-DEF")
            store.transition(State.BLOCKED, "test_blocked", "blocked by safety rule")
            result = queue_steer(store, "bypass instruction")
            self.assertFalse(result.ok)
            self.assertIn("terminal", result.message)

    def test_multi_summary_does_not_load_full_event_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("summary-run", "goal", str(root), thread_id="THREAD-SUM")
            with patch.object(NightwatchStore, "load_events", wraps=store.load_events) as mock_load_events:
                catalog = RunCatalog(Path(temporary) / "state")
                runs = catalog.discover()
                self.assertEqual(len(runs), 1)
                mock_load_events.assert_not_called()

    def test_timeline_lazy_loads_and_validates_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("lazy-run", "goal", str(root), thread_id="THREAD-LAZY")
            catalog = RunCatalog(Path(temporary) / "state")
            run = catalog.discover()[0]
            self.assertIsNone(run._events)
            events = run.events
            self.assertIsInstance(events, list)
            self.assertGreater(len(events), 0)
            self.assertEqual(events[0]["event"], "run_created")

    def test_tui_does_not_depend_on_private_cli_helpers(self):
        tui_source = (PRODUCT / "nightwatch" / "tui.py").read_text(encoding="utf-8")
        self.assertNotIn("from . import cli", tui_source)
        self.assertNotIn("import cli", tui_source)
        self.assertNotIn("cli._", tui_source)
        self.assertNotIn("cli.main", tui_source)

    def test_catalog_integrity_error_visible_in_dashboard(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            state_home = base / "state"
            state_home.mkdir(parents=True, exist_ok=True)
            corrupt_dir = state_home / "corrupted-run-dir"
            corrupt_dir.mkdir()
            (corrupt_dir / "metadata.json").write_text("invalid json syntax", encoding="utf-8")
            catalog = RunCatalog(state_home)
            runs = catalog.discover()
            self.assertEqual(len(runs), 0)
            self.assertEqual(len(catalog.errors), 1)
            self.assertIn("corrupted-run-dir", catalog.errors[0])
            rendered = render_dashboard(runs, errors=catalog.errors)
            self.assertIn("⚠ TRUSTED STATE ERRORS: 1", rendered)
            self.assertIn("corrupted-run-dir:", rendered)
            self.assertIn("Use explicit CLI/recovery inspection before touching that run.", rendered)
            self.assertIn("1 trusted run failed integrity validation", rendered)

    def test_worktree_creation_is_argv_only_and_uses_isolated_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            target = create_worktree(root, "payments")
            self.assertEqual(target, root.parent / ".worktrees" / root.name / "payments")
            self.assertEqual(subprocess.run(["git", "branch", "--show-current"], cwd=target, check=True, text=True, stdout=subprocess.PIPE).stdout.strip(), "nightwatch/payments")

    def test_worktree_creation_rejects_symlinked_layout_and_malicious_label(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            (root.parent / ".worktrees").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                create_worktree(root, "safe-label")
            with self.assertRaises(ValueError):
                create_worktree(root, "../escape")

    def test_multi_run_service_units_are_repo_specific(self):
        first = cli._service_name(Path("/repo-a"))
        second = cli._service_name(Path("/repo-b"))
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("nightwatch-") and first.endswith(".service"))

    def test_multi_run_install_writes_distinct_repo_bound_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            repos = [base / "repo-a", base / "repo-b"]
            for root in repos:
                root.mkdir()
                git_repo(root)
            with patch("nightwatch.cli.Path.home", return_value=home):
                _launcher_a, service_a = cli._install_user_files(repos[0])
                _launcher_b, service_b = cli._install_user_files(repos[1])
            self.assertIsNotNone(service_a)
            self.assertIsNotNone(service_b)
            self.assertNotEqual(service_a, service_b)
            self.assertTrue(service_a.exists() and service_b.exists())
            self.assertIn(f"WorkingDirectory={repos[0]}", service_a.read_text())
            self.assertIn(f"WorkingDirectory={repos[1]}", service_b.read_text())

    def test_zero_argument_cli_launches_tui_only_on_a_tty(self):
        with patch("nightwatch.cli._launch_tui", return_value=0) as launch, patch("nightwatch.cli.sys.stdin.isatty", return_value=True), patch("nightwatch.cli.sys.stdout.isatty", return_value=True):
            self.assertEqual(cli.main([]), 0)
        launch.assert_called_once()

        with patch("nightwatch.cli._launch_tui") as launch, patch("nightwatch.cli.sys.stdin.isatty", return_value=False), patch("nightwatch.cli.sys.stdout.isatty", return_value=False), patch("nightwatch.cli._parser") as parser:
            parser.return_value.print_help.return_value = None
            self.assertEqual(cli.main([]), 2)
        launch.assert_not_called()
        parser.return_value.print_help.assert_called_once()


if __name__ == "__main__":
    unittest.main()
