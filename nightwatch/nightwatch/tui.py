from __future__ import annotations

import io
import json
import os
import stat
import sys
import textwrap
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .git import GitError, repo_root
from .models import TERMINAL_STATES, State, plan_progress
from .operations import (
    ActionResult,
    RunSpec,
    adopt_run,
    create_worktree,
    doctor_snapshot,
    list_models,
    queue_steer,
    resume_service,
    start_run,
    stop_run,
    validate_human_text,
)
from .milestones import read_mailbox_json
from .quota import make_quota_provider
from .storage import MAX_EVENT_BYTES, NightwatchStore, StateIntegrityError, control_plane_root
from .supervisor import build_report, list_adoptable_sessions, pid_alive, process_matches

MAX_GOAL_CHARS = 4_000
MAX_INSTRUCTION_CHARS = 4_000
MAX_VERIFY_COMMANDS = 20
MAX_DISCOVERED_RUNS = 1_000
MAX_ADOPT_GOAL_DISPLAY = 240


def adopt_goal_text(title: str | None, thread_id: str | None = None, max_chars: int = MAX_GOAL_CHARS) -> str:
    """Turn a Codex session title into a Nightwatch goal that always fits the validator."""
    fallback = "Supervise adopted conversation"
    if isinstance(thread_id, str) and thread_id.strip():
        fallback = f"Supervise adopted conversation ({thread_id.strip()})"
    raw = str(title or "").strip()
    if not raw:
        return fallback
    first = next((line.strip() for line in raw.splitlines() if line.strip()), raw)
    if len(first) <= max_chars:
        return first
    trimmed = first[: max(1, max_chars - 1)].rstrip()
    return f"{trimmed}…"[:max_chars]


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    usage: str
    mutates: bool = False


COMMANDS = (
    SlashCommand("/run", "Create a supervised goal with a guided preview", "/run [goal]", True),
    SlashCommand("/multi", "Show every trusted run/thread across repositories and worktrees", "/multi"),
    SlashCommand("/status", "Open the selected run's live status", "/status"),
    SlashCommand("/plan", "Show trusted milestone progress", "/plan"),
    SlashCommand("/logs", "Show the recent supervisor log", "/logs"),
    SlashCommand("/timeline", "Show sequence-validated lifecycle events", "/timeline"),
    SlashCommand("/explain", "Explain why the selected run is in its current state", "/explain"),
    SlashCommand("/thread", "Show exact thread and generation history", "/thread"),
    SlashCommand("/quota", "Show 5h/weekly usage, authority, and reset time", "/quota"),
    SlashCommand("/recap", "Show an evidence-grounded completion recap", "/recap"),
    SlashCommand("/report", "Write the durable acceptance report and show its path", "/report", True),
    SlashCommand("/models", "Show installed Codex models and reasoning levels", "/models"),
    SlashCommand("/doctor", "Run environment, auth, quota, and service diagnostics", "/doctor"),
    SlashCommand("/adopt", "Bind an existing exact Codex thread", "/adopt [thread]", True),
    SlashCommand("/steer", "Queue a confirmed instruction to the selected exact thread", "/steer [instruction]", True),
    SlashCommand("/resume", "Resume the selected run in its repo-specific user service", "/resume", True),
    SlashCommand("/stop", "Stop the selected run while preserving durable state", "/stop", True),
    SlashCommand("/help", "Show commands, keys, and trust semantics", "/help"),
    SlashCommand("/quit", "Exit the TUI without stopping agents", "/quit"),
    SlashCommand("/exit", "Exit the TUI without stopping agents", "/exit"),
)

COMMAND_ALIASES = {"new": "run", "exit": "quit"}
VIEW_COMMANDS = {"status", "plan", "logs", "timeline", "explain", "thread", "quota", "recap"}
HELP_TEXT = """COMMANDS AND KEYS
Type / to open the command menu. Filter by typing; ↑/↓ select; Enter runs the highlighted command.
Without / , text is a new goal or a steer to the selected active run. Mutating actions confirm first.

Esc closes the command menu, then a picker/confirm card, then a view, then clears the input.
Empty dashboard Esc does not quit. Leave with /quit, /exit, or Ctrl+C.

↑/↓ with an empty input selects a run. PgUp/PgDn scroll the current view.
Absolute paths such as /home/... are not commands.

/run [goal]     confirm a supervised goal (verify commands optional on the confirm card)
/adopt [thread] pick a live or recent Codex session; binds the exact thread without starting a writer
/steer [text]   queue an instruction to the selected exact thread
/resume         start unattended supervision after the interactive Codex process exits
/stop           stop supervision and keep durable state
/status /plan /logs /timeline /explain /thread /quota /recap /models /doctor /help /multi
"""


def is_slash_composer(text: str) -> bool:
    if not text.startswith("/"):
        return False
    first = text.split(maxsplit=1)[0]
    return "/" not in first[1:]


def slash_commands(prefix: str = "") -> list[SlashCommand]:
    clean = prefix.strip().lower().removeprefix("/")
    if not clean:
        return list(COMMANDS)
    prefix_hits = [item for item in COMMANDS if item.name.removeprefix("/").startswith(clean)]
    if prefix_hits:
        return prefix_hits
    return [
        item
        for item in COMMANDS
        if clean in item.name.removeprefix("/").lower() or clean in item.summary.lower()
    ]


def palette_prefix(value: str) -> str:
    parts = value.removeprefix("/").split(maxsplit=1)
    return parts[0] if parts else ""


@dataclass(frozen=True)
class Intent:
    kind: str
    argument: str | None = None
    requires_confirmation: bool = False


def route_input(text: str, has_active_run: bool) -> Intent:
    value = text.strip()
    if not value:
        return Intent("noop")
    if value == "/":
        return Intent("palette")
    if is_slash_composer(value):
        command, _, argument = value.partition(" ")
        name = COMMAND_ALIASES.get(command[1:].lower(), command[1:].lower())
        item = next((candidate for candidate in COMMANDS if candidate.name[1:] == name), None)
        if item is None:
            return Intent("error", f"Unknown command: {command}")
        return Intent(name, argument.strip() or None, item.mutates)
    return Intent("steer" if has_active_run else "run", value, True)


def terminal_safe(value: Any) -> str:
    """Neutralize terminal controls and invisible formatting from all display data."""
    text = str(value)
    return "".join(
        character
        if character in "\n\t" or unicodedata.category(character) not in {"Cc", "Cf"}
        else ""
        for character in text
    )


