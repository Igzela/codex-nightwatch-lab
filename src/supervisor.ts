import { spawnSync } from "node:child_process";
import fs from "node:fs";
import { StringDecoder } from "node:string_decoder";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";

import { AccountBrokerError, AccountSchemaError } from "./account-errors.js";
import { AccountLeaseBroker, AccountPoolCoordinator, CodexAuthAdapter, accountFingerprint } from "./account-broker.js";
import { GitError, GitSnapshot, isAncestor, repoRoot, snapshot } from "./git.js";
import {
  ErrorKind,
  QuotaSnapshot,
  QuotaWindow,
  State,
  TERMINAL_STATES,
  crossAccountThreadModeForVersion,
  installedCodexVersion,
  type JsonObject,
  type ProviderResult,
} from "./models.js";
import { NightPath } from "./path.js";
import { LOCK_EX, LOCK_NB, LOCK_UN, WouldBlock, flock, getpgrp } from "./posix.js";
import { linuxProcessIdentity, pidAlive, processIdentityMatches } from "./process-identity.js";
import { getProviderAdapter } from "./providers.js";
import { makeQuotaProvider, quotaRecovered } from "./quota.js";
import { runCodex } from "./codex.js";
import { adoptProposedPlan, ingestProgress, verifyMilestones } from "./milestones.js";
import { NightwatchStore, StateIntegrityError, SupervisorAlreadyRunning, makeRunId, nowIso } from "./storage.js";
import { crashHook } from "./testing.js";
import { installUserFiles, serviceName, startUserService } from "./operations.js";

export { pidAlive, linuxProcessIdentity, processIdentityMatches };
export const processIdentity = linuxProcessIdentity;
export const processMatches = processIdentityMatches;

export const MAX_TRANSIENT_RETRIES = 3;
export const MAX_CRASH_RETRIES = 3;
// CURRENT_ONLY keeps its historical defensive recovery budget. AUTO_POOL
// quota rotation is normal operation and uses quota_cycles instead; only
// abnormal recovery failures consume MAX_RECOVERY_FAILURES.
export const MAX_QUOTA_RECOVERIES = 20;
export const MAX_RECOVERY_FAILURES = 3;
export const QUOTA_POLL_SECONDS = 30;
export const QUOTA_BUFFER_SECONDS = 60;
export const TRANSIENT_BACKOFF_SECONDS = [2, 5, 15] as const;
export const CRASH_BACKOFF_SECONDS = [1, 3, 10] as const;

export class SystemExit extends Error {
  constructor(message?: string) {
    super(message ?? "");
    this.name = "SystemExit";
  }
}

export class KeyboardInterrupt extends Error {
  constructor() {
    super("KeyboardInterrupt");
    this.name = "KeyboardInterrupt";
  }
}

class StopEvent {
  private flagged = false;
  set(): void {
    this.flagged = true;
  }
  isSet(): boolean {
    return this.flagged;
  }
}

interface Store {
  repo: { toString(): string };
  trustedCodexHome: unknown;
  exists(): boolean;
  loadState(): JsonObject;
  loadPlan(): JsonObject;
  loadPolicy(): JsonObject;
  loadAcceptance(): JsonObject;
  loadEvents(): JsonObject[];
  transition(target: string, event: string, reason: string, changes?: JsonObject | null): JsonObject;
  mutate(event: string, reason: string, mutator: (state: JsonObject) => JsonObject): JsonObject;
  appendEvent(event: string, reason: string, metadata?: JsonObject | null): void;
  writeReport(text: string): unknown;
  verifyCodexHome(): void;
  initialize(
    runId: string,
    goal: string,
    repo: string,
    timestamp?: string | null,
    verifyCommands?: string[] | null,
    threadId?: string | null,
    model?: string | null,
    reasoningEffort?: string | null,
    accountMode?: string,
    authorizedAccounts?: string[] | null,
    provider?: string,
    agyPrintTimeout?: string | null,
  ): JsonObject;
  withSupervisorLease<T>(body: () => T | Promise<T>): T | Promise<T>;
}

interface AccountRuntime {
  lease: { fd: number } | null;
  codexHome: unknown;
}

interface AccountCandidate {
  accountKey: string;
  fingerprint: string;
  quota: QuotaSnapshot | null;
  leased: boolean;
  authError: boolean;
}

interface PoolDecision {
  candidates: AccountCandidate[];
  selected: AccountCandidate | null;
  earliestReset: number | null;
}

interface PoolHandle {
  runCodexHome: unknown;
  activeAccount(): { accountKey: string; fingerprint: string } | null;
  probe(keys: string[], runId: string, repo: string, generation: number, excluded?: Set<string> | null): PoolDecision;
  withSession?<T>(key: string, runId: string, repo: string, generation: number, body: (runtime: AccountRuntime) => T): T;
  session?(key: string, runId: string, repo: string, generation: number, body?: (runtime: AccountRuntime) => unknown): unknown;
}

interface ProviderPort {
  resolveBinary(): string;
  runTurn(
    store: NightwatchStore,
    generation: number,
    prompt: string,
    threadId: string | null,
    onSpawn: (pid: number, action: string) => void,
    onThread: (threadId: string) => void,
    stopEvent?: StopEvent | null,
  ): ProviderResult | Promise<ProviderResult>;
  probeQuota(store: NightwatchStore, repo: unknown, model?: string | null): QuotaSnapshot | Promise<QuotaSnapshot>;
  findActiveProcesses(repo: unknown, excludePid?: number | null): JsonObject[];
}

type RunCodexFn = (
  store: NightwatchStore,
  generation: number,
  prompt: string,
  threadId: string | null,
  onSpawn?: (pid: number, action: string) => void,
  onThread?: (threadId: string) => void,
  stopEvent?: StopEvent | null,
  timeout?: number | null,
  codexHome?: unknown,
  leaseFd?: number | null,
  accountFingerprint?: string | null,
) => ProviderResult | Promise<ProviderResult>;

const invokeCodex = runCodex as RunCodexFn;
const adoptPlan = adoptProposedPlan as (store: NightwatchStore) => boolean;
const ingestPlanProgress = ingestProgress as (store: NightwatchStore) => boolean;
const verifyPlan = verifyMilestones as (store: NightwatchStore, git: GitSnapshot) => JsonObject;
const readQuota = makeQuotaProvider as () => { read(): QuotaSnapshot };
const quotaIsRecovered = quotaRecovered as (snapshot: QuotaSnapshot, names: Set<string>) => boolean;

function asStore(store: NightwatchStore): Store {
  return store as unknown as Store;
}

function providerPort(name?: string | null): ProviderPort {
  return (getProviderAdapter as (name?: string) => ProviderPort)(name || "codex");
}

function pathText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value instanceof NightPath) return value.raw;
  return String(value ?? "");
}

function isRecord(value: unknown): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasOwn(record: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function get(record: JsonObject, key: string, fallback?: unknown): unknown {
  return hasOwn(record, key) ? record[key] : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : fallback;
}

function py(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  return String(value);
}

function capitalize(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

function unixNow(): number {
  return Math.floor(Date.now() / 1000);
}

function isoUnix(seconds: number): string {
  return new Date(Math.trunc(seconds) * 1000).toISOString().replace(/\.000Z$/, "Z");
}

function parseTimestamp(text: unknown): Date | null {
  if (typeof text !== "string" || !text) return null;
  const hasZone = /(?:Z|[+-]\d{2}:\d{2})$/.test(text);
  const normalized = hasZone ? text.replace(/Z$/, "+00:00") : `${text}+00:00`;
  const parsed = Date.parse(normalized);
  return Number.isNaN(parsed) ? null : new Date(parsed);
}

function unixOf(date: Date): number {
  return Math.floor(date.getTime() / 1000);
}

function envInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  const trimmed = raw.trim();
  if (!/^[+-]?\d+$/.test(trimmed)) throw new ValueError(`invalid literal for int() with base 10: ${pyRepr(raw)}`);
  return Number(trimmed);
}

function envFloat(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  const value = Number(raw.trim());
  if (!Number.isFinite(value)) throw new ValueError(`could not convert string to float: ${pyRepr(raw)}`);
  return value;
}

class ValueError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValueError";
  }
}

function pyRepr(value: string): string {
  return `'${value.replaceAll("\\", "\\\\").replaceAll("'", "\\'")}'`;
}

function sameOwner(value: unknown, owner: JsonObject): boolean {
  if (!isRecord(value)) return false;
  const keys = new Set([...Object.keys(value), ...Object.keys(owner)]);
  for (const key of keys) if (value[key] !== owner[key]) return false;
  return true;
}

function sysPlatformLinux(): boolean {
  return process.platform.startsWith("linux");
}

function resolveReal(filePath: string): string {
  const lexical = path.resolve(filePath);
  try {
    return fs.realpathSync(lexical);
  } catch {
    return lexical;
  }
}

function commandAvailable(binary: string): boolean {
  const candidate = new NightPath(binary);
  if (candidate.isFile()) {
    try {
      fs.accessSync(candidate.raw, fs.constants.X_OK);
      return true;
    } catch {
      /* fall through to PATH lookup */
    }
  }
  return which(binary) !== null;
}

function which(command: string): string | null {
  if (command.includes("/")) {
    try {
      fs.accessSync(command, fs.constants.X_OK);
      return fs.statSync(command).isFile() ? command : null;
    } catch {
      return null;
    }
  }
  for (const dir of (process.env.PATH ?? "/usr/bin:/bin").split(path.delimiter)) {
    if (!dir) continue;
    const full = path.join(dir, command);
    try {
      fs.accessSync(full, fs.constants.X_OK);
      if (fs.statSync(full).isFile()) return full;
    } catch {
      /* continue */
    }
  }
  return null;
}

function verificationBlock(commands: unknown): string {
  const lines = Array.isArray(commands) ? commands.map((command) => `- ${String(command)}`) : [];
  return lines.join("\n") || "- No trusted automatic verification is configured.";
}

function quotaWindowsOrFallback(result: ProviderResult): QuotaWindow[] {
  if (result.quotaWindows?.length) return result.quotaWindows;
  const weekly = result.errorKind === ErrorKind.QUOTA_WEEKLY;
  return [new QuotaWindow(weekly ? "weekly" : "5h", 100, weekly ? 10080 : 300, result.resetAt)];
}

function maxReset(windows: QuotaWindow[]): number {
  let max = 0;
  for (const window of windows) if (window.resetsAt) max = Math.max(max, window.resetsAt);
  return max;
}

function getpgid(pid: number): number {
  const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
  const fields = stat.split(")").at(-1)?.trim().split(/\s+/) ?? [];
  const pgrp = Number(fields[2]);
  if (!Number.isInteger(pgrp)) {
    const error = new Error("getpgid") as NodeJS.ErrnoException;
    error.code = "ESRCH";
    throw error;
  }
  return pgrp;
}

