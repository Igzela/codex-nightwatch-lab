from __future__ import annotations

import json
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
from nightwatch.operations import ActionResult, adopt_run, resume_service, service_name  # noqa: E402
from nightwatch.tui import (  # noqa: E402
    MAX_GOAL_CHARS,
    RunCatalog,
    RunSpec,
    TuiController,
    TuiHooks,
    adopt_goal_text,
    agent_work_report,
    create_worktree,
    explain_run,
    is_slash_composer,
    palette_prefix,
    queue_steer,
    recap_run,
    render_dashboard,
    route_input,
    slash_commands,
    status_run,
    terminal_safe,
)
from nightwatch.views import render_dual_panel, render_modal  # noqa: E402


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


class TuiTests(unittest.TestCase):
    def test_pure_views_adapt_panels_and_modals_to_terminal_space(self):
        wide = render_dual_panel(["left"], ["right"], width=100)
        self.assertEqual(wide.splitlines()[0].count("╭"), 2)
        narrow = render_dual_panel(["left"], ["right"], width=80)
        self.assertGreaterEqual(narrow.splitlines()[0].count("╭"), 1)
        self.assertIn("SELECTED RUN", narrow)
        modal = render_modal(
            "ADOPT",
            "Choose an exact thread.",
            [("T1", "first", "LIVE + PROVEN"), ("T2", "second", "RECENT HISTORY")],
            selected=1,
            width=48,
            height=8,
        )
        self.assertIn("▶ T2", modal)
        self.assertIn("RECENT HISTORY", modal)

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
        self.assertEqual(route_input("/exit", has_active_run=False).kind, "quit")
        self.assertTrue(is_slash_composer("/"))
        self.assertTrue(is_slash_composer("/status"))
        self.assertTrue(is_slash_composer("/run a goal"))
        self.assertFalse(is_slash_composer("/home/igzela/Projects/app"))
        self.assertFalse(is_slash_composer("keep going"))

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

    def test_dashboard_and_status_show_untrusted_agent_work_separately_from_verified_percent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize(
                "live-run",
                "finish steward",
                str(root),
                thread_id="THREAD-LIVE",
                model="gpt-test",
                reasoning_effort="high",
            )
            mailbox = store.mailbox_directory
            mailbox.mkdir(parents=True, exist_ok=True)
            (mailbox / "progress.json").write_text(
                json.dumps({
                    "implemented": ["Merged production lifecycle on accepted main."],
                    "working": ["Draft PR 660 worker-recovery repair is in independent review."],
                    "blocked": [],
                }),
                encoding="utf-8",
            )
            report = agent_work_report(store)
            self.assertTrue(report["ok"])
            self.assertEqual(report["percent"], 50.0)
            run = RunCatalog(Path(temporary) / "state").discover()[0]
            rendered = render_dashboard([run], selected=0, width=120)
            self.assertIn("trusted", rendered)
            self.assertIn("0.0%", rendered)
            self.assertIn("agent 50.0%", rendered)
            self.assertIn("UNTRUSTED", rendered)
            self.assertIn("Draft PR 660", rendered)
            self.assertIn("Merged production lifecycle", rendered)
            status = status_run(run)
            self.assertIn("trusted verified", status)
            self.assertIn("agent-reported 50.0%", status)
            self.assertIn("doing 1", status)

    def test_canonical_milestone_mailbox_progress_is_visible_but_untrusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("canonical-progress", "finish the goal", str(root), verify_commands=["git status --short"])
            store.mailbox_directory.mkdir(parents=True, exist_ok=True)
            (store.mailbox_directory / "progress.json").write_text(
                json.dumps({
                    "milestones": [
                        {"id": "M1", "status": "implemented"},
                        {"id": "M2", "status": "working", "title": "Keep the operator informed"},
                    ],
                }),
                encoding="utf-8",
            )
            report = agent_work_report(store)
            self.assertTrue(report["ok"])
            self.assertEqual(report["percent"], 50.0)
            self.assertEqual(report["implemented"], ["M1"])
            self.assertEqual(report["working"], ["M2: Keep the operator informed"])
            rendered = render_dashboard(RunCatalog(Path(temporary) / "state").discover(), width=120)
            self.assertIn("agent 50.0%", rendered)
            self.assertIn("M2: Keep the operator informed", rendered)
            self.assertIn("UNTRUSTED", rendered)

    def test_adopted_run_and_current_branch_are_explicit_in_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            store = NightwatchStore(root, state_home=Path(temporary) / "state")
            store.initialize("adopted-run", "continue the conversation", str(root), thread_id="THREAD-ADOPT")
            run = RunCatalog(Path(temporary) / "state").discover()[0]
            rendered = render_dashboard([run], width=120)
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
            self.assertIn(f"Branch     {branch}", rendered)
            self.assertIn("Mode       ADOPTED · adopted, no writer started", rendered)
            self.assertIn("ADOPTED · exact thread bound · waiting for /resume", rendered)
            status = status_run(run)
            self.assertIn("Mode        ADOPTED · adopted, not supervised yet", status)

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