@dataclass
class OverlayItem:
    key: str
    title: str
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Overlay:
    kind: str
    title: str = ""
    body: str = ""
    items: list[OverlayItem] = field(default_factory=list)
    selected: int = 0
    mode: str = ""


@dataclass
class TuiHooks:
    discover_sessions: Callable[..., list[dict[str, Any]]] | None = None
    adopt: Callable[..., ActionResult] | None = None
    start_run: Callable[..., ActionResult] | None = None
    create_worktree: Callable[..., Path] | None = None
    queue_steer: Callable[..., ActionResult] | None = None
    resume: Callable[..., ActionResult] | None = None
    stop: Callable[..., ActionResult] | None = None
    run_exists: Callable[..., bool] | None = None
    list_models: Callable[..., list[dict[str, Any]]] | None = None
    doctor: Callable[..., dict[str, Any]] | None = None
    write_report: Callable[..., Path] | None = None


class TuiController:
    """Codex-style TUI state machine: persistent composer, slash menu, confirm cards."""

    def __init__(
        self,
        *,
        repo: Path | None = None,
        runs: list[Any] | None = None,
        hooks: TuiHooks | None = None,
    ):
        self.repo = Path(repo).expanduser() if repo is not None else None
        self.runs: list[Any] = list(runs or [])
        self.hooks = hooks or TuiHooks()
        self.selected = 0
        self.view = "dashboard"
        self.content: str | None = None
        self.composer = ""
        self.overlay: Overlay | None = None
        self.message = "Type / to discover commands."
        self.scroll = 0
        self.quit = False
        self.pending: dict[str, Any] | None = None
        self.awaiting: str | None = None
        self.catalog_errors: list[str] = []

    def handle_key(self, key: str) -> None:
        if self.quit:
            return
        if key == "ctrl-c":
            self.quit = True
            return
        if self.overlay is not None:
            self._handle_overlay_key(key)
            return
        if key == "esc":
            self._handle_esc()
            return
        if key == "up":
            if not self.composer:
                self.selected = max(0, self.selected - 1)
            return
        if key == "down":
            if not self.composer:
                self.selected = min(max(0, len(self.runs) - 1), self.selected + 1)
            return
        if key == "pageup":
            self.scroll = max(0, self.scroll - 10)
            return
        if key == "pagedown":
            self.scroll += 10
            return
        if key == "enter":
            self._submit()
            return
        if key == "backspace":
            self.composer = self.composer[:-1]
            return
        if len(key) == 1 and key.isprintable():
            if len(self.composer) < MAX_GOAL_CHARS:
                self.composer += key
            if is_slash_composer(self.composer):
                self._sync_slash_menu()

    def render(self, width: int = 80, height: int = 24) -> str:
        width = max(40, width)
        height = max(8, height)
        if self.content is not None:
            body = self.content
        else:
            body = render_dashboard(self.runs, self.selected, width, errors=self.catalog_errors)
        lines: list[str] = []
        for source in (terminal_safe(body).splitlines() or [""]):
            lines.extend(
                textwrap.wrap(source, max(20, width - 1), replace_whitespace=False, drop_whitespace=False) or [""]
            )
        overlay_lines = self._overlay_lines(width, height)
        footer = [terminal_safe(self._footer_message())[: max(1, width - 1)], f"Input › {self.composer}"[: max(1, width - 1)]]
        reserved = len(overlay_lines) + 2
        visible_h = max(1, height - reserved)
        start = min(self.scroll, max(0, len(lines) - visible_h))
        visible = lines[start : start + visible_h]
        while len(visible) < visible_h:
            visible.append("")
        return "\n".join(visible + overlay_lines + footer)

    def _overlay_lines(self, width: int, height: int) -> list[str]:
        overlay = self.overlay
        if overlay is None:
            return []
        out = [f"── {overlay.title} ──"[: width - 1]]
        if overlay.body:
            for line in overlay.body.splitlines() or [""]:
                out.extend(
                    textwrap.wrap(terminal_safe(line), max(20, width - 1), replace_whitespace=False, drop_whitespace=False)
                    or [""]
                )
        if overlay.items:
            limit = max(3, height - len(out) - 4)
            start = 0
            if overlay.selected >= start + limit:
                start = overlay.selected - limit + 1
            window = overlay.items[start : start + limit]
            for index, item in enumerate(window, start=start):
                mark = "▶" if index == overlay.selected else " "
                detail = f"  {item.detail}" if item.detail else ""
                out.append(f"{mark} {item.key:<12} {item.title}{detail}"[: width - 1])
        return out

    def _footer_message(self) -> str:
        overlay = self.overlay
        if overlay is None:
            return self.message
        if overlay.kind == "slash":
            return "↑/↓ select · Enter runs · Esc closes the menu"
        if overlay.kind == "picker":
            return "↑/↓ select a session · Enter continues · Esc cancels"
        if overlay.kind == "confirm":
            if overlay.mode == "worktree":
                return "Type a worktree label · Enter continues · Esc cancels"
            return "Empty Enter confirms · Esc cancels · type a verify command then Enter to add it"
        return self.message

    def _selected_run(self) -> Any | None:
        if not self.runs:
            return None
        return self.runs[min(max(0, self.selected), len(self.runs) - 1)]

    def _has_active(self) -> bool:
        run = self._selected_run()
        return bool(run is not None and getattr(run, "active", False))

    def _sync_slash_menu(self) -> None:
        if not is_slash_composer(self.composer):
            if self.overlay and self.overlay.kind == "slash":
                self.overlay = None
            return
        matches = slash_commands(palette_prefix(self.composer))
        selected = self.overlay.selected if self.overlay and self.overlay.kind == "slash" else 0
        items = [OverlayItem(item.name, item.summary, item.usage, {"mutates": item.mutates}) for item in matches]
        self.overlay = Overlay(
            kind="slash",
            title="COMMANDS",
            items=items,
            selected=min(selected, max(0, len(items) - 1)),
        )

    def _handle_esc(self) -> None:
        if self.view not in {"dashboard", "multi"}:
            self.view = "dashboard"
            self.content = None
            self.scroll = 0
            return
        if self.composer or self.awaiting:
            self.composer = ""
            self.awaiting = None
            return

    def _handle_overlay_key(self, key: str) -> None:
        overlay = self.overlay
        if overlay is None:
            return
        if overlay.kind == "slash":
            if key == "esc":
                self.overlay = None
                self.composer = ""
                return
            if key == "up":
                overlay.selected = max(0, overlay.selected - 1)
                return
            if key == "down":
                overlay.selected = min(max(0, len(overlay.items) - 1), overlay.selected + 1)
                return
            if key == "enter":
                self._submit_slash()
                return
            if key == "backspace":
                self.composer = self.composer[:-1]
                if not self.composer:
                    self.overlay = None
                else:
                    self._sync_slash_menu()
                return
            if len(key) == 1 and key.isprintable() and len(self.composer) < MAX_GOAL_CHARS:
                self.composer += key
                self._sync_slash_menu()
            return
        if overlay.kind == "picker":
            if key == "esc":
                self.overlay = None
                self.composer = ""
                return
            if key == "up":
                overlay.selected = max(0, overlay.selected - 1)
                return
            if key == "down":
                overlay.selected = min(max(0, len(overlay.items) - 1), overlay.selected + 1)
                return
            if key == "enter":
                typed = self.composer.strip()
                if typed and not typed.isdigit():
                    self._open_adopt_confirm(typed, None)
                    return
                if typed.isdigit() and overlay.items and 1 <= int(typed) <= len(overlay.items):
                    chosen = overlay.items[int(typed) - 1]
                elif overlay.items:
                    chosen = overlay.items[min(overlay.selected, len(overlay.items) - 1)]
                else:
                    self.message = "No conversation selected. Type an exact thread ID."
                    return
                thread = chosen.payload.get("thread_id")
                if not thread:
                    self.message = "Pick a conversation with a thread ID, or type the exact thread ID."
                    return
                self._open_adopt_confirm(str(thread), chosen.payload)
                return
            if key == "backspace":
                self.composer = self.composer[:-1]
                return
            if len(key) == 1 and key.isprintable() and len(self.composer) < MAX_GOAL_CHARS:
                self.composer += key
            return
        if overlay.kind == "confirm":
            if key == "esc":
                self.overlay = None
                self.pending = None
                self.composer = ""
                return
            if key == "enter":
                if overlay.mode == "worktree":
                    label = self.composer.strip()
                    if not label:
                        self.message = "Worktree label is required because this workspace already has a run."
                        return
                    if self.pending is not None:
                        self.pending["worktree"] = label
                    self.composer = ""
                    self._show_run_preview()
                    return
                typed = self.composer.strip()
                if typed == "/" or (is_slash_composer(typed) and typed.count("/") == 1):
                    self.composer = ""
                    self.message = "Slash commands are unavailable on this confirm card. Empty Enter confirms; Esc cancels."
                    return
                if self.pending and self.pending.get("kind") in {"run", "adopt"} and typed:
                    self._add_verify(typed)
                    self.composer = ""
                    self._refresh_confirm_body()
                    return
                self._commit_confirm()
                return
            if key == "backspace":
                self.composer = self.composer[:-1]
                return
            if len(key) == 1 and key.isprintable() and len(self.composer) < MAX_GOAL_CHARS:
                self.composer += key

    def _submit_slash(self) -> None:
        overlay = self.overlay
        composer = self.composer
        self.overlay = None
        self.composer = ""
        if overlay and overlay.items:
            chosen = overlay.items[min(overlay.selected, len(overlay.items) - 1)]
            kind = "quit" if chosen.key in {"/quit", "/exit"} else chosen.key[1:]
            argument = None
            if is_slash_composer(composer) and composer.strip() not in {"/", chosen.key}:
                parsed = route_input(composer, self._has_active())
                if parsed.kind == kind:
                    argument = parsed.argument
            mutates = bool(chosen.payload.get("mutates"))
            self._dispatch(Intent(kind, argument, mutates))
            return
        self._dispatch(route_input(composer, self._has_active()))

    def _submit(self) -> None:
        if self.awaiting == "run_goal":
            goal = self.composer.strip()
            self.composer = ""
            self.awaiting = None
            if goal:
                self._open_run_confirm(goal)
            return
        if self.awaiting == "steer":
            instruction = self.composer.strip()
            self.composer = ""
            self.awaiting = None
            if instruction:
                self._dispatch(Intent("steer", instruction, True))
            return
        text = self.composer
        if not text.strip():
            run = self._selected_run()
            if run is not None:
                self.view = "status"
                self.content = self._safe_view("status", run)
                self.scroll = 0
            return
        self.composer = ""
        self._dispatch(route_input(text, self._has_active()))

    def _dispatch(self, intent: Intent) -> None:
        if intent.kind == "noop":
            return
        if intent.kind == "quit":
            self.quit = True
            return
        if intent.kind == "error":
            self.message = intent.argument or "Unknown command"
            return
        if intent.kind in {"palette", "help"}:
            self.view = "help"
            self.content = HELP_TEXT
            self.scroll = 0
            return
        if intent.kind == "multi":
            self.view = "dashboard"
            self.content = None
            self.scroll = 0
            return
        if intent.kind == "run":
            if intent.argument:
                self._open_run_confirm(intent.argument)
            else:
                self.awaiting = "run_goal"
                self.message = "Type the overall goal, then Enter."
            return
        if intent.kind == "adopt":
            if intent.argument:
                self._open_adopt_confirm(intent.argument, None)
            else:
                self._open_adopt_picker()
            return
        if intent.kind == "models":
            self.view = "models"
            self.content = self._models_view()
            self.scroll = 0
            return
        if intent.kind == "doctor":
            self.view = "doctor"
            repo = (self._selected_run().repo if self._selected_run() is not None else self.repo) or Path.cwd()
            self.content = self._doctor_view(Path(repo))
            self.scroll = 0
            return
        if intent.kind == "steer":
            self._open_steer_confirm(intent.argument)
            return
        if intent.kind in VIEW_COMMANDS:
            run = self._selected_run()
            if run is None:
                self.message = "No run selected. Use /run or enter a natural-language goal."
                return
            self.view = intent.kind
            self.content = self._safe_view(intent.kind, run)
            self.scroll = 0
            return
        if intent.kind == "report":
            run = self._selected_run()
            if run is None:
                self.message = "No run selected. Use /run or enter a natural-language goal."
                return
            self.pending = {"kind": "report", "run": run}
            self.overlay = Overlay(
                kind="confirm",
                title="WRITE REPORT",
                body="Write a durable report from trusted state and verification evidence?",
            )
            return
        if intent.kind == "stop":
            run = self._selected_run()
            if run is None:
                self.message = "No run selected. Use /run or enter a natural-language goal."
                return
            self.pending = {"kind": "stop", "run": run}
            self.overlay = Overlay(
                kind="confirm",
                title="STOP RUN",
                body=f"Stop {run.state.get('run_id', run)} and preserve its exact thread/state?",
            )
            return
        if intent.kind == "resume":
            run = self._selected_run()
            if run is None:
                self.message = "No run selected. Use /run or enter a natural-language goal."
                return
            self.pending = {"kind": "resume", "run": run}
            self.overlay = Overlay(
                kind="confirm",
                title="RESUME RUN",
                body=f"Resume exact thread {run.thread_id or '(not captured)'} in its repo-specific user service?\nThis starts a writer only after no interactive Codex process is using the workspace.",
            )
            return
        self.message = f"Unknown command: {intent.kind}"

    def _safe_view(self, view: str, run: Any) -> str:
        try:
            return _CursesApp._view_content(view, run)
        except Exception as exc:
            return f"{view.upper()} unavailable: {type(exc).__name__}"

    def _models_view(self) -> str:
        try:
            models = self.hooks.list_models() if self.hooks.list_models else list_models()
        except RuntimeError as exc:
            return f"MODELS\n{exc}"
        return "MODELS · live installed Codex catalog\n" + "\n".join(
            f"{item['slug']:<24} default={item['default_reasoning_level']} · {','.join(item['supported_reasoning_levels'])}"
            for item in models
        )

    def _doctor_view(self, repo: Path) -> str:
        report = self.hooks.doctor(repo) if self.hooks.doctor else doctor_snapshot(repo)
        lines = [
            f"Nightwatch doctor: {report['status']}",
            f"Codex: {report.get('codex_version') or '(missing)'}",
            f"Auth: {report['auth']['status']}",
            f"Quota authority: {report['quota'].get('authority', 'unavailable')} ({report['quota'].get('source', 'none')})",
        ]
        if report["quota"].get("primary"):
            lines.append(f"5h: {report['quota']['primary'].get('used_percent')}% used, reset={report['quota']['primary'].get('resets_at')}")
        if report["quota"].get("secondary"):
            lines.append(f"weekly: {report['quota']['secondary'].get('used_percent')}% used, reset={report['quota']['secondary'].get('resets_at')}")
        lines.append(f"systemd-inhibit: {'available' if report['systemd_inhibit'] else 'unavailable'}")
        return "\n".join(lines)

    def _open_run_confirm(self, goal: str) -> None:
        repo = Path(self.repo or Path.cwd())
        exists_fn = self.hooks.run_exists
        if exists_fn is None:
            try:
                exists = NightwatchStore(repo).exists()
            except (OSError, StateIntegrityError, GitError):
                exists = False
        else:
            exists = bool(exists_fn(repo))
        self.pending = {"kind": "run", "goal": goal, "repo": repo, "verify": [], "model": None, "effort": None, "worktree": None}
        self.composer = ""
        if exists:
            self.overlay = Overlay(
                kind="confirm",
                mode="worktree",
                title="WORKTREE REQUIRED",
                body="This workspace already has a run. Type an isolated worktree label, then Enter.\nNightwatch will not start a second writer in the same working directory.",
            )
            return
        self._show_run_preview()

    def _show_run_preview(self) -> None:
        pending = self.pending or {}
        repo = Path(pending.get("repo") or self.repo or Path.cwd())
        label = pending.get("worktree")
        target = repo.parent / ".worktrees" / repo.name / label if label else repo
        checks = pending.get("verify") or []
        verify_text = "\n             ".join(checks) if checks else "none (cannot reach trusted DONE)"
        self.overlay = Overlay(
            kind="confirm",
            mode="run",
            title="CONFIRM RUN",
            body="\n".join([
                "NEW SUPERVISED RUN",
                f"Goal        {pending.get('goal')}",
                f"Workspace   {target}",
                f"Isolation   {'new worktree from committed HEAD' if label else 'current workspace'}",
                "Model       Codex default",
                "Reasoning   Codex default",
                f"Verification {verify_text}",
                "Service      repo-specific systemd user unit",
                "Type a verify command and Enter to add it; empty Enter confirms.",
            ]),
        )

    def _open_adopt_picker(self) -> None:
        repo = Path(self.repo or Path.cwd())
        discover = self.hooks.discover_sessions or list_adoptable_sessions
        try:
            sessions = discover(repo)
        except Exception as exc:
            sessions = []
            self.message = f"Could not list Codex sessions: {type(exc).__name__}"
        items: list[OverlayItem] = []
        unbound: list[dict[str, Any]] = []
        for session in sessions:
            if session.get("thread_source") == "subagent":
                continue
            if not session.get("thread_id"):
                unbound.append(session)
                continue
            title = str(session.get("title") or "")[:60]
            proof = "live" if session.get("live") else "recent"
            pid = f"PID {session['pid']}" if session.get("pid") else proof
            items.append(
                OverlayItem(
                    str(session["thread_id"]),
                    title or proof,
                    pid,
                    session,
                )
            )
        live_note = ""
        if unbound:
            live_note = "LIVE CODEX WITHOUT PROVEN THREAD\n" + "\n".join(
                f"PID {item.get('pid')} · {item.get('title')}" for item in unbound
            ) + "\nPick a conversation below or type the exact thread ID.\n\n"
        body = live_note + (
            "↑/↓ select · Enter adopts · type an exact thread ID as fallback"
            if items
            else "No adoptable conversations. Type an exact thread ID."
        )
        self.overlay = Overlay(kind="picker", title="ADOPT CODEX SESSION", body=body, items=items)
        self.composer = ""

    def _open_adopt_confirm(self, thread: str, session: dict[str, Any] | None) -> None:
        repo = Path(self.repo or Path.cwd())
        goal = adopt_goal_text((session or {}).get("title"), thread)
        self.pending = {
            "kind": "adopt",
            "thread": thread,
            "goal": goal,
            "repo": repo,
            "verify": [],
            "model": (session or {}).get("model"),
            "session": session,
        }
        self.composer = ""
        self._show_adopt_preview()

    def _show_adopt_preview(self) -> None:
        pending = self.pending or {}
        checks = pending.get("verify") or []
        verify_text = "\n             ".join(checks) if checks else "none (cannot reach trusted DONE)"
        goal = str(pending.get("goal") or "")
        if len(goal) > MAX_ADOPT_GOAL_DISPLAY:
            goal = goal[: MAX_ADOPT_GOAL_DISPLAY - 1] + "…"
        self.overlay = Overlay(
            kind="confirm",
            mode="adopt",
            title="CONFIRM ADOPT",
            body="\n".join([
                "ADOPT EXACT THREAD",
                f"Thread       {pending.get('thread')}",
                f"Workspace    {pending.get('repo')}",
                f"Goal         {goal}",
                "Model        preserve Codex/default",
                "Reasoning    preserve Codex/default",
                f"Verification {verify_text}",
                "Binds the thread only. Interactive Codex is not killed; /resume starts a writer after it exits.",
                "Type a verify command and Enter to add it; empty Enter confirms.",
            ]),
        )

    def _open_steer_confirm(self, instruction: str | None) -> None:
        run = self._selected_run()
        if run is None or not getattr(run, "active", False):
            self.message = "No active run selected. Use /run or /adopt first."
            return
        if getattr(run, "terminal", False):
            self.message = (
                "Instruction was NOT queued because this Nightwatch run is terminal.\n"
                "Use /resume or start a new supervised run before steering."
            )
            return
        if not instruction:
            self.awaiting = "steer"
            self.message = "Type the instruction for the selected exact thread."
            return
        self.pending = {"kind": "steer", "instruction": instruction, "run": run}
        self.overlay = Overlay(
            kind="confirm",
            title="CONFIRM STEER",
            body=f"Queue to exact thread {run.thread_id}?\n\n{instruction}",
        )

    def _add_verify(self, command: str) -> None:
        if self.pending is None:
            return
        checks = list(self.pending.get("verify") or [])
        if len(checks) >= MAX_VERIFY_COMMANDS:
            self.message = "Maximum verification commands reached."
            return
        try:
            checks.append(validate_human_text(command, "verification command", MAX_INSTRUCTION_CHARS))
        except ValueError as exc:
            self.message = str(exc)
            return
        self.pending["verify"] = checks

    def _refresh_confirm_body(self) -> None:
        if not self.pending:
            return
        if self.pending.get("kind") == "run":
            self._show_run_preview()
        elif self.pending.get("kind") == "adopt":
            self._show_adopt_preview()

    def _commit_confirm(self) -> None:
        pending = self.pending
        self.overlay = None
        self.pending = None
        self.composer = ""
        if not pending:
            return
        kind = pending.get("kind")
        if kind == "run":
            self.message = self._commit_run(pending)
        elif kind == "adopt":
            self.message = self._commit_adopt(pending)
        elif kind == "steer":
            fn = self.hooks.queue_steer or queue_steer
            result = fn(pending["run"].store, pending["instruction"])
            self.message = result.message
        elif kind == "stop":
            fn = self.hooks.stop or stop_run
            result = fn(pending["run"].repo)
            self.message = result.message
        elif kind == "resume":
            fn = self.hooks.resume or resume_service
            result = fn(pending["run"].repo)
            self.message = result.message
        elif kind == "report":
            self.message = self._commit_report(pending["run"])

    def _commit_run(self, pending: dict[str, Any]) -> str:
        repo = Path(pending["repo"])
        label = pending.get("worktree")
        try:
            if label:
                create = self.hooks.create_worktree or create_worktree
                repo = create(repo, label)
            spec = RunSpec(
                repo,
                pending["goal"],
                pending.get("model"),
                pending.get("effort"),
                tuple(pending.get("verify") or ()),
                service=True,
            )
            start = self.hooks.start_run or start_run
            result = start(spec, run_in_service=True)
            return result.message
        except (ValueError, GitError, StateIntegrityError) as exc:
            return f"Run not started: {exc}"

    def _commit_adopt(self, pending: dict[str, Any]) -> str:
        try:
            spec = RunSpec(
                Path(pending["repo"]),
                adopt_goal_text(pending.get("goal"), pending.get("thread")),
                pending.get("model") if isinstance(pending.get("model"), str) else None,
                None,
                tuple(pending.get("verify") or ()),
                thread_id=str(pending["thread"]),
                service=False,
            )
            adopt = self.hooks.adopt or adopt_run
            result = adopt(spec)
            return result.message
        except (ValueError, GitError) as exc:
            return f"Adoption invalid: {exc}"

    def _commit_report(self, run: Any) -> str:
        try:
            if self.hooks.write_report:
                path = self.hooks.write_report(run)
            else:
                path = run.store.write_report(build_report(run.store, run.store.load_state(), run.store.load_state().get("last_verification")))
            return f"Report written: {path}"
        except Exception as exc:
            return f"Report not written: {type(exc).__name__}"


