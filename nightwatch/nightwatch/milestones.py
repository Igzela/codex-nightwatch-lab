from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .git import GitSnapshot, diff_check, snapshot
from .models import plan_progress, validate_plan
from .storage import NightwatchStore, StateIntegrityError, redact


MAX_MAILBOX_BYTES = 1_000_000
MAX_MILESTONES = 100
MAX_TITLE = 500
MAX_DEPTH = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_DEPTH:
        raise ValueError("mailbox JSON is too deeply nested")
    if isinstance(value, dict):
        return max([depth, *(_json_depth(item, depth + 1) for item in value.values())])
    if isinstance(value, list):
        return max([depth, *(_json_depth(item, depth + 1) for item in value)])
    return depth


def read_mailbox_json(store: NightwatchStore, name: str) -> Any | None:
    try:
        raw = store.read_mailbox_file(name)
        if raw is None:
            return None
        if len(raw) > MAX_MAILBOX_BYTES:
            raise ValueError("mailbox entry is too large")
        value = json.loads(raw.decode("utf-8"))
        _json_depth(value)
        return value
    except (StateIntegrityError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("mailbox JSON is invalid") from exc


def _proposal_to_plan(store: NightwatchStore, proposed: Any) -> dict[str, Any]:
    if not isinstance(proposed, dict) or set(proposed) - {"goal_hash", "milestones"}:
        raise ValueError("proposal has unsupported fields")
    acceptance = store.load_acceptance()
    if proposed.get("goal_hash") != acceptance.get("goal_hash"):
        raise ValueError("proposal is not bound to this goal")
    rows = proposed.get("milestones")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_MILESTONES:
        raise ValueError("proposal milestone count is invalid")
    policy = store.load_policy()
    profile = "default" if policy["final_commands"] else "none"
    milestones = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict) or set(item) - {"id", "title", "weight"}:
            raise ValueError("proposal contains untrusted authority fields")
        ident, title, weight = item.get("id"), item.get("title"), item.get("weight", 1)
        if not isinstance(ident, str) or not ident or len(ident) > 80 or ident in seen:
            raise ValueError("proposal milestone id is invalid")
        if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
            raise ValueError("proposal milestone title is invalid")
        if not isinstance(weight, (int, float)) or not 0 < float(weight) <= 10_000:
            raise ValueError("proposal milestone weight is invalid")
        milestones.append({"id": ident, "title": title.strip(), "weight": weight, "required": True, "status": "pending", "verification_profile": profile, "evidence": []})
        seen.add(ident)
    return {"schema_version": 2, "authority": "nightwatch", "policy_hash": policy["policy_hash"], "milestones": milestones}


def adopt_proposed_plan(store: NightwatchStore) -> bool:
    try:
        proposed = read_mailbox_json(store, "proposed-plan.json")
        if proposed is None:
            return False
        plan = _proposal_to_plan(store, proposed)
        validate_plan(plan)
    except (ValueError, StateIntegrityError, TypeError) as exc:
        store.append_event("plan_rejected", "untrusted mailbox plan rejected", {"error": type(exc).__name__})
        return False
    current = store.load_plan()
    if current.get("authority") == "nightwatch" and current["milestones"] != [{"id": "M1", "title": "Complete the goal", "weight": 100, "required": True, "status": "pending", "verification_profile": "default" if store.load_policy()["final_commands"] else "none", "evidence": []}]:
        store.append_event("plan_change_ignored", "later untrusted plan replacement ignored")
        return False
    store.save_plan(plan)
    store.mutate("plan_adopted", "validated untrusted milestone structure adopted", lambda state: {**state, "plan_ready": True})
    return True