def _fake_run(*, name="run", active=True, thread_id="THREAD-1"):
    record = type("FakeRun", (), {})()
    record.active = active
    record.terminal = not active
    record.repo = Path(f"/tmp/{name}")
    record.thread_id = thread_id
    record.state = {
        "run_id": name,
        "state": "RUNNING" if active else "DONE",
        "goal": "goal",
        "generation": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "last_event": "run_created",
    }
    record.plan = {"milestones": []}
    record.store = type("Store", (), {"repo": record.repo})()
    record.events = []
    return record


def _type(ui: TuiController, text: str) -> None:
    for character in text:
        ui.handle_key(character)


class TuiControllerTests(unittest.TestCase):
    def test_empty_input_hotkeys_open_help_dashboard_and_run_prompt(self):
        ui = TuiController()
        ui.handle_key("?")
        self.assertEqual(ui.view, "help")
        self.assertIn("Empty-input hotkeys", ui.content or "")
        ui.handle_key("d")
        self.assertEqual(ui.view, "dashboard")
        self.assertIsNone(ui.content)
        ui.handle_key("r")
        self.assertEqual(ui.awaiting, "run_goal")

    def test_status_hotkey_uses_selected_run_without_creating_input(self):
        ui = TuiController(runs=[_fake_run()])
        ui.handle_key("s")
        self.assertEqual(ui.view, "status")
        self.assertEqual(ui.composer, "")

    def test_adopt_hotkey_opens_exact_thread_picker(self):
        ui = TuiController(
            repo=Path("/tmp/nightwatch-adopt-hotkey"),
            hooks=TuiHooks(
                discover_sessions=lambda _repo: [{
                    "thread_id": "THREAD-HOTKEY",
                    "title": "recent goal",
                    "live": False,
                    "proof": "history",
                }],
            ),
        )
        ui.handle_key("a")
        self.assertEqual(ui.overlay.kind, "picker")
        self.assertEqual(ui.overlay.items[0].key, "THREAD-HOTKEY")

    def test_slash_opens_full_command_menu(self):
        ui = TuiController()
        ui.handle_key("/")
        self.assertEqual(ui.overlay.kind, "slash")
        names = [item.key for item in ui.overlay.items]
        self.assertIn("/adopt", names)
        self.assertIn("/status", names)
        self.assertIn("/quit", names)
        self.assertIn("/exit", names)
        self.assertGreaterEqual(len(names), 18)
        rendered = ui.render(width=100, height=24)
        self.assertIn("/adopt", rendered)
        self.assertIn("/status", rendered)

    def test_unix_path_does_not_open_slash_menu(self):
        ui = TuiController()
        _type(ui, "/home/igzela/Projects/app")
        self.assertIsNone(ui.overlay)
        self.assertEqual(ui.composer, "/home/igzela/Projects/app")

    def test_slash_turns_into_path_and_closes_menu(self):
        ui = TuiController()
        ui.handle_key("/")
        self.assertEqual(ui.overlay.kind, "slash")
        _type(ui, "home/igzela")
        self.assertIsNone(ui.overlay)
        self.assertTrue(ui.composer.startswith("/home/"))

    def test_empty_composer_arrows_select_runs(self):
        ui = TuiController(runs=[_fake_run(name="a"), _fake_run(name="b")])
        ui.handle_key("down")
        self.assertEqual(ui.selected, 1)
        ui.handle_key("up")
        self.assertEqual(ui.selected, 0)

    def test_slash_arrows_do_not_change_run_selection(self):
        ui = TuiController(runs=[_fake_run(name="a"), _fake_run(name="b")])
        ui.handle_key("/")
        ui.handle_key("down")
        self.assertEqual(ui.selected, 0)
        self.assertEqual(ui.overlay.selected, 1)

    def test_typed_composer_arrows_do_not_change_run_selection(self):
        ui = TuiController(runs=[_fake_run(name="a"), _fake_run(name="b")])
        _type(ui, "hello")
        ui.handle_key("down")
        self.assertEqual(ui.selected, 0)

    def test_esc_closes_layers_and_does_not_quit_on_empty_dashboard(self):
        ui = TuiController()
        ui.handle_key("/")
        _type(ui, "sta")
        ui.handle_key("esc")
        self.assertIsNone(ui.overlay)
        self.assertEqual(ui.composer, "")
        ui.handle_key("h")
        ui.handle_key("esc")
        self.assertEqual(ui.composer, "")
        ui.handle_key("esc")
        self.assertFalse(ui.quit)

    def test_help_view_esc_returns_to_dashboard(self):
        ui = TuiController()
        _type(ui, "/help")
        ui.handle_key("enter")
        self.assertEqual(ui.view, "help")
        self.assertIsNone(ui.overlay)
        ui.handle_key("esc")
        self.assertEqual(ui.view, "dashboard")

    def test_quit_and_exit_commands_leave_tui(self):
        ui = TuiController()
        _type(ui, "/quit")
        ui.handle_key("enter")
        self.assertTrue(ui.quit)
        ui = TuiController()
        _type(ui, "/exit")
        ui.handle_key("enter")
        self.assertTrue(ui.quit)

    def test_unknown_command_errors_without_running_anything(self):
        ui = TuiController()
        _type(ui, "/does-not-exist")
        ui.handle_key("enter")
        self.assertIsNone(ui.overlay)
        self.assertIn("Unknown command", ui.message)

    def test_natural_language_opens_run_confirm_and_adds_verify_before_start(self):
        started: list[RunSpec] = []
        ui = TuiController(
            repo=Path("/tmp/nightwatch-run-target"),
            hooks=TuiHooks(
                run_exists=lambda _repo: False,
                start_run=lambda spec, run_in_service=True: started.append(spec) or ActionResult(True, "started"),
            ),
        )
        _type(ui, "implement retry")
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertEqual(started, [])
        self.assertIn("cannot reach trusted DONE", ui.overlay.body)
        _type(ui, "pytest -q")
        ui.handle_key("enter")
        self.assertEqual(started, [])
        self.assertIn("pytest -q", ui.overlay.body)
        ui.handle_key("enter")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].goal, "implement retry")
        self.assertEqual(started[0].verify_commands, ("pytest -q",))
        self.assertTrue(started[0].service)

    def test_run_collision_asks_for_worktree_label(self):
        started: list[RunSpec] = []
        trees: list[str] = []
        ui = TuiController(
            repo=Path("/tmp/nightwatch-existing"),
            hooks=TuiHooks(
                run_exists=lambda _repo: True,
                create_worktree=lambda repo, label: trees.append(label) or Path("/tmp/.worktrees/repo") / label,
                start_run=lambda spec, run_in_service=True: started.append(spec) or ActionResult(True, "started"),
            ),
        )
        _type(ui, "/run payments")
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertIn("worktree", ui.overlay.body.lower())
        _type(ui, "retry")
        ui.handle_key("enter")
        self.assertIn("retry", ui.overlay.body)
        ui.handle_key("enter")
        self.assertEqual(trees, ["retry"])
        self.assertEqual(started[0].repo, Path("/tmp/.worktrees/repo/retry"))

    def test_steer_confirms_then_queues_without_starting_supervisor(self):
        sent: list[str] = []
        ui = TuiController(
            runs=[_fake_run()],
            hooks=TuiHooks(queue_steer=lambda _store, text: sent.append(text) or ActionResult(True, "queued")),
        )
        _type(ui, "keep going")
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertEqual(sent, [])
        ui.handle_key("enter")
        self.assertEqual(sent, ["keep going"])

    def test_adopt_picker_hides_subagents_and_binds_without_starting_service(self):
        adopted: list[RunSpec] = []
        sessions = [
            {"kind": "live", "thread_id": "T-LIVE", "pid": 11, "title": "live goal", "proof": "pid_rollout", "live": True, "thread_source": "user", "model": "gpt-test"},
            {"kind": "live", "thread_id": None, "pid": 12, "title": "Interactive Codex", "proof": "pid_cwd", "live": True, "thread_source": "user"},
            {"kind": "recent", "thread_id": "T-OLD", "pid": None, "title": "older", "proof": "sqlite", "live": False, "thread_source": "user"},
            {"kind": "recent", "thread_id": "T-SUB", "pid": None, "title": "child", "proof": "sqlite", "live": False, "thread_source": "subagent"},
        ]
        ui = TuiController(
            repo=Path("/tmp/nightwatch-adopt"),
            hooks=TuiHooks(
                discover_sessions=lambda _repo: sessions,
                adopt=lambda spec: adopted.append(spec) or ActionResult(True, "adopted"),
                run_exists=lambda _repo: False,
            ),
        )
        _type(ui, "/adopt")
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "picker")
        ids = [item.payload.get("thread_id") for item in ui.overlay.items]
        self.assertEqual(ids, ["T-LIVE", "T-OLD"])
        self.assertIn("PID 12", ui.overlay.body)
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertIn("LIVE + PROVEN", ui.overlay.body)
        ui.handle_key("enter")
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0].thread_id, "T-LIVE")
        self.assertEqual(adopted[0].goal, "live goal")
        self.assertFalse(adopted[0].service)

    def test_confirm_card_slash_does_not_open_menu_or_add_verify(self):
        adopted: list[RunSpec] = []
        sessions = [
            {
                "kind": "live",
                "thread_id": "T-LIVE",
                "pid": 11,
                "title": "live goal",
                "proof": "pid_rollout",
                "live": True,
                "thread_source": "user",
            },
        ]
        ui = TuiController(
            repo=Path("/tmp/nightwatch-adopt-slash"),
            hooks=TuiHooks(
                discover_sessions=lambda _repo: sessions,
                adopt=lambda spec: adopted.append(spec) or ActionResult(True, "adopted"),
                run_exists=lambda _repo: False,
            ),
        )
        _type(ui, "/adopt")
        ui.handle_key("enter")
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "confirm")
        rendered = ui.render(width=100, height=24)
        self.assertIn("Empty Enter confirms", rendered)
        ui.handle_key("/")
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertNotEqual(getattr(ui.overlay, "kind", None) == "slash", True)
        ui.handle_key("enter")
        self.assertEqual(adopted, [])
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertEqual(ui.composer, "")
        ui.handle_key("enter")
        self.assertEqual(len(adopted), 1)

    def test_adopt_clamps_oversized_session_title_instead_of_failing(self):
        adopted: list[RunSpec] = []
        huge = "GOAL: AUTONOMY-CLOSURE\n" + ("keep going " * 2000)
        self.assertGreater(len(huge), MAX_GOAL_CHARS)
        self.assertEqual(adopt_goal_text(huge, "T-HUGE")[:22], "GOAL: AUTONOMY-CLOSURE")
        self.assertLessEqual(len(adopt_goal_text("G" * 15333, "T-HUGE")), MAX_GOAL_CHARS)
        sessions = [
            {
                "kind": "live",
                "thread_id": "T-HUGE",
                "pid": 11,
                "title": huge,
                "proof": "pid_rollout",
                "live": True,
                "thread_source": "user",
            },
        ]
        ui = TuiController(
            repo=Path("/tmp/nightwatch-adopt-huge"),
            hooks=TuiHooks(
                discover_sessions=lambda _repo: sessions,
                adopt=lambda spec: adopted.append(spec) or ActionResult(True, "adopted"),
                run_exists=lambda _repo: False,
            ),
        )
        _type(ui, "/adopt")
        ui.handle_key("enter")
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertIn("GOAL: AUTONOMY-CLOSURE", ui.overlay.body)
        self.assertNotIn("keep going " * 50, ui.overlay.body)
        ui.handle_key("enter")
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0].thread_id, "T-HUGE")
        self.assertLessEqual(len(adopted[0].goal), MAX_GOAL_CHARS)
        self.assertTrue(adopted[0].goal.startswith("GOAL: AUTONOMY-CLOSURE"))

    def test_resume_uses_hook_result_when_codex_is_live(self):
        ui = TuiController(
            runs=[_fake_run()],
            hooks=TuiHooks(resume=lambda _repo: ActionResult(False, "Interactive Codex still running (PID 2241252); close it before /resume")),
        )
        _type(ui, "/resume")
        ui.handle_key("enter")
        ui.handle_key("enter")
        self.assertIn("PID 2241252", ui.message)
        self.assertIsNone(ui.overlay)

    def test_stop_and_report_require_confirmation(self):
        stopped: list[Path] = []
        ui = TuiController(
            runs=[_fake_run()],
            hooks=TuiHooks(stop=lambda repo: stopped.append(repo) or ActionResult(True, "STOPPED")),
        )
        _type(ui, "/stop")
        ui.handle_key("enter")
        self.assertEqual(ui.overlay.kind, "confirm")
        self.assertEqual(stopped, [])
        ui.handle_key("enter")
        self.assertEqual(stopped, [Path("/tmp/run")])