@dataclass
class RunRecord:
    store: NightwatchStore
    state: dict[str, Any]
    plan: dict[str, Any]
    _events: list[dict[str, Any]] | None = None

    @property
    def events(self) -> list[dict[str, Any]]:
        if self._events is None:
            self._events = self.store.load_events()
        return self._events

    @property
    def repo(self) -> Path:
        return self.store.repo

    @property
    def thread_id(self) -> str | None:
        value = self.state.get("thread_id")
        return value if isinstance(value, str) else None

    @property
    def terminal(self) -> bool:
        return self.state.get("state") in {item.value for item in TERMINAL_STATES}

    @property
    def active(self) -> bool:
        return not self.terminal


class RunCatalog:
    """Discover and validate trusted runs behind one small read-only interface."""

    def __init__(self, state_home: str | Path | None = None):
        self.root = Path(state_home).expanduser().resolve() if state_home else control_plane_root().resolve()
        self.errors: list[str] = []

    def discover(self) -> list[RunRecord]:
        self.errors = []
        try:
            root_info = os.lstat(self.root)
        except FileNotFoundError:
            return []
        except OSError:
            self.errors.append("trusted state root is unreadable")
            return []
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            self.errors.append("trusted state root is not a real directory")
            return []
        records: list[RunRecord] = []
        try:
            children = sorted(self.root.iterdir(), key=lambda item: item.name)[:MAX_DISCOVERED_RUNS]
        except OSError:
            self.errors.append("trusted state root cannot be listed")
            return []
        for child in children:
            try:
                child_info = os.lstat(child)
                if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
                    continue
                metadata_path = child / "metadata.json"
                if not metadata_path.exists():
                    continue
                metadata = NightwatchStore._read_json_regular(metadata_path)
                repo_value = metadata.get("repo_path") if isinstance(metadata, dict) else None
                if not isinstance(repo_value, str) or not Path(repo_value).is_absolute():
                    raise StateIntegrityError("metadata repo path is invalid")
                store = NightwatchStore(repo_value, state_home=self.root)
                if store.directory != child.resolve():
                    raise StateIntegrityError("metadata does not match its trusted directory")
                state = store.load_state()
                plan = store.load_plan()
                records.append(RunRecord(store, state, plan))
            except (OSError, ValueError, TypeError, StateIntegrityError) as exc:
                self.errors.append(f"{child.name}: {type(exc).__name__}")
        return sorted(records, key=lambda item: (str(item.repo), item.state.get("created_at", "")))


