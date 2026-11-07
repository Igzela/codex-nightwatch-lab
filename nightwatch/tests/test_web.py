from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

PRODUCT = Path(__file__).resolve().parents[1]
if str(PRODUCT) not in sys.path:
    sys.path.insert(0, str(PRODUCT))

from nightwatch.git import repo_root  # noqa: E402
from nightwatch.models import plan_progress  # noqa: E402
from nightwatch.storage import NightwatchStore, make_run_id  # noqa: E402
from nightwatch.web import is_loopback_host, make_server, serve, spawn_supervisor  # noqa: E402


def git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "nightwatch@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Nightwatch Test"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


class WebControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_state_home = os.environ.get("NIGHTWATCH_STATE_HOME")
        self.state_home = tempfile.mkdtemp(prefix="nightwatch-web-state-")
        os.environ["NIGHTWATCH_STATE_HOME"] = self.state_home
        self.addCleanup(self._restore_state_home)
        self.server = make_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, name="nightwatch-web-test", daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._wait_until_ready()

    def _restore_state_home(self) -> None:
        if self._previous_state_home is None:
            os.environ.pop("NIGHTWATCH_STATE_HOME", None)
        else:
            os.environ["NIGHTWATCH_STATE_HOME"] = self._previous_state_home
        shutil.rmtree(self.state_home, ignore_errors=True)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 5
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                status, _payload, _headers, _raw = self.request("GET", "/api/runs")
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                last_error = exc
                time.sleep(0.05)
                continue
            if status == 200:
                return
            time.sleep(0.05)
        raise RuntimeError(f"test server did not accept connections: {last_error}")

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        raw: bytes | None = None,
    ) -> tuple[int, object, dict[str, str], bytes]:
        data: bytes | None
        headers: dict[str, str] = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        else:
            data = raw
            if raw is not None:
                headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read()
                status = response.status
                header_map = {key: value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            header_map = {key: value for key, value in exc.headers.items()}
        content_type = header_map.get("Content-Type", "")
        if "application/json" in content_type:
            parsed: object = json.loads(body.decode("utf-8"))
        else:
            parsed = body.decode("utf-8")
        return status, parsed, header_map, body

    def make_repo(self, name: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix=f"nightwatch-web-{name}-"))
        self.addCleanup(shutil.rmtree, root, True)
        git_repo(root)
        return root

    def test_empty_home_lists_no_runs(self) -> None:
        status, payload, headers, _raw = self.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"runs": [], "errors": []})
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_doctor_reports_the_five_checks(self) -> None:
        status, payload, headers, raw = self.request("GET", "/api/doctor")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertEqual([item["id"] for item in payload["checks"]], ["codex", "auth", "quota", "systemd", "agy"])
        for item in payload["checks"]:
            self.assertEqual(set(item), {"id", "level", "title", "detail"})
            self.assertIn(item["level"], {"ok", "warn", "fail"})
            self.assertTrue(item["title"].strip())
            self.assertTrue(item["detail"].strip())
        self.assertIsInstance(payload["summary"], str)
        self.assertEqual(payload["summary"].count("."), 1)
        self.assertIn("codex_binary", payload)
        self.assertIn("auth", payload)
        self.assertIn("quota", payload)
        self.assertIn("systemd_inhibit", payload)
        self.assertNotIn("\\u", raw.decode("utf-8"))
        systemd = next(item for item in payload["checks"] if item["id"] == "systemd")
        if systemd["level"] != "ok":
            self.assertEqual(systemd["level"], "warn")
            self.assertIn("Background runs still work", systemd["detail"])

    def test_empty_goal_is_rejected_and_does_not_spawn(self) -> None:
        with patch("nightwatch.web.spawn_supervisor") as spawn:
            status, payload, headers, _raw = self.request(
                "POST",
                "/api/runs",
                {"repo": "/tmp/nightwatch-web-empty-goal", "goal": "   "},
            )
        self.assertEqual(status, 400)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertFalse(payload["ok"])
        self.assertIn("goal", payload["message"].lower())
        spawn.assert_not_called()
        self.assertEqual(list(Path(self.state_home).iterdir()), [])

    def test_invalid_json_and_oversized_body_are_400(self) -> None:
        with patch("nightwatch.web.spawn_supervisor") as spawn:
            status, payload, _headers, _raw = self.request("POST", "/api/runs", raw=b"{")
            self.assertEqual(status, 400)
            assert isinstance(payload, dict)
            self.assertFalse(payload["ok"])
            oversized = b'{"repo":"/tmp/x","goal":"' + (b"a" * (70 * 1024)) + b'"}'
            status, payload, _headers, _raw = self.request("POST", "/api/runs", raw=oversized)
            self.assertEqual(status, 400)
            assert isinstance(payload, dict)
            self.assertFalse(payload["ok"])
            self.assertIn("64", payload["message"])
        spawn.assert_not_called()

    def test_post_run_initializes_and_returns_patched_pid(self) -> None:
        repo = self.make_repo("post")
        goal = "Ship the feature 修复配额"
        with (
            patch("nightwatch.web.spawn_supervisor", return_value=4242) as spawn,
            patch("nightwatch.operations.start_user_service", side_effect=AssertionError("systemd must not start")),
        ):
            status, payload, headers, raw = self.request(
                "POST",
                "/api/runs",
                {"repo": str(repo), "goal": goal, "verify": ["git diff --check"]},
            )
        self.assertEqual(status, 201)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIsInstance(payload, dict)
        assert isinstance(payload, dict)
        self.assertEqual(
            set(payload),
            {"ok", "message", "run_id", "repo", "supervisor_pid"},
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["supervisor_pid"], 4242)
        self.assertEqual(payload["repo"], str(repo_root(repo)))
        self.assertNotIn("\\u", raw.decode("utf-8"))
        spawn.assert_called_once()
        self.assertEqual(Path(spawn.call_args.args[0]).resolve(), repo_root(repo))
        self.assertEqual(spawn.call_args.kwargs, {})

        store = NightwatchStore(repo)
        state = store.load_state()
        self.assertEqual(payload["run_id"], state["run_id"])
        self.assertEqual(state["state"], "NEW")
        self.assertEqual(state["account_mode"], "CURRENT_ONLY")
        self.assertEqual(store.load_policy()["final_commands"], ["git diff --check"])

        status, listed, _headers, listed_raw = self.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        assert isinstance(listed, dict)
        self.assertEqual([item["run_id"] for item in listed["runs"]], [state["run_id"]])
        self.assertEqual(listed["runs"][0]["id"], store.directory.name)
        self.assertEqual(listed["runs"][0]["goal"], goal)
        self.assertIn(goal, listed_raw.decode("utf-8"))
        self.assertNotIn("\\u", listed_raw.decode("utf-8"))
        self.assertFalse(listed["runs"][0]["terminal"])

    def test_list_and_detail_after_initialize(self) -> None:
        older = self.make_repo("older")
        newer = self.make_repo("newer")
        older_store = NightwatchStore(older)
        newer_store = NightwatchStore(newer)
        older_store.initialize(
            make_run_id(str(older)),
            "Older goal",
            str(older),
            timestamp="2020-01-01T00:00:00Z",
            verify_commands=["git diff --check"],
        )
        newer_state = newer_store.initialize(
            make_run_id(str(newer)),
            "修复配额恢复",
            str(newer),
            timestamp="2024-06-01T00:00:00Z",
            verify_commands=["git diff --check"],
        )
        status, listed, _headers, raw = self.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        assert isinstance(listed, dict)
        self.assertEqual([item["id"] for item in listed["runs"]], [newer_store.directory.name, older_store.directory.name])
        self.assertEqual([item["updated_at"] for item in listed["runs"]], ["2024-06-01T00:00:00Z", "2020-01-01T00:00:00Z"])
        summary_keys = {
            "id",
            "run_id",
            "repo",
            "goal",
            "state",
            "thread_id",
            "provider",
            "model",
            "verified_percent",
            "verified_count",
            "total_count",
            "updated_at",
            "terminal",
        }
        self.assertEqual(set(listed["runs"][0]), summary_keys)
        self.assertIn("修复配额恢复", raw.decode("utf-8"))
        self.assertNotIn("\\u", raw.decode("utf-8"))

        status, detail, headers, _raw = self.request("GET", f"/api/runs/{newer_store.directory.name}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        assert isinstance(detail, dict)
        self.assertTrue(summary_keys <= set(detail))
        for key in ("milestones", "quota", "last_error", "progress", "log_tail"):
            self.assertIn(key, detail)
        plan = newer_store.load_plan()
        self.assertEqual(
            detail["milestones"],
            [{"id": item["id"], "title": item["title"], "status": item["status"]} for item in plan["milestones"]],
        )
        self.assertEqual(detail["progress"], plan_progress(plan))
        self.assertEqual(detail["run_id"], newer_state["run_id"])
        self.assertEqual(detail["goal"], "修复配额恢复")
        self.assertEqual(detail["state"], "NEW")
        self.assertFalse(detail["terminal"])
        self.assertIsNone(detail["quota"])
        self.assertIsNone(detail["last_error"])
        self.assertEqual(detail["log_tail"], newer_store.log_path.read_text(encoding="utf-8").splitlines()[-80:])
        self.assertTrue(any("NEW:" in line for line in detail["log_tail"]))

        newer_store.log_path.unlink()
        status, missing_log, _headers, _raw = self.request("GET", f"/api/runs/{newer_store.directory.name}")
        self.assertEqual(status, 200)
        assert isinstance(missing_log, dict)
        self.assertEqual(missing_log["log_tail"], [])

    def test_stop_new_run_returns_ok_true(self) -> None:
        repo = self.make_repo("stop")
        store = NightwatchStore(repo)
        store.initialize(make_run_id(str(repo)), "Stop me", str(repo), verify_commands=["git diff --check"])
        status, payload, _headers, _raw = self.request("POST", f"/api/runs/{store.directory.name}/stop")
        self.assertEqual(status, 200)
        assert isinstance(payload, dict)
        self.assertEqual(payload, {"ok": True, "message": "Nightwatch STOPPED; durable state preserved"})
        self.assertEqual(store.load_state()["state"], "STOPPED")

    def test_resume_finished_run_does_not_spawn(self) -> None:
        repo = self.make_repo("resume")
        store = NightwatchStore(repo)
        store.initialize(make_run_id(str(repo)), "Already done", str(repo))
        store.mutate(
            "marked_done",
            "test closed the run",
            lambda item: {**item, "state": "DONE", "thread_id": "TEST-001"},
        )
        with (
            patch("nightwatch.web.spawn_supervisor") as spawn,
            patch("nightwatch.operations.resume_service", side_effect=AssertionError("systemctl must not run")),
        ):
            status, payload, _headers, _raw = self.request("POST", f"/api/runs/{store.directory.name}/resume")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": False, "message": "This run is already finished. Start a new goal."})
        spawn.assert_not_called()

    def test_resume_stopped_run_spawns_supervisor(self) -> None:
        repo = self.make_repo("resume-stopped")
        store = NightwatchStore(repo)
        store.initialize(make_run_id(str(repo)), "Paused", str(repo), thread_id="TEST-001")
        self.request("POST", f"/api/runs/{store.directory.name}/stop")
        with patch("nightwatch.web.spawn_supervisor", return_value=6161) as spawn:
            status, payload, _headers, _raw = self.request("POST", f"/api/runs/{store.directory.name}/resume")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "message": "Detached supervisor started.", "supervisor_pid": 6161})
        spawn.assert_called_once_with(store.repo)

    def test_resume_new_run_returns_patched_pid(self) -> None:
        repo = self.make_repo("resume-new")
        store = NightwatchStore(repo)
        store.initialize(make_run_id(str(repo)), "Keep going", str(repo))
        with patch("nightwatch.web.spawn_supervisor", return_value=5150) as spawn:
            status, payload, _headers, _raw = self.request("POST", f"/api/runs/{store.directory.name}/resume")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "message": "Detached supervisor started.", "supervisor_pid": 5150})
        spawn.assert_called_once_with(store.repo)

    def test_id_traversal_is_404(self) -> None:
        for path in ("/api/runs/../x", "/api/runs/..%2Fx", "/api/runs/%2e%2e%2fx", "/api/runs/%2e%2e"):
            status, payload, headers, _raw = self.request("GET", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
            assert isinstance(payload, dict)
            self.assertFalse(payload["ok"])
            self.assertIn("message", payload)
        status, payload, _headers, _raw = self.request("GET", "/api/runs/not-a-real-run")
        self.assertEqual(status, 404)
        assert isinstance(payload, dict)
        self.assertEqual(payload, {"ok": False, "message": "No Nightwatch run matches that id."})

    def test_unknown_path_is_404(self) -> None:
        status, payload, _headers, _raw = self.request("GET", "/api/missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"ok": False, "message": "Not found."})

    def test_index_is_html_or_a_plain_500(self) -> None:
        index = Path(serve.__code__.co_filename).resolve().parent / "web_static" / "index.html"
        for path in ("/", "/index.html"):
            status, payload, headers, raw = self.request("GET", path)
            if index.is_file():
                self.assertEqual(status, 200, path)
                self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
                self.assertEqual(raw, index.read_bytes())
            else:
                self.assertEqual(status, 500, path)
                self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
                self.assertIsInstance(payload, str)
                self.assertTrue(payload.strip())

    def test_steer_without_a_thread_returns_200(self) -> None:
        repo = self.make_repo("steer")
        store = NightwatchStore(repo)
        store.initialize(make_run_id(str(repo)), "Steer me", str(repo))
        status, payload, _headers, _raw = self.request(
            "POST",
            f"/api/runs/{store.directory.name}/steer",
            {"instruction": "Keep the public API stable."},
        )
        self.assertEqual(status, 200)
        assert isinstance(payload, dict)
        self.assertEqual(set(payload), {"ok", "message"})
        self.assertFalse(payload["ok"])
        self.assertIn("thread", payload["message"].lower())
        status, missing, _headers, _raw = self.request(
            "POST",
            "/api/runs/does-not-exist/steer",
            {"instruction": "nope"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing, {"ok": False, "message": "No Nightwatch run matches that id."})

    def test_spawn_supervisor_returns_the_child_pid(self) -> None:
        repo = self.make_repo("spawn")
        store = NightwatchStore(repo)
        store.initialize(make_run_id(str(repo)), "Detach me", str(repo))
        root = repo_root(repo)
        recorded: list[tuple[list[str], dict]] = []
        real_popen = subprocess.Popen

        def fake_popen(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and "nightwatch.cli" in argv:
                recorded.append((list(argv), kwargs))
                process = Mock()
                process.pid = 77
                return process
            return real_popen(argv, *args, **kwargs)

        with patch("nightwatch.web.subprocess.Popen", side_effect=fake_popen):
            pid = spawn_supervisor(root)
        self.assertEqual(pid, 77)
        self.assertEqual(len(recorded), 1)
        argv, kwargs = recorded[0]
        self.assertEqual(
            argv,
            [sys.executable, "-m", "nightwatch.cli", "resume", "--no-inhibit", "--repo", str(root)],
        )
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], kwargs["stderr"])
        self.assertEqual(Path(kwargs["stdout"].name), store.log_path)

    def test_make_server_binds_ipv6_loopback(self) -> None:
        server = make_server("::1", 0)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "::1")
        self.assertGreater(server.server_address[1], 0)

    def test_serve_rejects_wildcard_without_binding(self) -> None:
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))
        self.assertFalse(is_loopback_host("localhost"))
        self.assertFalse(is_loopback_host("::"))
        stderr = io.StringIO()
        with patch("nightwatch.web.make_server", side_effect=AssertionError("must not bind")) as mocked, redirect_stderr(stderr):
            code = serve("0.0.0.0", 8765)
        self.assertEqual(code, 2)
        mocked.assert_not_called()
        self.assertIn("0.0.0.0", stderr.getvalue())
        with patch("nightwatch.web.ThreadingHTTPServer.__init__", side_effect=AssertionError("bound")) as init:
            with self.assertRaises(ValueError):
                make_server("0.0.0.0", 0)
        init.assert_not_called()


if __name__ == "__main__":
    unittest.main()
