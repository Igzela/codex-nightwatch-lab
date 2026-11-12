/**
 * Interactive Nightwatch TUI (TypeScript port of nightwatch/tui.py).
 * Raw-mode stdin + ANSI full-screen redraw — no extra npm deps.
 */

import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

import { GitError, repoRoot } from "./git.js";
import { readMailboxJson } from "./milestones.js";
import { State, TERMINAL_STATES, planProgress, type JsonObject } from "./models.js";
import {
  ActionResult,
  MAX_GOAL_CHARS,
  MAX_INSTRUCTION_CHARS,
  MAX_VERIFY_COMMANDS,
  RunSpec,
  ValueError,
  adoptRun,
  createWorktree,
  doctorSnapshot,
  listAccountChoices,
  listModels,
  queueSteer,
  resumeService,
  startRun,
  stopRun,
  validateHumanText,
} from "./operations.js";
import { NightPath } from "./path.js";
import { makeQuotaProvider } from "./quota.js";
import { RunCatalog, RunRecord } from "./run-catalog.js";
import { MAX_EVENT_BYTES, NightwatchStore, StateIntegrityError } from "./storage.js";
import { buildReport, listAdoptableSessions, pidAlive, processMatches } from "./supervisor.js";

export { RunCatalog, RunRecord };

const MAX_ADOPT_GOAL_DISPLAY = 240;
const VIEW_COMMANDS = new Set(["status", "plan", "logs", "timeline", "explain", "thread", "quota", "recap"]);

function nightwatchVersion(): string {
  try {
    const pkgPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "package.json");
    const raw = JSON.parse(fs.readFileSync(pkgPath, "utf8")) as { version?: string };
    return raw.version ?? "0.7.0";
  } catch {
    return "0.7.0";
  }
}

export function adoptGoalText(title: string | null | undefined, threadId: string | null = null, maxChars = MAX_GOAL_CHARS): string {
  let fallback = "Supervise adopted conversation";
  if (typeof threadId === "string" && threadId.trim()) {
    fallback = `Supervise adopted conversation (${threadId.trim()})`;
  }
  const raw = String(title ?? "").trim();
  if (!raw) return fallback;
  const first = raw.split(/\r\n|\n|\r/).map((line) => line.trim()).find((line) => line) ?? raw;
  if (first.length <= maxChars) return first;
  const trimmed = first.slice(0, Math.max(1, maxChars - 1)).replace(/\s+$/u, "");
  return `${trimmed}…`.slice(0, maxChars);
}

export class SlashCommand {
  constructor(
    readonly name: string,
    readonly summary: string,
    readonly usage: string,
    readonly mutates = false,
  ) {}
}

export const COMMANDS: readonly SlashCommand[] = [
  new SlashCommand("/run", "Create a supervised goal with a guided preview", "/run [goal]", true),
  new SlashCommand("/multi", "Show every trusted run/thread across repositories and worktrees", "/multi"),
  new SlashCommand("/status", "Open the selected run's live status", "/status"),
  new SlashCommand("/plan", "Show trusted milestone progress", "/plan"),
  new SlashCommand("/logs", "Show the recent supervisor log", "/logs"),
  new SlashCommand("/timeline", "Show sequence-validated lifecycle events", "/timeline"),
  new SlashCommand("/explain", "Explain why the selected run is in its current state", "/explain"),
  new SlashCommand("/thread", "Show exact thread and generation history", "/thread"),
  new SlashCommand("/quota", "Show 5h/weekly usage, authority, and reset time", "/quota"),
  new SlashCommand("/recap", "Show an evidence-grounded completion recap", "/recap"),
  new SlashCommand("/report", "Write the durable acceptance report and show its path", "/report", true),
  new SlashCommand("/models", "Show installed Codex models and reasoning levels", "/models"),
  new SlashCommand("/doctor", "Run environment, auth, quota, and service diagnostics", "/doctor"),
  new SlashCommand("/adopt", "Bind an existing exact Codex thread", "/adopt [thread]", true),
  new SlashCommand("/steer", "Queue a confirmed instruction to the selected exact thread", "/steer [instruction]", true),
  new SlashCommand("/resume", "Resume the selected run in its repo-specific user service", "/resume", true),
  new SlashCommand("/stop", "Stop the selected run while preserving durable state", "/stop", true),
  new SlashCommand("/help", "Show commands, keys, and trust semantics", "/help"),
  new SlashCommand("/quit", "Exit the TUI without stopping agents", "/quit"),
  new SlashCommand("/exit", "Exit the TUI without stopping agents", "/exit"),
];

const COMMAND_ALIASES: Record<string, string> = { new: "run", exit: "quit" };

export const HELP_TEXT = `COMMANDS AND KEYS
Type / to open the command menu. Filter by typing; ↑/↓ select; Enter runs the highlighted command.
Without / , text is a new goal or a steer to the selected active run. Mutating actions confirm first.

Esc closes the command menu, then a picker/confirm card, then a view, then clears the input.
Empty dashboard Esc does not quit. Leave with /quit, /exit, q, or Ctrl+C.

↑/↓ with an empty input selects a run. PgUp/PgDn scroll the current view.
Absolute paths such as /home/... are not commands.

/run [goal]     confirm a supervised goal (verify commands optional on the confirm card)
/adopt [thread] pick a live or recent Codex session; binds the exact thread without starting a writer
/steer [text]   queue an instruction to the selected exact thread
/resume         start unattended supervision after the interactive Codex process exits
/stop           stop supervision and keep durable state
/status /plan /logs /timeline /explain /thread /quota /recap /models /doctor /help /multi
`;

export function isSlashComposer(text: string): boolean {
  if (!text.startsWith("/")) return false;
  const first = text.split(/\s+/, 1)[0] ?? text;
  return !first.slice(1).includes("/");
}

