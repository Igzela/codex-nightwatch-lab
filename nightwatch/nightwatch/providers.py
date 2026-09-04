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
    parse_agy_duration_seconds,
    validate_agy_print_timeout,
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
        **kwargs: Any,
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
    def probe_quota(
        self,
        store: NightwatchStore | None = None,
        repo: Path | None = None,
        model: str | None = None,
    ) -> QuotaSnapshot:
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
        **kwargs: Any,
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

    def probe_quota(
        self,
        store: NightwatchStore | None = None,
        repo: Path | None = None,
        model: str | None = None,
    ) -> QuotaSnapshot:
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


def agy_model_family(model: str) -> str:
    """Return 'gemini' or '3p' based on model name slug.

    Gemini family: gemini-*
    Third-party family: claude-*, gpt-*, gpt-oss-*
    Unknown model family: raises ValueError (fail closed).
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"unknown AGY model family for model {model!r}")
    m = model.strip().lower()
    if m.startswith("gemini-"):
        return "gemini"
    if m.startswith(("claude-", "gpt-", "gpt-oss-")):
        return "3p"
    raise ValueError(f"unknown AGY model family for model {model!r}")


def _abort_process(process: subprocess.Popen, grace_seconds: float = 0.15) -> int:
    """Escalate SIGINT -> SIGTERM -> SIGKILL to immediately terminate and reap child."""
    if process.poll() is not None:
        return process.returncode
    try:
        process.send_signal(signal_module.SIGINT)
    except OSError:
        pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < grace_seconds:
        if process.poll() is not None:
            return process.returncode
        time.sleep(0.02)
    try:
        process.terminate()
    except OSError:
        pass
    t0 = time.monotonic()
    while time.monotonic() - t0 < grace_seconds:
        if process.poll() is not None:
            return process.returncode
        time.sleep(0.02)
    try:
        process.kill()
    except OSError:
        pass
    try:
        return process.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, OSError):
        return process.poll() or -9


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
        print_timeout: str | None = None,
        **kwargs: Any,
    ) -> tuple[list[str], str]:
        # OWNER_APPROVED_UNATTENDED_AGY_PERMISSION_POLICY:
        # The repository owner has explicitly approved Nightwatch's unattended AGY policy of
        # invoking --dangerously-skip-permissions for supervised AGY provider turns.
        binary = self._resolve_binary()
        args = [
            binary,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
        ]
        timeout_val = print_timeout or "60m"
        validate_agy_print_timeout(timeout_val)
        args.extend(["--print-timeout", timeout_val])
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
        timeout_str = state.get("agy_print_timeout") or "60m"
        args, action = self.build_command(
            store.repo,
            thread_id,
            prompt,
            model=state.get("model"),
            reasoning_effort=state.get("reasoning_effort"),
            print_timeout=timeout_str,
        )
        budget_seconds = float(parse_agy_duration_seconds(timeout_str))
        watchdog_limit = timeout if timeout is not None else (budget_seconds + 30.0)
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
        stderr_lines: list[str] = []

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
        mismatch_detected = False
        mismatch_expected: str | None = None
        mismatch_observed: str | None = None
        has_init = False
        init_conv_id: str | None = None
        has_result_event = False
        last_result_event: dict[str, Any] | None = None
        result_status: str | None = None
        timed_out = False

        stdout_selector = selectors.DefaultSelector()
        stdout_open = process.stdout is not None
        if process.stdout is not None:
            stdout_selector.register(process.stdout, selectors.EVENT_READ)

        def _drain_stderr() -> None:
            nonlocal conversation_not_found, mismatch_detected, mismatch_expected, mismatch_observed
            while not stderr_queue.empty():
                try:
                    s_line = stderr_queue.get_nowait()
                    stderr_lines.append(s_line)
                    if thread_id and f'conversation "{thread_id.lower()}" not found' in s_line.lower():
                        conversation_not_found = True
                        mismatch_detected = True
                        mismatch_expected = thread_id
                        mismatch_observed = "(conversation not found in stderr)"
                except queue.Empty:
                    break

        while stdout_open:
            if process.poll() is not None and not stdout_open:
                break
            if stop_event and stop_event.is_set() and process.poll() is None:
                try:
                    process.send_signal(signal_module.SIGINT)
                except OSError:
                    pass
            if time.monotonic() - start > watchdog_limit and process.poll() is None:
                timed_out = True
                _abort_process(process)
                break
            _drain_stderr()
            if mismatch_detected:
                found_thread = "__MISMATCH__"
                store.write_run_event(
                    generation,
                    {"type": "thread_id_mismatch", "expected": mismatch_expected, "observed": mismatch_observed},
                )
                _abort_process(process)
                if process.stdout is not None:
                    try:
                        stdout_selector.unregister(process.stdout)
                    except (KeyError, OSError):
                        pass
                stdout_open = False
                break

            ready = stdout_selector.select(0.1)
            if not ready:
                _drain_stderr()
                if mismatch_detected:
                    found_thread = "__MISMATCH__"
                    store.write_run_event(
                        generation,
                        {"type": "thread_id_mismatch", "expected": mismatch_expected, "observed": mismatch_observed},
                    )
                    _abort_process(process)
                    if process.stdout is not None:
                        try:
                            stdout_selector.unregister(process.stdout)
                        except (KeyError, OSError):
                            pass
                    stdout_open = False
                    break
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

            # Check for authoritative init event
            if event_type == "init":
                has_init = True
                init_data = event.get("init")
                cid_from_init = event.get("conversation_id")
                if not cid_from_init and isinstance(init_data, dict):
                    cid_from_init = init_data.get("conversation_id")
                if isinstance(cid_from_init, str) and cid_from_init.strip():
                    init_conv_id = cid_from_init.strip()
                    if action == "start":
                        if found_thread is None:
                            found_thread = init_conv_id
                            if on_thread:
                                on_thread(found_thread)
                        elif found_thread != init_conv_id:
                            mismatch_detected = True
                            mismatch_expected = found_thread
                            mismatch_observed = init_conv_id
                    elif action == "resume":
                        if thread_id and init_conv_id != thread_id:
                            mismatch_detected = True
                            mismatch_expected = thread_id
                            mismatch_observed = init_conv_id
                else:
                    store.write_run_event(generation, {"type": "invalid_init_event", "event": event})
                    if action == "start":
                        mismatch_detected = True
                        mismatch_expected = "(valid conversation ID in init)"
                        mismatch_observed = "(none)"

            # Check ANY event carrying conversation_id against the established identity
            explicit_cid = None
            if isinstance(event.get("conversation_id"), str) and event["conversation_id"].strip():
                explicit_cid = event["conversation_id"].strip()
            elif isinstance(event.get("step_update"), dict) and isinstance(event["step_update"].get("conversation_id"), str) and event["step_update"]["conversation_id"].strip():
                explicit_cid = event["step_update"]["conversation_id"].strip()
            elif isinstance(event.get("result"), dict) and isinstance(event["result"].get("conversation_id"), str) and event["result"]["conversation_id"].strip():
                explicit_cid = event["result"]["conversation_id"].strip()

            if explicit_cid:
                if action == "resume" and thread_id:
                    if explicit_cid != thread_id:
                        mismatch_detected = True
                        mismatch_expected = thread_id
                        mismatch_observed = explicit_cid
                elif action == "start":
                    if found_thread and found_thread != "__MISMATCH__":
                        if explicit_cid != found_thread:
                            mismatch_detected = True
                            mismatch_expected = found_thread
                            mismatch_observed = explicit_cid

            if mismatch_detected:
                found_thread = "__MISMATCH__"
                store.write_run_event(
                    generation,
                    {"type": "thread_id_mismatch", "expected": mismatch_expected, "observed": mismatch_observed},
                )
                _abort_process(process)
                if process.stdout is not None:
                    try:
                        stdout_selector.unregister(process.stdout)
                    except (KeyError, OSError):
                        pass
                stdout_open = False
                break

            if event_type == "result":
                has_result_event = True
                last_result_event = event
                if isinstance(event.get("result"), dict):
                    raw_status = event["result"].get("status")
                    result_status = str(raw_status).upper() if raw_status is not None else None

        try:
            stdout_selector.close()
        except OSError:
            pass

        returncode = process.wait()
        stderr_thread.join(timeout=2)
        while not stderr_queue.empty():
            try:
                stderr_lines.append(stderr_queue.get_nowait())
            except queue.Empty:
                break
        stderr = "".join(stderr_lines)
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

        if not conversation_not_found and thread_id and f"conversation \"{thread_id.lower()}\" not found" in stderr.lower():
            conversation_not_found = True
            found_thread = "__MISMATCH__"

        if found_thread == "__MISMATCH__":
            kind = ErrorKind.STATE
            detail = (
                f"AGY conversation {thread_id} not found"
                if conversation_not_found
                else f"AGY conversation mismatch: expected exact thread {mismatch_expected or thread_id}"
            )
            return ProviderResult(returncode, signal_name, None, len(events), malformed, event_types, kind, detail, run_log=str(run_log))


        # Classify failures with strict precedence: Quota/Auth > Start missing init > Result status / Crash
        kind: ErrorKind | None = None
        detail: str | None = None
        reset_at: int | None = None
        reset_source: str | None = None
        windows: list[QuotaWindow] = []
        blocker: str | None = None
        aborted = bool(stop_event and stop_event.is_set())

        error_parts = [stderr]
        if result_status == "ERROR":
            res_payload = last_result_event.get("result", {}) if isinstance(last_result_event, dict) else {}
            if isinstance(res_payload, dict):
                error_parts.append(str(res_payload.get("error") or ""))
                error_parts.append(str(res_payload.get("response") or ""))
        for ev in events:
            if isinstance(ev, dict) and ev.get("type") == "error":
                error_parts.append(str(ev.get("error") or ev.get("error_message") or ""))
        error_text = " ".join(error_parts)
        lower_err = error_text.lower()
        selected_model = state.get("model")

        is_quota = any(token in lower_err for token in ("resource_exhausted", "quota exceeded", "rate limit", "usage limit")) or bool(re.search(r"\b429\b", error_text))
        is_auth = any(token in lower_err for token in ("unauthorized", "authentication", "invalid token", "login required", "403 forbidden")) or bool(re.search(r"\b401\b", error_text))

        failed_turn = (
            returncode != 0
            or bool(signal_name)
            or (action == "start" and not has_init)
            or bool(malformed)
            or not has_result_event
            or result_status != "SUCCESS"
        )

        if timed_out:
            kind = ErrorKind.CRASH
            detail = f"AGY print-mode turn timed out after {watchdog_limit:.1f}s watchdog"
            store.write_run_event(
                generation,
                {"type": "agy_watchdog_timeout", "watchdog_limit": watchdog_limit, "print_timeout": timeout_str},
            )
        elif failed_turn and is_quota:
            try:
                snap = self.probe_quota(store, store.repo, model=selected_model)
                windows = snap.windows()
                for w in windows:
                    if w.resets_at and (reset_at is None or w.resets_at > reset_at):
                        reset_at = w.resets_at
                        reset_source = "agy_usage_probe"
            except Exception:
                pass
            is_weekly = "weekly" in lower_err or any(w.name == "weekly" and w.exhausted for w in windows)
            kind = ErrorKind.QUOTA_WEEKLY if is_weekly else ErrorKind.QUOTA_5H
            detail = "AGY quota limit reached"
        elif failed_turn and is_auth:
            kind = ErrorKind.AUTH
            detail = "AGY authentication failure"
        elif action == "start" and not has_init:
            kind = ErrorKind.STATE
            detail = "AGY start turn missing authoritative init event with conversation_id"
        elif malformed:
            kind = ErrorKind.MALFORMED
            detail = f"AGY stdout contained {malformed} malformed JSONL event(s)"
        elif has_result_event:
            if result_status in {"CANCELED", "INTERRUPTED"}:
                aborted = True
                kind = None
                detail = f"AGY turn was {result_status.lower()}"
            elif result_status in {"WAITING", "RUNNING"}:
                kind = ErrorKind.STATE
                detail = f"AGY emitted non-terminal result status: {result_status}"
            elif result_status == "ERROR":
                kind = ErrorKind.CRASH
                detail = f"AGY result status ERROR (exit code {returncode or signal_name})"
            elif result_status == "SUCCESS":
                if returncode != 0 or signal_name:
                    kind = ErrorKind.CRASH
                    detail = f"AGY emitted SUCCESS but exited with {returncode or signal_name}"
                else:
                    kind = None
                    detail = None
            else:
                kind = ErrorKind.STATE
                detail = f"AGY emitted unknown result status: {result_status}"
        else:
            if returncode != 0 or signal_name:
                kind = ErrorKind.CRASH
                detail = f"AGY process terminated with {returncode or signal_name} without result event"
            else:
                kind = ErrorKind.STATE
                detail = "AGY exited 0 without emitting a terminal result event"

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
        bin_path = binary or self._resolve_binary()
        try:
            res = subprocess.run(
                [bin_path, "--output-format", "stream-json", "-p", "/usage"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
            return res.returncode == 0 and any("command_result" in line for line in res.stdout.splitlines())
        except (OSError, subprocess.TimeoutExpired):
            return False

    def probe_quota(
        self,
        store: NightwatchStore | None = None,
        repo: Path | None = None,
        model: str | None = None,
    ) -> QuotaSnapshot:
        binary = self._resolve_binary()
        read_at = _utc_now_iso()

        target_model = model
        if target_model is None and store is not None and store.exists():
            target_model = store.load_state().get("model")
        if target_model is None:
            target_model = "gemini-3.8-flash-high"

        try:
            family = agy_model_family(target_model)
        except ValueError as exc:
            return QuotaSnapshot("AGY_CLI", read_at, error=f"authoritative quota unavailable: {exc}")

        try:
            res = subprocess.run(
                [binary, "--output-format", "stream-json", "-p", "/usage"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return QuotaSnapshot("AGY_CLI", read_at, error=f"AGY quota probe timed out or failed: {type(exc).__name__}")
        if res.returncode != 0:
            err_msg = (res.stderr or res.stdout or "").strip()
            err_msg_lower = err_msg.lower()
            if any(k in err_msg_lower for k in ("unauthorized", "authentication", "not authenticated", "login", "401", "403")) or " auth " in f" {err_msg_lower} ":
                return QuotaSnapshot("AGY_CLI", read_at, error=f"AGY /usage authentication failure: {err_msg}")
            return QuotaSnapshot("AGY_CLI", read_at, error=f"AGY /usage command failed with code {res.returncode}: {err_msg}")


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

        target_5h_id = "gemini-5h" if family == "gemini" else "3p-5h"
        target_weekly_id = "gemini-weekly" if family == "gemini" else "3p-weekly"

        primary_window: QuotaWindow | None = None
        secondary_window: QuotaWindow | None = None

        for group in parsed_data["groups"]:
            if not isinstance(group, dict):
                continue
            for bucket in group.get("buckets", []):
                if not isinstance(bucket, dict):
                    continue
                bid = bucket.get("id")
                if bid not in (target_5h_id, target_weekly_id):
                    continue
                rem_frac = bucket.get("remaining_fraction")
                if rem_frac is None or isinstance(rem_frac, bool) or not isinstance(rem_frac, (int, float)) or not (0.0 <= float(rem_frac) <= 1.0):
                    return QuotaSnapshot("AGY_CLI", read_at, error=f"malformed remaining_fraction in bucket {bid}: {rem_frac!r}")
                reset_iso = bucket.get("reset_time")
                reset_epoch = _parse_iso_epoch(reset_iso)
                used_pct = round((1.0 - float(rem_frac)) * 100, 1)

                win = bucket.get("window")
                if bid == target_5h_id or win == "5h":
                    primary_window = QuotaWindow("5h", used_pct, 300, reset_epoch)
                elif bid == target_weekly_id or win == "weekly":
                    secondary_window = QuotaWindow("weekly", used_pct, 10080, reset_epoch)

        if primary_window is None:
            return QuotaSnapshot("AGY_CLI", read_at, error=f"missing selected-family 5h bucket for family {family!r}")
        if secondary_window is None:
            return QuotaSnapshot("AGY_CLI", read_at, error=f"missing selected-family weekly bucket for family {family!r}")

        return QuotaSnapshot(
            source="AGY_CLI",
            read_at=read_at,
            primary=primary_window,
            secondary=secondary_window,
            plan_type=f"Google Antigravity ({family})",
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
        quota_data: dict[str, Any] = {}
        if auth_ok:
            try:
                gemini_snap = self.probe_quota(repo=repo, model="gemini-3.8-flash-high")
                quota_data["gemini"] = gemini_snap.to_dict()
            except Exception:
                pass
            try:
                tp_snap = self.probe_quota(repo=repo, model="claude-sonnet-4-6")
                quota_data["3p"] = tp_snap.to_dict()
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
