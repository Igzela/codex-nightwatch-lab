#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)

def _spawn_descendant(sentinel_name: str = "DESCENDANT_SHOULD_NOT_EXIST.txt", sleep_seconds: float = 0.5) -> subprocess.Popen:
    code = f"import time, pathlib; time.sleep({sleep_seconds}); pathlib.Path({sentinel_name!r}).write_text('LEAKED\\n')"
    proc = subprocess.Popen([sys.executable, "-c", code])
    Path("descendant.pid").write_text(str(proc.pid))
    return proc

def main() -> int:
    if "--version" in sys.argv:
        print("1.1.26")
        return 0

    scenario = os.environ.get("FAKE_AGY_SCENARIO", "normal")

    # Check for usage probe
    if "-p" in sys.argv or "--print" in sys.argv:
        p_idx = sys.argv.index("-p") if "-p" in sys.argv else sys.argv.index("--print")
        if p_idx + 1 < len(sys.argv) and sys.argv[p_idx + 1] == "/usage":
            if scenario == "usage_malformed_json":
                print("MALFORMED NOT JSON", flush=True)
                return 0
            if scenario == "usage_exec_failure":
                sys.stderr.write("executable failure\n")
                return 1
            if scenario == "usage_auth_failure":
                sys.stderr.write("UNAUTHORIZED: login credentials expired\n")
                return 1

            gemini_frac = 0.0 if scenario in ("exhausted", "exhausted_gemini") else 0.40
            tp_frac = 0.0 if scenario in ("exhausted", "exhausted_3p") else 0.50


            gemini_buckets = []
            if scenario != "missing_gemini_5h":
                gemini_buckets.append({
                    "id": "gemini-5h",
                    "name": "Five Hour Limit",
                    "window": "5h",
                    "remaining_fraction": "invalid" if scenario == "malformed_fraction" else gemini_frac,
                    "reset_time": "invalid_date" if scenario == "invalid_reset_time" else "2026-09-04T18:00:00Z",
                })
            if scenario != "missing_gemini_weekly":
                gemini_buckets.append({
                    "id": "gemini-weekly",
                    "name": "Weekly Limit",
                    "window": "weekly",
                    "remaining_fraction": 0.65,
                    "reset_time": "2026-09-11T00:35:03Z",
                })

            tp_buckets = []
            if scenario != "missing_3p_5h":
                tp_buckets.append({
                    "id": "3p-5h",
                    "name": "Five Hour Limit Remaining",
                    "window": "5h",
                    "remaining_fraction": tp_frac,
                    "reset_time": "2026-09-04T18:00:00Z",
                })
            if scenario != "missing_3p_weekly":
                tp_buckets.append({
                    "id": "3p-weekly",
                    "name": "Weekly Limit Remaining",
                    "window": "weekly",
                    "remaining_fraction": 0.75,
                    "reset_time": "2026-09-11T00:35:03Z",
                })

            usage_data = {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": gemini_buckets,
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": tp_buckets,
                    },
                ],
            }
            emit({"event": "command_result", "command": {"name": "usage", "data": usage_data}})
            emit({"event": "result", "result": {"status": "SUCCESS", "response": "usage ok"}})
            return 0

    # Normal or error execution
    conv_id = None
    if "--conversation" in sys.argv:
        c_idx = sys.argv.index("--conversation")
        if c_idx + 1 < len(sys.argv):
            conv_id = sys.argv[c_idx + 1]

    if scenario == "not_found":
        sys.stderr.write(f'warning: conversation "{conv_id or "missing"}" not found\n')
        sys.stderr.flush()
        conv_id = "generated-new-uuid-000"
    elif scenario == "not_found_sentinel":
        sys.stderr.write(f'warning: conversation "{conv_id or "missing"}" not found\n')
        sys.stderr.flush()
        time.sleep(0.4)
        Path("SHOULD_NOT_EXIST_AFTER_MISMATCH.txt").write_text("SHOULD NEVER BE CREATED\n")
        return 1
    elif scenario == "mismatch":
        conv_id = "unexpected-uuid-999"
    elif scenario == "mismatch_sentinel":
        # Receives --conversation X, emits init with unexpected UUID, waits briefly, attempts to create file
        emit({"event": "init", "conversation_id": "unexpected-uuid-999", "init": {"cwd": os.getcwd()}})
        time.sleep(0.4)
        Path("SHOULD_NOT_EXIST_AFTER_MISMATCH.txt").write_text("SHOULD NEVER BE CREATED\n")
        emit({"event": "result", "result": {"status": "SUCCESS", "conversation_id": "unexpected-uuid-999"}})
        return 0
    elif scenario == "step_mismatch_sentinel":
        # Receives --conversation X, emits init with X, then step_update with divergent-step-uuid, waits briefly, attempts to create file
        emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
        time.sleep(0.01)
        emit({"event": "step_update", "step_update": {"conversation_id": "divergent-step-uuid", "step_index": 0, "state": "DONE"}})
        time.sleep(0.4)
        Path("SHOULD_NOT_EXIST_AFTER_MISMATCH.txt").write_text("SHOULD NEVER BE CREATED\n")
        emit({"event": "result", "result": {"status": "SUCCESS", "conversation_id": "divergent-step-uuid"}})
        return 0
    elif scenario in ("exhausted", "exhausted_gemini", "exhausted_3p", "error_result_with_quota"):
        sys.stderr.write("RESOURCE_EXHAUSTED: quota exceeded for 5h window\n")
        sys.stderr.flush()
        conv_id = conv_id or "conv-exhausted"
        emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
        emit({"event": "result", "result": {"status": "ERROR", "conversation_id": conv_id}})
        return 1
    elif scenario == "auth_failure":
        sys.stderr.write("UNAUTHORIZED: authentication token invalid\n")
        sys.stderr.flush()
        return 1
    elif scenario == "hang":
        conv_id = conv_id or "conv-hang-123"
        emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
        time.sleep(30)
        return 0
    elif scenario == "mismatch_descendant":
        _spawn_descendant("DESCENDANT_SHOULD_NOT_EXIST.txt", 0.5)
        emit({"event": "init", "conversation_id": "unexpected-uuid-999", "init": {"cwd": os.getcwd()}})
        time.sleep(2.0)
        return 0
    elif scenario == "step_mismatch_descendant":
        _spawn_descendant("DESCENDANT_SHOULD_NOT_EXIST.txt", 0.5)
        emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
        time.sleep(0.01)
        emit({"event": "step_update", "step_update": {"conversation_id": "divergent-step-uuid", "step_index": 0, "state": "DONE"}})
        time.sleep(2.0)
        return 0
    elif scenario == "timeout_descendant":
        _spawn_descendant("DESCENDANT_SHOULD_NOT_EXIST.txt", 1.0)
        conv_id = conv_id or "conv-timeout-descendant"
        emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
        time.sleep(30.0)
        return 0
    elif scenario == "stop_descendant":
        _spawn_descendant("DESCENDANT_SHOULD_NOT_EXIST.txt", 1.0)
        conv_id = conv_id or "conv-stop-descendant"
        emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
        for _ in range(300):
            time.sleep(0.1)
        return 0

    else:
        if not conv_id:
            conv_id = os.environ.get("FAKE_AGY_CONV_ID", "agy-conv-12345")

    mailbox = Path.cwd() / ".nightwatch-agent"
    if mailbox.exists() and (mailbox / "context.json").exists():
        try:
            context = json.loads((mailbox / "context.json").read_text())
            plan = {
                "goal_hash": context.get("goal_hash"),
                "milestones": [{"id": "M1", "title": "implement task", "weight": 100}],
            }
            mailbox.joinpath("proposed-plan.json").write_text(json.dumps(plan))
            progress = {
                "milestones": [{"id": "M1", "status": "implemented"}],
            }
            mailbox.joinpath("progress.json").write_text(json.dumps(progress))
        except Exception:
            pass

    if scenario == "no_init":
        # Missing authoritative init event
        emit({"event": "step_update", "step_update": {"conversation_id": conv_id, "step_index": 0, "state": "DONE"}})
        emit({"event": "result", "result": {"status": "SUCCESS", "conversation_id": conv_id}})
        return 0

    if scenario == "invalid_init":
        # Init without valid conversation_id
        emit({"event": "init", "init": {"cwd": os.getcwd()}})
        emit({"event": "result", "result": {"status": "SUCCESS"}})
        return 0

    emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
    time.sleep(0.01)

    step_cid = "divergent-step-uuid" if scenario == "mismatched_step" else conv_id
    emit({"event": "step_update", "step_update": {"conversation_id": step_cid, "step_index": 0, "state": "DONE"}})
    time.sleep(0.01)

    if scenario == "no_result_event":
        return 0

    result_cid = "divergent-result-uuid" if scenario == "mismatched_result" else conv_id
    if scenario == "non_terminal_result":
        emit({"event": "result", "result": {"status": "RUNNING", "conversation_id": result_cid}})
    elif scenario == "unknown_result_status":
        emit({"event": "result", "result": {"status": "CUSTOM_UNKNOWN", "conversation_id": result_cid}})
    elif scenario == "canceled_result":
        emit({"event": "result", "result": {"status": "CANCELED", "conversation_id": result_cid}})
    elif scenario == "error_result_generic":
        emit({"event": "result", "result": {"status": "ERROR", "conversation_id": result_cid}})
        return 1
    else:
        emit({"event": "result", "result": {"status": "SUCCESS", "conversation_id": result_cid}})
    return 0

if __name__ == "__main__":
    sys.exit(main())
