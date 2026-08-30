from __future__ import annotations

import io
import json
import os
import stat
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .git import GitError, repo_root
from .models import TERMINAL_STATES, State, plan_progress, validate_model_name, validate_reasoning_effort
from .operations import (
    ActionResult,
    RunSpec,
    create_worktree,
    doctor_snapshot,
    list_models,
    queue_steer,
    resume_service,
    start_run,
    stop_run,
    validate_human_text,
)
from .storage import MAX_EVENT_BYTES, NightwatchStore, StateIntegrityError, control_plane_root
from .supervisor import build_report, find_active_threads_for_repo, find_proven_codex_sessions, pid_alive, process_matches

_validate_human_text = validate_human_text

MAX_GOAL_CHARS = 4_000
MAX_INSTRUCTION_CHARS = 4_000
MAX_VERIFY_COMMANDS = 20
MAX_DISCOVERED_RUNS = 1_000


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
)


def slash_commands(prefix: str = "") -> list[SlashCommand]:
    clean = prefix.strip().lower().removeprefix("/")
    return [item for item in COMMANDS if item.name.removeprefix("/").startswith(clean)]


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
    if value.startswith("/"):
        command, _, argument = value.partition(" ")
        name = command[1:].lower()
        if name == "new":
            name = "run"
        known = {item.name[1:] for item in COMMANDS}
        if name not in known:
            return Intent("error", f"Unknown command: {command}")
        return Intent(name, argument.strip() or None, next(item.mutates for item in COMMANDS if item.name == f"/{name}"))
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
    mapping = {
        State.NEW.value: "start supervisor preflight",
        State.PREFLIGHT.value: "validate repo, auth, and quota",
        State.RUNNING.value: "continue current milestone",
        State.WAIT_QUOTA.value: "revalidate quota at reset, then resume exact thread",
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
    if isinstance(owner, dict):
        owner_alive = process_matches(owner) if owner.get("starttime") and owner.get("executable") else pid_alive(owner.get("pid"))
        if owner_alive:
            return f"SUPERVISOR {state.get('state')} · PID {owner.get('pid')}"
    return str(state.get("state") or "IDLE")


def render_dashboard(runs: list[RunRecord], selected: int = 0, width: int = 100, errors: list[str] | None = None) -> str:
    width = max(60, width)
    selected = min(max(0, selected), max(0, len(runs) - 1))
    error_header = f" · {len(errors)} trusted run failed integrity validation" if errors else ""
    lines = [
        f"Nightwatch {__version__} · MULTI-THREAD CONTROL",
        f"Runs {len(runs)}{error_header} · ↑/↓ select · / commands · Esc quit",
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
        thread = run.thread_id or "capturing…"
        model = state.get("model") or "Codex default"
        effort = state.get("reasoning_effort") or "default"
        lines.append(f"{marker} {state['state']:<19} {run.repo.name:<22} {thread[:24]}")
        lines.append(f"    {_bar(progress['verified_percent'])} {progress['verified_percent']:>5}%  {model} · {effort}  quota {_quota_line(state)}")
    if runs:
        run = runs[selected]
        state = run.state
        current = next((item for item in run.plan["milestones"] if item.get("status") != "verified"), None)
        lines.extend([
            "─" * min(width, 120),
            f"Goal       {str(state.get('goal') or '')[: max(10, width - 12)]}",
            f"Repository {run.repo}",
            f"Thread     {run.thread_id or 'Creating/capturing exact thread'} · generation {state.get('generation')}",
            f"Agent      {_agent_summary(state)}",
            f"Current    {(current or {}).get('id', 'complete')} · {(current or {}).get('title', 'all trusted milestones verified')}",
            f"Last       {state.get('last_event') or '(none)'}",
            f"Next       {_next_action(state)}",
            "Source: trusted state + sequence-validated events",
        ])
    lines.append("Input › natural language starts a goal (or steers an active run); / opens command palette")
    return terminal_safe("\n".join(lines))


def status_run(run: RunRecord) -> str:
    state = run.state
    progress = plan_progress(run.plan)
    current = next((item for item in run.plan["milestones"] if item.get("status") != "verified"), None)
    latest = run.events[-1] if run.events else {}
    lines = [
        f"STATUS · {state['state']}",
        f"Agent       {_agent_summary(state)}",
        f"Repository  {run.repo}",
        f"Run         {state.get('run_id')}",
        f"Thread      {run.thread_id or 'Creating/capturing exact thread'}",
        f"Generation  {state.get('generation')} · recoveries {state.get('recoveries', 0)}",
        f"Model       {state.get('model') or 'Codex default'} · {state.get('reasoning_effort') or 'default'}",
        f"Quota       {_quota_line(state)} · authority {state.get('quota_source') or '(none)'}",
        f"Progress    {_bar(progress['verified_percent'], 24)} {progress['verified_percent']}% verified",
        f"Milestones  {progress['implemented_count']}/{progress['total_count']} implemented · {progress['verified_count']}/{progress['total_count']} verified",
        f"Current     {(current or {}).get('id', 'complete')} · {(current or {}).get('title', 'all trusted milestones verified')}",
        f"Last        #{latest.get('seq', '?')} {latest.get('event') or state.get('last_event')} · {latest.get('reason') or '(no reason)'}",
        f"Next        {_next_action(state)}",
        f"Updated     {state.get('updated_at')}",
        "Provenance  trusted state + hash-bound policy + sequence-validated events",
    ]
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
    lines = [f"QUOTA · authority={state.get('quota_source') or '(none)'}"]
    for key, label in (("primary", "5h"), ("secondary", "weekly")):
        window = quota.get(key) if isinstance(quota, dict) else None
        if isinstance(window, dict):
            lines.append(f"{label:<8} {window.get('used_percent')}% used · reset={window.get('resets_at')} · window={window.get('window_duration_mins')}m")
    lines.append(f"Next resume {state.get('next_resume_at') or '(not waiting)'}")
    return terminal_safe("\n".join(lines))


class _CursesApp:
    def __init__(self, screen, initial_repo: Path | None = None):
        self.screen = screen
        self.initial_repo = initial_repo
        self.catalog = RunCatalog()
        self.runs: list[RunRecord] = []
        self.selected = 0
        self.view = "multi"
        self.content: str | None = None
        self.message = "Type / to discover commands."
        self.previous_states: dict[str, str] = {}
        self.scroll = 0

    def run(self) -> int:
        import curses

        curses.curs_set(0)
        self.screen.keypad(True)
        self.screen.timeout(1000)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)
        while True:
            self._refresh_runs()
            self._draw()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if key == curses.KEY_UP:
                self.selected = max(0, self.selected - 1)
                self.content = None
                self.scroll = 0
            elif key == curses.KEY_DOWN:
                self.selected = min(max(0, len(self.runs) - 1), self.selected + 1)
                self.content = None
                self.scroll = 0
            elif key in ("\n", "\r"):
                if self.runs:
                    self.view = "status"
                    self.content = status_run(self.runs[self.selected])
                    self.scroll = 0
            elif key == curses.KEY_NPAGE:
                self.scroll += max(1, self.screen.getmaxyx()[0] - 4)
            elif key == curses.KEY_PPAGE:
                self.scroll = max(0, self.scroll - max(1, self.screen.getmaxyx()[0] - 4))
            elif key == "\x1b":
                return 0
            elif isinstance(key, str) and (key == "/" or key.isprintable()):
                line = self._read_line(key)
                if line is not None and self._handle(route_input(line, bool((_selected(self.runs, self.selected) or _NullRun()).active))):
                    return 0

    def _refresh_runs(self) -> None:
        import curses

        self.runs = self.catalog.discover()
        self.selected = min(self.selected, max(0, len(self.runs) - 1))
        current = {item.state["run_id"]: item.state["state"] for item in self.runs}
        terminal = {item.value for item in TERMINAL_STATES}
        for run_id, state_value in current.items():
            previous = self.previous_states.get(run_id)
            if previous and previous != state_value and state_value in terminal:
                curses.beep()
                self.message = f"{state_value}: {run_id} · use /recap or /report"
        self.previous_states = current
        selected = _selected(self.runs, self.selected)
        if selected and self.view in {"status", "plan", "logs", "timeline", "explain", "thread", "quota", "recap"}:
            self.content = self._view_content(self.view, selected)

    def _draw(self) -> None:
        height, width = self.screen.getmaxyx()
        body = self.content if self.content is not None else render_dashboard(self.runs, self.selected, width, errors=self.catalog.errors)
        lines = []
        for source_line in body.splitlines():
            safe_line = terminal_safe(source_line)
            lines.extend(textwrap.wrap(safe_line, max(20, width - 1), replace_whitespace=False, drop_whitespace=False) or [""])
        self.screen.erase()
        visible = lines[self.scroll : self.scroll + max(0, height - 2)]
        for row, line in enumerate(visible):
            try:
                self.screen.addnstr(row, 0, line, max(1, width - 1))
            except Exception:
                pass
        try:
            self.screen.addnstr(max(0, height - 2), 0, "─" * max(1, width - 1), max(1, width - 1))
            footer = f"{self.message} · PgUp/PgDn scroll"
            self.screen.addnstr(max(0, height - 1), 0, footer, max(1, width - 1))
        except Exception:
            pass
        self.screen.refresh()

    def _read_line(self, initial: str) -> str | None:
        import curses

        value = initial
        while True:
            height, width = self.screen.getmaxyx()
            self.screen.move(max(0, height - 1), 0)
            self.screen.clrtoeol()
            self.screen.addnstr(max(0, height - 1), 0, f"› {value}", max(1, width - 1))
            if value.startswith("/"):
                matches = slash_commands(palette_prefix(value))
                for offset, command in enumerate(matches[: min(8, max(0, height - 3))], start=2):
                    row = height - offset
                    self.screen.move(row, 0)
                    self.screen.clrtoeol()
                    self.screen.addnstr(row, 0, f"{command.name:<12} {command.summary}", max(1, width - 1))
            self.screen.refresh()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if key in ("\n", "\r"):
                return value
            if key == "\x1b":
                return None
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                value = value[:-1]
            elif isinstance(key, str) and key.isprintable() and len(value) < MAX_GOAL_CHARS:
                value += key

    def _prompt(self, label: str, default: str = "") -> str | None:
        initial = default
        self.message = f"{label} (Esc cancels)"
        value = self._read_line(initial)
        self.message = "Type / to discover commands."
        return value

    def _confirm(self, preview: str) -> bool:
        import curses

        self.content = preview + "\n\nEnter confirms · Esc cancels"
        self.scroll = 0
        self._draw()
        while True:
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if key in ("\n", "\r", "\x1b"):
                self.content = None
                return key in ("\n", "\r")

    def _handle(self, intent: Intent) -> bool:
        run = _selected(self.runs, self.selected)
        if intent.kind == "quit":
            return True
        if intent.kind in {"palette", "help"}:
            self.view = "static"
            self.content = "COMMAND PALETTE\n" + "\n".join(f"{item.name:<12} {item.summary}\n             {item.usage}" for item in COMMANDS)
            return False
        if intent.kind == "error":
            self.message = intent.argument or "Unknown command"
            return False
        if intent.kind == "multi":
            self.view = "multi"
            self.content = None
            self.scroll = 0
            return False
        if intent.kind == "run":
            self._run_wizard(intent.argument)
            return False
        if intent.kind == "adopt":
            self._adopt_wizard(intent.argument)
            return False
        if intent.kind == "models":
            self.view = "static"
            self.content = self._models_view()
            self.scroll = 0
            return False
        if intent.kind == "doctor":
            self.view = "static"
            self.content = self._doctor_view((run.repo if run else self.initial_repo) or Path.cwd())
            self.scroll = 0
            return False
        if run is None:
            self.message = "No run selected. Use /run or enter a natural-language goal."
            return False
        if intent.kind in {"status", "plan", "timeline", "explain", "thread", "quota", "recap", "logs"}:
            self.view = intent.kind
            self.content = self._view_content(intent.kind, run)
        elif intent.kind == "report":
            if self._confirm("Write a durable report from trusted state and verification evidence?"):
                path = run.store.write_report(build_report(run.store, run.store.load_state(), run.store.load_state().get("last_verification")))
                self.message = f"Report written: {path}"
        elif intent.kind == "steer":
            if run.terminal:
                self.message = (
                    "Instruction was NOT queued because this Nightwatch run is terminal.\n"
                    "Use /resume or start a new supervised run before steering."
                )
                self.scroll = 0
                return False
            instruction = intent.argument or self._prompt("Instruction for the selected exact thread")
            if instruction and self._confirm(f"Queue to exact thread {run.thread_id}?\n\n{instruction}"):
                result = queue_steer(run.store, instruction)
                self.message = result.message
        elif intent.kind == "stop":
            if self._confirm(f"Stop {run.state['run_id']} and preserve its exact thread/state?"):
                result = stop_run(run.repo)
                self.message = result.message
        elif intent.kind == "resume":
            if self._confirm(f"Resume exact thread {run.thread_id or '(not captured)'} in its repo-specific user service?"):
                result = resume_service(run.repo)
                self.message = result.message
        self.scroll = 0
        return False

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

    def _run_wizard(self, initial_goal: str | None) -> None:
        goal = initial_goal or self._prompt("Overall goal")
        if not goal:
            return
        default_repo = str(self.initial_repo or Path.cwd())
        repo_text = self._prompt("Git repository or existing worktree", default_repo)
        if not repo_text:
            return
        try:
            root = repo_root(Path(repo_text).expanduser())
            existing = NightwatchStore(root).exists()
        except (GitError, StateIntegrityError) as exc:
            self.message = f"Repository unavailable: {type(exc).__name__}"
            return
        worktree_label = None
        if existing:
            worktree_label = self._prompt("This workspace already has a run; new isolated worktree label")
            if not worktree_label:
                self.message = "A second writer cannot use the same workspace; run cancelled."
                return
        model = self._prompt("Model (blank = Codex default)")
        effort = self._prompt("Reasoning level (blank = Codex default)")
        checks: list[str] = []
        while len(checks) < MAX_VERIFY_COMMANDS:
            command = self._prompt("Frozen verification command (blank finishes)")
            if not command:
                break
            checks.append(command)
        target = root.parent / ".worktrees" / root.name / worktree_label if worktree_label else root
        preview = "\n".join([
            "NEW SUPERVISED RUN",
            f"Goal        {goal}",
            f"Workspace   {target}",
            f"Isolation   {'new worktree from committed HEAD' if worktree_label else 'current workspace'}",
            f"Model       {model or 'Codex default'}",
            f"Reasoning   {effort or 'Codex default'}",
            "Verification " + ("\n             ".join(checks) if checks else "none (cannot reach trusted DONE)"),
            "Service      repo-specific systemd user unit",
        ])
        if not self._confirm(preview):
            return
        try:
            # Validate every user-controlled value before creating a worktree.
            spec = RunSpec(target, goal, model or None, effort or None, tuple(checks), service=True)
            if worktree_label:
                created = create_worktree(root, worktree_label)
                if created != spec.repo:
                    raise StateIntegrityError("created worktree does not match the confirmed target")
            root = spec.repo
            self.message = self._start_spec(spec)
            self.initial_repo = root
        except (ValueError, GitError, StateIntegrityError) as exc:
            self.message = f"Run not started: {exc}"

    def _adopt_wizard(self, initial_thread: str | None) -> None:
        repo_text = self._prompt("Git repository/worktree", str(self.initial_repo or Path.cwd()))
        if not repo_text:
            return
        try:
            root = repo_root(Path(repo_text).expanduser())
        except GitError as exc:
            self.message = f"Adoption invalid: {exc}"
            return
        sessions = find_proven_codex_sessions(root) if not initial_thread else []
        selected_session: dict[str, Any] | None = None
        thread = initial_thread
        if not thread and sessions:
            self.content = "ACTIVE CODEX SESSIONS · proven by PID + rollout + repository\n" + "\n".join(
                f"{index}. {item.get('thread_id')} · PID {item.get('pid')} · {item.get('model') or '(model unknown)'} · {str(item.get('title') or '')[:60]}"
                for index, item in enumerate(sessions, start=1)
            )
            choice = self._prompt("Session number or exact thread ID")
            self.content = None
            if choice and choice.isdigit() and 1 <= int(choice) <= len(sessions):
                selected_session = sessions[int(choice) - 1]
                thread = str(selected_session["thread_id"])
            else:
                thread = choice
        if not thread:
            recent_threads = find_active_threads_for_repo(root)
            if recent_threads:
                self.content = "RECENT CODEX CONVERSATIONS · from local repository history\n" + "\n".join(
                    f"{index}. {item.get('id')} · {item.get('model') or 'default'} · {str(item.get('title') or item.get('first_user_message') or '(no title)')[:60]}"
                    for index, item in enumerate(recent_threads, start=1)
                )
                choice = self._prompt("Conversation number or exact thread ID")
                self.content = None
                if choice and choice.isdigit() and 1 <= int(choice) <= len(recent_threads):
                    selected_session = recent_threads[int(choice) - 1]
                    thread = str(selected_session["id"])
                else:
                    thread = choice
        if not thread:
            thread = self._prompt("No proven active session found; enter exact thread ID")
        default_goal = str((selected_session or {}).get("title") or "Supervise adopted conversation")
        goal = self._prompt("Overall goal", default_goal)
        if not thread or not goal:
            return
        model = self._prompt("Model (blank = preserve Codex/default)")
        effort = self._prompt("Reasoning level (blank = preserve Codex/default)")
        checks: list[str] = []
        while len(checks) < MAX_VERIFY_COMMANDS:
            command = self._prompt("Frozen verification command (blank finishes)")
            if not command:
                break
            checks.append(command)
        try:
            spec = RunSpec(root, goal, model or None, effort or None, tuple(checks), thread_id=thread, service=True)
        except (ValueError, GitError) as exc:
            self.message = f"Adoption invalid: {exc}"
            return
        preview = "\n".join([
            "ADOPT EXACT THREAD",
            f"Thread       {thread}",
            f"Workspace    {root}",
            f"Goal         {goal}",
            f"Model        {model or 'preserve Codex/default'}",
            f"Reasoning    {effort or 'preserve Codex/default'}",
            "Verification " + ("\n             ".join(checks) if checks else "none (cannot reach trusted DONE)"),
        ])
        if self._confirm(preview):
            self.message = self._start_spec(spec)

    @staticmethod
    def _start_spec(spec: RunSpec) -> str:
        result = start_run(spec, run_in_service=spec.service)
        return result.message

    @staticmethod
    def _models_view() -> str:
        try:
            models = list_models()
        except RuntimeError as exc:
            return f"MODELS\n{exc}"
        return "MODELS · live installed Codex catalog\n" + "\n".join(
            f"{item['slug']:<24} default={item['default_reasoning_level']} · {','.join(item['supported_reasoning_levels'])}" for item in models
        )

    @staticmethod
    def _doctor_view(repo: Path) -> str:
        report = doctor_snapshot(repo)
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


class _NullRun:
    active = False


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