def _bar(percent: float, width: int = 18) -> str:
    filled = min(width, max(0, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _quota_line(state: dict[str, Any]) -> str:
    quota = state.get("quota") or {}
    values = []
    for key, label in (("primary", "5h"), ("secondary", "week")):
        window = quota.get(key) if isinstance(quota, dict) else None
        if isinstance(window, dict) and window.get("used_percent") is not None:
            values.append(f"{label} {window['used_percent']}%")
    return " · ".join(values) or "not sampled"


def _next_action(state: dict[str, Any]) -> str:
    if state.get("state") == State.WAIT_QUOTA.value:
        if not state.get("thread_id"):
            return "revalidate quota at reset, then start first thread"
        return "revalidate quota at reset, then resume exact thread"
    mapping = {
        State.NEW.value: "start supervisor preflight",
        State.PREFLIGHT.value: "validate repo, auth, and quota",
        State.RUNNING.value: "continue current milestone",
        State.RETRY_BACKOFF.value: "retry after bounded backoff",
        State.RECOVERING.value: "prove recovery state, then resume exact thread",
        State.VERIFYING.value: "run frozen trusted verification",
        State.AWAITING_ACCEPTANCE.value: "add an authorized verification policy",
        State.BLOCKED.value: "review blocker before explicit resume",
        State.STOPPED.value: "resume only on explicit request",
        State.DONE.value: "review recap/report or start another worktree run",
        State.FAILED.value: "inspect failure evidence before explicit resume",
    }
    return mapping.get(str(state.get("state")), "inspect trusted state")


def _agent_summary(state: dict[str, Any]) -> str:
    active = state.get("active_process")
    if isinstance(active, dict) and process_matches(active):
        return f"RUNNING · PID {active.get('pid')} · {active.get('action') or 'provider'}"
    owner = state.get("supervisor_owner")
    owner_alive = False
    if isinstance(owner, dict):
        owner_alive = process_matches(owner) if owner.get("starttime") and owner.get("executable") else pid_alive(owner.get("pid"))
    if state.get("state") == State.WAIT_QUOTA.value and not state.get("thread_id"):
        suffix = f" · PID {owner.get('pid')}" if owner_alive else ""
        return f"WAITING_QUOTA (first launch deferred){suffix}"
    if owner_alive:
        return f"SUPERVISOR {state.get('state')} · PID {owner.get('pid')}"
    return str(state.get("state") or "IDLE")


def agent_work_report(store: NightwatchStore) -> dict[str, Any]:
    """Untrusted mailbox work report. Never mixed into trusted verified percent."""
    empty = {"ok": False, "implemented": [], "working": [], "blocked": [], "percent": None, "untrusted": True}
    try:
        raw = read_mailbox_json(store, "progress.json")
    except (ValueError, StateIntegrityError, OSError):
        return empty
    if not isinstance(raw, dict):
        return empty

    def _lines(key: str) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, list):
            return []
        rows: list[str] = []
        for item in value[:8]:
            if isinstance(item, str) and item.strip():
                rows.append(" ".join(item.split()))
        return rows

    implemented = _lines("implemented")
    working = _lines("working")
    blocked = _lines("blocked")
    total = len(implemented) + len(working) + len(blocked)
    percent = round(100 * len(implemented) / total, 1) if total else None
    return {
        "ok": True,
        "implemented": implemented,
        "working": working,
        "blocked": blocked,
        "percent": percent,
        "untrusted": True,
    }


def _clip(text: str, width: int) -> str:
    width = max(24, width)
    return text if len(text) <= width else text[: width - 1] + "…"


def format_agent_work(store: NightwatchStore, width: int = 100) -> list[str]:
    report = agent_work_report(store)
    if not report["ok"]:
        return ["Work       (no agent mailbox progress yet)"]
    percent = f"{report['percent']}%" if report["percent"] is not None else "n/a"
    lines = [
        f"Work       agent-reported {percent} of {len(report['implemented']) + len(report['working']) + len(report['blocked'])} items · UNTRUSTED",
        f"           done {len(report['implemented'])} · doing {len(report['working'])} · blocked {len(report['blocked'])}",
    ]
    for label, rows in (("Done", report["implemented"][:3]), ("Now", report["working"][:3]), ("Left/blocked", report["blocked"][:3])):
        if not rows:
            continue
        lines.append(f"  {label:<12} {_clip(rows[0], width - 16)}")
        for extra in rows[1:]:
            lines.append(f"               {_clip(extra, width - 16)}")
    lines.append("Trusted    verified % only moves after frozen Nightwatch checks, not agent narrative")
    return lines


def render_dashboard(runs: list[RunRecord], selected: int = 0, width: int = 100, errors: list[str] | None = None) -> str:
    width = max(60, width)
    selected = min(max(0, selected), max(0, len(runs) - 1))
    error_header = f" · {len(errors)} trusted run failed integrity validation" if errors else ""
    lines = [
        f"Nightwatch {__version__} · MULTI-THREAD CONTROL",
        f"Runs {len(runs)}{error_header} · ↑/↓ select · / commands · Esc cancel · /quit leaves",
        "─" * min(width, 120),
    ]
    if errors:
        lines.append(f"⚠ TRUSTED STATE ERRORS: {len(errors)}")
        for err in errors[:5]:
            lines.append(f"  {err}")
        lines.append("  Use explicit CLI/recovery inspection before touching that run.")
        lines.append("─" * min(width, 120))
    if not runs:
        lines.extend(["No trusted runs discovered.", "Type a natural-language goal or /run to create one."])
    for index, run in enumerate(runs):
        state = run.state
        progress = plan_progress(run.plan)
        marker = "▶" if index == selected else " "
        thread = run.thread_id or ("First launch deferred" if state.get("state") == State.WAIT_QUOTA.value else "capturing…")
        model = state.get("model") or "Codex default"
        effort = state.get("reasoning_effort") or "default"
        work = agent_work_report(run.store)
        agent_pct = f"agent {work['percent']}%" if work.get("percent") is not None else "agent n/a"
        lines.append(f"{marker} {state['state']:<19} {run.repo.name:<22} {thread[:24]}")
        lines.append(f"    {_bar(progress['verified_percent'])} trusted {progress['verified_percent']:>5}% · {agent_pct}  {model} · {effort}  quota {_quota_line(state)}")
    if runs:
        run = runs[selected]
        state = run.state
        current = next((item for item in run.plan["milestones"] if item.get("status") != "verified"), None)
        thread_label = run.thread_id or ("First launch deferred — no Codex thread created yet" if state.get("state") == State.WAIT_QUOTA.value else "Creating/capturing exact thread")
        lines.extend([
            "─" * min(width, 120),
            f"Goal       {str(state.get('goal') or '')[: max(10, width - 12)]}",
            f"Repository {run.repo}",
            f"Thread     {thread_label} · generation {state.get('generation')}",
            f"Agent      {_agent_summary(state)}",
            f"Current    {(current or {}).get('id', 'complete')} · {(current or {}).get('title', 'all trusted milestones verified')}",
            f"Last       {state.get('last_event') or '(none)'}",
            f"Next       {_next_action(state)}",
        ])
        lines.extend(format_agent_work(run.store, width))
        lines.append("Source: trusted state + sequence-validated events · agent work is untrusted mailbox")
    return terminal_safe("\n".join(lines))


def status_run(run: RunRecord) -> str:
    state = run.state
    progress = plan_progress(run.plan)
    current = next((item for item in run.plan["milestones"] if item.get("status") != "verified"), None)
    latest = run.events[-1] if run.events else {}
    thread_label = run.thread_id or ("First launch deferred — no Codex thread created yet" if state.get("state") == State.WAIT_QUOTA.value else "Creating/capturing exact thread")
    lines = [
        f"STATUS · {state['state']}",
        f"Agent       {_agent_summary(state)}",
        f"Repository  {run.repo}",
        f"Run         {state.get('run_id')}",
        f"Thread      {thread_label}",
        f"Generation  {state.get('generation')} · recoveries {state.get('recoveries', 0)}",
        f"Model       {state.get('model') or 'Codex default'} · {state.get('reasoning_effort') or 'default'}",
        f"Quota       {_quota_line(state)} · authority {state.get('quota_source') or '(none)'}",
        f"Progress    {_bar(progress['verified_percent'], 24)} {progress['verified_percent']}% trusted verified",
        f"Milestones  {progress['implemented_count']}/{progress['total_count']} implemented · {progress['verified_count']}/{progress['total_count']} verified",
        f"Current     {(current or {}).get('id', 'complete')} · {(current or {}).get('title', 'all trusted milestones verified')}",
        f"Last        #{latest.get('seq', '?')} {latest.get('event') or state.get('last_event')} · {latest.get('reason') or '(no reason)'}",
        f"Next        {_next_action(state)}",
        f"Updated     {state.get('updated_at')}",
        "Provenance  trusted state + hash-bound policy + sequence-validated events",
    ]
    lines.extend(format_agent_work(run.store, 100))
    if state.get("last_error") or state.get("blocker"):
        lines.append(f"Blocker     {state.get('last_error') or state.get('blocker')}")
    return terminal_safe("\n".join(lines))


def explain_run(run: RunRecord) -> str:
    state = run.state
    latest = next(
        (item for item in reversed(run.events) if item.get("event") not in {"supervisor_lease_acquired", "supervisor_lease_released"}),
        run.events[-1] if run.events else {},
    )
    lines = [
        f"WHY {state['state']}",
        f"Reason      {latest.get('reason') or state.get('last_error') or 'No detailed reason persisted'}",
        f"Evidence    event #{latest.get('seq', '?')} · {latest.get('event') or state.get('last_event')}",
        f"Authority   {state.get('quota_source') or 'trusted Nightwatch state'}",
        f"Next        {_next_action(state)}",
    ]
    if state.get("next_resume_at"):
        lines.append(f"Reset       {state['next_resume_at']}")
    lines.extend([
        "Will not    use --last, create an unbound replacement thread, or accept model-authored verification",
        "Provenance  state.json + hash-bound policy + sequence-validated events.jsonl",
    ])
    return terminal_safe("\n".join(lines))


def recap_run(run: RunRecord) -> str:
    state = run.state
    progress = plan_progress(run.plan)
    verification = state.get("last_verification") or {}
    checks = verification.get("final_checks") or []
    lines = [
        "NIGHTWATCH RECAP — trusted evidence",
        f"Result       {state['state']}",
        f"Goal         {state.get('goal')}",
        f"Thread       {run.thread_id or '(not captured)'}",
        f"Model        {state.get('model') or 'Codex default'} · {state.get('reasoning_effort') or 'default'}",
        f"Runtime      {state.get('created_at')} → {state.get('updated_at')}",
        f"Recoveries   {state.get('recoveries', 0)}",
        f"Milestones   {progress['verified_count']}/{progress['total_count']} verified",
        "Checks       trusted verification policy:",
    ]
    policy = run.store.load_policy()
    by_command = {item.get("command"): item for item in checks if isinstance(item, dict)}
    for command in policy["final_commands"]:
        check = by_command.get(command)
        status_value = "PASS" if check and check.get("ok") else "FAIL" if check else "NOT RUN"
        lines.append(f"  {status_value:<7} {command}")
    if not policy["final_commands"]:
        lines.append("  NONE    no frozen acceptance command")
    if state.get("last_error") or state.get("blocker"):
        lines.append(f"Blocker      {state.get('last_error') or state.get('blocker')}")
    lines.append("Agent narrative is intentionally excluded from trusted facts.")
    return terminal_safe("\n".join(lines))


def _safe_tail(path: Path, maximum: int = 32_768) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EVENT_BYTES:
            raise StateIntegrityError("trusted text file is unsafe")
        os.lseek(descriptor, max(0, info.st_size - maximum), os.SEEK_SET)
        return os.read(descriptor, maximum).decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)




