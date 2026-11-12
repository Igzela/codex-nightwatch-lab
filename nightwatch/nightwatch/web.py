"""Localhost JSON control plane for Nightwatch.

The curses TUI remains available. This module serves a static page plus a
small JSON API on a loopback socket so a browser can list runs, start a
detached supervisor, and stop or steer an existing run. It does not bind a
non-loopback address and it does not talk to systemd.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import sys
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .git import GitError, repo_root
from .models import State, TERMINAL_STATES, plan_progress
from .operations import RunSpec, doctor_snapshot, queue_steer, start_run, stop_run
from .storage import NightwatchStore, StateIntegrityError
from .tui import RunCatalog, RunRecord


MAX_BODY_BYTES = 64 * 1024
LOG_TAIL_LINES = 80
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_TERMINAL_VALUES = frozenset(item.value for item in TERMINAL_STATES)
_INDEX_PATH = Path(__file__).parent / "web_static" / "index.html"
_RUN_ACTIONS = frozenset({"stop", "steer", "resume"})
_FINISHED_MESSAGE = "This run is already finished. Start a new goal."
_CLOSED_STATES = frozenset({State.DONE.value, State.AWAITING_ACCEPTANCE.value})


def is_loopback_host(host: str) -> bool:
    """Return True only for the literal addresses this server may bind.

    ``0.0.0.0``, ``::``, ``localhost``, and every other name are rejected.
    ``serve`` checks this before ``make_server`` so a refused host never opens
    a socket.
    """

    return host in _LOOPBACK_HOSTS


def listen_url(host: str, port: int) -> str:
    if ":" in host:
        return f"http://[{host}]:{port}/"
    return f"http://{host}:{port}/"


class _LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _LoopbackHTTPServerV6(_LoopbackHTTPServer):
    address_family = socket.AF_INET6


def make_server(host: str, port: int) -> socketserver.TCPServer:
    """Bind a ``ThreadingHTTPServer`` on a loopback address.

    ``port`` may be ``0`` so tests receive an ephemeral port. A host other
    than ``127.0.0.1`` or ``::1`` raises ``ValueError`` and does not bind.
    """

    if not is_loopback_host(host):
        raise ValueError(f"refusing to bind non-loopback host {host!r}")
    handler = ControlHandler
    if host == "::1":
        return _LoopbackHTTPServerV6((host, port), handler)
    return _LoopbackHTTPServer((host, port), handler)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> int:
    """Serve until ``KeyboardInterrupt``, then return ``0``.

    A non-loopback ``host`` prints an error to stderr and returns ``2``
    without binding. ``0.0.0.0`` is never bound.
    """

    if not is_loopback_host(host):
        print(
            f"nightwatch: refusing to bind {host}; the web control plane listens only on 127.0.0.1 or ::1",
            file=sys.stderr,
        )
        return 2
    server = make_server(host, port)
    url = listen_url(host, int(server.server_address[1]))
    print(f"Nightwatch web control plane: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except webbrowser.Error:
            print("nightwatch: could not open a browser", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def spawn_supervisor(repo: Path) -> int | None:
    """Start a detached ``nightwatch.cli resume --no-inhibit`` and return its pid.

    The child argv is fixed:

    ``[sys.executable, "-m", "nightwatch.cli", "resume", "--no-inhibit", "--repo", root]``

    ``repo`` must already be the absolute path ``repo_root`` returned. It is
    passed through as one argv element and is not resolved again with git.
    Nothing is passed through a shell. The child gets a new session, stdin
    from ``DEVNULL``, and stdout plus stderr appended to the store log (the
    parent directory is created when it is missing).

    Returns the child pid as an ``int``. Returns ``None`` when the process
    cannot be created. Callers copy that value into ``supervisor_pid``, so
    ``None`` becomes JSON ``null``. Tests patch this function and should
    return an ``int`` (the pid they want in the response) or ``None``.
    """

    root = Path(repo).expanduser().resolve()
    store = NightwatchStore(root)
    log_path = store.log_path
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_file = open(log_path, "ab")
    except OSError:
        return None
    try:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(
            [sys.executable, "-m", "nightwatch.cli", "resume", "--no-inhibit", "--repo", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            cwd=str(root),
        )
    except OSError:
        return None
    finally:
        log_file.close()
    return process.pid


def doctor_payload() -> dict[str, Any]:
    report = dict(doctor_snapshot(None))
    checks = doctor_checks(report)
    report["summary"] = doctor_summary(checks)
    report["checks"] = checks
    return report


def doctor_checks(report: dict[str, Any]) -> list[dict[str, str]]:
    codex_present = bool(report.get("codex_binary"))
    version = report.get("codex_version")
    if codex_present:
        shown = version or str(report.get("codex_binary"))
        codex = {
            "id": "codex",
            "level": "ok",
            "title": "Codex CLI",
            "detail": f"Codex is installed ({shown}).",
        }
    else:
        codex = {
            "id": "codex",
            "level": "fail",
            "title": "Codex CLI",
            "detail": "The codex binary was not found. Install Codex before starting a Codex run.",
        }

    auth_status = auth_field(report).get("status")
    if auth_status == "ok":
        auth = {
            "id": "auth",
            "level": "ok",
            "title": "Codex authentication",
            "detail": "Codex login status succeeded.",
        }
    else:
        auth = {
            "id": "auth",
            "level": "fail",
            "title": "Codex authentication",
            "detail": "Codex is not signed in. Run codex login on this machine, then refresh.",
        }

    quota_status = quota_field(report).get("status")
    if quota_status == "ok":
        authority = quota_field(report).get("authority") or "the quota provider"
        quota = {
            "id": "quota",
            "level": "ok",
            "title": "Codex quota",
            "detail": f"Quota was read from {authority}.",
        }
    elif codex_present:
        quota = {
            "id": "quota",
            "level": "warn",
            "title": "Codex quota",
            "detail": "Quota could not be read. You can still start a run; Nightwatch retries the reading before waiting on quota.",
        }
    else:
        quota = {
            "id": "quota",
            "level": "fail",
            "title": "Codex quota",
            "detail": "Quota is unavailable because the codex binary is missing.",
        }

    if report.get("systemd_inhibit"):
        systemd = {
            "id": "systemd",
            "level": "ok",
            "title": "systemd",
            "detail": "systemd-inhibit is usable, so a foreground supervisor can block sleep.",
        }
    else:
        systemd = {
            "id": "systemd",
            "level": "warn",
            "title": "systemd",
            "detail": "systemd is unavailable. Background runs still work.",
        }

    agy = report.get("agy") if isinstance(report.get("agy"), dict) else {}
    if agy.get("status") == "ok" and agy.get("binary"):
        agy_check = {
            "id": "agy",
            "level": "ok",
            "title": "AGY CLI",
            "detail": f"AGY is installed ({agy.get('version') or agy.get('binary')}) and signed in.",
        }
    else:
        agy_check = {
            "id": "agy",
            "level": "warn",
            "title": "AGY CLI",
            "detail": "AGY is missing or not signed in. It is optional; Codex runs do not need it.",
        }
    return [codex, auth, quota, systemd, agy_check]


def doctor_summary(checks: list[dict[str, str]]) -> str:
    levels = {item["id"]: item["level"] for item in checks}
    if levels.get("codex") == "fail":
        return "Install Codex on this machine before starting a run."
    if levels.get("auth") == "fail":
        return "Run codex login on this machine, then refresh this page before starting a run."
    if levels.get("quota") == "fail":
        return "Install Codex so Nightwatch can read quota before starting a run."
    if levels.get("quota") == "warn":
        return "Codex is signed in, but quota could not be read; you can still start a run from this page."
    if levels.get("systemd") == "warn":
        return "Codex is ready, and background runs still work even though systemd is unavailable."
    if levels.get("agy") == "warn":
        return "Codex is ready to start a run, and AGY remains optional."
    return "Codex is ready, so start a run from this page."


def auth_field(report: dict[str, Any]) -> dict[str, Any]:
    auth = report.get("auth")
    return auth if isinstance(auth, dict) else {}


def quota_field(report: dict[str, Any]) -> dict[str, Any]:
    quota = report.get("quota")
    return quota if isinstance(quota, dict) else {}


def unsafe_run_id(run_id: str) -> bool:
    """Reject path separators and any id that is not a single directory name."""

    if not isinstance(run_id, str) or not run_id or run_id in {".", ".."}:
        return True
    if "/" in run_id or "\\" in run_id or ".." in run_id or "\x00" in run_id:
        return True
    return False


def goal_text(record: RunRecord) -> str:
    goal = record.state.get("goal")
    if isinstance(goal, str) and goal.strip():
        return goal
    try:
        return record.store.goal_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def log_tail(path: Path, limit: int = LOG_TAIL_LINES) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    return text.splitlines()[-limit:]


def run_summary(record: RunRecord) -> dict[str, Any]:
    state = record.state
    progress = plan_progress(record.plan)
    provider = state.get("provider")
    if not isinstance(provider, str) or not provider:
        provider = record.provider
    model = state.get("model") if isinstance(state.get("model"), str) else None
    thread_id = state.get("thread_id") if isinstance(state.get("thread_id"), str) else None
    return {
        "id": record.store.directory.name,
        "run_id": state.get("run_id"),
        "repo": str(record.repo),
        "goal": goal_text(record),
        "state": state.get("state"),
        "thread_id": thread_id,
        "provider": provider,
        "model": model,
        "verified_percent": progress["verified_percent"],
        "verified_count": progress["verified_count"],
        "total_count": progress["total_count"],
        "updated_at": state.get("updated_at"),
        "terminal": state.get("state") in _TERMINAL_VALUES,
    }


def run_detail(record: RunRecord) -> dict[str, Any]:
    payload = run_summary(record)
    milestones: list[dict[str, Any]] = []
    for item in record.plan.get("milestones", []):
        if not isinstance(item, dict):
            continue
        milestones.append({"id": item.get("id"), "title": item.get("title"), "status": item.get("status")})
    payload["milestones"] = milestones
    payload["quota"] = record.state.get("quota")
    payload["last_error"] = record.state.get("last_error")
    payload["progress"] = plan_progress(record.plan)
    payload["log_tail"] = log_tail(record.store.log_path)
    return payload


def list_payload() -> dict[str, Any]:
    catalog = RunCatalog()
    records = catalog.discover()
    records.sort(key=lambda item: str(item.state.get("updated_at") or ""), reverse=True)
    return {"runs": [run_summary(item) for item in records], "errors": list(catalog.errors)}


def find_run(run_id: str) -> RunRecord | None:
    if unsafe_run_id(run_id):
        return None
    catalog = RunCatalog()
    for record in catalog.discover():
        if record.store.directory.name == run_id:
            return record
    return None


def run_spec_from_body(body: dict[str, Any]) -> RunSpec:
    if "repo" not in body or "goal" not in body:
        raise ValueError("repo and goal are required")
    repo = body["repo"]
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo must be a path string")
    verify = body.get("verify", [])
    if verify is None:
        verify = []
    if not isinstance(verify, list) or not all(isinstance(item, str) for item in verify):
        raise ValueError("verify must be a list of strings")
    model = body.get("model", None)
    if "model" in body and model is not None and not isinstance(model, str):
        raise ValueError("model must be a string or null")
    effort = body.get("reasoning_effort", None)
    if "reasoning_effort" in body and effort is not None and not isinstance(effort, str):
        raise ValueError("reasoning_effort must be a string or null")
    provider = body.get("provider", "codex")
    if provider is None:
        provider = "codex"
    if not isinstance(provider, str):
        raise ValueError("provider must be 'codex' or 'agy'")
    return RunSpec(
        repo=Path(repo),
        goal=body["goal"],
        model=model,
        reasoning_effort=effort,
        verify_commands=tuple(verify),
        service=False,
        account_mode="current-only",
        provider=provider,
    )


def index_bytes() -> tuple[int, str, bytes]:
    try:
        data = _INDEX_PATH.read_bytes()
    except OSError:
        return 500, "text/plain; charset=utf-8", b"index.html is missing\n"
    try:
        data.decode("utf-8")
    except UnicodeError:
        return 500, "text/plain; charset=utf-8", b"index.html is not valid UTF-8\n"
    return 200, "text/html; charset=utf-8", data


def request_path(raw: str) -> str:
    path = urllib.parse.urlsplit(raw).path
    path = urllib.parse.unquote(path)
    if not path.startswith("/"):
        path = "/" + path
    return path


def split_run_path(path: str) -> tuple[str, str] | None:
    """Return ``(id, action)`` for a run URL, without normalizing ``..`` away."""

    prefix = "/api/runs/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if not rest:
        return None
    parts = rest.split("/")
    if len(parts) == 1:
        return parts[0], ""
    if len(parts) == 2 and parts[1] in _RUN_ACTIONS:
        return parts[0], parts[1]
    return None


class ControlHandler(BaseHTTPRequestHandler):
    server_version = f"NightwatchHTTP/{__version__}"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            path = request_path(self.path)
            if "\x00" in path:
                self._send_json(404, {"ok": False, "message": "Not found."})
                return
            if path in {"/", "/index.html"}:
                if method != "GET":
                    self._send_json(405, {"ok": False, "message": "Method not allowed."})
                    return
                status, content_type, body = index_bytes()
                self._send_bytes(status, content_type, body)
                return
            if path == "/api/doctor":
                if method != "GET":
                    self._send_json(405, {"ok": False, "message": "Method not allowed."})
                    return
                self._send_json(200, doctor_payload())
                return
            if path == "/api/runs":
                if method == "GET":
                    self._send_json(200, list_payload())
                    return
                if method == "POST":
                    self._post_run()
                    return
                self._send_json(405, {"ok": False, "message": "Method not allowed."})
                return
            matched = split_run_path(path)
            if matched is None:
                self._send_json(404, {"ok": False, "message": "Not found."})
                return
            run_id, action = matched
            record = find_run(run_id)
            if record is None:
                self._send_json(404, {"ok": False, "message": "No Nightwatch run matches that id."})
                return
            if action == "" and method == "GET":
                self._send_json(200, run_detail(record))
                return
            if action == "stop" and method == "POST":
                self._post_stop(record)
                return
            if action == "steer" and method == "POST":
                self._post_steer(record)
                return
            if action == "resume" and method == "POST":
                self._post_resume(record)
                return
            if action == "" and method != "GET":
                self._send_json(405, {"ok": False, "message": "Method not allowed."})
                return
            self._send_json(405, {"ok": False, "message": "Method not allowed."})
        except Exception:
            traceback.print_exc()
            if not getattr(self, "_response_started", False):
                self._send_json(500, {"ok": False, "message": "internal error"})

    def _post_run(self) -> None:
        body, error = self._json_object()
        if error or body is None:
            self._send_json(400, {"ok": False, "message": error or "request body is not valid JSON"})
            return
        try:
            spec = run_spec_from_body(body)
            root = repo_root(spec.repo)
            result = start_run(spec, run_in_service=False)
        except (TypeError, ValueError, GitError, OSError) as exc:
            self._send_json(400, {"ok": False, "message": str(exc)})
            return
        except SystemExit as exc:
            self._send_json(400, {"ok": False, "message": str(exc)})
            return
        if not result.ok:
            self._send_json(400, {"ok": False, "message": result.message})
            return
        try:
            state = NightwatchStore(root).load_state()
        except (OSError, StateIntegrityError) as exc:
            self._send_json(500, {"ok": False, "message": str(exc)})
            return
        try:
            pid = spawn_supervisor(root)
        except (OSError, GitError):
            pid = None
        self._send_json(
            201,
            {
                "ok": True,
                "message": result.message,
                "run_id": state.get("run_id"),
                "repo": str(root),
                "supervisor_pid": pid,
            },
        )

    def _post_stop(self, record: RunRecord) -> None:
        _raw, error = self._body_bytes()
        if error:
            self._send_json(400, {"ok": False, "message": error})
            return
        result = stop_run(record.repo)
        self._send_json(200, {"ok": result.ok, "message": result.message})

    def _post_steer(self, record: RunRecord) -> None:
        body, error = self._json_object()
        if error or body is None:
            self._send_json(400, {"ok": False, "message": error or "request body is not valid JSON"})
            return
        instruction = body.get("instruction")
        if not isinstance(instruction, str):
            self._send_json(400, {"ok": False, "message": "instruction must be a string"})
            return
        try:
            result = queue_steer(record.store, instruction)
        except (TypeError, ValueError) as exc:
            self._send_json(400, {"ok": False, "message": str(exc)})
            return
        self._send_json(200, {"ok": result.ok, "message": result.message})

    def _post_resume(self, record: RunRecord) -> None:
        _raw, error = self._body_bytes()
        if error:
            self._send_json(400, {"ok": False, "message": error})
            return
        if record.state.get("state") in _CLOSED_STATES:
            self._send_json(200, {"ok": False, "message": _FINISHED_MESSAGE})
            return
        try:
            pid = spawn_supervisor(record.repo)
        except (OSError, GitError):
            pid = None
        if pid is None:
            self._send_json(
                200,
                {"ok": False, "message": "The detached supervisor did not start.", "supervisor_pid": None},
            )
            return
        self._send_json(200, {"ok": True, "message": "Detached supervisor started.", "supervisor_pid": pid})

    def _json_object(self) -> tuple[dict[str, Any] | None, str | None]:
        raw, error = self._body_bytes()
        if error:
            return None, error
        if not raw:
            return None, "request body is not valid JSON"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "request body is not valid JSON"
        if not isinstance(payload, dict):
            return None, "request body must be a JSON object"
        return payload, None

    def _body_bytes(self) -> tuple[bytes, str | None]:
        header = self.headers.get("Content-Length")
        if header is None:
            return b"", None
        try:
            length = int(header)
        except ValueError:
            return b"", "Content-Length is invalid"
        if length < 0 or length > MAX_BODY_BYTES:
            return b"", "request body exceeds 64 KiB"
        try:
            return self.rfile.read(length), None
        except OSError:
            return b"", "request body could not be read"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self._response_started = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