def ingest_progress(store: NightwatchStore) -> bool:
    try:
        raw = read_mailbox_json(store, "progress.json")
    except ValueError as exc:
        store.append_event("progress_rejected", "untrusted mailbox progress rejected", {"error": type(exc).__name__})
        return False
    if raw is None:
        return False
    updates = raw.get("milestones") if isinstance(raw, dict) and set(raw) == {"milestones"} else None
    if not isinstance(updates, list) or len(updates) > MAX_MILESTONES:
        store.append_event("progress_rejected", "untrusted mailbox progress shape rejected")
        return False
    plan = store.load_plan()
    by_id = {item["id"]: item for item in plan["milestones"]}
    changed = False
    for update in updates:
        if not isinstance(update, dict) or set(update) - {"id", "status", "reason"} or update.get("id") not in by_id:
            continue
        requested = update.get("status")
        item = by_id[update["id"]]
        if requested in {"working", "implemented", "blocked"} and item["status"] != "verified":
            if item["status"] != requested:
                item["status"] = requested
                changed = True
            if requested == "blocked" and isinstance(update.get("reason"), str):
                item.setdefault("evidence", []).append({"at": _now(), "kind": "untrusted_blocker", "detail": redact(update["reason"][:MAX_TITLE])})
        elif requested == "verified":
            store.append_event("model_reported_verified", "model cannot verify a milestone", {"id": update["id"]})
            if item["status"] != "verified":
                item["status"] = "implemented"
                changed = True
    if changed:
        store.save_plan(plan)
        store.append_event("progress_ingested", "untrusted implementation progress ingested")
    return changed


def trusted_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "CODEX_HOME"}
    return {key: value for key, value in os.environ.items() if key in allowed or key.startswith("LC_") or key.startswith("FAKE_CODEX_")}


def _run_trusted_command(root: Path, command: str, timeout: float = 120.0) -> dict[str, Any]:
    started = _now()
    if command.strip() == "git diff --check":
        ok, output = diff_check(root)
        return {"command": command, "ok": ok, "output": redact(output), "started_at": started, "finished_at": _now()}
    try:
        result = subprocess.run(["/bin/sh", "-lc", command], cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout, check=False, env=trusted_environment())
        return {"command": command, "ok": result.returncode == 0, "returncode": result.returncode, "output": redact(result.stdout[-4000:]), "started_at": started, "finished_at": _now()}
    except subprocess.TimeoutExpired:
        return {"command": command, "ok": False, "output": "verification timed out", "started_at": started, "finished_at": _now()}
    except OSError as exc:
        return {"command": command, "ok": False, "output": f"verification failed: {type(exc).__name__}", "started_at": started, "finished_at": _now()}


def verify_milestones(store: NightwatchStore, git: GitSnapshot | None = None) -> dict[str, Any]:
    plan = store.load_plan()
    commands = store.load_policy()["final_commands"]
    results: list[dict[str, Any]] = []
    changed = False
    for item in plan["milestones"]:
        if item["status"] not in {"working", "implemented"} or item["verification_profile"] != "default" or not commands:
            continue
        checks = [_run_trusted_command(store.repo, command) for command in commands]
        results.extend({"id": item["id"], **check} for check in checks)
        if all(check["ok"] for check in checks):
            item["status"] = "verified"
            item.setdefault("evidence", []).append({"at": _now(), "kind": "trusted_policy_commands", "policy_hash": plan["policy_hash"], "checks": checks})
            changed = True
            store.append_event("milestone_verified", "trusted policy checks passed", {"id": item["id"]})
    if changed:
        store.save_plan(plan)
    final_checks = [_run_trusted_command(store.repo, command) for command in commands]
    return {"milestones": results, "final_checks": final_checks, "all_milestones_verified": all(item["status"] == "verified" for item in plan["milestones"] if item["required"]), "all_final_checks_passed": bool(final_checks) and all(check["ok"] for check in final_checks), "progress": plan_progress(plan), "git": (git or snapshot(store.repo)).to_dict()}


def current_milestone(store: NightwatchStore) -> dict[str, Any] | None:
    plan = store.load_plan()
    return next((item for item in plan["milestones"] if item["status"] in {"working", "implemented", "pending"}), None)
