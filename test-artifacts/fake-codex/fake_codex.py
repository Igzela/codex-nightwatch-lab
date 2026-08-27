#!/usr/bin/env python3
"""A deterministic Codex JSONL double for Nightwatch tests.

It intentionally speaks only the small event surface Nightwatch consumes. It
never contacts a provider and writes state only below the test repository.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(value: object) -> None:
    print(json.dumps(value), flush=True)


def state_path() -> Path:
    return Path.cwd() / ".fake-codex-state.json"


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {"starts": 0, "resumes": 0, "thread_id": os.environ.get("FAKE_CODEX_THREAD_ID", "TEST-001")}


def save_state(value: dict) -> None:
    state_path().write_text(json.dumps(value, indent=2) + "\n")


def arg_value(name: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def is_resume() -> bool:
    return "resume" in sys.argv


def main() -> int:
    if "--version" in sys.argv:
        print("codex-cli fake-0.1")
        return 0
    if len(sys.argv) >= 2 and sys.argv[1:3] == ["login", "status"]:
        print("Logged in using fake auth")
        return 0
    if "app-server" in sys.argv:
        for line in sys.stdin:
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            if request.get("id") == 1:
                emit({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
            elif request.get("id") == 2:
                emit({"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": int(time.time()) + 3600}, "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": int(time.time()) + 3600}}}})
        return 0

    state = load_state()
    resume = is_resume()
    if resume:
        state["resumes"] = state.get("resumes", 0) + 1
    else:
        state["starts"] = state.get("starts", 0) + 1
    thread_id = state.get("thread_id", "TEST-001")
    save_state(state)
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "normal")
    if resume and os.environ.get("FAKE_CODEX_RESUME_SCENARIO"):
        scenario = os.environ["FAKE_CODEX_RESUME_SCENARIO"]
    if resume and scenario == "quota_then_success":
        scenario = "normal"
    if scenario == "quota_again" and resume and state["resumes"] > 1:
        scenario = "normal"

    if scenario != "missing_thread":
        emit({"type": "thread.started", "thread_id": thread_id})
    if scenario == "duplicate_event":
        value = {"type": "turn.started", "turn_id": "T1"}
        emit(value)
        emit(value)
        emit({"type": "turn.completed"})
        return 0
    if scenario == "malformed":
        print("not-json", flush=True)
        return 1
    if scenario == "auth":
        emit({"type": "error", "error": {"code": "authentication_error", "message": "authentication required"}})
        return 1
    if scenario in {"quota", "weekly", "quota_again"} or (scenario == "quota_then_success" and not resume):
        weekly = scenario == "weekly"
        reset = int(time.time()) + int(os.environ.get("FAKE_CODEX_RESET_SECONDS", "1"))
        emit({"type": "error", "error": {"code": "usage_limit_reached", "rateLimitReachedType": "weekly" if weekly else "5h", "resetsAt": reset, "message": "usage limit reached"}, "rate_limits": {"primary" if not weekly else "secondary": {"used_percent": 100, "window_minutes": 10080 if weekly else 300, "resets_at": reset}}})
        return 1
    if scenario == "temporary_429":
        emit({"type": "error", "error": {"code": "http_429", "message": "429 Too Many Requests"}})
        return 1
    if scenario == "capacity":
        emit({"type": "error", "error": {"code": "capacity", "message": "model capacity overloaded"}})
        return 1
    if scenario == "network":
        emit({"type": "error", "error": {"code": "network_error", "message": "stream disconnected"}})
        return 1
    if scenario == "blocker":
        emit({"type": "error", "error": {"code": "task_blocker", "message": "task blocker: requires human decision"}})
        return 1
    if scenario == "crash":
        os.kill(os.getpid(), signal.SIGKILL)
    if scenario == "slow":
        for index in range(100):
            emit({"type": "turn.started", "turn_id": f"slow-{index}"})
            time.sleep(0.2)
        return 0

    plan_file = os.environ.get("FAKE_CODEX_PLAN_FILE")
    if plan_file and not (Path.cwd() / ".nightwatch" / "proposed-plan.json").exists():
        Path.cwd().joinpath(".nightwatch", "proposed-plan.json").write_text(Path(plan_file).read_text())
    progress_file = os.environ.get("FAKE_CODEX_PROGRESS_FILE")
    if progress_file:
        Path.cwd().joinpath(".nightwatch", "progress.json").write_text(Path(progress_file).read_text())
    if scenario in {"normal", "done_but_fails"}:
        Path.cwd().joinpath("fake-implemented.txt").write_text("implemented\n")
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