def _selected(runs: list[RunRecord], index: int) -> RunRecord | None:
    return runs[min(max(0, index), len(runs) - 1)] if runs else None


def _timeline(run: RunRecord) -> str:
    lines = ["TRUSTED TIMELINE"]
    for item in run.events[-80:]:
        lines.append(f"#{item.get('seq', '?'):<4} {item.get('ts', '')} {item.get('state', ''):<20} {item.get('event', '')} — {item.get('reason', '')}")
    return terminal_safe("\n".join(lines))


def _plan(run: RunRecord) -> str:
    lines = ["TRUSTED MILESTONES"]
    for item in run.plan["milestones"]:
        lines.append(f"[{item.get('status', 'pending'):<11}] {item['id']} · {item['title']} · weight {item['weight']}")
    lines.append("Only frozen Nightwatch verification can change implemented → verified.")
    return terminal_safe("\n".join(lines))


def _thread(run: RunRecord) -> str:
    provider_commands = []
    for path in sorted(run.store.runs_path.glob("generation-*.events.jsonl")):
        try:
            for line in _safe_tail(path, MAX_EVENT_BYTES).splitlines():
                item = json.loads(line)
                if item.get("type") == "provider_command":
                    provider_commands.append((path.stem, item.get("action")))
        except (OSError, StateIntegrityError, json.JSONDecodeError):
            continue
    lines = [
        "EXACT THREAD",
        f"Thread      {run.thread_id or '(not captured)'}",
        f"Generation  {run.state.get('generation')}",
        f"Recoveries  {run.state.get('recoveries', 0)}",
        "Provider turns:",
    ]
    lines.extend(f"  {generation}: {action}" for generation, action in provider_commands)
    return terminal_safe("\n".join(lines))


