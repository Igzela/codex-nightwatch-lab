from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import selectors
import signal as signal_module
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import ErrorKind, ProviderResult, QuotaWindow, validate_model_name, validate_reasoning_effort
from .milestones import trusted_environment
from .storage import NightwatchStore, redact


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number or number < 0:
        return None
    if number > 10_000_000_000:
        number /= 1000
    return int(number)


def parse_iso(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def parse_relative_seconds(text: str) -> int | None:
    matches = re.findall(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", text, re.I)
    if not matches:
        return None
    total = 0
    for raw, unit in matches:
        value = int(raw)
        if unit.lower().startswith("h"):
            total += value * 3600
        elif unit.lower().startswith("m"):
            total += value * 60
        else:
            total += value
    return total or None


def extract_reset(text: str, now: datetime | None = None) -> tuple[int | None, str | None]:
    now = now or utc_now()
    epoch_match = re.search(r"(?:resets?_at|resets?At)[\"']?\s*[=:]\s*[\"']?(\d{10,13})", text, re.I)
    if epoch_match:
        value = parse_epoch(epoch_match.group(1))
        if value is not None:
            return value, "provider_epoch"
    iso_match = re.search(r"(?:resets?_at|resets?At)[\"']?\s*[=:]\s*[\"']?(\d{4}-\d{2}-\d{2}T[^\"'\s]+Z)|(?:try again|retry|available)\s+(?:at|on)\s*[\"']?(\d{4}-\d{2}-\d{2}T[^\"'\s]+Z)", text, re.I)
    if iso_match:
        value = parse_iso(iso_match.group(1) or iso_match.group(2))
        if value is not None:
            return value, "provider_reset_at"
    relative_match = re.search(r"(?:try again|reset|retry|available)\s+(?:in|after)\s+([^\n.]+)", text, re.I)
    if relative_match:
        seconds = parse_relative_seconds(relative_match.group(1))
        if seconds is not None:
            return int(now.timestamp()) + seconds, "provider_relative"
    return None, None


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _first(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def extract_thread_id(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type", ""))
    if event_type not in {"thread.started", "thread_started", "session_meta", "session.started"} and "thread_id" not in event:
        return None
    for item in _walk_dicts(event):
        for key in ("thread_id", "threadId", "session_id", "sessionId", "conversation_id", "conversationId"):
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _limit_name(key: str, window: dict[str, Any]) -> str:
    duration = _first(window, "window_duration_mins", "windowDurationMins", "window_minutes", "windowMinutes")
    try:
        minutes = float(duration)
    except (TypeError, ValueError):
        minutes = 0
    if minutes >= 7 * 24 * 60:
        return "weekly"
    if minutes > 0 and minutes <= 12 * 60:
        return "5h"
    return "primary" if key == "primary" else "secondary"


def extract_quota_windows(event: dict[str, Any]) -> list[QuotaWindow]:
    found: list[QuotaWindow] = []
    for item in _walk_dicts(event):
        limits = _first(item, "rate_limits", "rateLimits")
        if not isinstance(limits, dict):
            continue
        for key in ("primary", "secondary"):
            raw = limits.get(key)
            if not isinstance(raw, dict):
                continue
            used = _first(raw, "used_percent", "usedPercent")
            duration = _first(raw, "window_duration_mins", "windowDurationMins", "window_minutes", "windowMinutes")
            reset = _first(raw, "resets_at", "resetsAt")
            used_float = float(used) if isinstance(used, (int, float)) else None
            duration_int = int(duration) if isinstance(duration, (int, float)) else None
            reset_int = parse_epoch(reset)
            found.append(QuotaWindow(_limit_name(key, raw), used_float, duration_int, reset_int))
        if found:
            break
    return _dedupe_windows(found)


def _dedupe_windows(windows: list[QuotaWindow]) -> list[QuotaWindow]:
    result: dict[str, QuotaWindow] = {}
    for window in windows:
        result[window.name] = window
    return list(result.values())


def _text_fields(value: Any) -> list[str]:
    result: list[str] = []
    for item in _walk_dicts(value):
        for key in ("code", "error", "message", "detail", "reason", "status", "type", "rateLimitReachedType"):
            candidate = item.get(key)
            if isinstance(candidate, str):
                result.append(candidate)
    return result


def classify_failure(
    events: list[dict[str, Any]],
    stderr: str,
    exit_code: int | None,
    signal_name: str | None,
) -> tuple[ErrorKind | None, str | None, int | None, str | None, list[QuotaWindow], str | None]:
    texts = _text_fields(events) + [stderr]
    combined = "\n".join(texts)
    lower = combined.lower()
    windows: list[QuotaWindow] = []
    reset_at = None
    reset_source = None
    for event in events:
        windows.extend(extract_quota_windows(event))
        for item in _walk_dicts(event):
            raw_reset = _first(item, "resets_at", "resetsAt")
            structured_reset = parse_epoch(raw_reset) or parse_iso(raw_reset)
            if structured_reset and (reset_at is None or structured_reset > reset_at):
                reset_at = structured_reset
                reset_source = "provider_epoch" if isinstance(raw_reset, (int, float)) else "provider_reset_at"
                break
    windows = _dedupe_windows(windows)
    for window in windows:
        if window.exhausted and window.resets_at and (reset_at is None or window.resets_at > reset_at):
            reset_at = window.resets_at
            reset_source = "rollout_rate_limits"
    if reset_at is None:
        reset_at, reset_source = extract_reset(combined)

    structured_codes = " ".join(
        str(value.get("code", ""))
        for event in events
        for value in _walk_dicts(event)
        if isinstance(value.get("code"), str)
    ).lower()
    reached_types = [str(item.get("rateLimitReachedType", "")).lower() for event in events for item in _walk_dicts(event)]
    is_weekly = any(value in {"weekly", "week", "7d", "7_day"} for value in reached_types) or any(token in lower for token in ("weekly", "7 day", "7-day"))
    quota_code = any(token in structured_codes for token in ("usage_limit", "usage-limit", "quota_exhausted", "rate_limit_reached"))
    quota_text = "usage limit" in lower or "you've hit your usage" in lower or "quota exhausted" in lower
    if quota_code or quota_text or any(window.exhausted for window in windows):
        kind = ErrorKind.QUOTA_WEEKLY if is_weekly or any(window.name == "weekly" and window.exhausted for window in windows) else ErrorKind.QUOTA_5H
        return kind, _safe_detail(combined), reset_at, reset_source, windows, None
    if "task_blocker" in lower or "blocked" in lower or "blocker" in lower:
        return ErrorKind.BLOCKER, _safe_detail(combined), None, None, windows, _safe_detail(combined)
    if any(token in lower for token in ("unauthorized", "authentication", "invalid api key", "token expired", "login required", "401", "403 forbidden")):
        return ErrorKind.AUTH, _safe_detail(combined), None, None, windows, None
    if any(token in lower for token in ("capacity", "overloaded", "temporarily unavailable", "529")):
        return ErrorKind.CAPACITY, _safe_detail(combined), None, None, windows, None
    if "429" in lower or "too many requests" in lower or "rate limit" in lower:
        return ErrorKind.TEMPORARY_429, _safe_detail(combined), None, None, windows, None
    if any(token in lower for token in ("connection", "network", "timed out", "timeout", "dns", "broken pipe", "stream disconnected", "websocket")):
        return ErrorKind.NETWORK, _safe_detail(combined), None, None, windows, None
    if exit_code not in (None, 0) or signal_name:
        return ErrorKind.CRASH, _safe_detail(combined) or f"Codex exited {exit_code or signal_name}", None, None, windows, None
    return None, None, reset_at, reset_source, windows, None


def _safe_detail(text: str) -> str | None:
    compact = " ".join(redact(text).split())
    return compact[:500] or None


def build_command(
    repo: str | Path,
    thread_id: str | None,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[list[str], str]:
    binary = os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
    # Prompt is sent over stdin, avoiding argv/process-list leakage.
    base = [binary, "exec"]
    if model:
        base.extend(["--model", validate_model_name(model)])
    if reasoning_effort:
        # JSON strings are valid TOML scalar values. Encoding the value keeps
        # the repeatable Codex config override a single, non-injectable argv.
        base.extend(["--config", f"model_reasoning_effort={json.dumps(validate_reasoning_effort(reasoning_effort))}"])
    if thread_id:
        # Resume accepts the persisted session identity and inherits the
        # session's execution policy. Never use the recency-based --last path.
        args = [*base, "--json", "resume", thread_id, "-"]
        action = "resume"
    else:
        # Do not enable Codex's automatic approval or dangerous bypass flags.
        args = [*base, "--json", "--sandbox", "workspace-write", "-"]
        action = "start"
    return args, action


def _signal_name(returncode: int | None) -> str | None:
    if returncode is None or returncode >= 0:
        return None
    try:
        return signal_module.Signals(-returncode).name
    except ValueError:
        return f"SIG{-returncode}"


def _sanitized_event(event: dict[str, Any]) -> dict[str, Any]:
    allowed = {"type", "thread_id", "threadId", "session_id", "sessionId", "turn_id", "turnId", "timestamp", "code", "status", "rate_limits", "rateLimits", "usage", "resets_at", "resetsAt", "error"}
    result: dict[str, Any] = {}
    for key, value in event.items():
        if key in allowed:
            result[key] = redact(value)
    if "type" not in result:
        result["type"] = "unknown"
    return result


def run_codex(
    store: NightwatchStore,
    generation: int,
    prompt: str,
    thread_id: str | None = None,
    on_spawn: Callable[[int, str], None] | None = None,
    on_thread: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    timeout: float | None = None,
) -> ProviderResult:
    state = store.load_state()
    args, action = build_command(
        store.repo,
        thread_id,
        prompt,
        model=state.get("model"),
        reasoning_effort=state.get("reasoning_effort"),
    )
    run_log = store.runs_path / f"generation-{generation}.stderr.log"
    store.runs_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    store.write_run_event(generation, {"type": "provider_command", "action": action, "argv": [item for item in args if item != prompt]})
    start = time.monotonic()
    try:
        process = subprocess.Popen(
            args,
            cwd=str(store.repo),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        env=trusted_environment(),
        )
    except OSError as exc:
        return ProviderResult(None, None, thread_id, 0, 0, error_kind=ErrorKind.UNKNOWN, error_detail=f"Codex spawn failed: {type(exc).__name__}", run_log=str(run_log))
    if on_spawn:
        on_spawn(process.pid, action)
    try:
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
    except OSError:
        pass

    stderr_queue: queue.Queue[str] = queue.Queue()

    def read_stderr() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            stderr_queue.put(line)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()
    events: list[dict[str, Any]] = []
    event_types: list[str] = []
    malformed = 0
    seen_hashes: set[str] = set()
    found_thread = thread_id
    stdout_lines = 0
    stdout_selector = selectors.DefaultSelector()
    stdout_open = process.stdout is not None
    if process.stdout is not None:
        stdout_selector.register(process.stdout, selectors.EVENT_READ)
    while stdout_open:
        if process.poll() is not None and not stdout_open:
            break
        if stop_event and stop_event.is_set() and process.poll() is None:
            try:
                process.send_signal(signal_module.SIGINT)
            except OSError:
                pass
        if timeout is not None and time.monotonic() - start > timeout and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        ready = stdout_selector.select(0.1)
        if not ready:
            if process.poll() is not None and process.stdout is not None and process.stdout.closed:
                stdout_open = False
            continue
        line = process.stdout.readline() if process.stdout is not None else ""
        if not line:
            stdout_open = False
            if process.stdout is not None:
                try:
                    stdout_selector.unregister(process.stdout)
                except KeyError:
                    pass
            continue
        stdout_lines += 1
        raw = line.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            malformed += 1
            store.write_run_event(generation, {"type": "malformed_json", "line_hash": hashlib.sha256(raw.encode()).hexdigest()[:16]})
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if digest in seen_hashes:
            store.write_run_event(generation, {"type": "duplicate_event", "line_hash": digest[:16]})
            continue
        seen_hashes.add(digest)
        events.append(event)
        event_type = str(event.get("type", "unknown"))
        event_types.append(event_type)
        store.write_run_event(generation, _sanitized_event(event))
        candidate = extract_thread_id(event)
        if candidate:
            if found_thread and candidate != found_thread:
                store.write_run_event(generation, {"type": "thread_id_mismatch", "expected": found_thread, "observed": candidate})
                found_thread = "__MISMATCH__"
            elif not found_thread:
                found_thread = candidate
                if on_thread:
                    on_thread(candidate)
    try:
        stdout_selector.close()
    except OSError:
        pass
    returncode = process.wait()
    stderr_thread.join(timeout=2)
    stderr = "".join(list(stderr_queue.queue))
    try:
        run_log.write_text(redact(stderr), encoding="utf-8")
        os.chmod(run_log, 0o600)
    except OSError:
        pass
    signal_name = _signal_name(returncode)
    try:
        process.stdout.close() if process.stdout is not None else None
        process.stderr.close() if process.stderr is not None else None
    except OSError:
        pass
    if found_thread == "__MISMATCH__":
        kind, detail = ErrorKind.STATE, "Codex emitted a different thread ID than the durable thread"
        return ProviderResult(returncode, signal_name, None, len(events), malformed, event_types, kind, detail, run_log=str(run_log))
    kind, detail, reset_at, reset_source, windows, blocker = classify_failure(events, stderr, returncode, signal_name)
    if malformed:
        kind, detail = ErrorKind.MALFORMED, f"Codex stdout contained {malformed} malformed JSONL event(s)"
    if not found_thread and action == "start":
        kind, detail = ErrorKind.STATE, "Codex did not emit thread.started with a thread_id"
    aborted = bool(stop_event and stop_event.is_set())
    return ProviderResult(returncode, signal_name, found_thread, len(events), malformed, event_types, kind, detail, reset_at, reset_source, windows, blocker, aborted, str(run_log))