async function withAccountSession<T>(pool: PoolHandle, key: string, runId: string, repo: string, generation: number, body: (runtime: AccountRuntime) => T | Promise<T>): Promise<T> {
  if (typeof pool.withSession === "function") return await pool.withSession(key, runId, repo, generation, (runtime) => body(runtime));
  if (typeof pool.session !== "function") throw new AccountSchemaError("account coordinator session API is unavailable");
  if (pool.session.length >= 5) return await (pool.session(key, runId, repo, generation, (runtime) => body(runtime)) as T | Promise<T>);
  const opened = pool.session(key, runId, repo, generation) as {
    with?: (fn: (runtime: AccountRuntime) => T | Promise<T>) => T | Promise<T>;
    enter?: () => AccountRuntime;
    close?: () => void;
    exit?: () => void;
  };
  if (opened && typeof opened.with === "function") return await opened.with((runtime) => body(runtime));
  let entered = false;
  try {
    const runtime = opened && typeof opened.enter === "function" ? opened.enter() : (opened as AccountRuntime);
    entered = Boolean(opened && typeof opened.enter === "function");
    return await body(runtime);
  } finally {
    if (entered) {
      if (typeof opened.close === "function") opened.close();
      else opened.exit?.();
    }
  }
}

export function findRepoCodexProcesses(repo: string | NightPath, excludePid?: number | null): JsonObject[] {
  if (!sysPlatformLinux()) return [];
  const target = resolveReal(pathText(repo));
  const procDir = "/proc";
  if (!fs.existsSync(procDir)) return [];
  const found: JsonObject[] = [];
  for (const name of fs.readdirSync(procDir)) {
    if (!/^\d+$/.test(name)) continue;
    const pid = Number(name);
    if (excludePid && pid === excludePid) continue;
    try {
      const cwd = resolveReal(fs.readlinkSync(`/proc/${pid}/cwd`));
      if (cwd !== target) continue;
      const executable = fs.readlinkSync(`/proc/${pid}/exe`);
      const cmdline = fs.readFileSync(`/proc/${pid}/cmdline`).toString("utf8").replaceAll("\0", " ").toLowerCase();
      const exe = executable.toLowerCase();
      const isCodex =
        (exe.includes("codex") || cmdline.includes("codex")) &&
        !exe.includes("nightwatch") &&
        !cmdline.includes("nightwatch") &&
        !cmdline.includes("pytest") &&
        !cmdline.includes("unittest");
      if (isCodex) found.push({ pid, executable, cmdline, cwd });
    } catch {
      continue;
    }
  }
  return found;
}

export function findActiveThreadsForRepo(repo: string | NightPath, codexHome?: string | NightPath | null): JsonObject[] {
  const target = resolveReal(pathText(repo));
  const home = codexHome ? pathText(codexHome) : process.env.CODEX_HOME || path.join(NightPath.home().raw, ".codex");
  const dbPath = path.join(home, "state_5.sqlite");
  if (!fs.existsSync(dbPath)) return [];
  let db: DatabaseSync | null = null;
  try {
    db = new DatabaseSync(dbPath, { readOnly: true });
    const rows = db
      .prepare(
        "SELECT id, rollout_path, updated_at_ms, model, git_branch, thread_source, first_user_message, title FROM threads WHERE cwd = ? ORDER BY updated_at_ms DESC LIMIT 10",
      )
      .all(target) as Array<Record<string, unknown>>;
    return rows.map((row) => ({
      id: row.id,
      rollout_path: row.rollout_path,
      updated_at_ms: row.updated_at_ms,
      model: row.model,
      git_branch: row.git_branch,
      thread_source: row.thread_source,
      first_user_message: row.first_user_message,
      title: row.title,
    }));
  } catch {
    return [];
  } finally {
    db?.close();
  }
}

export function extractRolloutMeta(rolloutPath: string | NightPath): JsonObject | null {
  const filePath = pathText(rolloutPath);
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) return null;
  let found: JsonObject | null = null;
  try {
    readLines(filePath, (line) => {
      if (!line.trim()) return false;
      try {
        const data = JSON.parse(line) as JsonObject;
        if (data.type !== "session_meta") return false;
        const payload = isRecord(data.payload) ? data.payload : null;
        if (!payload) return false;
        const threadId = payload.id ?? payload.session_id;
        if (!threadId) return false;
        const cwd = payload.cwd;
        found = {
          thread_id: String(threadId),
          cwd: cwd ? resolveReal(String(cwd)) : null,
          thread_source: payload.thread_source ?? null,
          model_provider: payload.model_provider ?? null,
          git: payload.git ?? null,
        };
        return true;
      } catch {
        return false;
      }
    });
  } catch {
    return null;
  }
  return found;
}

export function findProvenCodexSessions(repo: string | NightPath, codexHome?: string | NightPath | null, explicitThread?: string | null): JsonObject[] {
  if (!sysPlatformLinux()) return [];
  const targetRepo = resolveReal(pathText(repo));
  const candidateProcesses = findRepoCodexProcesses(targetRepo, process.pid);
  const proven: JsonObject[] = [];
  const sqliteThreads = new Map(findActiveThreadsForRepo(targetRepo, codexHome).map((thread) => [String(thread.id), thread]));
  for (const proc of candidateProcesses) {
    const pid = proc.pid as number;
    const identStart = linuxProcessIdentity(pid);
    if (!identStart) continue;
    const fdDir = `/proc/${pid}/fd`;
    if (!fs.existsSync(fdDir)) continue;
    let names: string[];
    try {
      names = fs.readdirSync(fdDir);
    } catch {
      continue;
    }
    const matches: Array<[string, JsonObject]> = [];
    for (const name of names) {
      try {
        const targetLink = fs.readlinkSync(path.join(fdDir, name));
        if (targetLink.includes("rollout-") && targetLink.endsWith(".jsonl")) {
          const meta = extractRolloutMeta(targetLink);
          if (meta?.thread_id && meta.cwd === targetRepo) matches.push([targetLink, meta]);
        }
      } catch {
        continue;
      }
    }
    if (!matches.length) continue;
    const preferred = matches.filter((item) => item[1].thread_source === "user");
    const [provenRollout, provenMeta] = (preferred.length ? preferred : matches)[0];
    if (!processIdentityMatches(identStart)) continue;
    const threadId = String(provenMeta.thread_id);
    if (explicitThread && threadId !== explicitThread) continue;
    const sqliteMeta = sqliteThreads.get(threadId) ?? {};
    const git = isRecord(provenMeta.git) ? provenMeta.git : {};
    proven.push({
      pid,
      pid_identity: identStart,
      rollout_path: provenRollout,
      thread_id: threadId,
      thread_source: provenMeta.thread_source || sqliteMeta.thread_source,
      model: sqliteMeta.model,
      branch: sqliteMeta.git_branch || git.branch,
      title: sqliteMeta.title || sqliteMeta.first_user_message,
      sqlite_thread: sqliteMeta,
    });
  }
  return proven;
}

/** List live and recent Codex sessions for adoption.
 *
 * Live processes are included when their cwd matches the repository, even if a
 * rollout JSONL file descriptor cannot be proven. Subagent threads stay hidden
 * unless explicitly requested.
 */
export function listAdoptableSessions(repo: string | NightPath, codexHome?: string | NightPath | null, includeSubagents = false): JsonObject[] {
  const targetRepo = resolveReal(pathText(repo));
  const proven = findProvenCodexSessions(targetRepo, codexHome);
  const processes = findRepoCodexProcesses(targetRepo);
  const sqliteThreads = findActiveThreadsForRepo(targetRepo, codexHome);
  const items: JsonObject[] = [];
  const seen = new Set<string>();
  const provenPids = new Set<number>();
  for (const session of proven) {
    const threadId = session.thread_id;
    provenPids.add(session.pid as number);
    if (session.thread_source === "subagent" && !includeSubagents) continue;
    items.push({ ...session, kind: "live", live: true, proof: "pid_rollout" });
    if (typeof threadId === "string") seen.add(threadId);
  }
  for (const proc of processes) {
    if (provenPids.has(proc.pid as number)) continue;
    items.push({
      kind: "live",
      live: true,
      proof: "pid_cwd",
      pid: proc.pid,
      executable: proc.executable,
      cmdline: proc.cmdline,
      thread_id: null,
      title: "Interactive Codex (thread not proven from rollout)",
      thread_source: "user",
    });
  }
  for (const thread of sqliteThreads) {
    const threadId = thread.id;
    if (typeof threadId !== "string" || seen.has(threadId)) continue;
    if (thread.thread_source === "subagent" && !includeSubagents) continue;
    items.push({
      kind: "recent",
      live: false,
      proof: "sqlite",
      pid: null,
      thread_id: threadId,
      title: thread.title || thread.first_user_message,
      model: thread.model,
      branch: thread.git_branch,
      rollout_path: thread.rollout_path,
      thread_source: thread.thread_source,
      sqlite_thread: thread,
    });
    seen.add(threadId);
  }
  return items;
}

export function startPrompt(store: NightwatchStore, goal: string): string {
  const db = asStore(store);
  const goalHash = db.loadAcceptance().goal_hash;
  const commands = db.loadPolicy().final_commands;
  const verification = verificationBlock(commands);
  return `You are working under Nightwatch supervision in this Git repository.

Read Git status/diff and the untrusted mailbox \`.nightwatch-agent/context.json\` first. Nightwatch's authoritative state, acceptance policy, quota evidence, thread ID, and verification policy are outside the workspace and unavailable to you.
Before making implementation changes, create \`.nightwatch-agent/proposed-plan.json\` with this exact shape:
{"goal_hash":"${String(goalHash)}","milestones":[{"id":"M1","title":"...","weight":1}]}
Do not add verification commands or policy to the proposal: models may suggest task structure but cannot authorize host commands. After each milestone is implemented, update \`.nightwatch-agent/progress.json\` with \`{"milestones":[{"id":"M1","status":"implemented"}]}\`.

Goal:
${goal}

The frozen, user-authorized verification commands are:
${verification}
Arrange the repository so these exact commands pass.  You may run them yourself, but you may not change their authority.

Work only on unfinished work. If a real blocker prevents progress, write \`.nightwatch-agent/blocker.json\` with a concise reason and explain it. Never use dangerous approval or sandbox bypass flags.`;
}

export function resumePrompt(store: NightwatchStore, state: JsonObject): string {
  const db = asStore(store);
  const commands = db.loadPolicy().final_commands;
  const goalHash = db.loadAcceptance().goal_hash;
  const verification = verificationBlock(commands);
  return `Resume the same exact Nightwatch task thread. Read \`.nightwatch-agent/context.json\`, \`.nightwatch-agent/proposed-plan.json\`, \`.nightwatch-agent/progress.json\` if present, Git status, Git diff, and recent commits. Trusted Nightwatch control-plane state is outside the workspace. The durable task state says generation=${py(state.generation)} and thread_id=${py(get(state, "thread_id"))}.

If \`.nightwatch-agent/proposed-plan.json\` is absent, create it before continuing with this exact shape:
{"goal_hash":"${String(goalHash)}","milestones":[{"id":"M1","title":"...","weight":1}]}
Do not add verification commands or policy to the proposal: models may suggest task structure but cannot authorize host commands.

Determine which milestones are actually verified from available repository evidence. Do not repeat completed work. Continue only unfinished work. Update \`.nightwatch-agent/progress.json\` only with implemented/working/blocked facts. Nightwatch, not the model, decides verified/DONE and only runs frozen user-authorized checks. The frozen user-authorized verification commands are:
${verification}
Repair unfinished work until those exact commands pass; do not edit policy or claim verification authority. If blocked, write \`.nightwatch-agent/blocker.json\` and explain the missing human decision or external prerequisite.`;
}

