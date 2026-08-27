#!/usr/bin/env python3
"""Deterministic stdio JSON-RPC server for Nightwatch App Server tests."""
from __future__ import annotations

import json
import os
import sys


def recv() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(2)
    return json.loads(line)


def send(value: dict) -> None:
    print(json.dumps(value), flush=True)


def main() -> int:
    scenario = os.environ.get("FAKE_APP_SERVER_SCENARIO", "normal")
    initialize = recv()
    if initialize.get("method") != "initialize" or initialize.get("id") != 1:
        send({"jsonrpc": "2.0", "id": initialize.get("id"), "error": {"message": "initialize required"}})
        return 1
    if scenario == "timeout":
        return 0
    send({"jsonrpc": "2.0", "id": 1, "result": {"userAgent": "fake", "platformOs": "linux", "codexHome": "/tmp"}})
    initialized = recv()
    if initialized.get("method") != "initialized" or "id" in initialized:
        send({"jsonrpc": "2.0", "id": 2, "error": {"message": "initialized notification required"}})
        return 1
    request = recv()
    if request.get("method") != "account/rateLimits/read" or request.get("id") != 2 or "params" in request:
        send({"jsonrpc": "2.0", "id": request.get("id"), "error": {"message": "invalid rate limits request"}})
        return 1
    if scenario == "exit":
        return 0
    if scenario == "error":
        send({"jsonrpc": "2.0", "id": 2, "error": {"message": "provider rejected"}})
        return 0
    if scenario == "missing_rate_limits":
        send({"jsonrpc": "2.0", "id": 2, "result": {"planType": "plus"}})
        return 0
    if scenario == "malformed":
        print("not json", flush=True)
    send({"jsonrpc": "2.0", "method": "account/rateLimits/updated", "params": {"rateLimits": {}}})
    send({"jsonrpc": "2.0", "id": 999, "result": {"ignored": True}})
    reset = 1787859341000 if scenario == "milliseconds" else 1787859341
    send({"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 99.9, "windowDurationMins": 300, "resetsAt": reset}, "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": reset}}}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
