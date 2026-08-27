from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys

TEST_STATE_HOME = tempfile.mkdtemp(prefix="nightwatch-trusted-tests-")
os.environ["NIGHTWATCH_STATE_HOME"] = TEST_STATE_HOME

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nightwatch import cli  # noqa: E402
from nightwatch.codex import (  # noqa: E402
    build_command,
    classify_failure,
    extract_reset,
    extract_thread_id,
    extract_quota_windows,
)
from nightwatch.models import ErrorKind, QuotaSnapshot, QuotaWindow, State, plan_progress, validate_plan  # noqa: E402
from nightwatch.quota import RolloutQuotaProvider  # noqa: E402
from nightwatch.storage import NightwatchStore, StateIntegrityError, redact  # noqa: E402
from nightwatch.supervisor import Supervisor  # noqa: E402


class UnitTests(unittest.TestCase):
    def test_service_cli_and_unit_are_repo_bound_and_fail_closed(self):
        args = cli._parser().parse_args(["run", "--service", "goal", "--repo", "/repo"])
        self.assertTrue(args.service)
        resume = cli._parser().parse_args(["resume", "--repo", "/repo", "--no-inhibit"])
        self.assertTrue(resume.no_inhibit)
        unit = cli._service_text(Path("/repo"))
        self.assertIn("WorkingDirectory=/repo", unit)
        self.assertIn('resume --repo "/repo"', unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartPreventExitStatus=10 11 12", unit)
        self.assertNotIn("--last", unit)
        self.assertNotIn("Restart=on-abnormal", unit)
        self.assertEqual(cli._systemd_quote("/repo with space/100%"), '"/repo with space/100%%"')
        self.assertIn("WorkingDirectory=/repo with space/100%%", cli._service_text(Path("/repo with space/100%")))

    def test_user_service_start_uses_user_manager(self):
        completed = type("Completed", (), {"returncode": 0})()
        with patch("nightwatch.cli.shutil.which", return_value="/bin/systemctl"), patch(
            "nightwatch.cli.subprocess.run", return_value=completed
        ) as run:
            cli._start_user_service()
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["/bin/systemctl", "--user", "daemon-reload"], ["/bin/systemctl", "--user", "enable", "--now", "nightwatch.service"]],
        )

    def test_run_service_persists_new_goal_then_returns_after_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            args = cli._parser().parse_args(["run", "--service", "goal", "--repo", str(root)])
            with patch("nightwatch.cli._validate_install_targets"), patch(
                "nightwatch.cli._install_user_files", return_value=(root / "nightwatch", root / "nightwatch.service")
            ), patch("nightwatch.cli._start_user_service") as start, patch("nightwatch.cli.Supervisor.execute") as execute:
                self.assertEqual(cli._run(args), 0)
            state = NightwatchStore(root).load_state()
            self.assertEqual(state["state"], State.NEW.value)
            self.assertIsNone(state["thread_id"])
            start.assert_called_once()
            execute.assert_not_called()

    def test_run_service_bus_failure_keeps_goal_new_and_does_not_start_codex(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            args = cli._parser().parse_args(["run", "--service", "goal", "--repo", str(root)])
            with patch("nightwatch.cli._validate_install_targets"), patch(
                "nightwatch.cli._install_user_files", return_value=(root / "nightwatch", root / "nightwatch.service")
            ), patch("nightwatch.cli._start_user_service", side_effect=RuntimeError("no user bus")), patch(
                "nightwatch.cli.Supervisor.execute"
            ) as execute:
                self.assertEqual(cli._run(args), 1)
            self.assertEqual(NightwatchStore(root).load_state()["state"], State.NEW.value)
            execute.assert_not_called()

    def test_reset_epoch_seconds_and_millis(self):
        self.assertEqual(extract_reset('{"resets_at": 1770000000}')[0], 1770000000)
        self.assertEqual(extract_reset('{"resetsAt": 1770000000000}')[0], 1770000000)

    def test_reset_relative(self):
        reset, source = extract_reset("usage limit; try again in 5 seconds")
        self.assertEqual(source, "provider_relative")
        self.assertIsNotNone(reset)

    def test_thread_id_only_from_thread_event(self):
        self.assertEqual(extract_thread_id({"type": "thread.started", "thread_id": "T-1"}), "T-1")
        self.assertIsNone(extract_thread_id({"type": "item.completed", "message": "thread_id T-2"}))

    def test_quota_window_shape(self):
        windows = extract_quota_windows({"type": "event", "rateLimits": {"primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1700000000}}})
        self.assertEqual(windows[0].name, "5h")
        self.assertTrue(windows[0].exhausted)

    def test_classify_5h_quota(self):
        kind, _detail, reset, source, windows, _blocker = classify_failure(
            [{"type": "error", "error": {"code": "usage_limit_reached", "resetsAt": 1770000000}}], "", 1, None
        )
        self.assertEqual(kind, ErrorKind.QUOTA_5H)
        self.assertEqual(reset, 1770000000)
        self.assertEqual(source, "provider_epoch")
        self.assertEqual(windows, [])

    def test_classify_weekly_quota(self):
        kind, *_ = classify_failure(
            [{"type": "error", "error": {"code": "usage_limit_reached", "rateLimitReachedType": "weekly"}}], "", 1, None
        )
        self.assertEqual(kind, ErrorKind.QUOTA_WEEKLY)

    def test_classify_temporary_429(self):
        kind, *_ = classify_failure([{"type": "error", "error": {"code": "http_429", "message": "429 Too Many Requests"}}], "", 1, None)
        self.assertEqual(kind, ErrorKind.TEMPORARY_429)

    def test_classify_capacity_network_auth_crash(self):
        cases = [
            ("capacity overloaded", ErrorKind.CAPACITY, 1, None),
            ("stream disconnected", ErrorKind.NETWORK, 1, None),
            ("authentication required", ErrorKind.AUTH, 1, None),
            ("", ErrorKind.CRASH, 1, "SIGKILL"),
        ]
        for text, expected, code, sig in cases:
            with self.subTest(expected=expected):
                kind, *_ = classify_failure([], text, code, sig)
                self.assertEqual(kind, expected)

    def test_plan_progress_is_mechanical(self):
        plan = {"schema_version": 2, "authority": "nightwatch", "policy_hash": "test", "milestones": [
            {"id": "M1", "title": "one", "weight": 2, "required": True, "status": "verified", "verification_profile": "default", "evidence": []},
            {"id": "M2", "title": "two", "weight": 3, "required": True, "status": "implemented", "verification_profile": "default", "evidence": []},
        ]}
        progress = plan_progress(plan)
        self.assertEqual(progress["implemented_count"], 2)
        self.assertEqual(progress["verified_count"], 1)
        self.assertEqual(progress["verified_percent"], 40.0)

    def test_required_milestone_needs_verification(self):
        with self.assertRaises(ValueError):
            validate_plan({"schema_version": 2, "authority": "nightwatch", "policy_hash": "test", "milestones": [{"id": "M1", "title": "x", "weight": 1, "required": True, "status": "pending", "verification_profile": "bad", "evidence": []}]})

    def test_command_uses_exact_thread_and_never_last(self):
        with patch.dict(os.environ, {"NIGHTWATCH_CODEX_BIN": "/fake/codex"}):
            args, action = build_command("/repo", "EXACT-1", "prompt")
        self.assertEqual(action, "resume")
        self.assertIn("EXACT-1", args)
        self.assertNotIn("--last", args)
        self.assertEqual(args[0:2], ["/fake/codex", "exec"])
        self.assertEqual(args[2], "--json")
        self.assertEqual(args[3], "resume")
        self.assertEqual(args[4], "EXACT-1")

    def test_redaction_removes_known_secrets(self):
        value = redact({"token": "secret", "message": "Bearer abc123 and sk-abcdefghijklmnopqrstuvwxyz"})
        self.assertEqual(value["token"], "[REDACTED]")
        self.assertNotIn("abc123", value["message"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", value["message"])

    def test_atomic_state_and_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            store = NightwatchStore(repo)
            store.initialize("run-1", "goal", str(repo), "2026-01-01T00:00:00Z")
            state = store.transition(State.PREFLIGHT, "preflight_started", "test")
            self.assertEqual(state["state"], "PREFLIGHT")
            lines = store.events_path.read_text().splitlines()
            self.assertEqual(json.loads(lines[-1])["event"], "preflight_started")

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            store = NightwatchStore(repo)
            store.initialize("run-1", "goal", str(repo))
            store.state_path.write_text("not json")
            with self.assertRaises(StateIntegrityError):
                store.load_state()

    def test_single_flight_claim_is_exactly_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            store = NightwatchStore(repo)
            store.initialize("run-1", "goal", str(repo))
            store.transition(State.PREFLIGHT, "preflight_started", "test")
            store.transition(State.RUNNING, "provider_launch_ready", "test")
            store.transition(State.WAIT_QUOTA, "quota_exhausted", "test", {"thread_id": "T-1", "next_resume_at": "2030-01-01T00:00:00Z", "generation": 2})
            supervisor = Supervisor(store, quota_provider=object())
            self.assertTrue(supervisor._claim_resume())
            self.assertFalse(supervisor._claim_resume())
            self.assertEqual(store.load_state()["resume_claim"]["generation"], 2)

    def test_unknown_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            store = NightwatchStore(repo)
            store.initialize("run-1", "goal", str(repo))
            value = json.loads(store.state_path.read_text())
            value["state"] = "GUESSING"
            store.state_path.write_text(json.dumps(value))
            with self.assertRaises(StateIntegrityError):
                store.load_state()

    def test_quota_snapshot_recovery_requires_selected_windows(self):
        snapshot = QuotaSnapshot("fake", "now", QuotaWindow("5h", 0, 300, 1), QuotaWindow("weekly", 100, 10080, 2))
        self.assertTrue(snapshot.recovered({"5h"}))
        self.assertFalse(snapshot.recovered({"weekly"}))

    def test_rollout_quota_fallback_reads_recent_structured_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions" / "2026" / "08" / "27"
            sessions.mkdir(parents=True)
            path = sessions / "rollout-test.jsonl"
            path.write_text(json.dumps({
                "timestamp": "2026-08-27T00:00:00Z",
                "type": "event_msg",
                "payload": {"rate_limits": {
                    "primary": {"used_percent": 59, "window_minutes": 300, "resets_at": 1787841340},
                    "secondary": {"used_percent": 18, "window_minutes": 10080, "resets_at": 1788273143},
                }},
            }) + "\n")
            # Use a current timestamp so the test is independent of the
            # fixture's committed date.
            value = json.loads(path.read_text())
            value["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            path.write_text(json.dumps(value) + "\n")
            quota = RolloutQuotaProvider(temporary, max_age_seconds=60).read()
            self.assertEqual(quota.source, "rollout_jsonl")
            self.assertEqual(quota.primary.used_percent, 59)
            self.assertEqual(quota.secondary.window_duration_mins, 10080)


if __name__ == "__main__":
    unittest.main()