export function handoffPrompt(_store: NightwatchStore, _state: JsonObject, packet: JsonObject): string {
  const verification = verificationBlock(packet.verification_commands);
  const verified = Array.isArray(packet.verified_milestones) ? (packet.verified_milestones as string[]) : [];
  const remaining = Array.isArray(packet.remaining_milestones) ? (packet.remaining_milestones as string[]) : [];
  return `Continue the same Nightwatch mission through a controlled account handoff.

This is a NEW Codex conversation, not an exact resume of the prior conversation.
Nightwatch has not proven cross-account exact-thread portability for this Codex
version. Use only the trusted facts below; model narrative from the prior
conversation is not authority.

Original goal:
${String(packet.goal)}

Repository/worktree: ${String(packet.repo)}
Trusted Git HEAD: ${py(packet.git_head)}
Prior thread ID (audit only): ${py(packet.prior_thread_id)}
Account handoff generation: ${py(packet.generation)}
Verified milestones: ${verified.join(", ") || "(none)"}
Remaining milestones: ${remaining.join(", ") || "(none)"}
Durable blocker: ${packet.blocker ? String(packet.blocker) : "(none)"}
Frozen verification commands:
${verification}

Before changing files, create \`.nightwatch-agent/proposed-plan.json\` bound to
the Nightwatch goal hash. Continue the same mission and preserve the frozen
verification policy; do not claim that this is the same Codex conversation.`;
}

export class Supervisor {
  readonly store: NightwatchStore;
  quotaProvider: { read(): QuotaSnapshot };
  accountPool: PoolHandle | null;
  private stopRequested = false;
  private readonly stopEvent = new StopEvent();
  private interruptRequested = false;
  private sleepCancel: (() => void) | null = null;
  private sigintInstalled = false;
  private readonly onSigint = () => {
    this.interruptRequested = true;
    try {
      this.requestStop();
    } catch {
      /* signal delivery is best-effort */
    }
    this.sleepCancel?.();
  };

  constructor(store: NightwatchStore, quotaProvider?: { read(): QuotaSnapshot } | null, accountPool?: PoolHandle | null) {
    this.store = store;
    this.quotaProvider = quotaProvider ?? readQuota();
    this.accountPool = accountPool ?? null;
    if (this.accountPool && this.accountPool.runCodexHome == null) this.accountPool.runCodexHome = trustedHomePath(store);
  }

