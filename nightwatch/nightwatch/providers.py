from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import selectors
import shutil
import signal as signal_module
import sqlite3
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .models import (
    ErrorKind,
    ProviderResult,
    QuotaSnapshot,
    QuotaWindow,
    validate_model_name,
    validate_reasoning_effort,
)
from .process_identity import linux_process_identity, process_identity_matches, sys_platform_linux
from .storage import NightwatchStore, redact
from .testing import crash_hook


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_epoch(text: str | None) -> int | None:
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


class ProviderAdapter(ABC):
    """Abstract base seam decoupling Nightwatch supervisor from model CLI providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider slug: 'codex' or 'agy'."""

    @abstractmethod
    def build_command(
        self,
        repo: str | Path,
        thread_id: str | None,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[list[str], str]:
        """Construct execution argv and action ('start' or 'resume')."""

    @abstractmethod
    def run_turn(
        self,
        store: NightwatchStore,
        generation: int,
        prompt: str,
        thread_id: str | None = None,
        on_spawn: Callable[[int, str], None] | None = None,
        on_thread: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ProviderResult:
        """Execute a single supervisor turn with streaming capture."""

    @abstractmethod
    def auth_sanity(self, binary: str | None = None) -> bool:
        """Verify that credentials and CLI login status are healthy."""

    @abstractmethod
    def probe_quota(self, store: NightwatchStore | None = None, repo: Path | None = None) -> QuotaSnapshot:
        """Probe authoritative quota / rate-limit state."""

    @abstractmethod
    def list_models(self) -> list[dict[str, Any]]:
        """Return list of allowlisted models for this provider."""

    @abstractmethod
    def default_model(self) -> str:
        """Default model slug when not explicitly specified."""

    @abstractmethod
    def validate_model(self, model: str) -> str:
        """Validate model slug."""

    @abstractmethod
    def validate_reasoning_effort(self, effort: str) -> str:
        """Validate reasoning effort."""

    @abstractmethod
    def supports_auto_pool(self) -> bool:
        """Whether provider supports multi-account pool rotation."""

    @abstractmethod
    def find_active_processes(self, repo: str | Path, exclude_pid: int | None = None) -> list[dict[str, Any]]:
        """Find running provider processes associated with repository."""

    @abstractmethod
    def find_active_threads_for_repo(self, repo: str | Path) -> list[dict[str, Any]]:
        """Find previous threads/sessions for this repo from provider storage."""

    @abstractmethod
    def doctor_check(self, repo: Path | None = None) -> dict[str, Any]:
        """Check provider installation and status for nightwatch doctor."""


class CodexProviderAdapter(ProviderAdapter):
    """Adapter for OpenAI Codex CLI provider."""

    @property
    def name(self) -> str:
        return "codex"

    def build_command(
        self,
        repo: str | Path,
        thread_id: str | None,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[list[str], str]:
        from .codex import build_command as _codex_build_command
        return _codex_build_command(repo, thread_id, prompt, model, reasoning_effort)

    def run_turn(
        self,
        store: NightwatchStore,
        generation: int,
        prompt: str,
        thread_id: str | None = None,
        on_spawn: Callable[[int, str], None] | None = None,
        on_thread: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ProviderResult:
        from .codex import run_codex as _run_codex
        return _run_codex(
            store,
            generation,
            prompt,
            thread_id,
            on_spawn,
            on_thread,
            stop_event,
            timeout,
            codex_home=kwargs.get("codex_home"),
            lease_fd=kwargs.get("lease_fd"),
            account_fingerprint=kwargs.get("account_fingerprint"),
        )

    def auth_sanity(self, binary: str | None = None) -> bool:
        if os.environ.get("NIGHTWATCH_SKIP_AUTH_CHECK") == "1":
            return True
        bin_path = binary or os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
        try:
            result = subprocess.run(
                [bin_path, "login", "status"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def probe_quota(self, store: NightwatchStore | None = None, repo: Path | None = None) -> QuotaSnapshot:
        from .quota import make_quota_provider
        return make_quota_provider().read()

    def list_models(self) -> list[dict[str, Any]]:
        from .operations import list_models as _list_models
        return _list_models()

    def default_model(self) -> str:
        return "gpt-5.6-sol"

    def validate_model(self, model: str) -> str:
        return validate_model_name(model)

    def validate_reasoning_effort(self, effort: str) -> str:
        return validate_reasoning_effort(effort)

    def supports_auto_pool(self) -> bool:
        return True

    def find_active_processes(self, repo: str | Path, exclude_pid: int | None = None) -> list[dict[str, Any]]:
        if not sys_platform_linux():
            return []
        target = Path(repo).resolve()
        found: list[dict[str, Any]] = []
        proc_dir = Path("/proc")
        if not proc_dir.exists():
            return []
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if exclude_pid and pid == exclude_pid:
                continue
            try:
                cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
                if cwd == target:
                    exe = os.readlink(f"/proc/{pid}/exe")
                    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").lower()
                    is_codex = ("codex" in exe.lower() or "codex" in cmdline) and "nightwatch" not in exe.lower() and "nightwatch" not in cmdline and "pytest" not in cmdline and "unittest" not in cmdline
                    if is_codex:
                        found.append({"pid": pid, "executable": exe, "cmdline": cmdline, "cwd": str(cwd)})
            except (OSError, ValueError):
                continue
        return found

    def find_active_threads_for_repo(self, repo: str | Path) -> list[dict[str, Any]]:
        target = str(Path(repo).resolve())
        db_path = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "state_5.sqlite"
        if not db_path.exists():
            return []
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, rollout_path, updated_at_ms, model, git_branch, thread_source, first_user_message, title "
                    "FROM threads WHERE cwd = ? ORDER BY updated_at_ms DESC LIMIT 10",
                    (target,),
                )
                rows = cursor.fetchall()
                return [
                    {
                        "id": row[0],
                        "rollout_path": row[1],
                        "updated_at_ms": row[2],
                        "model": row[3],
                        "git_branch": row[4],
                        "thread_source": row[5],
                        "first_user_message": row[6],
                        "title": row[7],
                    }
                    for row in rows
                ]
        except (sqlite3.DatabaseError, OSError):
            return []

    def doctor_check(self, repo: Path | None = None) -> dict[str, Any]:
        binary = os.environ.get("NIGHTWATCH_CODEX_BIN", "codex")
        bin_path = shutil.which(binary) or (binary if Path(binary).is_file() else None)
        version = None
        if bin_path:
            try:
                res = subprocess.run([bin_path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False)
                version = res.stdout.strip() if res.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                pass
        auth_ok = self.auth_sanity(bin_path) if bin_path else False
        return {
            "binary": bin_path,
            "version": version,
            "auth_ok": auth_ok,
            "status": "ok" if bin_path and auth_ok else "fail",
        }


class AgyProviderAdapter(ProviderAdapter):
    """Adapter for Google Antigravity (AGY) CLI provider."""

    DEFAULT_AGY_MODELS = [
        {"slug": "gemini-3.8-flash-high", "display_name": "Gemini 3.8 Flash (High)", "default_reasoning_level": "high", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.8-flash-medium", "display_name": "Gemini 3.8 Flash (Medium)", "default_reasoning_level": "medium", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.8-flash-low", "display_name": "Gemini 3.8 Flash (Low)", "default_reasoning_level": "low", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.7-flash-high", "display_name": "Gemini 3.7 Flash (High)", "default_reasoning_level": "high", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.7-flash-medium", "display_name": "Gemini 3.7 Flash (Medium)", "default_reasoning_level": "medium", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.7-flash-low", "display_name": "Gemini 3.7 Flash (Low)", "default_reasoning_level": "low", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.6-flash-high", "display_name": "Gemini 3.6 Flash (High)", "default_reasoning_level": "high", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.6-flash-medium", "display_name": "Gemini 3.6 Flash (Medium)", "default_reasoning_level": "medium", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.6-flash-low", "display_name": "Gemini 3.6 Flash (Low)", "default_reasoning_level": "low", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)", "default_reasoning_level": "high", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gemini-3.1-pro-low", "display_name": "Gemini 3.1 Pro (Low)", "default_reasoning_level": "low", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6 (Thinking)", "default_reasoning_level": "high", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "claude-opus-4-6-thinking", "display_name": "Claude Opus 4.6 (Thinking)", "default_reasoning_level": "high", "supported_reasoning_levels": ["low", "medium", "high"]},
        {"slug": "gpt-oss-120b-medium", "display_name": "GPT-OSS 120B (Medium)", "default_reasoning_level": "medium", "supported_reasoning_levels": ["low", "medium", "high"]},
    ]

    @property
    def name(self) -> str:
        return "agy"

    def _resolve_binary(self) -> str:
        override = os.environ.get("NIGHTWATCH_AGY_BIN")
        if override:
            return override
        system_bin = shutil.which("agy")
        if system_bin:
            return system_bin
        fallback = Path.home() / ".local" / "bin" / "agy"
        if fallback.is_file():
            return str(fallback)
        return "agy"

    def build_command(
        self,
        repo: str | Path,
        thread_id: str | None,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[list[str], str]:
        binary = self._resolve_binary()
        args = [
            binary,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
        ]
        if model:
            args.extend(["--model", self.validate_model(model)])
        if reasoning_effort:
            args.extend(["--effort", self.validate_reasoning_effort(reasoning_effort)])
        if thread_id:
            # Deterministic exact conversation identity only. Never use heuristic -c/--continue.
            args.extend(["--conversation", thread_id])
            action = "resume"
        else:
            action = "start"
        args.extend(["-p", prompt])
        return args, action

    def run_turn(
        self,
        store: NightwatchStore,
        generation: int,
        prompt: str,
        thread_id: str | None = None,
        on_spawn: Callable[[int, str], None] | None = None,
        on_thread: Callable[[str], None] | None = None,
        stop_event: threading.Event | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> ProviderResult:
        state = store.load_state()
        args, action = self.build_command(
            store.repo,
            thread_id,
            prompt,
            model=state.get("model"),
            reasoning_effort=state.get("reasoning_effort"),
        )
        run_log = store.runs_path / f"generation-{generation}.stderr.log"
        store.runs_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Redact prompt from recorded command
        store.write_run_event(generation, {"type": "provider_command", "provider": "agy", "action": action, "argv": [item for item in args if item != prompt]})
        start = time.monotonic()
        try:
            environment = dict(os.environ)
            process = subprocess.Popen(
                args,
                cwd=str(store.repo),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            return ProviderResult(
                None, None, thread_id, 0, 0,
                error_kind=ErrorKind.UNKNOWN,
                error_detail=f"AGY spawn failed: {type(exc).__name__}",
                run_log=str(run_log),
            )
        if on_spawn:
            on_spawn(process.pid, action)

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
        conversation_not_found = False
        result_status: str | None = None

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
            event_type = str(event.get("event", "unknown"))
            event_types.append(event_type)
            store.write_run_event(generation, {"type": f"agy_{event_type}", "event": event})

            # Extract conversation_id from event
            cid = event.get("conversation_id")
            if not cid and isinstance(event.get("init"), dict):
                cid = event["init"].get("conversation_id")
            if not cid and isinstance(event.get("step_update"), dict):
                cid = event["step_update"].get("conversation_id")
            if not cid and isinstance(event.get("result"), dict):
                cid = event["result"].get("conversation_id")

            if cid and isinstance(cid, str) and cid.strip():
                cid = cid.strip()
                if action == "resume" and thread_id:
                    if cid != thread_id:
                        # Exact conversation rule violation: AGY emitted a different conversation ID!
                        store.write_run_event(generation, {"type": "thread_id_mismatch", "expected": thread_id, "observed": cid})
                        found_thread = "__MISMATCH__"
                elif not found_thread:
                    found_thread = cid
                    if on_thread:
                        on_thread(cid)

            if event_type == "result" and isinstance(event.get("result"), dict):
                result_status = event["result"].get("status")

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

        signal_name = None
        if returncode < 0:
            try:
                signal_name = signal_module.Signals(-returncode).name
            except ValueError:
                signal_name = f"SIG{-returncode}"

        try:
            if process.stdout is not None and hasattr(process.stdout, "close"):
                process.stdout.close()
            if process.stderr is not None and hasattr(process.stderr, "close"):
                process.stderr.close()
        except OSError:
            pass

        if thread_id and f"conversation \"{thread_id.lower()}\" not found" in stderr.lower():
            conversation_not_found = True
            found_thread = "__MISMATCH__"

        if found_thread == "__MISMATCH__":
            kind = ErrorKind.STATE
            detail = f"AGY conversation mismatch: expected exact thread {thread_id}" if not conversation_not_found else f"AGY conversation {thread_id} not found"
            return ProviderResult(returncode, signal_name, None, len(events), malformed, event_types, kind, detail, run_log=str(run_log))

        # Classify failures
        kind: ErrorKind | None = None
        detail: str | None = None
        reset_at: int | None = None
        reset_source: str | None = None
        windows: list[QuotaWindow] = []
        blocker: str | None = None

        combined_text = stderr + " " + " ".join(json.dumps(ev) for ev in events)
        lower = combined_text.lower()

        # Check for quota / rate limits
        if any(token in lower for token in ("resource_exhausted", "quota exceeded", "rate limit", "429", "usage limit")):
            # Probe authoritative quota to get accurate reset times
            try:
                snap = self.probe_quota(store, store.repo)
                windows = snap.windows()
                for w in windows:
                    if w.resets_at and (reset_at is None or w.resets_at > reset_at):
                        reset_at = w.resets_at
                        reset_source = "agy_usage_probe"
            except Exception:
                pass
            is_weekly = "weekly" in lower or any(w.name == "weekly" and w.exhausted for w in windows)
            kind = ErrorKind.QUOTA_WEEKLY if is_weekly else ErrorKind.QUOTA_5H
            detail = "AGY quota limit reached"
        elif any(token in lower for token in ("unauthorized", "authentication", "invalid token", "login required", "401", "403 forbidden")):
            kind = ErrorKind.AUTH
            detail = "AGY authentication failure"
        elif result_status == "ERROR" or returncode != 0 or signal_name:
            kind = ErrorKind.CRASH
            detail = f"AGY exited {returncode or signal_name}"
        elif malformed:
            kind = ErrorKind.MALFORMED
            detail = f"AGY stdout contained {malformed} malformed JSONL event(s)"
        elif not found_thread and action == "start":
            kind = ErrorKind.STATE
            detail = "AGY did not emit an init event with conversation_id"

        aborted = bool(stop_event and stop_event.is_set())
        return ProviderResult(
            returncode,
            signal_name,
            found_thread,
            len(events),
            malformed,
            event_types,
            kind,
            detail,
            reset_at,
            reset_source,
            windows,
            blocker,
            aborted,
            str(run_log),
        )

    def auth_sanity(self, binary: str | None = None) -> bool:
        if os.environ.get("NIGHTWATCH_SKIP_AUTH_CHECK") == "1":
            return True
        token_path = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        if not token_path.is_file():
            return False
        try:
            raw = token_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict) or not data.get("token"):
                return False
        except (OSError, json.JSONDecodeError):
            return False
        bin_path = binary or self._resolve_binary()
        try:
            res = subprocess.run(
                [bin_path, "--output-format", "stream-json", "-p", "/usage"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
                check=False,
            )
            return res.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def probe_quota(self, store: NightwatchStore | None = None, repo: Path | None = None) -> QuotaSnapshot:
        binary = self._resolve_binary()
        read_at = _utc_now_iso()
        try:
            res = subprocess.run(
                [binary, "--output-format", "stream-json", "-p", "/usage"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return QuotaSnapshot("AGY_CLI", read_at, error=f"AGY quota probe timed out or failed: {type(exc).__name__}")
        if res.returncode != 0:
            return QuotaSnapshot("AGY_CLI", read_at, error=f"AGY /usage command failed with code {res.returncode}")

        parsed_data = None
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if item.get("event") == "command_result" and isinstance(item.get("command"), dict):
                    cmd = item["command"]
                    if cmd.get("name") == "usage" and isinstance(cmd.get("data"), dict):
                        parsed_data = cmd["data"]
                        break
            except json.JSONDecodeError:
                continue

        if not parsed_data or not isinstance(parsed_data.get("groups"), list):
            return QuotaSnapshot("AGY_CLI", read_at, error="AGY /usage returned unexpected response format")

        primary_window: QuotaWindow | None = None
        secondary_window: QuotaWindow | None = None

        for group in parsed_data["groups"]:
            if not isinstance(group, dict):
                continue
            for bucket in group.get("buckets", []):
                if not isinstance(bucket, dict):
                    continue
                win = bucket.get("window")
                rem_frac = bucket.get("remaining_fraction")
                reset_iso = bucket.get("reset_time")
                reset_epoch = _parse_iso_epoch(reset_iso)
                used_pct = round((1.0 - float(rem_frac)) * 100, 1) if isinstance(rem_frac, (int, float)) else None
                if win == "5h" and not primary_window:
                    primary_window = QuotaWindow("5h", used_pct, 300, reset_epoch)
                elif win == "weekly" and not secondary_window:
                    secondary_window = QuotaWindow("weekly", used_pct, 10080, reset_epoch)

        return QuotaSnapshot(
            source="AGY_CLI",
            read_at=read_at,
            primary=primary_window,
            secondary=secondary_window,
            plan_type="Google Antigravity",
        )

    def list_models(self) -> list[dict[str, Any]]:
        binary = self._resolve_binary()
        try:
            res = subprocess.run(
                [binary, "models"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                found: list[dict[str, Any]] = []
                for line in res.stdout.splitlines():
                    parts = line.strip().split(maxsplit=1)
                    if parts:
                        slug = parts[0]
                        try:
                            valid_slug = self.validate_model(slug)
                            disp = parts[1] if len(parts) > 1 else slug
                            found.append({
                                "slug": valid_slug,
                                "display_name": disp,
                                "default_reasoning_level": "medium",
                                "supported_reasoning_levels": ["low", "medium", "high"],
                            })
                        except ValueError:
                            continue
                if found:
                    return found
        except (OSError, subprocess.TimeoutExpired):
            pass
        return list(self.DEFAULT_AGY_MODELS)

    def default_model(self) -> str:
        return "gemini-3.8-flash-medium"

    def validate_model(self, model: str) -> str:
        return validate_model_name(model)

    def validate_reasoning_effort(self, effort: str) -> str:
        level = validate_reasoning_effort(effort).lower()
        if level not in {"low", "medium", "high"}:
            raise ValueError(f"AGY reasoning effort must be one of low, medium, high (got {effort!r})")
        return level

    def supports_auto_pool(self) -> bool:
        return False

    def find_active_processes(self, repo: str | Path, exclude_pid: int | None = None) -> list[dict[str, Any]]:
        if not sys_platform_linux():
            return []
        target = Path(repo).resolve()
        found: list[dict[str, Any]] = []
        proc_dir = Path("/proc")
        if not proc_dir.exists():
            return []
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if exclude_pid and pid == exclude_pid:
                continue
            try:
                cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
                if cwd == target:
                    exe = os.readlink(f"/proc/{pid}/exe")
                    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").lower()
                    is_agy = ("agy" in exe.lower() or "agy " in cmdline or "antigravity" in exe.lower()) and "nightwatch" not in exe.lower() and "nightwatch" not in cmdline
                    if is_agy:
                        found.append({"pid": pid, "executable": exe, "cmdline": cmdline, "cwd": str(cwd)})
            except (OSError, ValueError):
                continue
        return found

    def find_active_threads_for_repo(self, repo: str | Path) -> list[dict[str, Any]]:
        target = str(Path(repo).resolve())
        db_path = Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db"
        if not db_path.exists():
            return []
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT conversation_id, title, last_modified_time, workspace_uris, agent_name "
                    "FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 50"
                )
                rows = cursor.fetchall()
                matching = []
                for row in rows:
                    uris = row[3] or ""
                    if target in uris:
                        matching.append({
                            "id": row[0],
                            "title": row[1],
                            "updated_at": row[2],
                            "workspace_uris": row[3],
                            "agent_name": row[4],
                        })
                return matching
        except (sqlite3.DatabaseError, OSError):
            return []

    def doctor_check(self, repo: Path | None = None) -> dict[str, Any]:
        bin_path = shutil.which(self._resolve_binary()) or (self._resolve_binary() if Path(self._resolve_binary()).is_file() else None)
        version = None
        if bin_path:
            try:
                res = subprocess.run([bin_path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False)
                version = res.stdout.strip() if res.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                pass
        auth_ok = self.auth_sanity(bin_path) if bin_path else False
        quota_data = None
        if auth_ok:
            try:
                snap = self.probe_quota(repo=repo)
                quota_data = snap.to_dict()
            except Exception:
                pass
        return {
            "binary": bin_path,
            "version": version,
            "auth_ok": auth_ok,
            "quota": quota_data,
            "status": "ok" if bin_path and auth_ok else "fail",
        }


_REGISTRY: dict[str, ProviderAdapter] = {
    "codex": CodexProviderAdapter(),
    "agy": AgyProviderAdapter(),
}


def get_provider_adapter(name: str = "codex") -> ProviderAdapter:
    normalized = (name or "codex").strip().lower()
    if normalized not in _REGISTRY:
        raise ValueError(f"unknown provider: {name!r}; supported providers: {sorted(_REGISTRY.keys())}")
    return _REGISTRY[normalized]