export function slashCommands(prefix = ""): SlashCommand[] {
  const clean = prefix.trim().toLowerCase().replace(/^\//, "");
  if (!clean) return [...COMMANDS];
  const prefixHits = COMMANDS.filter((item) => item.name.replace(/^\//, "").startsWith(clean));
  if (prefixHits.length) return prefixHits;
  return COMMANDS.filter(
    (item) => item.name.replace(/^\//, "").toLowerCase().includes(clean) || item.summary.toLowerCase().includes(clean),
  );
}

export function palettePrefix(value: string): string {
  const parts = value.replace(/^\//, "").split(/\s+/, 2);
  return parts[0] ?? "";
}

export class Intent {
  constructor(
    readonly kind: string,
    readonly argument: string | null = null,
    readonly requiresConfirmation = false,
  ) {}
}

export function routeInput(text: string, hasActiveRun: boolean): Intent {
  const value = text.trim();
  if (!value) return new Intent("noop");
  if (value === "/") return new Intent("palette");
  if (isSlashComposer(value)) {
    const space = value.indexOf(" ");
    const command = space === -1 ? value : value.slice(0, space);
    const argument = space === -1 ? "" : value.slice(space + 1);
    const name = COMMAND_ALIASES[command.slice(1).toLowerCase()] ?? command.slice(1).toLowerCase();
    const item = COMMANDS.find((candidate) => candidate.name.slice(1) === name);
    if (!item) return new Intent("error", `Unknown command: ${command}`);
    return new Intent(name, argument.trim() || null, item.mutates);
  }
  return new Intent(hasActiveRun ? "steer" : "run", value, true);
}

export function terminalSafe(value: unknown): string {
  const text = String(value);
  let out = "";
  for (const character of text) {
    if (character === "\n" || character === "\t") {
      out += character;
      continue;
    }
    const code = character.codePointAt(0) ?? 0;
    // Strip Cc / Cf categories (control + format), matching Python unicodedata filter.
    if ((code <= 0x1f && character !== "\n" && character !== "\t") || code === 0x7f || (code >= 0x80 && code <= 0x9f) || (code >= 0x200b && code <= 0x200f) || code === 0xfeff || (code >= 0x202a && code <= 0x202e)) {
      continue;
    }
    out += character;
  }
  return out;
}

function wrapLine(source: string, width: number): string[] {
  const maxWidth = Math.max(20, width);
  if (!source) return [""];
  if (source.length <= maxWidth) return [source];
  const lines: string[] = [];
  let rest = source;
  while (rest.length > maxWidth) {
    let cut = rest.lastIndexOf(" ", maxWidth);
    if (cut < Math.floor(maxWidth * 0.4)) cut = maxWidth;
    lines.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\s+/, "");
  }
  if (rest.length || !lines.length) lines.push(rest);
  return lines;
}

export class OverlayItem {
  constructor(
    public key: string,
    public title: string,
    public detail = "",
    public payload: Record<string, unknown> = {},
  ) {}
}

export class Overlay {
  constructor(
    public kind: string,
    public title = "",
    public body = "",
    public items: OverlayItem[] = [],
    public selected = 0,
    public mode = "",
  ) {}
}

export type DiscoverSessionsFn = (repo: string | NightPath) => JsonObject[];
export type AdoptFn = (spec: RunSpec) => ActionResult;
export type StartRunFn = (spec: RunSpec, runInService?: boolean) => ActionResult;
export type CreateWorktreeFn = (sourceRepo: string | NightPath, label: string) => NightPath;
export type QueueSteerFn = (store: NightwatchStore, instruction: string) => ActionResult;
export type ResumeFn = (repo: string | NightPath) => ActionResult;
export type StopFn = (repo: string | NightPath) => ActionResult;
export type RunExistsFn = (repo: string | NightPath) => boolean;
export type ListModelsFn = (provider?: string) => JsonObject[];
export type ListAccountsFn = () => ReturnType<typeof listAccountChoices>;
export type DoctorFn = (repo?: string | NightPath | null) => JsonObject;
export type WriteReportFn = (run: RunRecord) => NightPath;

export class TuiHooks {
  discoverSessions: DiscoverSessionsFn | null = null;
  adopt: AdoptFn | null = null;
  startRun: StartRunFn | null = null;
  createWorktree: CreateWorktreeFn | null = null;
  queueSteer: QueueSteerFn | null = null;
  resume: ResumeFn | null = null;
  stop: StopFn | null = null;
  runExists: RunExistsFn | null = null;
  listModels: ListModelsFn | null = null;
  listAccounts: ListAccountsFn | null = null;
  doctor: DoctorFn | null = null;
  writeReport: WriteReportFn | null = null;
}

type Pending =
  | {
      kind: "run";
      goal: string;
      repo: NightPath;
      verify: string[];
      model: string | null;
      effort: string | null;
      worktree: string | null;
      account_mode: string;
      account_selectors: string[];
    }
  | {
      kind: "adopt";
      thread: string;
      goal: string;
      repo: NightPath;
      verify: string[];
      model: string | null;
      session: JsonObject | null;
    }
  | { kind: "steer"; instruction: string; run: RunRecord }
  | { kind: "stop"; run: RunRecord }
  | { kind: "resume"; run: RunRecord }
  | { kind: "report"; run: RunRecord };

export class TuiController {
  repo: NightPath | null;
  runs: RunRecord[] = [];
  hooks: TuiHooks;
  selected = 0;
  view = "dashboard";
  content: string | null = null;
  composer = "";
  overlay: Overlay | null = null;
  message = "Type / to discover commands.";
  scroll = 0;
  quit = false;
  pending: Pending | null = null;
  awaiting: string | null = null;
  catalogErrors: string[] = [];

  constructor(options?: { repo?: string | NightPath | null; runs?: RunRecord[]; hooks?: TuiHooks }) {
    const repo = options?.repo ?? null;
    this.repo = repo == null ? null : new NightPath(typeof repo === "string" ? repo : repo.raw).expanduser();
    this.runs = [...(options?.runs ?? [])];
    this.hooks = options?.hooks ?? new TuiHooks();
  }

  handleKey(key: string): void {
    if (this.quit) return;
    if (key === "ctrl-c") {
      this.quit = true;
      return;
    }
    if (this.overlay != null) {
      this.handleOverlayKey(key);
      return;
    }
    if (key === "esc") {
      this.handleEsc();
      return;
    }
    if (key === "up") {
      if (!this.composer) this.selected = Math.max(0, this.selected - 1);
      return;
    }
    if (key === "down") {
      if (!this.composer) this.selected = Math.min(Math.max(0, this.runs.length - 1), this.selected + 1);
      return;
    }
    if (key === "pageup") {
      this.scroll = Math.max(0, this.scroll - 10);
      return;
    }
    if (key === "pagedown") {
      this.scroll += 10;
      return;
    }
    if (key === "enter") {
      this.submit();
      return;
    }
    if (key === "backspace") {
      this.composer = this.composer.slice(0, -1);
      return;
    }
    if (key === "q" && !this.composer) {
      this.quit = true;
      return;
    }
    if (key.length === 1 && isPrintable(key)) {
      if (this.composer.length < MAX_GOAL_CHARS) this.composer += key;
      if (isSlashComposer(this.composer)) this.syncSlashMenu();
    }
  }

  render(width = 80, height = 24): string {
    width = Math.max(40, width);
    height = Math.max(8, height);
    const body = this.content != null ? this.content : renderDashboard(this.runs, this.selected, width, this.catalogErrors);
    const lines: string[] = [];
    const bodyLines = terminalSafe(body).split(/\r\n|\n|\r/);
    for (const source of bodyLines.length ? bodyLines : [""]) {
      lines.push(...wrapLine(source, width - 1));
    }
    const overlayLines = this.overlayLines(width, height);
    const footer = [
      terminalSafe(this.footerMessage()).slice(0, Math.max(1, width - 1)),
      `Input › ${this.composer}`.slice(0, Math.max(1, width - 1)),
    ];
    const reserved = overlayLines.length + 2;
    const visibleH = Math.max(1, height - reserved);
    const start = Math.min(this.scroll, Math.max(0, lines.length - visibleH));
    const visible = lines.slice(start, start + visibleH);
    while (visible.length < visibleH) visible.push("");
    return [...visible, ...overlayLines, ...footer].join("\n");
  }

  private overlayLines(width: number, height: number): string[] {
    const overlay = this.overlay;
    if (!overlay) return [];
    const out = [`── ${overlay.title} ──`.slice(0, width - 1)];
    if (overlay.body) {
      for (const line of overlay.body.split(/\r\n|\n|\r/).length ? overlay.body.split(/\r\n|\n|\r/) : [""]) {
        out.push(...wrapLine(terminalSafe(line), width - 1));
      }
    }
    if (overlay.items.length) {
      const limit = Math.max(3, height - out.length - 4);
      let start = 0;
      if (overlay.selected >= start + limit) start = overlay.selected - limit + 1;
      const window = overlay.items.slice(start, start + limit);
      for (let index = 0; index < window.length; index += 1) {
        const absolute = start + index;
        const item = window[index]!;
        let mark = absolute === overlay.selected ? "▶" : " ";
        if (overlay.kind === "accounts") mark = item.payload.selected ? "[x]" : "[ ]";
        const detail = item.detail ? `  ${item.detail}` : "";
        out.push(`${mark} ${padRight(item.key, 12)} ${item.title}${detail}`.slice(0, width - 1));
      }
    }
    return out;
  }

  private footerMessage(): string {
    const overlay = this.overlay;
    if (!overlay) return this.message;
    if (overlay.kind === "slash") return "↑/↓ select · Enter runs · Esc closes the menu";
    if (overlay.kind === "picker") return "↑/↓ select a session · Enter continues · Esc cancels";
    if (overlay.kind === "accounts") return "↑/↓ move · Space toggle · Enter confirms pool · c current-only · Esc cancels";
    if (overlay.kind === "confirm") {
      if (overlay.mode === "worktree") return "Type a worktree label · Enter continues · Esc cancels";
      return "Empty Enter confirms · Esc cancels · type a verify command then Enter to add it";
    }
    return this.message;
  }

  selectedRun(): RunRecord | null {
    if (!this.runs.length) return null;
    return this.runs[Math.min(Math.max(0, this.selected), this.runs.length - 1)] ?? null;
  }

  private hasActive(): boolean {
    const run = this.selectedRun();
    return Boolean(run?.active);
  }

  private syncSlashMenu(): void {
    if (!isSlashComposer(this.composer)) {
      if (this.overlay?.kind === "slash") this.overlay = null;
      return;
    }
    const matches = slashCommands(palettePrefix(this.composer));
    const selected = this.overlay?.kind === "slash" ? this.overlay.selected : 0;
    const items = matches.map((item) => new OverlayItem(item.name, item.summary, item.usage, { mutates: item.mutates }));
    this.overlay = new Overlay("slash", "COMMANDS", "", items, Math.min(selected, Math.max(0, items.length - 1)));
  }

  private handleEsc(): void {
    if (this.view !== "dashboard" && this.view !== "multi") {
      this.view = "dashboard";
      this.content = null;
      this.scroll = 0;
      return;
    }
    if (this.composer || this.awaiting) {
      this.composer = "";
      this.awaiting = null;
    }
  }

  private handleOverlayKey(key: string): void {
    const overlay = this.overlay;
    if (!overlay) return;
    if (overlay.kind === "slash") {
      if (key === "esc") {
        this.overlay = null;
        this.composer = "";
        return;
      }
      if (key === "up") {
        overlay.selected = Math.max(0, overlay.selected - 1);
        return;
      }
      if (key === "down") {
        overlay.selected = Math.min(Math.max(0, overlay.items.length - 1), overlay.selected + 1);
        return;
      }
      if (key === "enter") {
        this.submitSlash();
        return;
      }
      if (key === "backspace") {
        this.composer = this.composer.slice(0, -1);
        if (!this.composer) this.overlay = null;
        else this.syncSlashMenu();
        return;
      }
      if (key.length === 1 && isPrintable(key) && this.composer.length < MAX_GOAL_CHARS) {
        this.composer += key;
        this.syncSlashMenu();
      }
      return;
    }
    if (overlay.kind === "picker") {
      if (key === "esc") {
        this.overlay = null;
        this.composer = "";
        return;
      }
      if (key === "up") {
        overlay.selected = Math.max(0, overlay.selected - 1);
        return;
      }
      if (key === "down") {
        overlay.selected = Math.min(Math.max(0, overlay.items.length - 1), overlay.selected + 1);
        return;
      }
      if (key === "enter") {
        const typed = this.composer.trim();
        if (typed && !/^\d+$/.test(typed)) {
          this.openAdoptConfirm(typed, null);
          return;
        }
        let chosen: OverlayItem | undefined;
        if (/^\d+$/.test(typed) && overlay.items.length && Number(typed) >= 1 && Number(typed) <= overlay.items.length) {
          chosen = overlay.items[Number(typed) - 1];
        } else if (overlay.items.length) {
          chosen = overlay.items[Math.min(overlay.selected, overlay.items.length - 1)];
        } else {
          this.message = "No conversation selected. Type an exact thread ID.";
          return;
        }
        const thread = chosen?.payload.thread_id;
        if (typeof thread !== "string" || !thread) {
          this.message = "Pick a conversation with a thread ID, or type the exact thread ID.";
          return;
        }
        this.openAdoptConfirm(thread, chosen.payload as JsonObject);
        return;
      }
      if (key === "backspace") {
        this.composer = this.composer.slice(0, -1);
        return;
      }
      if (key.length === 1 && isPrintable(key) && this.composer.length < MAX_GOAL_CHARS) this.composer += key;
      return;
    }
    if (overlay.kind === "accounts") {
      if (key === "esc") {
        this.overlay = null;
        this.composer = "";
        return;
      }
      if (key === "up") {
        overlay.selected = Math.max(0, overlay.selected - 1);
        return;
      }
      if (key === "down") {
        overlay.selected = Math.min(Math.max(0, overlay.items.length - 1), overlay.selected + 1);
        return;
      }
      if (key === "space") {
        if (overlay.items.length) {
          const item = overlay.items[overlay.selected]!;
          item.payload.selected = !Boolean(item.payload.selected);
        }
        return;
      }
      if (key === "enter") {
        const selected = overlay.items.filter((item) => item.payload.selected).map((item) => String(item.payload.account_key));
        if (!selected.length) {
          this.message = "Select at least one account, or Esc for current account only.";
          return;
        }
        if (this.pending && this.pending.kind === "run") {
          this.pending.account_mode = "auto-pool";
          this.pending.account_selectors = selected;
        }
        this.overlay = null;
        this.composer = "";
        this.refreshConfirmBody();
        return;
      }
      if (key === "c") {
        if (this.pending && this.pending.kind === "run") {
          this.pending.account_mode = "current-only";
          this.pending.account_selectors = [];
        }
        this.overlay = null;
        this.composer = "";
        this.refreshConfirmBody();
      }
      return;
    }
    if (overlay.kind === "confirm") {
      if (key === "esc") {
        this.overlay = null;
        this.pending = null;
        this.composer = "";
        return;
      }
      if (key === "enter") {
        if (overlay.mode === "worktree") {
          const label = this.composer.trim();
          if (!label) {
            this.message = "Worktree label is required because this workspace already has a run.";
            return;
          }
          if (this.pending && this.pending.kind === "run") this.pending.worktree = label;
          this.composer = "";
          this.showRunPreview();
          return;
        }
        const typed = this.composer.trim();
        if (typed === "/" || (isSlashComposer(typed) && (typed.match(/\//g) ?? []).length === 1)) {
          this.composer = "";
          this.message = "Slash commands are unavailable on this confirm card. Empty Enter confirms; Esc cancels.";
          return;
        }
        if (this.pending && (this.pending.kind === "run" || this.pending.kind === "adopt") && typed) {
          this.addVerify(typed);
          this.composer = "";
          this.refreshConfirmBody();
          return;
        }
        this.commitConfirm();
        return;
      }
      if (overlay.mode === "run" && key === "a" && !this.composer) {
        this.openAccountPicker();
        return;
      }
      if (key === "backspace") {
        this.composer = this.composer.slice(0, -1);
        return;
      }
      if (key.length === 1 && isPrintable(key) && this.composer.length < MAX_GOAL_CHARS) this.composer += key;
    }
  }

  private submitSlash(): void {
    const overlay = this.overlay;
    const composer = this.composer;
    this.overlay = null;
    this.composer = "";
    if (overlay && overlay.items.length) {
      const chosen = overlay.items[Math.min(overlay.selected, overlay.items.length - 1)]!;
      const kind = chosen.key === "/quit" || chosen.key === "/exit" ? "quit" : chosen.key.slice(1);
      let argument: string | null = null;
      if (isSlashComposer(composer) && composer.trim() !== "/" && composer.trim() !== chosen.key) {
        const parsed = routeInput(composer, this.hasActive());
        if (parsed.kind === kind) argument = parsed.argument;
      }
      this.dispatch(new Intent(kind, argument, Boolean(chosen.payload.mutates)));
      return;
    }
    this.dispatch(routeInput(composer, this.hasActive()));
  }

  private submit(): void {
    if (this.awaiting === "run_goal") {
      const goal = this.composer.trim();
      this.composer = "";
      this.awaiting = null;
      if (goal) this.openRunConfirm(goal);
      return;
    }
    if (this.awaiting === "steer") {
      const instruction = this.composer.trim();
      this.composer = "";
      this.awaiting = null;
      if (instruction) this.dispatch(new Intent("steer", instruction, true));
      return;
    }
    const text = this.composer;
    if (!text.trim()) {
      const run = this.selectedRun();
      if (run) {
        this.view = "status";
        this.content = this.safeView("status", run);
        this.scroll = 0;
      }
      return;
    }
    this.composer = "";
    this.dispatch(routeInput(text, this.hasActive()));
  }

  private dispatch(intent: Intent): void {
    if (intent.kind === "noop") return;
    if (intent.kind === "quit") {
      this.quit = true;
      return;
    }
    if (intent.kind === "error") {
      this.message = intent.argument || "Unknown command";
      return;
    }
    if (intent.kind === "palette" || intent.kind === "help") {
      this.view = "help";
      this.content = HELP_TEXT;
      this.scroll = 0;
      return;
    }
    if (intent.kind === "multi") {
      this.view = "dashboard";
      this.content = null;
      this.scroll = 0;
      return;
    }
    if (intent.kind === "run") {
      if (intent.argument) this.openRunConfirm(intent.argument);
      else {
        this.awaiting = "run_goal";
        this.message = "Type the overall goal, then Enter.";
      }
      return;
    }
    if (intent.kind === "adopt") {
      if (intent.argument) this.openAdoptConfirm(intent.argument, null);
      else this.openAdoptPicker();
      return;
    }
    if (intent.kind === "models") {
      this.view = "models";
      this.content = this.modelsView();
      this.scroll = 0;
      return;
    }
    if (intent.kind === "doctor") {
      this.view = "doctor";
      const selected = this.selectedRun();
      const repo = selected?.repo ?? this.repo ?? new NightPath(process.cwd());
      this.content = this.doctorView(repo);
      this.scroll = 0;
      return;
    }
    if (intent.kind === "steer") {
      this.openSteerConfirm(intent.argument);
      return;
    }
    if (VIEW_COMMANDS.has(intent.kind)) {
      const run = this.selectedRun();
      if (!run) {
        this.message = "No run selected. Use /run or enter a natural-language goal.";
        return;
      }
      this.view = intent.kind;
      this.content = this.safeView(intent.kind, run);
      this.scroll = 0;
      return;
    }
    if (intent.kind === "report") {
      const run = this.selectedRun();
      if (!run) {
        this.message = "No run selected. Use /run or enter a natural-language goal.";
        return;
      }
      this.pending = { kind: "report", run };
      this.overlay = new Overlay("confirm", "WRITE REPORT", "Write a durable report from trusted state and verification evidence?");
      return;
    }
    if (intent.kind === "stop") {
      const run = this.selectedRun();
      if (!run) {
        this.message = "No run selected. Use /run or enter a natural-language goal.";
        return;
      }
      this.pending = { kind: "stop", run };
      this.overlay = new Overlay("confirm", "STOP RUN", `Stop ${String(run.state.run_id ?? run)} and preserve its exact thread/state?`);
      return;
    }
    if (intent.kind === "resume") {
      const run = this.selectedRun();
      if (!run) {
        this.message = "No run selected. Use /run or enter a natural-language goal.";
        return;
      }
      this.pending = { kind: "resume", run };
      this.overlay = new Overlay(
        "confirm",
        "RESUME RUN",
        `Resume exact thread ${run.threadId || "(not captured)"} in its repo-specific user service?\nThis starts a writer only after no interactive Codex process is using the workspace.`,
      );
      return;
    }
    this.message = `Unknown command: ${intent.kind}`;
  }

  safeView(view: string, run: RunRecord): string {
    try {
      return viewContent(view, run);
    } catch (error) {
      return `${view.toUpperCase()} unavailable: ${error instanceof Error ? error.name : "Error"}`;
    }
  }

  private modelsView(): string {
    try {
      const models = this.hooks.listModels ? this.hooks.listModels() : listModels();
      return (
        "MODELS · live installed Codex catalog\n" +
        models
          .map((item) => {
            const slug = String(item.slug ?? "");
            const levels = Array.isArray(item.supported_reasoning_levels) ? item.supported_reasoning_levels.join(",") : "";
            return `${padRight(slug, 24)} default=${String(item.default_reasoning_level)} · ${levels}`;
          })
          .join("\n")
      );
    } catch (error) {
      return `MODELS\n${error instanceof Error ? error.message : String(error)}`;
    }
  }

  private doctorView(repo: NightPath): string {
    const report = this.hooks.doctor ? this.hooks.doctor(repo) : doctorSnapshot(repo);
    const auth = isRecord(report.auth) ? report.auth : {};
    const quota = isRecord(report.quota) ? report.quota : {};
    const pool = isRecord(report.account_pool) ? report.account_pool : {};
    const lines = [
      `Nightwatch doctor: ${String(report.status ?? "")}`,
      `Codex: ${report.codex_version == null ? "(missing)" : String(report.codex_version)}`,
      `Auth: ${String(auth.status ?? "")}`,
      `Account pool: ${String(pool.status ?? "optional unavailable")} (${String(pool.count ?? 0)} stored)`,
      `Quota authority: ${String(quota.authority ?? "unavailable")} (${String(quota.source ?? "none")})`,
    ];
    if (isRecord(quota.primary)) {
      lines.push(`5h: ${usedPercentText(quota.primary) ?? "?"}% used, reset=${String(quota.primary.resets_at)}`);
    }
    if (isRecord(quota.secondary)) {
      lines.push(`weekly: ${usedPercentText(quota.secondary) ?? "?"}% used, reset=${String(quota.secondary.resets_at)}`);
    }
    lines.push(`systemd-inhibit: ${report.systemd_inhibit ? "available" : "unavailable"}`);
    return lines.join("\n");
  }

  private openRunConfirm(goal: string): void {
    const repo = this.repo ?? new NightPath(process.cwd());
    let exists: boolean;
    if (this.hooks.runExists) exists = Boolean(this.hooks.runExists(repo));
    else {
      try {
        exists = new NightwatchStore(repo.raw).exists();
      } catch (error) {
        if (error instanceof StateIntegrityError || error instanceof GitError || isErrno(error)) exists = false;
        else throw error;
      }
    }
    this.pending = {
      kind: "run",
      goal,
      repo,
      verify: [],
      model: null,
      effort: null,
      worktree: null,
      account_mode: "current-only",
      account_selectors: [],
    };
    this.composer = "";
    if (exists) {
      this.overlay = new Overlay(
        "confirm",
        "WORKTREE REQUIRED",
        "This workspace already has a run. Type an isolated worktree label, then Enter.\nNightwatch will not start a second writer in the same working directory.",
        [],
        0,
        "worktree",
      );
      return;
    }
    this.showRunPreview();
  }

  private showRunPreview(): void {
    const pending = this.pending;
    if (!pending || pending.kind !== "run") return;
    const repo = pending.repo;
    const label = pending.worktree;
    const target = label ? repo.parent.join(".worktrees", repo.name, label) : repo;
    const checks = pending.verify;
    const verifyText = checks.length ? checks.join("\n             ") : "none (cannot reach trusted DONE)";
    const accountMode = pending.account_mode;
    const accountSelectors = pending.account_selectors;
    const accountText =
      accountMode === "auto-pool" ? `AUTO_POOL · ${accountSelectors.length} explicitly selected` : "CURRENT_ONLY · current account";
    this.overlay = new Overlay(
      "confirm",
      "CONFIRM RUN",
      [
        "NEW SUPERVISED RUN",
        `Goal        ${pending.goal}`,
        `Workspace   ${target.raw}`,
        `Isolation   ${label ? "new worktree from committed HEAD" : "current workspace"}`,
        "Model       Codex default",
        "Reasoning   Codex default",
        `Verification ${verifyText}`,
        `Accounts    ${accountText}`,
        accountMode === "auto-pool" ? "Authority   Codex App Server · Policy max usable capacity" : "Authority   Codex App Server",
        "Service      repo-specific systemd user unit",
        "Press `a` to select an explicit account pool; type a verify command and Enter to add it; empty Enter confirms.",
      ].join("\n"),
      [],
      0,
      "run",
    );
  }

  private openAccountPicker(): void {
    if (!this.pending || this.pending.kind !== "run") return;
    let accounts: ReturnType<typeof listAccountChoices>;
    try {
      accounts = this.hooks.listAccounts ? this.hooks.listAccounts() : listAccountChoices();
    } catch (error) {
      this.message = `Account pool unavailable; current account only (${error instanceof Error ? error.name : "Error"})`;
      return;
    }
    const selected = new Set(this.pending.account_selectors);
    const items = accounts.map(
      (item) =>
        new OverlayItem(item.fingerprint, item.displayName, item.plan || item.authMode || "stored account", {
          account_key: item.accountKey,
          selected: selected.has(item.accountKey),
        }),
    );
    if (!items.length) {
      this.message = "No stored accounts are available; current account only.";
      return;
    }
    this.overlay = new Overlay(
      "accounts",
      "SELECT ACCOUNT POOL",
      "Space toggles an account · Enter confirms AUTO_POOL · c keeps CURRENT_ONLY · Esc cancels",
      items,
    );
    this.composer = "";
  }

  private openAdoptPicker(): void {
    const repo = this.repo ?? new NightPath(process.cwd());
    const discover = this.hooks.discoverSessions ?? listAdoptableSessions;
    let sessions: JsonObject[] = [];
    try {
      sessions = discover(repo);
    } catch (error) {
      this.message = `Could not list Codex sessions: ${error instanceof Error ? error.name : "Error"}`;
    }
    const items: OverlayItem[] = [];
    const unbound: JsonObject[] = [];
    for (const session of sessions) {
      if (session.thread_source === "subagent") continue;
      if (!session.thread_id) {
        unbound.push(session);
        continue;
      }
      const title = String(session.title ?? "").slice(0, 60);
      const proof = session.live ? "live" : "recent";
      const pid = session.pid != null ? `PID ${session.pid}` : proof;
      items.push(new OverlayItem(String(session.thread_id), title || proof, pid, session));
    }
    let liveNote = "";
    if (unbound.length) {
      liveNote =
        "LIVE CODEX WITHOUT PROVEN THREAD\n" +
        unbound.map((item) => `PID ${String(item.pid)} · ${String(item.title)}`).join("\n") +
        "\nPick a conversation below or type the exact thread ID.\n\n";
    }
    const body =
      liveNote +
      (items.length
        ? "↑/↓ select · Enter adopts · type an exact thread ID as fallback"
        : "No adoptable conversations. Type an exact thread ID.");
    this.overlay = new Overlay("picker", "ADOPT CODEX SESSION", body, items);
    this.composer = "";
  }

  private openAdoptConfirm(thread: string, session: JsonObject | null): void {
    const repo = this.repo ?? new NightPath(process.cwd());
    const goal = adoptGoalText(session ? String(session.title ?? "") : null, thread);
    this.pending = {
      kind: "adopt",
      thread,
      goal,
      repo,
      verify: [],
      model: session && typeof session.model === "string" ? session.model : null,
      session,
    };
    this.composer = "";
    this.showAdoptPreview();
  }

  private showAdoptPreview(): void {
    const pending = this.pending;
    if (!pending || pending.kind !== "adopt") return;
    const checks = pending.verify;
    const verifyText = checks.length ? checks.join("\n             ") : "none (cannot reach trusted DONE)";
    let goal = pending.goal;
    if (goal.length > MAX_ADOPT_GOAL_DISPLAY) goal = `${goal.slice(0, MAX_ADOPT_GOAL_DISPLAY - 1)}…`;
    this.overlay = new Overlay(
      "confirm",
      "CONFIRM ADOPT",
      [
        "ADOPT EXACT THREAD",
        `Thread       ${pending.thread}`,
        `Workspace    ${pending.repo.raw}`,
        `Goal         ${goal}`,
        "Model        preserve Codex/default",
        "Reasoning    preserve Codex/default",
        `Verification ${verifyText}`,
        "Binds the thread only. Interactive Codex is not killed; /resume starts a writer after it exits.",
        "Type a verify command and Enter to add it; empty Enter confirms.",
      ].join("\n"),
      [],
      0,
      "adopt",
    );
  }

  private openSteerConfirm(instruction: string | null): void {
    const run = this.selectedRun();
    if (!run || !run.active) {
      this.message = "No active run selected. Use /run or /adopt first.";
      return;
    }
    if (run.terminal) {
      this.message =
        "Instruction was NOT queued because this Nightwatch run is terminal.\nUse /resume or start a new supervised run before steering.";
      return;
    }
    if (run.provider === "agy") {
      this.message = "AGY live steering is not supported by the current upstream CLI; no instruction was sent.";
      return;
    }
    if (!instruction) {
      this.awaiting = "steer";
      this.message = "Type the instruction for the selected exact thread.";
      return;
    }
    this.pending = { kind: "steer", instruction, run };
    this.overlay = new Overlay("confirm", "CONFIRM STEER", `Queue to exact thread ${run.threadId}?\n\n${instruction}`);
  }

  private addVerify(command: string): void {
    if (!this.pending || (this.pending.kind !== "run" && this.pending.kind !== "adopt")) return;
    const checks = [...this.pending.verify];
    if (checks.length >= MAX_VERIFY_COMMANDS) {
      this.message = "Maximum verification commands reached.";
      return;
    }
    try {
      checks.push(validateHumanText(command, "verification command", MAX_INSTRUCTION_CHARS));
    } catch (error) {
      this.message = error instanceof Error ? error.message : String(error);
      return;
    }
    this.pending.verify = checks;
  }

  private refreshConfirmBody(): void {
    if (!this.pending) return;
    if (this.pending.kind === "run") this.showRunPreview();
    else if (this.pending.kind === "adopt") this.showAdoptPreview();
  }

  private commitConfirm(): void {
    const pending = this.pending;
    this.overlay = null;
    this.pending = null;
    this.composer = "";
    if (!pending) return;
    if (pending.kind === "run") this.message = this.commitRun(pending);
    else if (pending.kind === "adopt") this.message = this.commitAdopt(pending);
    else if (pending.kind === "steer") {
      const fn = this.hooks.queueSteer ?? queueSteer;
      this.message = fn(pending.run.store, pending.instruction).message;
    } else if (pending.kind === "stop") {
      const fn = this.hooks.stop ?? stopRun;
      this.message = fn(pending.run.repo).message;
    } else if (pending.kind === "resume") {
      const fn = this.hooks.resume ?? resumeService;
      this.message = fn(pending.run.repo).message;
    } else if (pending.kind === "report") this.message = this.commitReport(pending.run);
  }

  private commitRun(pending: Extract<Pending, { kind: "run" }>): string {
    let repo = pending.repo;
    try {
      if (pending.worktree) {
        const create = this.hooks.createWorktree ?? createWorktree;
        repo = create(repo, pending.worktree);
      }
      const spec = new RunSpec({
        repo,
        goal: pending.goal,
        model: pending.model,
        reasoningEffort: pending.effort,
        verifyCommands: pending.verify,
        service: true,
        accountMode: pending.account_mode,
        accountSelectors: pending.account_selectors,
      });
      const start = this.hooks.startRun ?? startRun;
      return start(spec, true).message;
    } catch (error) {
      if (error instanceof ValueError || error instanceof GitError || error instanceof StateIntegrityError) {
        return `Run not started: ${error.message}`;
      }
      throw error;
    }
  }

  private commitAdopt(pending: Extract<Pending, { kind: "adopt" }>): string {
    try {
      const model = typeof pending.model === "string" ? pending.model : null;
      const spec = new RunSpec({
        repo: pending.repo,
        goal: adoptGoalText(pending.goal, pending.thread),
        model,
        verifyCommands: pending.verify,
        threadId: String(pending.thread),
        service: false,
      });
      const adopt = this.hooks.adopt ?? adoptRun;
      return adopt(spec).message;
    } catch (error) {
      if (error instanceof ValueError || error instanceof GitError) return `Adoption invalid: ${error.message}`;
      throw error;
    }
  }

  private commitReport(run: RunRecord): string {
    try {
      let reportPath: NightPath;
      if (this.hooks.writeReport) reportPath = this.hooks.writeReport(run);
      else {
        const state = run.store.loadState();
        const verification = isRecord(state.last_verification) ? state.last_verification : null;
        reportPath = run.store.writeReport(buildReport(run.store, state, verification));
      }
      return `Report written: ${reportPath.raw}`;
    } catch (error) {
      return `Report not written: ${error instanceof Error ? error.name : "Error"}`;
    }
  }
}

function isPrintable(key: string): boolean {
  if (key.length !== 1) return false;
  const code = key.charCodeAt(0);
  return code >= 32 && code !== 127;
}

function isRecord(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isErrno(error: unknown): error is NodeJS.ErrnoException {
  return typeof error === "object" && error !== null && typeof (error as NodeJS.ErrnoException).code === "string";
}

function padRight(value: string, width: number): string {
  return value.length >= width ? value : value + " ".repeat(width - value.length);
}

function bar(percent: number, width = 18): string {
  const filled = Math.min(width, Math.max(0, Math.round((percent / 100) * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}

function usedPercentText(window: JsonObject): string | null {
  const raw = window.used_percent;
  if (typeof raw === "number" && Number.isFinite(raw)) return String(raw);
  // Some fixtures / adapters nest `{ value: number }`.
  if (isRecord(raw) && typeof raw.value === "number" && Number.isFinite(raw.value)) return String(raw.value);
  return raw == null ? null : String(raw);
}

function quotaLine(state: JsonObject): string {
  const quota = isRecord(state.quota) ? state.quota : {};
  const values: string[] = [];
  for (const [key, label] of [
    ["primary", "5h"],
    ["secondary", "week"],
  ] as const) {
    const window = quota[key];
    if (!isRecord(window)) continue;
    const used = usedPercentText(window);
    if (used != null) values.push(`${label} ${used}%`);
  }
  return values.join(" · ") || "not sampled";
}

function accountLine(state: JsonObject): string {
  const mode = state.account_mode ?? "CURRENT_ONLY";
  if (mode !== "AUTO_POOL") return "CURRENT_ONLY · current account";
  const total = Array.isArray(state.authorized_accounts) ? state.authorized_accounts.length : 0;
  const current = state.current_account_fingerprint || "none selected";
  if (state.state === State.WAIT_QUOTA) {
    const reason = state.pool_wait_reason || "re-probe pending";
    return `AUTO_POOL · ${total} authorized · ${reason} · next ${state.next_resume_at || "unknown"}`;
  }
  const lease = state.account_lease ? "lease owned" : "lease not held";
  return `AUTO_POOL · ${total} authorized · ${current} · ${lease}`;
}

function nextAction(state: JsonObject): string {
  if (state.state === State.WAIT_QUOTA) {
    if ((state.account_mode ?? "CURRENT_ONLY") === "AUTO_POOL") return "re-probe account pool at the earliest relevant reset";
    if (!state.thread_id) return "revalidate quota at reset, then start first thread";
    return "revalidate quota at reset, then resume exact thread";
  }
  const mapping: Record<string, string> = {
    [State.NEW]: "start supervisor preflight",
    [State.PREFLIGHT]: "validate repo, auth, and quota",
    [State.RUNNING]: "continue current milestone",
    [State.RETRY_BACKOFF]: "retry after bounded backoff",
    [State.RECOVERING]: "prove recovery state, then resume exact thread",
    [State.VERIFYING]: "run frozen trusted verification",
    [State.AWAITING_ACCEPTANCE]: "add an authorized verification policy",
    [State.BLOCKED]: "review blocker before explicit resume",
    [State.STOPPED]: "resume only on explicit request",
    [State.DONE]: "review recap/report or start another worktree run",
    [State.FAILED]: "inspect failure evidence before explicit resume",
  };
  return mapping[String(state.state)] ?? "inspect trusted state";
}

function agentSummary(state: JsonObject): string {
  const active = state.active_process;
  if (isRecord(active) && processMatches(active as { pid?: unknown; starttime?: unknown; executable?: unknown })) {
    return `RUNNING · PID ${String(active.pid)} · ${String(active.action || "provider")}`;
  }
  const owner = state.supervisor_owner;
  let ownerAlive = false;
  if (isRecord(owner)) {
    ownerAlive =
      owner.starttime != null && owner.executable != null
        ? processMatches(owner as { pid?: unknown; starttime?: unknown; executable?: unknown })
        : pidAlive(typeof owner.pid === "number" ? owner.pid : null);
  }
  if (state.state === State.WAIT_QUOTA && !state.thread_id) {
    const suffix = ownerAlive && isRecord(owner) ? ` · PID ${String(owner.pid)}` : "";
    return `WAITING_QUOTA (first launch deferred)${suffix}`;
  }
  if (ownerAlive && isRecord(owner)) return `SUPERVISOR ${String(state.state)} · PID ${String(owner.pid)}`;
  return String(state.state || "IDLE");
}

export function agentWorkReport(store: NightwatchStore): JsonObject {
  const empty = { ok: false, implemented: [], working: [], blocked: [], percent: null, untrusted: true };
  let raw: unknown;
  try {
    raw = readMailboxJson(store, "progress.json");
  } catch {
    return empty;
  }
  if (!isRecord(raw)) return empty;
  const linesOf = (key: string): string[] => {
    const value = raw[key];
    if (!Array.isArray(value)) return [];
    const rows: string[] = [];
    for (const item of value.slice(0, 8)) {
      if (typeof item === "string" && item.trim()) rows.push(item.split(/\s+/).filter(Boolean).join(" "));
    }
    return rows;
  };
  const implemented = linesOf("implemented");
  const working = linesOf("working");
  const blocked = linesOf("blocked");
  const total = implemented.length + working.length + blocked.length;
  const percent = total ? Math.round((1000 * implemented.length) / total) / 10 : null;
  return { ok: true, implemented, working, blocked, percent, untrusted: true };
}

function clip(text: string, width: number): string {
  const max = Math.max(24, width);
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

export function formatAgentWork(store: NightwatchStore, width = 100): string[] {
  const report = agentWorkReport(store);
  if (!report.ok) return ["Work       (no agent mailbox progress yet)"];
  const implemented = report.implemented as string[];
  const working = report.working as string[];
  const blocked = report.blocked as string[];
  const percent = report.percent != null ? `${report.percent}%` : "n/a";
  const lines = [
    `Work       agent-reported ${percent} of ${implemented.length + working.length + blocked.length} items · UNTRUSTED`,
    `           done ${implemented.length} · doing ${working.length} · blocked ${blocked.length}`,
  ];
  for (const [label, rows] of [
    ["Done", implemented.slice(0, 3)],
    ["Now", working.slice(0, 3)],
    ["Left/blocked", blocked.slice(0, 3)],
  ] as const) {
    if (!rows.length) continue;
    lines.push(`  ${padRight(label, 12)} ${clip(rows[0]!, width - 16)}`);
    for (const extra of rows.slice(1)) lines.push(`               ${clip(extra, width - 16)}`);
  }
  lines.push("Trusted    verified % only moves after frozen Nightwatch checks, not agent narrative");
  return lines;
}

export function renderDashboard(runs: RunRecord[], selected = 0, width = 100, errors: string[] | null = null): string {
  width = Math.max(60, width);
  selected = Math.min(Math.max(0, selected), Math.max(0, runs.length - 1));
  const errorHeader = errors?.length ? ` · ${errors.length} trusted run failed integrity validation` : "";
  const lines = [
    `Nightwatch ${nightwatchVersion()} · MULTI-THREAD CONTROL`,
    `Runs ${runs.length}${errorHeader} · ↑/↓ select · / commands · Esc cancel · /quit leaves`,
    "─".repeat(Math.min(width, 120)),
  ];
  if (errors?.length) {
    lines.push(`⚠ TRUSTED STATE ERRORS: ${errors.length}`);
    for (const err of errors.slice(0, 5)) lines.push(`  ${err}`);
    lines.push("  Use explicit CLI/recovery inspection before touching that run.");
    lines.push("─".repeat(Math.min(width, 120)));
  }
  if (!runs.length) lines.push("No trusted runs discovered.", "Type a natural-language goal or /run to create one.");
  for (let index = 0; index < runs.length; index += 1) {
    const run = runs[index]!;
    const state = run.state;
    const progress = planProgress(run.plan);
    const marker = index === selected ? "▶" : " ";
    const thread =
      run.threadId ||
      (state.state === State.WAIT_QUOTA ? "First launch deferred" : "capturing…");
    const model = state.model || "Codex default";
    const effort = state.reasoning_effort || "default";
    const work = agentWorkReport(run.store);
    const agentPct = work.percent != null ? `agent ${work.percent}%` : "agent n/a";
    lines.push(`${marker} ${padRight(String(state.state ?? ""), 19)} ${padRight(run.repo.name, 22)} ${String(thread).slice(0, 24)}`);
    lines.push(
      `    ${bar(Number(progress.verified_percent))} trusted ${String(progress.verified_percent).padStart(5)}% · ${agentPct}  ${model} · ${effort}  quota ${quotaLine(state)}`,
    );
  }
  if (runs.length) {
    const run = runs[selected]!;
    const state = run.state;
    const milestones = Array.isArray(run.plan.milestones) ? (run.plan.milestones as JsonObject[]) : [];
    const current = milestones.find((item) => item.status !== "verified") ?? null;
    const threadLabel =
      run.threadId ||
      (state.state === State.WAIT_QUOTA
        ? "First launch deferred — no Codex thread created yet"
        : "Creating/capturing exact thread");
    lines.push(
      "─".repeat(Math.min(width, 120)),
      `Goal       ${String(state.goal ?? "").slice(0, Math.max(10, width - 12))}`,
      `Repository ${run.repo.raw}`,
      `Thread     ${threadLabel} · generation ${String(state.generation ?? "")}`,
      `Account    ${accountLine(state)}`,
      `Agent      ${agentSummary(state)}`,
      `Current    ${String(current?.id ?? "complete")} · ${String(current?.title ?? "all trusted milestones verified")}`,
      `Last       ${String(state.last_event ?? "(none)")}`,
      `Next       ${nextAction(state)}`,
    );
    lines.push(...formatAgentWork(run.store, width));
    lines.push("Source: trusted state + sequence-validated events · agent work is untrusted mailbox");
  }
  return terminalSafe(lines.join("\n"));
}

export function statusRun(run: RunRecord): string {
  const state = run.state;
  const progress = planProgress(run.plan);
  const milestones = Array.isArray(run.plan.milestones) ? (run.plan.milestones as JsonObject[]) : [];
  const current = milestones.find((item) => item.status !== "verified") ?? null;
  const latest = run.events.length ? run.events[run.events.length - 1]! : {};
  const handoff = state.thread_handoff;
  let threadMode = "EXACT";
  if (isRecord(handoff) && (handoff.status === "prepared" || handoff.status === "captured")) {
    const packet = isRecord(handoff.packet) ? handoff.packet : {};
    const prior = packet.prior_thread_id || handoff.prior_thread_id || "none";
    threadMode = `CONTROLLED HANDOFF (prior ${prior})`;
  }
  const threadLabel =
    run.threadId ||
    (state.state === State.WAIT_QUOTA ? "First launch deferred — no Codex thread created yet" : "Creating/capturing exact thread");
  const lines = [
    `STATUS · ${String(state.state)}`,
    `Agent       ${agentSummary(state)}`,
    `Repository  ${run.repo.raw}`,
    `Run         ${String(state.run_id ?? "")}`,
    `Thread      ${threadLabel}`,
    `Thread Mode ${threadMode}`,
    "Run Store   persistent",
    `Auth Lease  ${state.account_lease ? "leased / active" : "inactive / scrubbed"}`,
    `Generation  ${String(state.generation)} · quota cycles ${String(state.quota_cycles ?? 0)} · recovery failures ${String(state.recovery_failures ?? 0)}`,
    `Model       ${String(state.model || "Codex default")} · ${String(state.reasoning_effort || "default")}`,
    `Account     ${accountLine(state)}`,
    `Quota       ${quotaLine(state)} · authority ${String(state.quota_source || "(none)")}`,
    `Progress    ${bar(Number(progress.verified_percent), 24)} ${progress.verified_percent}% trusted verified`,
    `Milestones  ${progress.implemented_count}/${progress.total_count} implemented · ${progress.verified_count}/${progress.total_count} verified`,
    `Current     ${String(current?.id ?? "complete")} · ${String(current?.title ?? "all trusted milestones verified")}`,
    `Last        #${String(latest.seq ?? "?")} ${String(latest.event || state.last_event)} · ${String(latest.reason || "(no reason)")}`,
    `Next        ${nextAction(state)}`,
    `Updated     ${String(state.updated_at ?? "")}`,
    "Provenance  trusted state + hash-bound policy + sequence-validated events",
  ];
  lines.push(...formatAgentWork(run.store, 100));
  if (state.last_error || state.blocker) lines.push(`Blocker     ${String(state.last_error || state.blocker)}`);
  return terminalSafe(lines.join("\n"));
}

export function explainRun(run: RunRecord): string {
  const state = run.state;
  const latest =
    [...run.events].reverse().find((item) => item.event !== "supervisor_lease_acquired" && item.event !== "supervisor_lease_released") ??
    (run.events.length ? run.events[run.events.length - 1]! : {});
  const lines = [
    `WHY ${String(state.state)}`,
    `Reason      ${String(latest.reason || state.last_error || "No detailed reason persisted")}`,
    `Evidence    event #${String(latest.seq ?? "?")} · ${String(latest.event || state.last_event)}`,
    `Authority   ${String(state.quota_source || "trusted Nightwatch state")}`,
    `Next        ${nextAction(state)}`,
  ];
  if (state.next_resume_at) lines.push(`Reset       ${String(state.next_resume_at)}`);
  lines.push(
    "Will not    use --last, create an unbound replacement thread, or accept model-authored verification",
    "Provenance  state.json + hash-bound policy + sequence-validated events.jsonl",
  );
  return terminalSafe(lines.join("\n"));
}

export function recapRun(run: RunRecord): string {
  const state = run.state;
  const progress = planProgress(run.plan);
  const verification = isRecord(state.last_verification) ? state.last_verification : {};
  const checks = Array.isArray(verification.final_checks) ? verification.final_checks : [];
  const lines = [
    "NIGHTWATCH RECAP — trusted evidence",
    `Result       ${String(state.state)}`,
    `Goal         ${String(state.goal ?? "")}`,
    `Thread       ${run.threadId || "(not captured)"}`,
    `Model        ${String(state.model || "Codex default")} · ${String(state.reasoning_effort || "default")}`,
    `Runtime      ${String(state.created_at ?? "")} → ${String(state.updated_at ?? "")}`,
    `Recoveries   ${String(state.recoveries ?? 0)}`,
    `Quota cycles ${String(state.quota_cycles ?? 0)} · recovery failures ${String(state.recovery_failures ?? 0)}`,
    `Milestones   ${progress.verified_count}/${progress.total_count} verified`,
    "Checks       trusted verification policy:",
  ];
  const policy = run.store.loadPolicy();
  const byCommand = new Map<string, JsonObject>();
  for (const item of checks) {
    if (isRecord(item) && typeof item.command === "string") byCommand.set(item.command, item);
  }
  const finalCommands = Array.isArray(policy.final_commands) ? (policy.final_commands as string[]) : [];
  for (const command of finalCommands) {
    const check = byCommand.get(command);
    const statusValue = check && check.ok ? "PASS" : check ? "FAIL" : "NOT RUN";
    lines.push(`  ${padRight(statusValue, 7)} ${command}`);
  }
  if (!finalCommands.length) lines.push("  NONE    no frozen acceptance command");
  if (state.last_error || state.blocker) lines.push(`Blocker      ${String(state.last_error || state.blocker)}`);
  lines.push("Agent narrative is intentionally excluded from trusted facts.");
  return terminalSafe(lines.join("\n"));
}

function safeTail(filePath: string | NightPath, maximum = 32_768): string {
  const raw = typeof filePath === "string" ? filePath : filePath.raw;
  const flags = fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0);
  const fd = fs.openSync(raw, flags);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size > MAX_EVENT_BYTES) throw new StateIntegrityError("trusted text file is unsafe");
    const start = Math.max(0, info.size - maximum);
    const length = info.size - start;
    const buffer = Buffer.alloc(length);
    fs.readSync(fd, buffer, 0, length, start);
    return buffer.toString("utf8");
  } finally {
    fs.closeSync(fd);
  }
}

function timelineView(run: RunRecord): string {
  const lines = ["TRUSTED TIMELINE"];
  for (const item of run.events.slice(-80)) {
    lines.push(
      `#${padRight(String(item.seq ?? "?"), 4)} ${String(item.ts ?? "")} ${padRight(String(item.state ?? ""), 20)} ${String(item.event ?? "")} — ${String(item.reason ?? "")}`,
    );
  }
  return terminalSafe(lines.join("\n"));
}

function planView(run: RunRecord): string {
  const lines = ["TRUSTED MILESTONES"];
  const milestones = Array.isArray(run.plan.milestones) ? (run.plan.milestones as JsonObject[]) : [];
  for (const item of milestones) {
    lines.push(`[${padRight(String(item.status ?? "pending"), 11)}] ${String(item.id)} · ${String(item.title)} · weight ${String(item.weight)}`);
  }
  lines.push("Only frozen Nightwatch verification can change implemented → verified.");
  return terminalSafe(lines.join("\n"));
}

function threadView(run: RunRecord): string {
  const providerCommands: Array<[string, unknown]> = [];
  try {
    for (const child of run.store.runsPath.iterdir().sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
      if (!child.name.startsWith("generation-") || !child.name.endsWith(".events.jsonl")) continue;
      try {
        for (const line of safeTail(child, MAX_EVENT_BYTES).split(/\r\n|\n|\r/)) {
          if (!line.trim()) continue;
          const item = JSON.parse(line) as JsonObject;
          if (item.type === "provider_command") providerCommands.push([child.name.replace(/\.events\.jsonl$/, ""), item.action]);
        }
      } catch {
        continue;
      }
    }
  } catch {
    /* missing runs dir is fine */
  }
  const lines = [
    "EXACT THREAD",
    `Thread      ${run.threadId || "(not captured)"}`,
    `Generation  ${String(run.state.generation ?? "")}`,
    `Recoveries  ${String(run.state.recoveries ?? 0)}`,
    `Quota cycles ${String(run.state.quota_cycles ?? 0)} · recovery failures ${String(run.state.recovery_failures ?? 0)}`,
    "Provider turns:",
  ];
  for (const [generation, action] of providerCommands) lines.push(`  ${generation}: ${String(action)}`);
  return terminalSafe(lines.join("\n"));
}

function quotaView(run: RunRecord): string {
  const state = run.state;
  const quota = isRecord(state.quota) ? state.quota : {};
  const lines = [`QUOTA · last trusted sample authority=${String(state.quota_source || "(none)")}`];
  for (const [key, label] of [
    ["primary", "5h"],
    ["secondary", "weekly"],
  ] as const) {
    const window = quota[key];
    if (isRecord(window)) {
      lines.push(
        `${padRight(label, 8)} ${usedPercentText(window) ?? "?"}% used · reset=${String(window.resets_at)} · window=${String(window.window_duration_mins)}m`,
      );
    }
  }
  lines.push(`Sampled at ${String(quota.read_at ?? null)}`);
  try {
    const live = makeQuotaProvider().read();
    const primary = live.primary ? `${live.primary.usedPercent}%` : "?";
    const weekly = live.secondary ? `${live.secondary.usedPercent}%` : "?";
    lines.push(`Live now   5h ${primary} · week ${weekly} · ${live.source}`);
  } catch (error) {
    lines.push(`Live now   unavailable (${error instanceof Error ? error.name : "Error"})`);
  }
  lines.push(`Next resume ${String(state.next_resume_at || "(not waiting)")}`);
  lines.push("Dashboard quota is the last trusted sample; live refresh is this /quota view.");
  return terminalSafe(lines.join("\n"));
}

export function viewContent(view: string, run: RunRecord): string {
  if (view === "status") return statusRun(run);
  if (view === "plan") return planView(run);
  if (view === "timeline") return timelineView(run);
  if (view === "explain") return explainRun(run);
  if (view === "thread") return threadView(run);
  if (view === "quota") return quotaView(run);
  if (view === "recap") return recapRun(run);
  try {
    return `SUPERVISOR LOG\n${safeTail(run.store.logPath)}`;
  } catch {
    return "No safe supervisor log is available.";
  }
}

function normalizeKeypress(str: string | undefined, key: readline.Key | undefined): string {
  if (key?.ctrl && key.name === "c") return "ctrl-c";
  if (key?.name === "up") return "up";
  if (key?.name === "down") return "down";
  if (key?.name === "pageup") return "pageup";
  if (key?.name === "pagedown") return "pagedown";
  if (key?.name === "backspace" || str === "\x7f" || str === "\b") return "backspace";
  if (key?.name === "return" || str === "\n" || str === "\r") return "enter";
  if (key?.name === "escape" || str === "\x1b") return "esc";
  if (str === " ") return "space";
  if (typeof str === "string" && str.length === 1) return str;
  if (key?.sequence && key.sequence.length === 1 && !key.ctrl && !key.meta) return key.sequence;
  return "";
}

function termSize(): { width: number; height: number } {
  return {
    width: process.stdout.columns || 80,
    height: process.stdout.rows || 24,
  };
}

class RawModeApp {
  private readonly catalog = new RunCatalog();
  private readonly controller: TuiController;
  private previousStates = new Map<string, string>();
  private timer: NodeJS.Timeout | null = null;
  private resolveExit: ((code: number) => void) | null = null;
  private running = false;

  constructor(initialRepo: NightPath | null) {
    this.controller = new TuiController({ repo: initialRepo });
  }

  async run(): Promise<number> {
    if (!process.stdin.isTTY || !process.stdout.isTTY) {
      console.error("nightwatch: interactive UI requires a terminal");
      return 2;
    }
    this.running = true;
    readline.emitKeypressEvents(process.stdin);
    process.stdin.setRawMode(true);
    process.stdin.resume();
    process.stdout.write("\x1b[?25h");
    const onKeypress = (str: string | undefined, key: readline.Key | undefined): void => {
      const normalized = normalizeKeypress(str, key);
      if (!normalized) return;
      this.controller.handleKey(normalized);
      if (this.controller.quit) {
        this.finish(0);
        return;
      }
      this.refreshRuns();
      this.draw();
    };
    process.stdin.on("keypress", onKeypress);
    const onResize = (): void => this.draw();
    process.stdout.on("resize", onResize);
    this.refreshRuns();
    this.draw();
    this.timer = setInterval(() => {
      if (this.controller.quit) {
        this.finish(0);
        return;
      }
      this.refreshRuns();
      this.draw();
    }, 1000);
    return await new Promise<number>((resolve) => {
      this.resolveExit = (code) => {
        process.stdin.off("keypress", onKeypress);
        process.stdout.off("resize", onResize);
        resolve(code);
      };
    });
  }

  private finish(code: number): void {
    if (!this.running) return;
    this.running = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    try {
      if (process.stdin.isTTY) process.stdin.setRawMode(false);
    } catch {
      /* ignore */
    }
    process.stdout.write("\x1b[0m\x1b[2J\x1b[H");
    this.resolveExit?.(code);
  }

  private refreshRuns(): void {
    this.controller.runs = this.catalog.discover();
    this.controller.catalogErrors = this.catalog.errors;
    this.controller.selected = Math.min(this.controller.selected, Math.max(0, this.controller.runs.length - 1));
    const current = new Map<string, string>();
    for (const item of this.controller.runs) {
      const runId = String(item.state.run_id ?? "");
      const stateValue = String(item.state.state ?? "");
      current.set(runId, stateValue);
      const previous = this.previousStates.get(runId);
      if (previous && previous !== stateValue && TERMINAL_STATES.has(stateValue as never)) {
        process.stdout.write("\x07");
        this.controller.message = `${stateValue}: ${runId} · use /recap or /report`;
      }
    }
    this.previousStates = current;
    const selected = this.controller.selectedRun();
    if (selected && VIEW_COMMANDS.has(this.controller.view) && this.controller.overlay == null) {
      this.controller.content = this.controller.safeView(this.controller.view, selected);
    }
  }

  private draw(): void {
    const { width, height } = termSize();
    const body = this.controller.render(width, height);
    const lines = body.split("\n");
    process.stdout.write("\x1b[H\x1b[J");
    for (let row = 0; row < Math.min(lines.length, height); row += 1) {
      const line = terminalSafe(lines[row] ?? "").slice(0, Math.max(1, width - 1));
      process.stdout.write(line);
      if (row < height - 1) process.stdout.write("\n");
    }
    const prefix = "Input › ";
    const cursorCol = Math.min(width - 2, prefix.length + this.controller.composer.length);
    process.stdout.write(`\x1b[${height};${Math.max(1, cursorCol + 1)}H`);
  }
}

/** Launch the interactive TUI. Returns a process exit code. */
export async function launchTui(repo?: string | null): Promise<number> {
  let root: NightPath | null = null;
  if (repo) {
    try {
      root = repoRoot(repo);
    } catch (error) {
      if (!(error instanceof GitError)) throw error;
      root = null;
    }
  }
  try {
    return await new RawModeApp(root).run();
  } catch (error) {
    if (error && typeof error === "object" && (error as { name?: string }).name === "AbortError") return 0;
    throw error;
  }
}

/** Alias matching Python `run_tui`. */
export const runTui = launchTui;