  requestStop(): void {
    this.stopRequested = true;
    this.stopEvent.set();
    let state: JsonObject;
    try {
      state = this.db().loadState();
    } catch (error) {
      if (error instanceof StateIntegrityError) return;
      throw error;
    }
    const active = state.active_process;
    if (!isRecord(active) || !processIdentityMatches(active)) return;
    const pid = active.pid as number;
    if (state.provider === "agy") {
      const pgid = active.pgid;
      if (typeof pgid === "number" && pgid === pid && pgid > 1 && pgid !== getpgrp()) {
        try {
          process.kill(-pgid, "SIGINT");
          return;
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === "ESRCH") return;
        }
      }
    }
    try {
      process.kill(pid, "SIGINT");
    } catch {
      /* process already gone */
    }
  }

  async execute(start = false): Promise<JsonObject> {
    const remove = this.installSigint();
    try {
      try {
        return await this.underSupervisorLease(async () => {
          const owner = { ...(linuxProcessIdentity(process.pid) ?? { pid: process.pid }), acquired_at: nowIso() };
          this.db().mutate("supervisor_lease_acquired", "supervisor lifetime lease acquired", (item) => ({ ...item, supervisor_owner: owner }));
          try {
            return await this.executeLoop(start);
          } finally {
            try {
              this.db().mutate("supervisor_lease_released", "supervisor lifetime lease released", (item) => ({
                ...item,
                supervisor_owner: sameOwner(item.supervisor_owner, owner) ? null : item.supervisor_owner,
              }));
            } catch (error) {
              if (!(error instanceof StateIntegrityError)) throw error;
            }
          }
        });
      } catch (error) {
        if (error instanceof SupervisorAlreadyRunning) throw error;
        throw error;
      }
    } finally {
      remove();
    }
  }

  private db(): Store {
    return asStore(this.store);
  }

  private installSigint(): () => void {
    if (this.sigintInstalled) return () => undefined;
    this.sigintInstalled = true;
    process.on("SIGINT", this.onSigint);
    return () => {
      this.sigintInstalled = false;
      process.off("SIGINT", this.onSigint);
    };
  }

  private async underSupervisorLease<T>(body: () => Promise<T>): Promise<T> {
    // withSupervisorLease unlocks when its callback returns. This loop awaits,
    // so that call only creates the control-plane directory and rejects an
    // owner already inside it. The flock below is the lifetime lease.
    this.store.withSupervisorLease(() => undefined);
    const handle = this.store.supervisorLockPath.open("a+");
    try {
      fs.chmodSync(this.store.supervisorLockPath.raw, 0o600);
      try {
        flock(handle.fileno(), LOCK_EX | LOCK_NB);
      } catch (error) {
        if (error instanceof WouldBlock) throw new SupervisorAlreadyRunning("another Nightwatch supervisor owns this run", { cause: error });
        throw error;
      }
      return await body();
    } finally {
      try {
        flock(handle.fileno(), LOCK_UN);
      } finally {
        handle.close();
      }
    }
  }

  private async sleepSeconds(seconds: number): Promise<void> {
    if (this.interruptRequested) throw new KeyboardInterrupt();
    if (seconds <= 0) return;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.sleepCancel = null;
        resolve();
      }, seconds * 1000);
      this.sleepCancel = () => {
        clearTimeout(timer);
        this.sleepCancel = null;
        resolve();
      };
    });
    if (this.interruptRequested) throw new KeyboardInterrupt();
  }

  private async sleepUntil(target: number, pollSeconds = 30): Promise<void> {
    while (true) {
      const remaining = target - unixNow();
      if (remaining <= 0) return;
      await this.sleepSeconds(Math.min(remaining, pollSeconds));
    }
  }

  private poolCoordinator(): PoolHandle {
    if (!this.accountPool) {
      const Coordinator = AccountPoolCoordinator as unknown as new (auth: CodexAuthAdapter, broker: AccountLeaseBroker, options?: { runCodexHome?: NightPath | null }) => PoolHandle;
      this.accountPool = new Coordinator(new CodexAuthAdapter(), new AccountLeaseBroker(), { runCodexHome: trustedHomePath(this.store) });
    } else if (this.accountPool.runCodexHome == null) {
      this.accountPool.runCodexHome = trustedHomePath(this.store);
    }
    return this.accountPool;
  }

  private isAutoPool(state?: JsonObject | null): boolean {
    const current = state || this.db().loadState();
    return (get(current, "account_mode", "CURRENT_ONLY") as string) === "AUTO_POOL";
  }

  private reconcilePoolAuthority(state: JsonObject): boolean {
    let active: { accountKey: string; fingerprint: string } | null;
    try {
      active = this.poolCoordinator().activeAccount();
    } catch (error) {
      if (!(error instanceof AccountBrokerError)) throw error;
      this.failClosed(`AUTO_POOL active-account reconciliation failed: ${error.name}`, ErrorKind.STATE);
      return false;
    }
    if (active == null) return true;
    const authorized = new Set((Array.isArray(state.authorized_accounts) ? state.authorized_accounts : []) as string[]);
    if (!authorized.has(active.accountKey)) {
      this.failClosed("codex-auth active account is outside the explicitly authorized pool", ErrorKind.STATE);
      return false;
    }
    this.db().mutate("account_authority_reconciled", "active account identity re-read after supervisor restart", (item) => ({
      ...item,
      active_account_fingerprint: active!.fingerprint,
    }));
    return true;
  }

  private async executeLoop(start: boolean): Promise<JsonObject> {
    let state = this.db().loadState();
    if (state.state === State.DONE) return state;
    if (!start && (state.state === State.BLOCKED || state.state === State.STOPPED || state.state === State.FAILED)) {
      if (!state.thread_id) return this.failClosed("cannot manually resume without an exact durable thread_id", ErrorKind.STATE);
      this.db().transition(State.RECOVERING, "manual_resume_requested", "explicit resume requested for a terminal run; exact thread preserved");
    }
    if (start && state.state === State.NEW) {
      if (!(await this.preflight())) return this.db().loadState();
    } else if (state.state === State.NEW) {
      this.db().transition(State.PREFLIGHT, "preflight_started", "resume requested for a new run");
      if (!(await this.preflightChecks())) return this.db().loadState();
    }
    if (!(await this.recoverSupervisorRestart())) return this.db().loadState();
    try {
      while (true) {
        if (this.interruptRequested) throw new KeyboardInterrupt();
        if (this.stopRequested) break;
        state = this.db().loadState();
        const current = state.state as State;
        if (TERMINAL_STATES.has(current)) return state;
        if (current === State.WAIT_QUOTA) {
          if (!(await this.waitAndRevalidateQuota())) return this.db().loadState();
          continue;
        }
        if (current === State.RETRY_BACKOFF) {
          await this.sleepBackoff(state);
          this.db().transition(State.RECOVERING, "transient_retry_ready", "bounded transient backoff elapsed");
          continue;
        }
        if (current === State.RECOVERING || current === State.RUNNING || current === State.PREFLIGHT || current === State.STOPPED || current === State.FAILED) {
          if (current === State.PREFLIGHT) this.db().transition(State.RUNNING, "provider_launch_ready", "preflight passed");
          else if (current === State.STOPPED || current === State.FAILED) {
            if (!state.thread_id) return this.failClosed("cannot resume without an exact durable thread_id", ErrorKind.STATE);
            this.db().transition(State.RECOVERING, "manual_resume_requested", "resume command requested exact thread");
          }
          await this.runTurn();
          continue;
        }
        if (current === State.VERIFYING) {
          if (this.finishVerification()) return this.db().loadState();
          continue;
        }
        return this.failClosed(`unsupported state ${current}`, ErrorKind.STATE);
      }
    } catch (error) {
      if (!(error instanceof KeyboardInterrupt)) throw error;
      this.requestStop();
      this.db().transition(State.STOPPED, "supervisor_interrupted", "Ctrl-C preserved durable state");
      return this.db().loadState();
    }
    if (this.stopRequested) this.db().transition(State.STOPPED, "supervisor_stopped", "stop requested; state preserved");
    return this.db().loadState();
  }

  private async getQuotaSnapshot(): Promise<QuotaSnapshot> {
    const state = this.db().loadState();
    if (state.provider === "agy") return await Promise.resolve(providerPort("agy").probeQuota(this.store, this.db().repo, (state.model as string | null) ?? null));
    return await Promise.resolve(this.quotaProvider.read());
  }

  private async preflight(): Promise<boolean> {
    this.db().transition(State.PREFLIGHT, "preflight_started", "repo, provider, auth, and quota sanity checks");
    return this.preflightChecks();
  }

  private async preflightChecks(): Promise<boolean> {
    const state = this.db().loadState();
    let currentRoot = "";
    let binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
    try {
      currentRoot = repoRoot(pathText(this.db().repo)).raw;
      if (currentRoot !== new NightPath(String(state.repo)).resolve().raw) {
        this.failClosed("repository root changed", ErrorKind.GIT);
        return false;
      }
      const git = snapshot(currentRoot);
      if (git.conflicts) {
        this.failClosed("repository has unresolved merge conflicts", ErrorKind.GIT);
        return false;
      }
      this.db().mutate("git_preflight_observed", "Git HEAD captured before account-pool/provider work", (item) => ({ ...item, last_git_head: git.head }));
    } catch (error) {
      if (!(error instanceof GitError)) throw error;
      this.failClosed("Git preflight failed", ErrorKind.GIT);
      return false;
    }
    const providerName = String(state.provider ?? "codex");
    const provider = providerPort(providerName);
    if (process.env.NIGHTWATCH_IGNORE_CONCURRENT_CODEX !== "1") {
      let running = providerName === "codex" ? findRepoCodexProcesses(currentRoot, process.pid) : provider.findActiveProcesses(currentRoot, process.pid);
      const currentActive = state.active_process;
      if (isRecord(currentActive) && currentActive.pid) running = running.filter((item) => item.pid !== currentActive.pid);
      if (running.length) {
        const pids = running.map((item) => String(item.pid)).join(", ");
        this.failClosed(
          `another ${capitalize(providerName)} process (PID ${pids}) is active in this repository; use \`nightwatch watch\` to monitor it passively or wait until it exits`,
          ErrorKind.STATE,
        );
        return false;
      }
    }
    if (providerName === "agy") {
      const binPath = provider.resolveBinary();
      if (!commandAvailable(binPath)) {
        this.failClosed("AGY CLI was not found", ErrorKind.UNKNOWN);
        return false;
      }
    } else {
      binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
      if (!commandAvailable(binary)) {
        this.failClosed("Codex CLI was not found", ErrorKind.UNKNOWN);
        return false;
      }
      if (!this.authSanity(binary)) {
        this.failClosed("Codex authentication sanity check failed", ErrorKind.AUTH);
        return false;
      }
    }
    if (this.isAutoPool(state)) {
      const codexVer = installedCodexVersion(binary);
      const threadMode = crossAccountThreadModeForVersion(codexVer);
      const cleanVer = codexVer ? codexVer.replace(/^codex-cli /, "").trim() : null;
      this.db().mutate("account_thread_mode_reconciled", "cross-account thread capability reconciled with installed Codex", (item) => ({
        ...item,
        cross_account_thread_mode: threadMode,
        cross_account_thread_capability: { codex_version: cleanVer, mode: threadMode },
      }));
      return (await this.preparePoolAccount(true)) || this.db().loadState().state === State.WAIT_QUOTA;
    }
    if (providerName === "agy") {
      let quota: QuotaSnapshot;
      try {
        quota = await this.getQuotaSnapshot();
      } catch (error) {
        this.failClosed(`AGY quota authority error: ${error instanceof Error ? error.message : String(error)}`, ErrorKind.STATE);
        return false;
      }
      if (quota.error) {
        const errLow = quota.error.toLowerCase();
        if (["unauthorized", "authentication", "not authenticated", "login", "401", "403"].some((key) => errLow.includes(key)) || ` ${errLow} `.includes(" auth ")) {
          this.failClosed(`AGY quota auth check failed: ${quota.error}`, ErrorKind.AUTH);
        } else if (errLow.includes("unknown agy model") || errLow.includes("unknown model")) {
          this.failClosed(`AGY configuration error: ${quota.error}`, ErrorKind.STATE);
        } else {
          this.failClosed(`AGY quota authority unavailable: ${quota.error}`, ErrorKind.STATE);
        }
        return false;
      }
      this.db().mutate("quota_sanity_ok", "quota provider returned a validated snapshot", (item) => ({ ...item, quota: quota.toDict(), quota_source: quota.source }));
      if (quota.exhaustedWindows().length) {
        this.enterInitialQuotaWait(quota);
        return true;
      }
      return true;
    }
    try {
      const quota = await this.getQuotaSnapshot();
      this.db().mutate("quota_sanity_ok", "quota provider returned a validated snapshot", (item) => ({ ...item, quota: quota.toDict(), quota_source: quota.source }));
      if ((quota.source === "live_app_server" || quota.source === "fake_file") && quota.exhaustedWindows().length) {
        this.enterInitialQuotaWait(quota);
        return true;
      }
    } catch (error) {
      // A first run can still start: quota may be healthy but its optional
      // read may be network-blocked. Automatic recovery remains fail closed.
      this.db().appendEvent("quota_sanity_unavailable", "quota source unavailable during preflight", { error: error instanceof Error ? error.name : "Error" });
    }
    return true;
  }

  private authSanity(binary: string): boolean {
    if (process.env.NIGHTWATCH_SKIP_AUTH_CHECK === "1") return true;
    const result = spawnSync(binary, ["login", "status"], { timeout: 8_000, stdio: "ignore" });
    return !result.error && result.status === 0;
  }

  private async recoverSupervisorRestart(): Promise<boolean> {
    let state = this.db().loadState();
    const active = state.active_process;
    if (isRecord(active)) {
      if (pidAlive(typeof active.pid === "number" ? active.pid : null) && !processIdentityMatches(active)) {
        this.failClosed("active provider PID identity cannot be proven after restart", ErrorKind.STATE);
        return false;
      }
      this.db().transition(State.RECOVERING, "active_process_reconciled", "supervisor restart found a prior Codex child", { active_process: active });
      const deadline = performance.now() + 60_000;
      while (processIdentityMatches(active) && performance.now() < deadline) await this.sleepSeconds(0.25);
      if (processIdentityMatches(active)) {
        this.failClosed("prior Codex process is still alive but its output is unobserved", ErrorKind.STATE);
        return false;
      }
      state = this.db().mutate("active_process_exited", "prior Codex child is no longer alive", (item) => ({ ...item, active_process: null }));
      if (!state.thread_id) {
        this.failClosed("supervisor restarted before exact thread_id was durable", ErrorKind.STATE);
        return false;
      }
      if (state.resume_claim) {
        this.failClosed("prior exact-thread resume result is ambiguous after supervisor restart", ErrorKind.STATE);
        return false;
      }
      if (this.isAutoPool(state)) {
        if (!this.reconcilePoolAuthority(state)) return false;
        this.db().mutate("account_reselection_required", "supervisor restart requires fresh account authority reconciliation", (item) => ({
          ...item,
          account_reselect: true,
          account_lease: null,
        }));
      }
      return true;
    }
    if (state.state === State.RUNNING && state.thread_id) {
      this.db().transition(State.RECOVERING, "supervisor_restart_recovery", "running state with exact thread recovered from disk");
    }
    if (this.isAutoPool(state)) {
      if (!this.reconcilePoolAuthority(state)) return false;
      this.db().mutate("account_reselection_required", "supervisor restart requires fresh account authority reconciliation", (item) => ({
        ...item,
        account_reselect: true,
        account_lease: null,
      }));
    }
    if (state.resume_claim && isRecord(state.resume_claim)) {
      if (state.resume_claim.phase === "claimed") {
        this.db().mutate("resume_claim_proven_not_sent", "claim existed before provider spawn preparation; safe exact-thread retry", (item) => ({
          ...item,
          resume_claim: null,
        }));
        return true;
      }
      this.failClosed("ambiguous resume claim after supervisor restart; run `nightwatch recover --ack-ambiguous` after review", ErrorKind.STATE);
      return false;
    }
    return true;
  }

  private async runTurn(): Promise<JsonObject> {
    let state = this.db().loadState();
    const providerName = String(state.provider ?? "codex");
    let thread = state.thread_id;
    if (state.state === State.PREFLIGHT || state.state === State.RECOVERING) {
      state = this.db().transition(State.RUNNING, "provider_launch_ready", state.state === State.PREFLIGHT ? "preflight passed" : "quota recovered; ready for provider launch");
    }
    if (this.isAutoPool(state)) {
      const key = state.current_account_key;
      if (typeof key !== "string" || !key) {
        if (!(await this.preparePoolAccount())) return this.db().loadState();
        state = this.db().loadState();
      }
      thread = state.thread_id;
    }
    const handoff = state.thread_handoff;
    const handoffActive =
      this.isAutoPool(state) &&
      isRecord(handoff) &&
      handoff.generation === state.generation &&
      handoff.status === "prepared" &&
      isRecord(handoff.packet);
    const providerThread = handoffActive ? null : typeof thread === "string" ? thread : null;
    let action: string;
    let prompt: string;
    if (handoffActive && isRecord(handoff) && isRecord(handoff.packet)) {
      action = "starting controlled account-handoff thread";
      prompt = handoffPrompt(this.store, state, handoff.packet);
    } else if (!providerThread) {
      action = "starting new exact-thread goal";
      prompt = startPrompt(this.store, String(state.goal));
    } else {
      action = "resuming exact thread";
      prompt = resumePrompt(this.store, state);
    }
    this.db().mutate("provider_launch_prepared", action, (item) => ({
      ...item,
      resume_claim: isRecord(item.resume_claim) ? { ...item.resume_claim, phase: "spawn_prepared" } : null,
      last_error: null,
    }));
    const onSpawn = (pid: number, childAction: string) => {
      const identity = linuxProcessIdentity(pid);
      if (!identity) throw new StateIntegrityError("cannot persist Linux provider process identity");
      const extra: JsonObject = {};
      if (state.provider === "agy") {
        try {
          const pgrp = getpgid(pid);
          if (pgrp === pid && pgrp !== getpgrp() && pgrp > 1) extra.pgid = pgrp;
        } catch {
          /* process group is optional */
        }
      }
      const eventMsg = state.provider === "agy" ? "AGY child process started" : "Codex child process started";
      this.db().mutate("provider_started", eventMsg, (item) => ({
        ...item,
        active_process: {
          ...identity,
          ...extra,
          action: childAction,
          thread_id: item.thread_id ?? null,
          started_at: nowIso(),
        },
        resume_claim: isRecord(item.resume_claim) ? { ...item.resume_claim, phase: "spawned" } : null,
      }));
      crashHook("AFTER_PROVIDER_SPAWN");
    };
    const onThread = (candidate: string) => {
      this.db().mutate("thread_started", "thread ID captured from Codex JSONL", (item) => {
        const handoffState = item.thread_handoff;
        let updated = handoffState;
        if (isRecord(handoffState) && handoffState.status === "prepared" && handoffState.generation === item.generation) {
          updated = { ...handoffState, status: "captured", new_thread_id: candidate };
        }
        return { ...item, thread_id: candidate, thread_handoff: updated };
      });
      crashHook("AFTER_THREAD_CAPTURE");
    };
    crashHook("BEFORE_PROVIDER_SPAWN");
    let result: ProviderResult;
    if (this.isAutoPool(state)) {
      let key = state.current_account_key;
      if (typeof key !== "string" || !key) {
        if (!(await this.preparePoolAccount())) return this.db().loadState();
        state = this.db().loadState();
        key = state.current_account_key;
      }
      if (typeof key !== "string" || !key) return this.failClosed("AUTO_POOL has no selected account", ErrorKind.STATE);
      const coordinator = this.poolCoordinator();
      try {
        this.db().mutate("account_switch_prepared", "account lease acquired before capsule activation", (item) => ({
          ...item,
          account_claim: {
            ...(isRecord(item.account_claim) ? item.account_claim : {}),
            generation: item.generation,
            to: accountFingerprint(key as string),
            phase: "switch_prepared",
          },
        }));
        result = await withAccountSession(coordinator, key, String(state.run_id), pathText(this.db().repo), num(state.generation), async (runtime) => {
          if (!runtime.lease || runtime.codexHome == null) throw new AccountSchemaError("account session did not provide a lease and CODEX_HOME");
          this.db().mutate("account_switched", "selected account capsule activated before provider launch", (item) => ({
            ...item,
            account_claim: { ...(isRecord(item.account_claim) ? item.account_claim : {}), phase: "switched" },
          }));
          this.db().mutate("account_lease_owned", "selected account lease held for provider boundary", (item) => ({
            ...item,
            account_lease: { fingerprint: accountFingerprint(key as string), run_id: state.run_id, phase: "provider" },
          }));
          this.db().verifyCodexHome();
          return await invokeCodex(this.store, num(state.generation), prompt, providerThread, onSpawn, onThread, null, null, runtime.codexHome, runtime.lease.fd, accountFingerprint(key as string));
        });
      } catch (error) {
        if (!(error instanceof AccountBrokerError)) throw error;
        this.db().mutate("account_lease_released", "account session failed or could not synchronize safely", (item) => ({
          ...item,
          active_process: null,
          account_lease: null,
          last_error: `account session failed: ${error.name}`,
        }));
        return this.failClosed(`account session failed safely: ${error.name}`, ErrorKind.AUTH);
      }
      this.db().mutate("account_lease_released", "provider child exited before account lease release", (item) => ({
        ...item,
        account_lease: null,
        account_claim: { ...(isRecord(item.account_claim) ? item.account_claim : {}), phase: "provider_exited" },
      }));
    } else if (providerName === "agy") {
      result = await Promise.resolve(providerPort("agy").runTurn(this.store, num(state.generation), prompt, providerThread, onSpawn, onThread, this.stopEvent));
    } else {
      result = await Promise.resolve(invokeCodex(this.store, num(state.generation), prompt, providerThread, onSpawn, onThread));
    }
    if (this.interruptRequested) throw new KeyboardInterrupt();
    this.db().mutate("provider_finished", `${capitalize(providerName)} child process finished`, (item) => ({
      ...item,
      active_process: null,
      last_provider_exit: result.exitCode,
      last_provider_signal: result.signal,
    }));
    crashHook("AFTER_PROVIDER_EXIT");
    this.db().appendEvent("provider_result", `classified ${capitalize(providerName)} result`, {
      error_kind: result.errorKind,
      exit_code: result.exitCode,
      signal: result.signal,
      event_count: result.eventCount,
      malformed_count: result.malformedCount,
    });
    return this.handleResult(result);
  }

  private handleResult(result: ProviderResult): JsonObject {
    if (result.aborted || this.stopRequested) {
      return this.db().transition(State.STOPPED, "provider_interrupted", "provider interrupted by user; state preserved", {
        last_error: result.errorDetail,
        resume_claim: null,
      });
    }
    if (this.isAutoPool() && (result.errorKind === ErrorKind.QUOTA_5H || result.errorKind === ErrorKind.QUOTA_WEEKLY)) return this.rotatePoolAfterQuota(result);
    if (this.isAutoPool() && result.errorKind === ErrorKind.AUTH) return this.rotatePoolAfterAuth(result);
    if (result.errorKind === ErrorKind.QUOTA_5H || result.errorKind === ErrorKind.QUOTA_WEEKLY) return this.enterQuotaWait(result);
    if (result.errorKind === ErrorKind.TEMPORARY_429 || result.errorKind === ErrorKind.CAPACITY || result.errorKind === ErrorKind.NETWORK) {
      if (this.db().loadState().resume_claim) {
        return this.db().transition(State.BLOCKED, "quota_resume_not_retried", "quota recovery provider turn failed after its single-flight lease; refusing a second resume for the same generation", {
          last_error: result.errorDetail,
          error_kind: result.errorKind,
          resume_claim: null,
        });
      }
      return this.handleTransient(result);
    }
    if (result.errorKind === ErrorKind.CRASH) return this.handleCrash(result);
    if (result.errorKind === ErrorKind.BLOCKER) {
      return this.db().transition(State.BLOCKED, "task_blocked", result.blocker || result.errorDetail || "Codex reported a task blocker", {
        blocker: result.blocker || result.errorDetail,
        last_error: result.errorDetail,
        error_kind: result.errorKind,
        resume_claim: null,
      });
    }
    if (
      result.errorKind === ErrorKind.AUTH ||
      result.errorKind === ErrorKind.STATE ||
      result.errorKind === ErrorKind.MALFORMED ||
      result.errorKind === ErrorKind.GIT ||
      result.errorKind === ErrorKind.UNKNOWN
    ) {
      const target = result.errorKind === ErrorKind.STATE || result.errorKind === ErrorKind.GIT || result.errorKind === ErrorKind.MALFORMED ? State.BLOCKED : State.FAILED;
      return this.db().transition(target, "provider_failed", result.errorDetail || result.errorKind, {
        last_error: result.errorDetail,
        error_kind: result.errorKind,
        resume_claim: null,
      });
    }
    return this.afterSuccess();
  }

  private recordPoolDecision(decision: PoolDecision): void {
    const snapshots: JsonObject = {};
    const resets: JsonObject = {};
    for (const candidate of decision.candidates) {
      if (!candidate.quota) continue;
      snapshots[candidate.fingerprint] = candidate.quota.toDict();
      resets[candidate.fingerprint] = candidate.quota.windows().map((window) => window.resetsAt).filter((value) => value != null);
    }
    const selected = decision.selected;
    this.db().mutate("account_pool_probed", "authorized accounts reconciled through fresh App Server quota sessions", (item) => ({
      ...item,
      account_snapshots: { ...(isRecord(item.account_snapshots) ? item.account_snapshots : {}), ...snapshots },
      account_reset_times: { ...(isRecord(item.account_reset_times) ? item.account_reset_times : {}), ...resets },
      quota: selected?.quota ? selected.quota.toDict() : item.quota,
      quota_source: selected?.quota ? selected.quota.source : item.quota_source,
      pool_probe_failures: 0,
      recovery_failures: 0,
    }));
  }

  private persistPoolSelection(decision: PoolDecision, reason: string): boolean {
    const selected = decision.selected;
    if (!selected) return false;
    const state = this.db().loadState();
    const previous = (state.current_account_fingerprint as string | null) || (isRecord(state.account_claim) ? (state.account_claim.from as string | null) : null);
    let handoffPacket: JsonObject | null = null;
    if (previous && previous !== selected.fingerprint && state.thread_id && (get(state, "cross_account_thread_mode", "INCONCLUSIVE") as string) !== "PROVEN") {
      const plan = this.db().loadPlan();
      const milestones = (plan.milestones as JsonObject[]) ?? [];
      handoffPacket = {
        goal: state.goal,
        verification_commands: [...((this.db().loadPolicy().final_commands as string[]) ?? [])],
        repo: state.repo,
        git_head: state.last_git_head ?? null,
        prior_thread_id: state.thread_id,
        generation: state.generation,
        verified_milestones: milestones.filter((item) => item.status === "verified").map((item) => item.id),
        remaining_milestones: milestones.filter((item) => item.status !== "verified").map((item) => item.id),
        blocker: state.blocker ?? null,
      };
    }
    this.recordPoolDecision(decision);
    this.db().mutate("account_selected", reason, (item) => ({
      ...item,
      current_account_key: selected.accountKey,
      current_account_fingerprint: selected.fingerprint,
      account_generation: num(item.account_generation) + 1,
      account_reselect: false,
      last_switch_reason: reason,
      thread_handoff: handoffPacket
        ? { mode: "CONTROLLED_THREAD_HANDOFF", status: "prepared", generation: item.generation, from: previous, to: selected.fingerprint, packet: handoffPacket }
        : item.thread_handoff,
      account_claim: { generation: item.generation, from: previous, to: selected.fingerprint, phase: "selected" },
    }));
    return true;
  }

  async preparePoolAccount(reselect = false): Promise<boolean> {
    const state = this.db().loadState();
    if (!this.isAutoPool(state)) return true;
    if (!reselect && !state.account_reselect && typeof state.current_account_key === "string") return true;
    const keys = state.authorized_accounts;
    if (!Array.isArray(keys) || !keys.length) {
      this.failClosed("AUTO_POOL has no explicitly authorized accounts", ErrorKind.STATE);
      return false;
    }
    let decision: PoolDecision;
    try {
      decision = this.poolCoordinator().probe(
        keys as string[],
        String(state.run_id),
        pathText(this.db().repo),
        num(state.generation),
        new Set(Object.keys(isRecord(state.account_errors) ? state.account_errors : {})),
      );
    } catch (error) {
      if (!(error instanceof AccountBrokerError)) throw error;
      this.countPoolRecoveryFailure(`AUTO_POOL account discovery/probe failed: ${error.name}`);
      this.failClosed(`AUTO_POOL account discovery/probe failed: ${error.name}`, ErrorKind.AUTH);
      return false;
    }
    if (this.persistPoolSelection(decision, "highest usable account capacity selected")) return true;
    const candidates = decision.candidates;
    if (candidates.length && candidates.every((candidate) => candidate.authError)) {
      this.db().transition(State.BLOCKED, "account_pool_auth_failed", "all explicitly authorized accounts have authentication errors", {
        last_error: "all explicitly authorized accounts have authentication errors",
        error_kind: ErrorKind.AUTH,
      });
      return false;
    }
    if (
      candidates.length &&
      candidates.every(
        (candidate) =>
          candidate.leased ||
          candidate.authError ||
          (candidate.quota != null &&
            (candidate.quota.source === "live_app_server" || candidate.quota.source === "fake_file") &&
            !candidate.quota.error &&
            candidate.quota.primary != null &&
            candidate.quota.secondary != null &&
            candidate.quota.primary.usedPercent != null &&
            candidate.quota.secondary.usedPercent != null),
      )
    ) {
      this.enterPoolWait(decision, "all authorized accounts are exhausted or busy");
      return false;
    }
    this.failClosed("AUTO_POOL could not obtain authoritative quota for an eligible account", ErrorKind.STATE);
    return false;
  }

  private enterPoolWait(decision: PoolDecision, reason: string): boolean {
    const state = this.db().loadState();
    if (TERMINAL_STATES.has(state.state as State)) return false;
    const busy = decision.candidates.some((candidate) => candidate.leased);
    let target = decision.earliestReset;
    if (target == null || busy) target = unixNow() + (busy ? envInt("NIGHTWATCH_WAIT_POLL_SECONDS", QUOTA_POLL_SECONDS) : 5 * 3600);
    const bufferSeconds = envInt("NIGHTWATCH_QUOTA_BUFFER_SECONDS", QUOTA_BUFFER_SECONDS);
    target += busy ? 0 : Math.max(0, bufferSeconds);
    this.db().transition(State.WAIT_QUOTA, busy ? "account_pool_busy_wait" : "account_pool_wait", reason, {
      current_account_key: null,
      current_account_fingerprint: null,
      account_reselect: true,
      account_lease: null,
      next_resume_at: isoUnix(target),
      pool_wait_reason: busy ? "busy" : "exhausted",
      last_error: reason,
      resume_claim: null,
    });
    return true;
  }

  rotatePoolAfterQuota(result: ProviderResult): JsonObject {
    const state = this.db().loadState();
    const windows = quotaWindowsOrFallback(result);
    return this.db().transition(State.RECOVERING, "account_quota_exhausted", result.errorDetail || result.errorKind || "", {
      generation: num(state.generation) + 1,
      quota_cycles: num(state.quota_cycles) + 1,
      current_account_key: null,
      current_account_fingerprint: null,
      account_reselect: true,
      account_lease: null,
      account_claim: { generation: num(state.generation) + 1, from: state.current_account_fingerprint ?? null, to: null, phase: "provider_exited" },
      quota: state.quota ?? null,
      quota_windows: windows.map((window) => window.toDict()),
      last_error: result.errorDetail,
      error_kind: result.errorKind,
      retry_attempt: 0,
      crash_attempt: 0,
    });
  }

  private rotatePoolAfterAuth(result: ProviderResult): JsonObject {
    const state = this.db().loadState();
    const key = state.current_account_key;
    const errors = { ...(isRecord(state.account_errors) ? state.account_errors : {}) };
    if (typeof key === "string" && key) errors[key] = { fingerprint: accountFingerprint(key), error_kind: ErrorKind.AUTH, at: nowIso() };
    return this.db().transition(State.RECOVERING, "account_auth_error", result.errorDetail || "selected account authentication failed", {
      current_account_key: null,
      current_account_fingerprint: null,
      account_reselect: true,
      account_lease: null,
      account_claim: {
        generation: state.generation,
        from: typeof key === "string" && key ? accountFingerprint(key) : (state.current_account_fingerprint ?? null),
        to: null,
        phase: "provider_exited",
      },
      account_errors: errors,
      last_error: result.errorDetail,
      error_kind: ErrorKind.AUTH,
    });
  }

  private enterInitialQuotaWait(quota: QuotaSnapshot): JsonObject {
    const windows = quota.windows();
    const exhausted = quota.exhaustedWindows();
    let reset = exhausted.reduce((max, window) => Math.max(max, window.resetsAt || 0), 0);
    if (!reset) reset = windows.reduce((max, window) => Math.max(max, window.resetsAt || 0), 0);
    const weeklyOnly = exhausted.some((window) => window.name === "weekly") && !exhausted.some((window) => window.name === "5h");
    if (!reset) reset = unixNow() + (weeklyOnly ? 7 * 86400 : 5 * 3600);
    const nextAt = reset + Math.max(0, envInt("NIGHTWATCH_QUOTA_BUFFER_SECONDS", QUOTA_BUFFER_SECONDS));
    const errorKind = weeklyOnly ? ErrorKind.QUOTA_WEEKLY : ErrorKind.QUOTA_5H;
    const waiting = this.db().transition(State.WAIT_QUOTA, "quota_exhausted", "authoritative quota is exhausted; first launch deferred", {
      next_resume_at: isoUnix(nextAt),
      quota_source: quota.source,
      quota: quota.toDict(),
      quota_windows: windows.map((window) => window.toDict()),
      resume_claim: null,
      last_error: "authoritative quota is exhausted; first launch deferred",
      error_kind: errorKind,
    });
    crashHook("AFTER_QUOTA_DETECT");
    return waiting;
  }

  private enterQuotaWait(result: ProviderResult): JsonObject {
    const state = this.db().loadState();
    if (num(state.recovery_failures) >= MAX_RECOVERY_FAILURES || (num(state.quota_cycles) === 0 && num(state.recoveries) >= MAX_QUOTA_RECOVERIES)) {
      return this.failClosed("quota recovery circuit breaker reached", ErrorKind.QUOTA_5H);
    }
    const windows = quotaWindowsOrFallback(result);
    let reset = result.resetAt || maxReset(windows);
    let source = result.resetSource || "bounded_fallback";
    if (!reset) {
      reset = unixNow() + (result.errorKind === ErrorKind.QUOTA_WEEKLY ? 7 * 86400 : 5 * 3600);
      source = "bounded_fallback";
    }
    const nextAt = reset + Math.max(0, envInt("NIGHTWATCH_QUOTA_BUFFER_SECONDS", QUOTA_BUFFER_SECONDS));
    const waiting = this.db().transition(State.WAIT_QUOTA, "quota_exhausted", result.errorDetail || result.errorKind || "", {
      generation: num(state.generation) + 1,
      next_resume_at: isoUnix(nextAt),
      quota_source: source,
      quota_windows: windows.map((window) => window.toDict()),
      resume_claim: null,
      retry_attempt: 0,
      crash_attempt: 0,
      quota_cycles: num(state.quota_cycles) + 1,
      recoveries: num(state.recoveries) + 1,
      last_error: result.errorDetail,
      error_kind: result.errorKind,
    });
    crashHook("AFTER_QUOTA_DETECT");
    return waiting;
  }

  private async waitAndRevalidateQuota(): Promise<boolean> {
    if (this.isAutoPool()) return this.waitAndRevalidatePool();
    const state = this.db().loadState();
    const target = parseTimestamp(state.next_resume_at);
    if (!target) {
      this.failClosed("WAIT_QUOTA has no valid next_resume_at", ErrorKind.STATE);
      return false;
    }
    await this.sleepUntil(unixOf(target), envFloat("NIGHTWATCH_WAIT_POLL_SECONDS", QUOTA_POLL_SECONDS));
    const windows = Array.isArray(state.quota_windows) ? (state.quota_windows as JsonObject[]) : [];
    let governing = new Set(windows.filter((window) => isRecord(window) && window.name).map((window) => String(window.name)));
    if (!governing.size) governing = new Set(["5h", "weekly"]);
    if (this.stopRequested) {
      this.db().transition(State.STOPPED, "supervisor_stopped", "stop requested during quota wait");
      return false;
    }
    try {
      const quota = await this.getQuotaSnapshot();
      if (quota.error) return this.retryQuotaProbe(state, `quota authority error: ${quota.error}`, quota.error);
      this.db().mutate("quota_revalidated", "quota authority queried after reset", (item) => ({ ...item, quota: quota.toDict(), quota_source: quota.source }));
      if (quota.source === "live_app_server" || quota.source === "fake_file" || quota.source === "AGY_CLI") {
        if (!quotaIsRecovered(quota, governing)) {
          const relevant = quota.windows().filter((window) => governing.has(window.name));
          const later = relevant.length ? Math.max(...relevant.map((window) => window.resetsAt || unixNow() + QUOTA_POLL_SECONDS)) : unixNow() + QUOTA_POLL_SECONDS;
          const buffered = later + Math.max(0, envInt("NIGHTWATCH_QUOTA_BUFFER_SECONDS", QUOTA_BUFFER_SECONDS));
          this.db().transition(State.WAIT_QUOTA, "quota_still_exhausted", "live quota authority is still exhausted; no resume sent", {
            next_resume_at: isoUnix(buffered),
            resume_claim: null,
          });
          return true;
        }
        return this.startQuotaRecovery("live quota authority confirmed recovery");
      }
      return this.guardedQuotaProbe("live quota authority unavailable; rollout is schedule-only");
    } catch (error) {
      const name = error instanceof Error ? error.name : "Error";
      return this.retryQuotaProbe(state, `quota authority unavailable: ${name}`, name);
    }
  }

  private retryQuotaProbe(state: JsonObject, detail: string, breakerDetail: string): boolean {
    const failures = num(state.recovery_failures) + 1;
    if (failures >= MAX_RECOVERY_FAILURES) {
      const exhausted = `quota recovery circuit breaker reached: ${breakerDetail}`;
      this.db().transition(State.FAILED, "quota_recovery_budget_exhausted", exhausted, {
        recovery_failures: failures,
        last_error: exhausted,
        error_kind: ErrorKind.QUOTA_5H,
        resume_claim: null,
      });
      return false;
    }
    this.db().transition(State.WAIT_QUOTA, "quota_probe_failed", detail, {
      next_resume_at: isoUnix(unixNow() + QUOTA_POLL_SECONDS * failures),
      recovery_failures: failures,
      last_error: detail,
      resume_claim: null,
    });
    return true;
  }

  private async waitAndRevalidatePool(): Promise<boolean> {
    const state = this.db().loadState();
    const target = parseTimestamp(state.next_resume_at);
    if (!target) {
      this.failClosed("AUTO_POOL wait has no valid next_resume_at", ErrorKind.STATE);
      return false;
    }
    await this.sleepUntil(unixOf(target), envFloat("NIGHTWATCH_WAIT_POLL_SECONDS", QUOTA_POLL_SECONDS));
    if (this.stopRequested) {
      this.db().transition(State.STOPPED, "supervisor_stopped", "stop requested during account-pool wait");
      return false;
    }
    try {
      const decision = this.poolCoordinator().probe(
        [...((state.authorized_accounts as string[]) ?? [])],
        String(state.run_id),
        pathText(this.db().repo),
        num(state.generation),
        new Set(Object.keys(isRecord(state.account_errors) ? state.account_errors : {})),
      );
      if (decision.selected) {
        this.persistPoolSelection(decision, "account pool re-probed after wait; greatest usable capacity selected");
        this.db().transition(State.RECOVERING, "account_pool_recovered", "authoritative account pool revalidation found usable quota", {
          pool_wait_reason: null,
          last_error: null,
        });
        return true;
      }
      this.recordPoolDecision(decision);
      this.enterPoolWait(decision, "account pool remains exhausted or busy");
      return true;
    } catch (error) {
      if (!(error instanceof AccountBrokerError)) throw error;
      const failures = num(get(state, "recovery_failures", get(state, "pool_probe_failures", 0))) + 1;
      const detail = `account-pool revalidation failed: ${error.name}`;
      if (failures >= MAX_RECOVERY_FAILURES) {
        this.db().transition(State.BLOCKED, "account_pool_recovery_budget_exhausted", "account-pool authoritative revalidation failed repeatedly", {
          recovery_failures: failures,
          pool_probe_failures: failures,
          last_error: detail,
          error_kind: ErrorKind.STATE,
          resume_claim: null,
        });
        return false;
      }
      this.db().transition(State.WAIT_QUOTA, "account_pool_probe_failed", detail, {
        next_resume_at: isoUnix(unixNow() + QUOTA_POLL_SECONDS * failures),
        pool_probe_failures: failures,
        recovery_failures: failures,
        last_error: detail,
      });
      return true;
    }
  }

  private startQuotaRecovery(reason: string): boolean {
    if (!this.claimResume()) return false;
    this.db().transition(State.RECOVERING, "resume_started", reason, { recovery_failures: 0 });
    return true;
  }

  private guardedQuotaProbe(reason: string): boolean {
    if (this.db().loadState().resume_claim) {
      this.failClosed("quota generation already used its guarded provider availability attempt", ErrorKind.STATE);
      return false;
    }
    if (!this.claimResume()) return false;
    this.db().transition(State.RECOVERING, "quota_guarded_probe_started", reason);
    return true;
  }

  private claimResume(): boolean {
    let claimed = false;
    const claimId = `${String(this.db().loadState().run_id)}:${String(this.db().loadState().generation)}`;
    this.db().mutate("resume_claimed", "single-flight lease claimed for quota generation", (state) => {
      if (state.state !== State.WAIT_QUOTA) return state;
      const existing = state.resume_claim;
      if (isRecord(existing) && existing.generation === state.generation) return state;
      claimed = true;
      return {
        ...state,
        resume_claim: { generation: state.generation, claim_id: claimId, claimed_at: nowIso(), pid: process.pid, phase: "claimed" },
        last_error: null,
      };
    });
    if (claimed) crashHook("AFTER_RESUME_CLAIM");
    return claimed;
  }

  private handleTransient(result: ProviderResult): JsonObject {
    const state = this.db().loadState();
    const attempt = num(state.retry_attempt) + 1;
    if (attempt > MAX_TRANSIENT_RETRIES) {
      return this.db().transition(State.BLOCKED, "transient_retry_exhausted", "transient failure retry budget exhausted", {
        last_error: result.errorDetail,
        error_kind: result.errorKind,
      });
    }
    const delay = TRANSIENT_BACKOFF_SECONDS[Math.min(attempt - 1, TRANSIENT_BACKOFF_SECONDS.length - 1)];
    return this.db().transition(State.RETRY_BACKOFF, "transient_error", `bounded retry ${attempt}/${MAX_TRANSIENT_RETRIES} after ${result.errorKind || "transient"}`, {
      retry_attempt: attempt,
      next_resume_at: isoUnix(Date.now() / 1000 + delay),
      last_error: result.errorDetail,
      error_kind: result.errorKind,
      retry_delay_seconds: delay,
      resume_claim: null,
    });
  }

  private handleCrash(result: ProviderResult): JsonObject {
    const state = this.db().loadState();
    if (state.resume_claim) {
      return this.db().transition(State.BLOCKED, "quota_resume_crashed", "quota recovery provider turn crashed after its single-flight lease; manual review required", {
        last_error: result.errorDetail,
        error_kind: ErrorKind.CRASH,
        resume_claim: null,
      });
    }
    const attempt = num(state.crash_attempt) + 1;
    if (!state.thread_id || attempt > MAX_CRASH_RETRIES) {
      return this.db().transition(State.FAILED, "codex_crash_unrecoverable", "Codex crashed without a safe bounded recovery", {
        last_error: result.errorDetail,
        error_kind: ErrorKind.CRASH,
        crash_attempt: attempt,
        resume_claim: null,
      });
    }
    const delay = CRASH_BACKOFF_SECONDS[Math.min(attempt - 1, CRASH_BACKOFF_SECONDS.length - 1)];
    return this.db().transition(State.RETRY_BACKOFF, "codex_crash", `exact-thread crash recovery ${attempt}/${MAX_CRASH_RETRIES}`, {
      retry_attempt: 0,
      crash_attempt: attempt,
      last_error: result.errorDetail,
      error_kind: ErrorKind.CRASH,
      next_resume_at: isoUnix(Date.now() / 1000 + delay),
      retry_delay_seconds: delay,
      resume_claim: null,
    });
  }

  private async sleepBackoff(state: JsonObject): Promise<void> {
    const target = parseTimestamp(state.next_resume_at);
    if (target) await this.sleepUntil(unixOf(target), 0.25);
  }

  private afterSuccess(): JsonObject {
    if (this.db().loadState().resume_claim) {
      crashHook("BEFORE_CLAIM_CLEAR");
      this.db().mutate("resume_completed", "exact-thread provider result consumed by Nightwatch", (item) => ({ ...item, resume_claim: null }));
    }
    this.checkGitRecovery();
    const adopted = adoptPlan(this.store);
    if (!this.db().loadState().plan_ready && !adopted) return this.failClosed("Codex completed without a valid durable milestone plan", ErrorKind.STATE);
    ingestPlanProgress(this.store);
    this.db().transition(State.VERIFYING, "verification_started", "Codex turn ended; Nightwatch is running milestone and final checks");
    return this.finishVerification();
  }

  private finishVerification(): JsonObject {
    const state = this.db().loadState();
    let git: GitSnapshot;
    let result: JsonObject;
    try {
      git = this.checkGitRecovery();
      result = verifyPlan(this.store, git);
    } catch (error) {
      if (error instanceof GitError || error instanceof StateIntegrityError) return this.failClosed(`verification precondition failed: ${error.name}`, ErrorKind.GIT);
      throw error;
    }
    const attempts = num(state.verification_attempts) + 1;
    const gitResult = isRecord(result.git) ? result.git : {};
    const allOk = Boolean(result.all_milestones_verified && result.all_final_checks_passed && state.plan_ready && state.acceptance_ready && !gitResult.conflicts);
    this.db().mutate("verification_completed", "mechanical milestone/final verification completed", (item) => ({
      ...item,
      verification_attempts: attempts,
      last_verification: result,
      last_verified_commit: allOk ? (gitResult.head ?? null) : item.last_verified_commit,
      final_verification_passed: allOk,
      last_git_head: gitResult.head ?? null,
    }));
    if (allOk) return this.writeDoneReport(result);
    if (!state.acceptance_ready) {
      return this.db().transition(State.AWAITING_ACCEPTANCE, "awaiting_trusted_acceptance", "implementation may be complete but no frozen user verification policy can authorize DONE", {
        last_error: "no trusted --verify policy was supplied",
      });
    }
    if (attempts >= MAX_CRASH_RETRIES) {
      return this.db().transition(State.BLOCKED, "verification_failed", "verification did not pass within the bounded correction budget", {
        last_error: "one or more milestone/final checks failed",
        error_kind: ErrorKind.BLOCKER,
      });
    }
    return this.db().transition(State.RUNNING, "verification_failed_continue", "verification failed; exact thread must correct the repository");
  }

  private checkGitRecovery(): GitSnapshot {
    const state = this.db().loadState();
    const git = snapshot(pathText(this.db().repo));
    if (git.conflicts) {
      this.failClosed("Git contains unresolved conflicts", ErrorKind.GIT);
      throw new GitError("Git contains unresolved conflicts");
    }
    const previous = state.last_verified_commit;
    if (typeof previous === "string" && previous && git.head && git.head !== previous && !isAncestor(previous, git.head, pathText(this.db().repo))) {
      this.failClosed("repository HEAD is not a descendant of the last verified commit", ErrorKind.GIT);
      throw new GitError("repository HEAD is not a descendant of the last verified commit");
    }
    this.db().mutate("git_observed", "Git reality captured for recovery", (item) => ({ ...item, last_git_head: git.head }));
    return git;
  }

  private failClosed(reason: string, kind: ErrorKind): JsonObject {
    const current = this.db().loadState();
    const blocked = kind === ErrorKind.STATE || kind === ErrorKind.GIT || kind === ErrorKind.MALFORMED || kind === ErrorKind.BLOCKER;
    const target = blocked ? State.BLOCKED : State.FAILED;
    return this.db().transition(target, "fail_closed", reason, {
      last_error: reason,
      error_kind: kind,
      blocker: target === State.BLOCKED ? reason : (current.blocker ?? null),
    });
  }

  private countPoolRecoveryFailure(detail: string): void {
    this.db().mutate("account_pool_recovery_failure", detail, (item) => ({ ...item, recovery_failures: num(item.recovery_failures) + 1 }));
  }

  private writeDoneReport(verification: JsonObject): JsonObject {
    crashHook("BEFORE_DONE_WRITE");
    const git = isRecord(verification.git) ? verification.git : {};
    this.db().transition(State.DONE, "done_guard_passed", "all required milestones and final verification passed", {
      final_verification_passed: true,
      last_verified_commit: git.head ?? null,
    });
    const state = this.db().loadState();
    this.db().writeReport(buildReport(this.store, state, verification));
    this.db().appendEvent("final_report_written", "final report generated after DONE guard");
    return this.db().loadState();
  }
}