class AdoptAndResumeOperationTests(unittest.TestCase):
    def test_adopt_run_binds_thread_without_starting_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            spec = RunSpec(root, "Supervise adopted conversation", thread_id="THREAD-ADOPT", service=False)
            with patch("nightwatch.operations.start_user_service") as start_service, \
                 patch("nightwatch.operations.find_repo_codex_processes", return_value=[{"pid": 2241252}]):
                result = adopt_run(spec)
            start_service.assert_not_called()
            self.assertTrue(result.ok)
            self.assertIn("THREAD-ADOPT", result.message)
            self.assertIn("2241252", result.message)
            loaded = NightwatchStore(root).load_state()
            self.assertEqual(loaded["thread_id"], "THREAD-ADOPT")
            self.assertEqual(loaded["state"], State.NEW.value)

    def test_resume_service_refuses_while_repo_codex_is_alive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            with patch("nightwatch.operations.find_repo_codex_processes", return_value=[{"pid": 922916, "executable": "/bin/codex"}]), \
                 patch("nightwatch.operations.start_user_service") as start_service:
                result = resume_service(root)
            start_service.assert_not_called()
            self.assertFalse(result.ok)
            self.assertIn("922916", result.message)

    def test_resume_service_keeps_pip_launcher_and_writes_unit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            home = Path(temporary) / "home"
            launcher = home / ".local" / "bin" / "nightwatch"
            launcher.parent.mkdir(parents=True)
            pip_launcher = "#!/usr/bin/python3\nfrom nightwatch.cli import main\nsys.exit(main())\n"
            launcher.write_text(pip_launcher)
            with patch("nightwatch.operations.Path.home", return_value=home), \
                 patch("nightwatch.operations.find_repo_codex_processes", return_value=[]), \
                 patch("nightwatch.operations.start_user_service") as start_service:
                result = resume_service(root)
            self.assertTrue(result.ok, result.message)
            start_service.assert_called_once()
            self.assertEqual(launcher.read_text(), pip_launcher)
            unit = home / ".config" / "systemd" / "user" / service_name(root)
            self.assertTrue(unit.exists())
            self.assertIn(f"WorkingDirectory={root}", unit.read_text())

    def test_resume_service_still_refuses_unrelated_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            git_repo(root)
            home = Path(temporary) / "home"
            launcher = home / ".local" / "bin" / "nightwatch"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\necho not-nightwatch\n")
            with patch("nightwatch.operations.Path.home", return_value=home), \
                 patch("nightwatch.operations.find_repo_codex_processes", return_value=[]), \
                 patch("nightwatch.operations.start_user_service") as start_service:
                result = resume_service(root)
            start_service.assert_not_called()
            self.assertFalse(result.ok)
            self.assertIn("refusing to overwrite", result.message)


if __name__ == "__main__":
    unittest.main()
