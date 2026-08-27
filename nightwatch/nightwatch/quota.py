from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codex import extract_quota_windows, parse_iso
from .models import QuotaSnapshot, QuotaWindow
from .storage import redact


class QuotaError(RuntimeError):
    pass


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _window(raw: Any, name: str) -> QuotaWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("usedPercent", raw.get("used_percent"))
    duration = raw.get("windowDurationMins", raw.get("window_duration_mins", raw.get("windowMinutes", raw.get("window_minutes"))))
    reset = raw.get("resetsAt", raw.get("resets_at"))
    used_value = float(used) if isinstance(used, (int, float)) else None
    duration_value = int(duration) if isinstance(duration, (int, float)) else None
    reset_value = int(reset / 1000) if isinstance(reset, (int, float)) and reset > 10_000_000_000 else int(reset) if isinstance(reset, (int, float)) else None
    return QuotaWindow(name, used_value, duration_value, reset_value)


def parse_quota_result(result: Any, source: str = "app_server") -> QuotaSnapshot:
    if not isinstance(result, dict):
        raise QuotaError("quota response result is not an object")
    raw = result.get("rateLimits", result.get("rate_limits", result))
    if not isinstance(raw, dict):
        raise QuotaError("quota response has no rateLimits object")
    primary = _window(raw.get("primary"), "5h") or _window(raw.get("short"), "5h")
    secondary = _window(raw.get("secondary"), "weekly") or _window(raw.get("long"), "weekly")
    if primary is None and secondary is None:
        raise QuotaError("quota response has no primary or secondary window")
    plan = raw.get("planType", raw.get("plan_type"))
    return QuotaSnapshot(source, iso_now(), primary, secondary, str(plan) if plan else None)


class AppServerQuotaProvider:
    def __init__(self, binary: str | None = None, timeout: float = 8.0):
        self.binary = binary or os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
        self.timeout = timeout

    def read(self) -> QuotaSnapshot:
        try:
            process = subprocess.Popen(
                [self.binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise QuotaError(f"failed to start Codex App Server: {type(exc).__name__}") from exc
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "nightwatch", "version": "0.1.0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        ]
        try:
            assert process.stdin is not None
            for request in requests:
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
            process.stdin.close()
            responses: dict[int, dict[str, Any]] = {}
            selector = selectors.DefaultSelector()
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + self.timeout
            buffer = ""
            while time.monotonic() < deadline and 2 not in responses:
                events = selector.select(max(0.05, deadline - time.monotonic()))
                if not events:
                    continue
                chunk = process.stdout.readline()
                if not chunk:
                    break
                buffer += chunk
                for line in buffer.splitlines():
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ident = message.get("id")
                    if isinstance(ident, int):
                        responses[ident] = message
            response = responses.get(2)
            if not response:
                raise QuotaError("Codex App Server quota response timed out")
            if response.get("error"):
                error = response["error"]
                raise QuotaError(str(redact(error.get("message", "App Server returned an error"))) if isinstance(error, dict) else "App Server returned an error")
            return parse_quota_result(response.get("result"), "app_server")
        finally:
            try:
                selector.close()
            except (NameError, OSError):
                pass
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass


class RolloutQuotaProvider:
    """Read the newest structured rate-limit event from local Codex JSONL.

    This is intentionally a fallback only. The App Server is authoritative
    when reachable; a stale local rollout must never be treated as a fresh
    recovery confirmation.
    """

    def __init__(self, codex_home: str | Path | None = None, max_age_seconds: int = 3600):
        self.codex_home = Path(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.max_age_seconds = max_age_seconds

    def read(self) -> QuotaSnapshot:
        sessions = self.codex_home / "sessions"
        candidates = sorted(sessions.glob("**/rollout-*.jsonl"), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
        newest: tuple[int, list[QuotaWindow]] | None = None
        for path in candidates:
            try:
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        windows = extract_quota_windows(event)
                        if not windows:
                            continue
                        timestamp = None
                        if isinstance(event, dict):
                            timestamp = parse_iso(event.get("timestamp"))
                        timestamp = timestamp or int(path.stat().st_mtime)
                        if newest is None or timestamp >= newest[0]:
                            newest = (timestamp, windows)
            except OSError:
                continue
        if newest is None:
            raise QuotaError("no structured rate-limit event found in local Codex rollouts")
        event_timestamp, windows = newest
        if int(time.time()) - event_timestamp > self.max_age_seconds:
            raise QuotaError("local rollout rate-limit event is stale")
        primary = next((window for window in windows if window.name == "5h"), None)
        secondary = next((window for window in windows if window.name == "weekly"), None)
        return QuotaSnapshot("rollout_jsonl", datetime.fromtimestamp(event_timestamp, timezone.utc).isoformat().replace("+00:00", "Z"), primary, secondary)


class FallbackQuotaProvider:
    def __init__(self, primary: AppServerQuotaProvider, fallback: RolloutQuotaProvider):
        self.primary = primary
        self.fallback = fallback

    def read(self) -> QuotaSnapshot:
        try:
            return self.primary.read()
        except QuotaError as primary_error:
            try:
                return self.fallback.read()
            except QuotaError as fallback_error:
                raise QuotaError("App Server and local rollout quota sources unavailable") from fallback_error


class FileQuotaProvider:
    """Deterministic provider used by fault tests and disposable fixtures."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> QuotaSnapshot:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return parse_quota_result(value, "fake_file")
        except (OSError, json.JSONDecodeError, QuotaError) as exc:
            raise QuotaError("fake quota file is unavailable or invalid") from exc


def make_quota_provider() -> AppServerQuotaProvider | FileQuotaProvider | FallbackQuotaProvider:
    fake = os.environ.get("NIGHTWATCH_QUOTA_FILE")
    return FileQuotaProvider(fake) if fake else FallbackQuotaProvider(AppServerQuotaProvider(), RolloutQuotaProvider())


def quota_recovered(snapshot: QuotaSnapshot, names: set[str]) -> bool:
    if snapshot.error:
        return False
    selected = [window for window in snapshot.windows() if window.name in names]
    return bool(selected) and all(not window.exhausted for window in selected)