function trustedHomePath(store: NightwatchStore): NightPath {
  const home = store.trustedCodexHome;
  if (home instanceof NightPath) return home;
  if (home && typeof home === "object" && "path" in home && home.path instanceof NightPath) return home.path;
  return new NightPath(String(home));
}

export function buildReport(store: NightwatchStore, state: JsonObject, verification?: JsonObject | null): string {
  const db = asStore(store);
  const plan = db.loadPlan();
  const events = db.loadEvents();
  const reportVerification = verification || (isRecord(state.last_verification) ? state.last_verification : {});
  const progress = isRecord(reportVerification.progress) ? reportVerification.progress : {};
  const quota = isRecord(state.quota) ? state.quota : {};
  const quotaLines: string[] = [];
  for (const [label, key] of [["5h", "primary"], ["weekly", "secondary"]] as const) {
    const window = quota[key];
    if (isRecord(window)) quotaLines.push(`- ${label}: ${py(window.used_percent)}% used; duration=${py(window.window_duration_mins)}m; reset=${py(window.resets_at)}`);
  }
  if (!quotaLines.length) quotaLines.push("- (no validated snapshot persisted for this run)");
  const provider = String(state.provider ?? "codex");
  const milestones = Array.isArray(plan.milestones) ? (plan.milestones as JsonObject[]) : [];
  const reportLines = [
    "# Nightwatch report",
    "",
    "## GOAL",
    String(state.goal),
    "",
    `- RESULT: ${String(state.state)}`,
    `- RUN_ID: ${py(get(state, "run_id"))}`,
    `- RUNTIME: ${py(state.created_at)} → ${py(state.updated_at)}`,
    `- REPOSITORY: ${py(get(state, "repo"))}`,
    `- PROVIDER: ${provider}`,
    `- THREAD_ID: ${state.thread_id ? String(state.thread_id) : "(none)"}`,
    `- THREAD_MODE: ${state.thread_handoff ? "CONTROLLED HANDOFF" : "EXACT"}`,
    "- RUN_STORE: persistent",
    `- AUTH_LEASE: ${state.account_lease ? "leased / active" : "inactive / scrubbed"}`,
    `- GENERATION: ${py(get(state, "generation"))}`,
    `- MODEL: ${state.model ? String(state.model) : `(${capitalize(provider)} default)`}`,
    `- REASONING: ${state.reasoning_effort ? String(state.reasoning_effort) : `(${capitalize(provider)} default)`}`,
  ];
  if (provider === "agy") reportLines.push(`- PRINT TIMEOUT: ${state.agy_print_timeout ? String(state.agy_print_timeout) : "60m"}`);
  reportLines.push(
    `- QUOTA SOURCE: ${state.quota_source ? String(state.quota_source) : "(none)"}`,
    `- RECOVERIES: ${py(get(state, "recoveries", 0))}`,
    `- QUOTA CYCLES: ${py(get(state, "quota_cycles", 0))}`,
    `- RECOVERY FAILURES: ${py(get(state, "recovery_failures", 0))}`,
    `- FINAL HEAD: ${state.last_verified_commit ? String(state.last_verified_commit) : state.last_git_head ? String(state.last_git_head) : "(unknown)"}`,
    "",
  );
  const finalChecks = Array.isArray(reportVerification.final_checks) ? (reportVerification.final_checks as JsonObject[]) : [];
  const total = num(get(progress, "total_count", milestones.length));
  return [
    ...reportLines,
    "## QUOTA WINDOWS",
    ...quotaLines,
    "",
    "## MILESTONES",
    `- implemented: ${py(get(progress, "implemented_count", 0))} / ${py(total)}`,
    `- verified: ${py(get(progress, "verified_count", 0))} / ${py(total)}`,
    ...milestones.map((item) => `- [${py(get(item, "status"))}] ${py(get(item, "id"))}: ${py(get(item, "title"))} (weight=${py(get(item, "weight"))})`),
    "",
    "## VERIFICATION",
    ...finalChecks.map((check) => `- ${py(get(check, "command"))}: ${check.ok ? "PASS" : "FAIL"}`),
    "",
    "## ERRORS/BLOCKERS",
    `- ${state.last_error ? String(state.last_error) : state.blocker ? String(state.blocker) : "(none)"}`,
    "",
    "## COMMITS",
    `- last verified commit: ${state.last_verified_commit ? String(state.last_verified_commit) : "(none)"}`,
    "",
    "## TRUSTED TIMELINE",
    ...events.slice(-100).map((item) => `- #${py(get(item, "seq"))} ${py(get(item, "ts"))} ${py(get(item, "state"))} ${py(get(item, "event"))}: ${py(get(item, "reason"))}`),
    "",
    "## TRUST SEMANTICS",
    "- State, thread identity, milestones, quota snapshots, and checks above come from the Nightwatch trusted control plane.",
    "- Model-authored narrative and model-authored verification authority are intentionally excluded.",
    "",
  ].join("\n");
}

