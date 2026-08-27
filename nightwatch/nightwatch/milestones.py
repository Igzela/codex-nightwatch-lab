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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_plan(raw: dict[str, Any]) -> dict[str, Any]:
    milestones = []
    for item in raw.get("milestones", []):
        if not isinstance(item, dict):
            continue
        commands = item.get("verification_commands", item.get("verification", []))
        if isinstance(commands, str):
            commands = [commands]
        milestones.append({
            "id": item.get("id"),
            "title": item.get("title", item.get("name", "")),
            "weight": item.get("weight", 1),
            "required": bool(item.get("required", True)),
            # Codex's claim is informational; only Nightwatch's verification can set verified.
            "status": "pending",
            "verification_commands": commands if isinstance(commands, list) else [],
            "evidence": [],
        })
    required = raw.get("required_verification_commands", raw.get("verification_commands", ["git diff --check"]))
    if isinstance(required, str):
        required = [required]
    return {
        "schema_version": 1,
        "authority": "nightwatch",
        "required_verification_commands": required if isinstance(required, list) else ["git diff --check"],
        "milestones": milestones,
    }


def adopt_proposed_plan(store: NightwatchStore) -> bool:
    proposed_path = store.directory / "proposed-plan.json"
    if not proposed_path.exists():
        return False
    try:
        proposed = json.loads(proposed_path.read_text(encoding="utf-8"))
        plan = _normalize_plan(proposed)
        validate_plan(plan)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        store.append_event("plan_rejected", "Codex proposed plan is invalid", {"error": type(exc).__name__})
        return False
    current = store.load_plan()
    if current.get("authority") == "nightwatch" and current.get("milestones") != plan.get("milestones"):
        # The first proposal is adopted once. Later edits need to be explicit and
        # cannot silently erase already verified evidence.
        store.append_event("plan_change_ignored", "ignoring later plan replacement after adoption")
        return False
    store.save_plan(plan)
    store.mutate("plan_adopted", "validated structured milestone plan adopted", lambda state: {**state, "plan_ready": True})
    return True


def ingest_progress(store: NightwatchStore) -> bool:
    if not store.load_state().get("plan_ready"):
        return False
    path = store.directory / "progress.json"
    if not path.exists():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updates = raw.get("milestones", raw) if isinstance(raw, dict) else raw
        if not isinstance(updates, list):
            raise ValueError("progress milestones must be an array")
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        store.append_event("progress_rejected", "progress file is invalid", {"error": type(exc).__name__})
        return False
    plan = store.load_plan()
    by_id = {item["id"]: item for item in plan["milestones"]}
    changed = False
    for update in updates:
        if not isinstance(update, dict) or update.get("id") not in by_id:
            continue
        item = by_id[update["id"]]
        requested = update.get("status")
        if requested in {"working", "implemented", "blocked"} and item.get("status") != "verified":
            if item.get("status") != requested:
                item["status"] = requested
                changed = True
            if requested == "blocked" and update.get("reason"):
                item.setdefault("evidence", []).append({"at": _now(), "kind": "blocker", "detail": redact(str(update["reason"]))})
        elif requested == "verified":
            # Explicitly record the model claim, but keep the authority invariant.
            store.append_event("model_reported_verified", "model cannot mark a milestone verified", {"id": update["id"]})
            if item.get("status") != "verified":
                item["status"] = "implemented"
                changed = True
    if changed:
        store.save_plan(plan)
        store.append_event("progress_ingested", "mechanical progress update ingested")
    return changed


def _run_command(root: Path, command: str, timeout: float = 120.0) -> dict[str, Any]:
    started = _now()
    if command.strip() == "git diff --check":
        ok, output = diff_check(root)
        return {"command": command, "ok": ok, "output": redact(output), "started_at": started, "finished_at": _now()}
    try:
        result = subprocess.run(
            ["/bin/sh", "-lc", command],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(os.environ),
        )
        return {"command": command, "ok": result.returncode == 0, "returncode": result.returncode, "output": redact(result.stdout[-4000:]), "started_at": started, "finished_at": _now()}
    except subprocess.TimeoutExpired:
        return {"command": command, "ok": False, "output": "verification timed out", "started_at": started, "finished_at": _now()}
    except OSError as exc:
        return {"command": command, "ok": False, "output": f"verification failed: {type(exc).__name__}", "started_at": started, "finished_at": _now()}


def verify_milestones(store: NightwatchStore, git: GitSnapshot | None = None) -> dict[str, Any]:
    plan = store.load_plan()
    results: list[dict[str, Any]] = []
    changed = False
    for item in plan["milestones"]:
        if item.get("status") not in {"working", "implemented"}:
            continue
        commands = list(item.get("verification_commands", []))
        if not commands:
            results.append({"id": item["id"], "ok": False, "output": "no verification command defined"})
            continue
        checks = [_run_command(store.repo, command) for command in commands]
        results.extend({"id": item["id"], **check} for check in checks)
        if all(check["ok"] for check in checks):
            item["status"] = "verified"
            item.setdefault("evidence", []).append({"at": _now(), "kind": "commands", "checks": checks})
            changed = True
            store.append_event("milestone_verified", "verification commands passed", {"id": item["id"], "checks": checks})
    if changed:
        store.save_plan(plan)
    required = [command for command in plan.get("required_verification_commands", []) if isinstance(command, str) and command.strip()]
    final_checks = [_run_command(store.repo, command) for command in required]
    result = {
        "milestones": results,
        "final_checks": final_checks,
        "all_milestones_verified": all(item.get("status") == "verified" for item in plan["milestones"] if item.get("required", True)),
        "all_final_checks_passed": bool(final_checks) and all(check["ok"] for check in final_checks),
        "progress": plan_progress(plan),
        "git": (git or snapshot(store.repo)).to_dict(),
    }
    return result


def current_milestone(store: NightwatchStore) -> dict[str, Any] | None:
    plan = store.load_plan()
    for item in plan["milestones"]:
        if item.get("status") in {"working", "implemented"}:
            return item
    for item in plan["milestones"]:
        if item.get("status") == "pending":
            return item
    return None
