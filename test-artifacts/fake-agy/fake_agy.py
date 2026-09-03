#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)

def main() -> int:
    if "--version" in sys.argv:
        print("1.1.25")
        return 0

    scenario = os.environ.get("FAKE_AGY_SCENARIO", "normal")

    # Check for usage probe
    if "-p" in sys.argv:
        p_idx = sys.argv.index("-p")
        if p_idx + 1 < len(sys.argv) and sys.argv[p_idx + 1] == "/usage":
            if scenario == "exhausted":
                p_frac = 0.0
            else:
                p_frac = 0.40
            usage_data = {
                "groups": [{
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit",
                            "window": "5h",
                            "remaining_fraction": p_frac,
                            "reset_time": "2026-09-03T18:00:00Z",
                        },
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit",
                            "window": "weekly",
                            "remaining_fraction": 0.65,
                            "reset_time": "2026-09-06T00:35:03Z",
                        },
                    ],
                }],
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
    elif scenario == "mismatch":
        conv_id = "unexpected-uuid-999"
    elif scenario == "exhausted":
        sys.stderr.write("RESOURCE_EXHAUSTED: quota exceeded for 5h window\n")
        sys.stderr.flush()
        conv_id = conv_id or "conv-exhausted"
        emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
        emit({"event": "result", "result": {"status": "ERROR", "conversation_id": conv_id}})
        return 1
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

    emit({"event": "init", "conversation_id": conv_id, "init": {"cwd": os.getcwd()}})
    time.sleep(0.01)
    emit({"event": "step_update", "step_update": {"conversation_id": conv_id, "step_index": 0, "state": "DONE"}})
    time.sleep(0.01)
    emit({"event": "result", "result": {"status": "SUCCESS", "conversation_id": conv_id}})
    return 0

if __name__ == "__main__":
    sys.exit(main())