/** Non-invasive observer and automatic takeover supervisor for active Codex sessions. */
export class PassiveWatcher {
  readonly store: NightwatchStore;
  readonly codexHome: string;
  readonly explicitThread: string | null;
  private stopRequested = false;

  constructor(store: NightwatchStore, codexHome: string | null = null, explicitThread: string | null = null) {
    this.store = store;
    this.codexHome = codexHome || process.env.CODEX_HOME || path.join(NightPath.home().raw, ".codex");
    this.explicitThread = explicitThread;
  }

  requestStop(): void {
    this.stopRequested = true;
  }

  discoverActiveSession(): JsonObject {
    const proven = findProvenCodexSessions(pathText(asStore(this.store).repo), this.codexHome, this.explicitThread);
    if (!proven.length) {
      const candidateProcs = findRepoCodexProcesses(pathText(asStore(this.store).repo), process.pid);
      return {
        status: "NO_SESSION",
        active: false,
        pid: candidateProcs[0]?.pid ?? null,
        processes: candidateProcs,
        thread_id: this.explicitThread,
        rollout_path: null,
      };
    }
    if (proven.length > 1) {
      return {
        status: "AMBIGUOUS_ACTIVE_SESSIONS",
        active: false,
        sessions: proven,
        error: "multiple active Codex sessions found for repository; specify --thread <ID> to select one",
      };
    }
    const session = proven[0];
    return {
      status: "OK",
      active: true,
      pid: session.pid,
      pid_identity: session.pid_identity,
      pid_alive: true,
      thread_id: session.thread_id,
      rollout_path: session.rollout_path,
      model: session.model,
      branch: session.branch,
      title: session.title,
      session,
    };
  }