def _quota(run: RunRecord) -> str:
    state = run.state
    quota = state.get("quota") or {}
    lines = [f"QUOTA · last trusted sample authority={state.get('quota_source') or '(none)'}"]
    for key, label in (("primary", "5h"), ("secondary", "weekly")):
        window = quota.get(key) if isinstance(quota, dict) else None
        if isinstance(window, dict):
            lines.append(f"{label:<8} {window.get('used_percent')}% used · reset={window.get('resets_at')} · window={window.get('window_duration_mins')}m")
    lines.append(f"Sampled at {quota.get('read_at') if isinstance(quota, dict) else None}")
    try:
        live = make_quota_provider().read()
        primary = f"{live.primary.used_percent}%" if live.primary else "?"
        weekly = f"{live.secondary.used_percent}%" if live.secondary else "?"
        lines.append(f"Live now   5h {primary} · week {weekly} · {live.source}")
    except Exception as exc:
        lines.append(f"Live now   unavailable ({type(exc).__name__})")
    lines.append(f"Next resume {state.get('next_resume_at') or '(not waiting)'}")
    lines.append("Dashboard quota is the last trusted sample; live refresh is this /quota view.")
    return terminal_safe("\n".join(lines))


class _CursesApp:
    def __init__(self, screen, initial_repo: Path | None = None):
        self.screen = screen
        self.catalog = RunCatalog()
        self.controller = TuiController(repo=initial_repo)
        self.previous_states: dict[str, str] = {}

    def run(self) -> int:
        import curses

        curses.curs_set(1)
        self.screen.keypad(True)
        self.screen.timeout(1000)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
        while not self.controller.quit:
            self._refresh_runs()
            self._draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            self.controller.handle_key(self._normalize(key))
        return 0

    @staticmethod
    def _normalize(key: str | int) -> str:
        import curses

        if key in (curses.KEY_UP,):
            return "up"
        if key in (curses.KEY_DOWN,):
            return "down"
        if key in (curses.KEY_PPAGE,):
            return "pageup"
        if key in (curses.KEY_NPAGE,):
            return "pagedown"
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            return "backspace"
        if key in ("\n", "\r"):
            return "enter"
        if key == "\x1b":
            return "esc"
        if key == "\x03":
            return "ctrl-c"
        if isinstance(key, str):
            return key
        return ""

    def _refresh_runs(self) -> None:
        import curses

        self.controller.runs = self.catalog.discover()
        self.controller.catalog_errors = self.catalog.errors
        self.controller.selected = min(self.controller.selected, max(0, len(self.controller.runs) - 1))
        current = {item.state["run_id"]: item.state["state"] for item in self.controller.runs}
        terminal = {item.value for item in TERMINAL_STATES}
        for run_id, state_value in current.items():
            previous = self.previous_states.get(run_id)
            if previous and previous != state_value and state_value in terminal:
                curses.beep()
                self.controller.message = f"{state_value}: {run_id} · use /recap or /report"
        self.previous_states = current
        selected = _selected(self.controller.runs, self.controller.selected)
        if selected and self.controller.view in VIEW_COMMANDS and self.controller.overlay is None:
            self.controller.content = self._view_content(self.controller.view, selected)

    def _draw(self) -> None:
        height, width = self.screen.getmaxyx()
        body = self.controller.render(width, height)
        self.screen.erase()
        for row, line in enumerate(body.splitlines()[: max(0, height)]):
            try:
                self.screen.addnstr(row, 0, terminal_safe(line), max(1, width - 1))
            except Exception:
                pass
        try:
            cursor_row = max(0, height - 1)
            prefix = "Input › "
            cursor_col = min(width - 2, len(prefix) + len(self.controller.composer))
            self.screen.move(cursor_row, max(0, cursor_col))
        except Exception:
            pass
        self.screen.refresh()

    @staticmethod
    def _view_content(view: str, run: RunRecord) -> str:
        if view == "status":
            return status_run(run)
        if view == "plan":
            return _plan(run)
        if view == "timeline":
            return _timeline(run)
        if view == "explain":
            return explain_run(run)
        if view == "thread":
            return _thread(run)
        if view == "quota":
            return _quota(run)
        if view == "recap":
            return recap_run(run)
        try:
            return "SUPERVISOR LOG\n" + _safe_tail(run.store.log_path)
        except (OSError, StateIntegrityError):
            return "No safe supervisor log is available."


def run_tui(initial_repo: str | Path | None = None) -> int:
    try:
        import curses
    except ImportError:
        print("nightwatch: this Python build has no curses support; use explicit CLI subcommands", file=sys.stderr)
        return 20
    root = None
    if initial_repo:
        try:
            root = repo_root(initial_repo)
        except GitError:
            root = None
    try:
        return int(curses.wrapper(lambda screen: _CursesApp(screen, root).run()))
    except KeyboardInterrupt:
        return 0