  inspectLiveSnapshot(): JsonObject {
    const info = this.discoverActiveSession();
    if (info.status === "AMBIGUOUS_ACTIVE_SESSIONS") {
      return { status: "AMBIGUOUS_ACTIVE_SESSIONS", active: false, sessions: info.sessions ?? [], error: info.error };
    }
    if (info.status !== "OK" || !info.rollout_path) {
      return { status: info.status || "NO_SESSION", active: false, pid: info.pid ?? null, processes: info.processes ?? [], thread_id: info.thread_id ?? null };
    }
    const parsed = readRolloutSnapshot(String(info.rollout_path));
    const pidIdent = isRecord(info.pid_identity) ? info.pid_identity : null;
    const alive = pidIdent ? processIdentityMatches(pidIdent) : pidAlive(typeof info.pid === "number" ? info.pid : null);
    return {
      status: "OK",
      active: true,
      pid: info.pid ?? null,
      pid_identity: pidIdent,
      pid_alive: alive,
      thread_id: info.thread_id ?? null,
      model: info.model ?? null,
      branch: info.branch ?? null,
      title: info.title ?? null,
      rate_limits: parsed.rateLimits,
      tokens: parsed.tokens,
      subagents: parsed.subagents,
      last_turn_type: parsed.lastTurnType,
      rollout_path: info.rollout_path,
    };
  }

  watch(
    onUpdate: ((snap: JsonObject) => void) | null = null,
    pollInterval = 2,
    autoTakeover = false,
    goal: string | null = null,
    verifyCommands: string[] | null = null,
    model: string | null = null,
    reasoningEffort: string | null = null,
  ): Promise<JsonObject> {
    const initSnap = this.inspectLiveSnapshot();
    if (initSnap.status === "AMBIGUOUS_ACTIVE_SESSIONS") {
      onUpdate?.(initSnap);
      if (autoTakeover) throw new SystemExit("nightwatch: auto-takeover refused: multiple active sessions found for repository; specify --thread <ID>");
      return Promise.resolve(initSnap);
    }
    if (!initSnap.active || !initSnap.rollout_path) {
      onUpdate?.(initSnap);
      return Promise.resolve(initSnap);
    }
    return this.watchLoop(initSnap, onUpdate, pollInterval, autoTakeover, goal, verifyCommands, model, reasoningEffort);
  }

  private async watchLoop(
    initSnap: JsonObject,
    onUpdate: ((snap: JsonObject) => void) | null,
    pollInterval: number,
    autoTakeover: boolean,
    goal: string | null,
    verifyCommands: string[] | null,
    model: string | null,
    reasoningEffort: string | null,
  ): Promise<JsonObject> {
    const frozenThreadId = String(initSnap.thread_id);
    const frozenPidIdentity = isRecord(initSnap.pid_identity) ? initSnap.pid_identity : null;
    const frozenRolloutPath = String(initSnap.rollout_path);
    const frozenTitle = initSnap.title;
    let lastStatus = "";
    let takeoverPending = false;
    let snap: JsonObject = initSnap;
    while (!this.stopRequested) {
      const pidAliveNow = frozenPidIdentity ? processIdentityMatches(frozenPidIdentity) : pidAlive(typeof initSnap.pid === "number" ? initSnap.pid : null);
      const parsed = fs.existsSync(frozenRolloutPath) ? readRolloutSnapshot(frozenRolloutPath) : { rateLimits: null, tokens: null, subagents: [] as Array<[string, string]>, lastTurnType: null };
      const limits = isRecord(parsed.rateLimits) ? parsed.rateLimits : {};
      const primary = isRecord(limits.primary) ? limits.primary : {};
      const secondary = isRecord(limits.secondary) ? limits.secondary : {};
      const quotaHit = num(primary.used_percent) >= 100 || num(secondary.used_percent) >= 100;
      if (quotaHit && pidAliveNow) takeoverPending = true;
      snap = {
        status: takeoverPending && pidAliveNow ? "TAKEOVER_PENDING" : "OK",
        active: pidAliveNow,
        pid: initSnap.pid ?? null,
        pid_identity: frozenPidIdentity,
        pid_alive: pidAliveNow,
        thread_id: frozenThreadId,
        model: initSnap.model ?? null,
        branch: initSnap.branch ?? null,
        title: frozenTitle ?? null,
        rate_limits: parsed.rateLimits,
        tokens: parsed.tokens,
        subagents: parsed.subagents,
        last_turn_type: parsed.lastTurnType,
        rollout_path: frozenRolloutPath,
        takeover_pending: takeoverPending && pidAliveNow,
      };
      const statusStr = `${String(snap.status)}:${String(pidAliveNow)}:${JSON.stringify(parsed.rateLimits)}:${JSON.stringify(parsed.tokens)}`;
      if (statusStr !== lastStatus) {
        lastStatus = statusStr;
        onUpdate?.(snap);
      }
      if (!pidAliveNow) {
        if (autoTakeover) {
          let competing = findRepoCodexProcesses(pathText(asStore(this.store).repo), process.pid);
          if (frozenPidIdentity && typeof frozenPidIdentity.pid === "number") competing = competing.filter((item) => item.pid !== frozenPidIdentity.pid);
          if (competing.length) {
            const pids = competing.map((item) => String(item.pid)).join(", ");
            throw new SystemExit(`nightwatch: auto-takeover aborted: another competing Codex process (PID ${pids}) is active in repository`);
          }
          const db = asStore(this.store);
          if (!db.exists()) {
            db.initialize(
              makeRunId(pathText(db.repo)),
              goal || (typeof frozenTitle === "string" ? frozenTitle : "") || "Supervised session continuation",
              pathText(db.repo),
              null,
              verifyCommands ?? [],
              frozenThreadId,
              model,
              reasoningEffort,
            );
          }
          try {
            installUserFiles(pathText(db.repo));
            const name = serviceName(pathText(db.repo));
            startUserService(name);
            return { status: "TAKEOVER_HANDOFF_COMPLETE", service: name, thread_id: frozenThreadId, repo: pathText(db.repo), run_id: db.loadState().run_id };
          } catch (error) {
            if (error instanceof Error && (error.name === "RuntimeError" || error instanceof SystemExit || error instanceof StateIntegrityError || isErrno(error))) {
              return {
                status: "TAKEOVER_SERVICE_START_FAILED",
                error: error.message,
                service: serviceName(pathText(db.repo)),
                thread_id: frozenThreadId,
                repo: pathText(db.repo),
                run_id: db.loadState().run_id,
              };
            }
            throw error;
          }
        }
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, pollInterval * 1000));
    }
    return snap;
  }
}

function isErrno(error: Error): boolean {
  const code = (error as NodeJS.ErrnoException).code;
  return typeof code === "string" && /^E[A-Z0-9]+$/.test(code);
}

function readLines(filePath: string, onLine: (line: string) => boolean): void {
  const fd = fs.openSync(filePath, "r");
  const decoder = new StringDecoder("utf8");
  const buffer = Buffer.alloc(64 * 1024);
  let leftover = "";
  try {
    while (true) {
      const count = fs.readSync(fd, buffer, 0, buffer.length, null);
      if (count === 0) {
        leftover += decoder.end();
        if (leftover) onLine(leftover);
        return;
      }
      leftover += decoder.write(buffer.subarray(0, count));
      const parts = leftover.split("\n");
      leftover = parts.pop() ?? "";
      for (const line of parts) if (onLine(line)) return;
    }
  } finally {
    fs.closeSync(fd);
  }
}

function subagentPairs(line: string): Array<[string, string]> {
  return [...line.matchAll(/-\s*([0-9a-fA-F-]{36}):\s*(\w+)/g)].map((match) => [match[1], match[2]]);
}

function readRolloutSnapshot(filePath: string): { rateLimits: unknown; tokens: unknown; subagents: Array<[string, string]>; lastTurnType: unknown } {
  let rateLimits: unknown = null;
  let tokens: unknown = null;
  const subagents: Array<[string, string]> = [];
  let lastTurnType: unknown = null;
  if (!fs.existsSync(filePath)) return { rateLimits, tokens, subagents, lastTurnType };
  const text = fs.readFileSync(filePath).toString("utf8");
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      const data = JSON.parse(line) as JsonObject;
      const payload = isRecord(data.payload) ? data.payload : null;
      if (payload) {
        if ("rate_limits" in payload) rateLimits = payload.rate_limits;
        if (isRecord(payload.info) && "total_token_usage" in payload.info) tokens = payload.info.total_token_usage;
      }
      if (line.includes("<subagents>")) {
        for (const pair of subagentPairs(line)) {
          if (!subagents.some((item) => item[0] === pair[0] && item[1] === pair[1])) subagents.push(pair);
        }
      }
      lastTurnType = data.type ?? null;
    } catch {
      /* malformed rollout lines are ignored */
    }
  }
  return { rateLimits, tokens, subagents, lastTurnType };
}
