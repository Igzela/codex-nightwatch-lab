import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";

import { ManagedProcess, buildCommand as codexBuildCommand, parseIso, posixGetpgid, runCodex, type StopFlag } from "./codex.js";
import {
  ErrorKind,
  QuotaSnapshot,
  QuotaWindow,
  parseAgyDurationSeconds,
  validateAgyPrintTimeout,
  validateModelName,
  validateReasoningEffort,
  type JsonObject,
  type ProviderResult,
} from "./models.js";
import { NightPath } from "./path.js";
import { getpgrp } from "./posix.js";
import { linuxProcessIdentity, pidAlive, processGroupAlive } from "./process-identity.js";
import { makeQuotaProvider } from "./quota.js";
import { NightwatchStore, redact } from "./storage.js";

export interface ProviderRunOptions {
  codexHome?: string | NightPath | null;
  leaseFd?: number | null;
  accountFingerprint?: string | null;
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function repoPath(repo: string | NightPath): string {
  return typeof repo === "string" ? repo : repo.raw;
}

function pyRepr(value: unknown): string {
  if (typeof value === "string") return `'${value.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
  if (value === null || value === undefined) return "None";
  return String(value);
}

function osErrorName(error: unknown): string {
  const code = (error as NodeJS.ErrnoException)?.code;
  if (code === "ENOENT") return "FileNotFoundError";
  if (code === "EACCES" || code === "EPERM") return "PermissionError";
  if (code === "ETIMEDOUT") return "TimeoutExpired";
  return code || (error as Error)?.name || "OSError";
}

function which(command: string): string | null {
  if (command.includes("/")) return fs.existsSync(command) && fs.statSync(command).isFile() ? path.resolve(command) : null;
  for (const directory of (process.env.PATH ?? "").split(":")) {
    if (!directory) continue;
    const candidate = path.join(directory, command);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      continue;
    }
  }
  return null;
}

function signalGroup(pgid: number, signal: NodeJS.Signals): void {
  try {
    process.kill(-pgid, signal);
  } catch {
    /* The group can disappear between the liveness check and the signal. */
  }
}

function isSafePgid(pgid: number, proc: ManagedProcess | null = null): boolean {
  if (!Number.isInteger(pgid) || pgid <= 1) return false;
  try {
    if (pgid === getpgrp()) return false;
  } catch {
    return false;
  }
  if (proc != null && pgid !== proc.pid) return false;
  return true;
}

async function pause(proc: ManagedProcess, graceSeconds: number, done: () => boolean): Promise<boolean> {
  const started = performance.now();
  while (performance.now() - started < graceSeconds * 1000) {
    proc.poll();
    if (done()) return true;
    const remaining = graceSeconds * 1000 - (performance.now() - started);
    await proc.drain(Math.min(20, Math.max(0, remaining)));
  }
  proc.poll();
  return done();
}

async function reapLeader(proc: ManagedProcess, timeout = 2): Promise<number> {
  const polled = proc.poll();
  if (polled !== null) return polled;
  try {
    return await proc.wait(timeout);
  } catch {
    try {
      proc.kill("SIGKILL");
      return await proc.wait(timeout);
    } catch {
      return proc.poll() || -9;
    }
  }
}

async function signalLeader(proc: ManagedProcess, graceSeconds: number): Promise<void> {
  let supervisor: number | null = null;
  try {
    supervisor = getpgrp();
  } catch {
    supervisor = null;
  }
  if (!Number.isInteger(proc.pid) || proc.pid <= 1 || (supervisor != null && proc.pid === supervisor)) return;
  proc.kill("SIGINT");
  if (await pause(proc, graceSeconds, () => proc.poll() !== null)) return;
  proc.kill("SIGTERM");
  if (await pause(proc, graceSeconds, () => proc.poll() !== null)) return;
  proc.kill("SIGKILL");
}

/**
 * Escalate SIGINT, then SIGTERM, then SIGKILL.
 * Group liveness drives escalation. Leader exit does not.
 */
export async function abortProcessGroup(pgid: number, proc: ManagedProcess, graceSeconds = 0.5): Promise<number> {
  if (!isSafePgid(pgid, proc)) {
    await signalLeader(proc, graceSeconds);
    return reapLeader(proc);
  }
  if (!processGroupAlive(pgid)) {
    if (proc.poll() == null) await signalLeader(proc, graceSeconds);
    return reapLeader(proc);
  }
  signalGroup(pgid, "SIGINT");
  if (await pause(proc, graceSeconds, () => !processGroupAlive(pgid))) return reapLeader(proc);
  signalGroup(pgid, "SIGTERM");
  if (await pause(proc, graceSeconds, () => !processGroupAlive(pgid))) return reapLeader(proc);
  signalGroup(pgid, "SIGKILL");
  await pause(proc, graceSeconds, () => !processGroupAlive(pgid));
  return reapLeader(proc);
}

export async function abortProcess(proc: ManagedProcess, graceSeconds = 0.5): Promise<number> {
  let pgid = proc.pid;
  try {
    pgid = posixGetpgid(proc.pid);
  } catch {
    pgid = proc.pid;
  }
  return abortProcessGroup(pgid, proc, graceSeconds);
}

export abstract class ProviderAdapter {
  abstract get name(): string;

  abstract resolveBinary(): string;

  abstract buildCommand(
    repo: string | NightPath,
    threadId: string | null,
    prompt: string,
    model?: string | null,
    reasoningEffort?: string | null,
    printTimeout?: string | null,
  ): [string[], string];

  abstract runTurn(
    store: NightwatchStore,
    generation: number,
    prompt: string,
    threadId?: string | null,
    onSpawn?: ((pid: number, action: string) => void) | null,
    onThread?: ((threadId: string) => void) | null,
    stopEvent?: StopFlag | null,
    timeout?: number | null,
    options?: ProviderRunOptions,
  ): ProviderResult | Promise<ProviderResult>;

  abstract authSanity(binary?: string | null): boolean;
  abstract probeQuota(store?: NightwatchStore | null, repo?: NightPath | null, model?: string | null): QuotaSnapshot;
  abstract listModels(): JsonObject[];
  abstract defaultModel(): string;
  abstract validateModel(model: string): string;
  abstract validateReasoningEffort(effort: string): string;
  abstract supportsAutoPool(): boolean;
  abstract supportsLiveSteering(): boolean;
  abstract steer(repo: string | NightPath, threadId: string, instruction: string): [boolean, string];
  abstract findActiveProcesses(repo: string | NightPath, excludePid?: number | null): JsonObject[];
  abstract findActiveThreadsForRepo(repo: string | NightPath): JsonObject[];
  abstract doctorCheck(repo?: NightPath | null): JsonObject;
}

function providerResult(overrides: Partial<ProviderResult>): ProviderResult {
  return {
    exitCode: null,
    signal: null,
    threadId: null,
    eventCount: 0,
    malformedCount: 0,
    eventTypes: [],
    errorKind: null,
    errorDetail: null,
    resetAt: null,
    resetSource: null,
    quotaWindows: [],
    blocker: null,
    aborted: false,
    runLog: null,
    ...overrides,
  };
}

export class CodexProviderAdapter extends ProviderAdapter {
  get name(): string {
    return "codex";
  }

  resolveBinary(): string {
    return process.env.NIGHTWATCH_CODEX_BIN || "codex";
  }

  buildCommand(repo: string | NightPath, threadId: string | null, prompt: string, model: string | null = null, reasoningEffort: string | null = null): [string[], string] {
    return codexBuildCommand(repo, threadId, prompt, model, reasoningEffort);
  }

  runTurn(
    store: NightwatchStore,
    generation: number,
    prompt: string,
    threadId: string | null = null,
    onSpawn: ((pid: number, action: string) => void) | null = null,
    onThread: ((threadId: string) => void) | null = null,
    stopEvent: StopFlag | null = null,
    timeout: number | null = null,
    options: ProviderRunOptions = {},
  ): Promise<ProviderResult> {
    return runCodex(store, generation, prompt, threadId, onSpawn, onThread, stopEvent, timeout, options.codexHome, options.leaseFd, options.accountFingerprint);
  }

  authSanity(binary?: string | null): boolean {
    if (process.env.NIGHTWATCH_SKIP_AUTH_CHECK === "1") return true;
    const binPath = binary || process.env.NIGHTWATCH_CODEX_BIN || "codex";
    const result = spawnSync(binPath, ["login", "status"], { stdio: "ignore", timeout: 8000, windowsHide: true });
    return !result.error && result.status === 0;
  }

  probeQuota(_store?: NightwatchStore | null, _repo?: NightPath | null, _model?: string | null): QuotaSnapshot {
    return makeQuotaProvider().read();
  }

  listModels(): JsonObject[] {
    return listCodexModels();
  }

  defaultModel(): string {
    return "gpt-5.6-sol";
  }

  validateModel(model: string): string {
    return validateModelName(model);
  }

  validateReasoningEffort(effort: string): string {
    return validateReasoningEffort(effort);
  }

  supportsAutoPool(): boolean {
    return true;
  }

  supportsLiveSteering(): boolean {
    return true;
  }

  steer(_repo: string | NightPath, threadId: string, instruction: string): [boolean, string] {
    const binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
    const result = spawnSync(binary, ["queue", "--thread", threadId, "--message", instruction], {
      cwd: repoPath(_repo),
      encoding: "utf8",
      timeout: 15000,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    if (result.error) return [false, "Codex queue transport was unavailable; no instruction was recorded as delivered."];
    if (result.status !== 0) return [false, "Codex did not accept the queued instruction; inspect the exact thread state."];
    return [true, `Instruction queued to exact thread ${threadId}.`];
  }

  findActiveProcesses(repo: string | NightPath, excludePid: number | null = null): JsonObject[] {
    if (process.platform !== "linux") return [];
    const target = new NightPath(repoPath(repo)).resolve().raw;
    if (!fs.existsSync("/proc")) return [];
    const found: JsonObject[] = [];
    for (const name of fs.readdirSync("/proc")) {
      if (!/^\d+$/.test(name)) continue;
      const pid = Number(name);
      if (excludePid && pid === excludePid) continue;
      if (!pidAlive(pid)) continue;
      try {
        const cwd = fs.realpathSync(fs.readlinkSync(`/proc/${pid}/cwd`));
        if (cwd !== target) continue;
        const identity = linuxProcessIdentity(pid);
        const executable = identity?.executable ?? fs.readlinkSync(`/proc/${pid}/exe`);
        const cmdline = fs.readFileSync(`/proc/${pid}/cmdline`).toString("utf8").replaceAll("\u0000", " ").toLowerCase();
        const exeName = executable.toLowerCase();
        const isCodex = (exeName.includes("codex") || cmdline.includes("codex")) && !exeName.includes("nightwatch") && !cmdline.includes("nightwatch") && !cmdline.includes("pytest") && !cmdline.includes("unittest");
        if (isCodex) found.push({ pid, executable, cmdline, cwd });
      } catch {
        continue;
      }
    }
    return found;
  }

  findActiveThreadsForRepo(repo: string | NightPath): JsonObject[] {
    const target = new NightPath(repoPath(repo)).resolve().raw;
    const home = process.env.CODEX_HOME || NightPath.home().join(".codex").raw;
    const dbPath = new NightPath(home).expanduser().join("state_5.sqlite").raw;
    if (!fs.existsSync(dbPath)) return [];
    try {
      const db = new DatabaseSync(dbPath, { readOnly: true });
      try {
        const rows = db.prepare(
          "SELECT id, rollout_path, updated_at_ms, model, git_branch, thread_source, first_user_message, title FROM threads WHERE cwd = ? ORDER BY updated_at_ms DESC LIMIT 10",
        ).all(target) as Array<Record<string, unknown>>;
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
      } finally {
        db.close();
      }
    } catch {
      return [];
    }
  }

  doctorCheck(_repo?: NightPath | null): JsonObject {
    const binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
    const binPath = which(binary) || (fs.existsSync(binary) && fs.statSync(binary).isFile() ? binary : null);
    let version: string | null = null;
    if (binPath) {
      const result = spawnSync(binPath, ["--version"], { encoding: "utf8", timeout: 5000, stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
      version = !result.error && result.status === 0 ? result.stdout.trim() : null;
    }
    const authOk = binPath ? this.authSanity(binPath) : false;
    return { binary: binPath, version, auth_ok: authOk, status: binPath && authOk ? "ok" : "fail" };
  }
}

function listCodexModels(): JsonObject[] {
  const binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
  const result = spawnSync(binary, ["debug", "models"], { encoding: "utf8", timeout: 15000, stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
  if (result.error) throw new Error("installed Codex model catalog is unavailable", { cause: result.error });
  if (result.status !== 0) throw new Error("installed Codex model catalog command failed");
  let rawModels: unknown;
  try {
    rawModels = (JSON.parse(result.stdout) as JsonObject).models;
  } catch (error) {
    throw new Error("installed Codex returned an invalid model catalog", { cause: error });
  }
  if (!Array.isArray(rawModels)) throw new Error("installed Codex returned an invalid model catalog");
  const catalog: JsonObject[] = [];
  for (const item of rawModels) {
    if (!isRecord(item) || (item.visibility != null && item.visibility !== "list")) continue;
    let slug: string;
    try {
      if (typeof item.slug !== "string") continue;
      slug = validateModelName(item.slug);
    } catch {
      continue;
    }
    let fallback: string | null = null;
    if (item.default_reasoning_level != null) {
      try {
        fallback = typeof item.default_reasoning_level === "string" ? validateReasoningEffort(item.default_reasoning_level) : null;
      } catch {
        fallback = null;
      }
    }
    const levels: string[] = [];
    for (const level of Array.isArray(item.supported_reasoning_levels) ? item.supported_reasoning_levels : []) {
      const candidate = isRecord(level) ? level.effort : null;
      try {
        if (typeof candidate !== "string") continue;
        const valid = validateReasoningEffort(candidate);
        if (!levels.includes(valid)) levels.push(valid);
      } catch {
        continue;
      }
    }
    const display = typeof item.display_name === "string" ? item.display_name : slug;
    const displayName = display.split(/\s+/).filter(Boolean).join(" ").slice(0, 80) || slug;
    catalog.push({ slug, display_name: displayName, default_reasoning_level: fallback, supported_reasoning_levels: levels });
  }
  if (!catalog.length) throw new Error("installed Codex model catalog contains no visible models");
  return catalog;
}

export function agyModelFamily(model: string): string {
  if (typeof model !== "string" || !model.trim()) throw new Error(`unknown AGY model family for model ${pyRepr(model)}`);
  const normalized = model.trim().toLowerCase();
  if (normalized.startsWith("gemini-")) return "gemini";
  if (normalized.startsWith("claude-") || normalized.startsWith("gpt-") || normalized.startsWith("gpt-oss-")) return "3p";
  throw new Error(`unknown AGY model family for model ${pyRepr(model)}`);
}

const DEFAULT_AGY_MODELS: JsonObject[] = [
  { slug: "gemini-3.8-flash-high", display_name: "Gemini 3.8 Flash (High)", default_reasoning_level: "high", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.8-flash-medium", display_name: "Gemini 3.8 Flash (Medium)", default_reasoning_level: "medium", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.8-flash-low", display_name: "Gemini 3.8 Flash (Low)", default_reasoning_level: "low", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.7-flash-high", display_name: "Gemini 3.7 Flash (High)", default_reasoning_level: "high", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.7-flash-medium", display_name: "Gemini 3.7 Flash (Medium)", default_reasoning_level: "medium", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.7-flash-low", display_name: "Gemini 3.7 Flash (Low)", default_reasoning_level: "low", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.6-flash-high", display_name: "Gemini 3.6 Flash (High)", default_reasoning_level: "high", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.6-flash-medium", display_name: "Gemini 3.6 Flash (Medium)", default_reasoning_level: "medium", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.6-flash-low", display_name: "Gemini 3.6 Flash (Low)", default_reasoning_level: "low", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.1-pro-high", display_name: "Gemini 3.1 Pro (High)", default_reasoning_level: "high", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gemini-3.1-pro-low", display_name: "Gemini 3.1 Pro (Low)", default_reasoning_level: "low", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "claude-sonnet-4-6", display_name: "Claude Sonnet 4.6 (Thinking)", default_reasoning_level: "high", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "claude-opus-4-6-thinking", display_name: "Claude Opus 4.6 (Thinking)", default_reasoning_level: "high", supported_reasoning_levels: ["low", "medium", "high"] },
  { slug: "gpt-oss-120b-medium", display_name: "GPT-OSS 120B (Medium)", default_reasoning_level: "medium", supported_reasoning_levels: ["low", "medium", "high"] },
];

function round1(value: number): number {
  return Math.round((value + Number.EPSILON) * 10) / 10;
}

function utcNowIso(): string {
  return new Date().toISOString();
}

export class AgyProviderAdapter extends ProviderAdapter {
  static readonly defaultAgyModels = DEFAULT_AGY_MODELS;

  get name(): string {
    return "agy";
  }

  resolveBinary(): string {
    const override = process.env.NIGHTWATCH_AGY_BIN;
    if (override) return override;
    return which("agy") || (NightPath.home().join(".local", "bin", "agy").isFile() ? NightPath.home().join(".local", "bin", "agy").raw : "agy");
  }

  buildCommand(
    _repo: string | NightPath,
    threadId: string | null,
    prompt: string,
    model: string | null = null,
    reasoningEffort: string | null = null,
    printTimeout: string | null = null,
  ): [string[], string] {
    // OWNER_APPROVED_UNATTENDED_AGY_PERMISSION_POLICY:
    // Supervised AGY turns use --dangerously-skip-permissions.
    const args = [this.resolveBinary(), "--dangerously-skip-permissions", "--output-format", "stream-json"];
    const timeoutValue = printTimeout || "60m";
    validateAgyPrintTimeout(timeoutValue);
    args.push("--print-timeout", timeoutValue);
    if (model) args.push("--model", this.validateModel(model));
    if (reasoningEffort) args.push("--effort", this.validateReasoningEffort(reasoningEffort));
    if (threadId) {
      args.push("--conversation", threadId);
      args.push("-p", prompt);
      return [args, "resume"];
    }
    args.push("-p", prompt);
    return [args, "start"];
  }

  async runTurn(
    store: NightwatchStore,
    generation: number,
    prompt: string,
    threadId: string | null = null,
    onSpawn: ((pid: number, action: string) => void) | null = null,
    onThread: ((threadId: string) => void) | null = null,
    stopEvent: StopFlag | null = null,
    timeout: number | null = null,
  ): Promise<ProviderResult> {
    const state = store.loadState();
    const timeoutString = typeof state.agy_print_timeout === "string" && state.agy_print_timeout ? state.agy_print_timeout : "60m";
    const [args, action] = this.buildCommand(
      repoOfStore(store),
      threadId,
      prompt,
      typeof state.model === "string" ? state.model : null,
      typeof state.reasoning_effort === "string" ? state.reasoning_effort : null,
      timeoutString,
    );
    const budgetSeconds = parseAgyDurationSeconds(timeoutString);
    const watchdogLimit = timeout != null ? timeout : budgetSeconds + 30;
    const runs = asNightPath(store.runsPath as NightPath | string);
    runs.mkdir({ parents: true, existOk: true, mode: 0o700 });
    const runLog = runs.join(`generation-${generation}.stderr.log`);
    store.writeRunEvent(generation, { type: "provider_command", provider: "agy", action, argv: args.filter((item) => item !== prompt) });
    const started = performance.now();
    const environment: Record<string, string> = {};
    for (const [key, value] of Object.entries(process.env)) if (typeof value === "string") environment[key] = value;
    let proc: ManagedProcess | undefined;
    let pgid = 0;
    try {
      proc = ManagedProcess.start({ argv: args, cwd: repoOfStore(store), env: environment, detached: true, input: null });
      const leaderPid = proc.pid;
      try {
        pgid = posixGetpgid(leaderPid);
      } catch {
        pgid = leaderPid;
      }
      if (pgid !== leaderPid) {
        await abortProcessGroup(leaderPid, proc);
        throw new Error(`AGY process group ${pgid} does not match leader pid ${leaderPid}`);
      }
      if (pgid === getpgrp()) {
        await abortProcessGroup(leaderPid, proc);
        throw new Error("AGY process group matches supervisor process group");
      }
    } catch (error) {
      proc?.close();
      if (error instanceof Error && error.message.startsWith("AGY process group")) throw error;
      return providerResult({ threadId, errorKind: ErrorKind.UNKNOWN, errorDetail: `AGY spawn failed: ${osErrorName(error)}`, runLog: runLog.raw });
    }
    try {
      onSpawn?.(proc.pid, action);
      const events: JsonObject[] = [];
      const eventTypes: string[] = [];
      let malformed = 0;
      const seen = new Set<string>();
      let foundThread = threadId;
      let conversationNotFound = false;
      let mismatchDetected = false;
      let mismatchExpected: string | null = null;
      let mismatchObserved: string | null = null;
      let hasInit = false;
      let hasResultEvent = false;
      let lastResultEvent: JsonObject | null = null;
      let resultStatus: string | null = null;
      let timedOut = false;
      const noteNotFound = (line: string): void => {
        if (threadId && line.toLowerCase().includes(`conversation "${threadId.toLowerCase()}" not found`)) {
          conversationNotFound = true;
          mismatchDetected = true;
          mismatchExpected = threadId;
          mismatchObserved = "(conversation not found in stderr)";
        }
      };
      const failMismatch = async (): Promise<void> => {
        foundThread = "__MISMATCH__";
        store.writeRunEvent(generation, { type: "thread_id_mismatch", expected: mismatchExpected, observed: mismatchObserved });
        await abortProcessGroup(pgid, proc);
      };
      while (true) {
        if (stopEvent?.isSet()) {
          await abortProcessGroup(pgid, proc);
          break;
        }
        if (performance.now() - started > watchdogLimit * 1000) {
          timedOut = true;
          await abortProcessGroup(pgid, proc);
          break;
        }
        await proc.drain(0);
        for (const line of proc.stderrText().split(/\n/)) noteNotFound(line);
        if (mismatchDetected) {
          await failMismatch();
          break;
        }
        const line = await proc.readStdoutLine(100);
        if (stopEvent?.isSet()) {
          await abortProcessGroup(pgid, proc);
          break;
        }
        if (line == null) {
          await proc.drain(0);
          for (const stderrLine of proc.stderrText().split(/\n/)) noteNotFound(stderrLine);
          if (mismatchDetected) {
            await failMismatch();
            break;
          }
          continue;
        }
        if (line === "") break;
        const raw = line.trim();
        if (!raw) continue;
        let event: unknown;
        try {
          event = JSON.parse(raw);
        } catch {
          malformed += 1;
          store.writeRunEvent(generation, { type: "malformed_json", line_hash: sha16(raw) });
          continue;
        }
        if (!isRecord(event)) {
          malformed += 1;
          continue;
        }
        const digest = sha256(raw);
        if (seen.has(digest)) {
          store.writeRunEvent(generation, { type: "duplicate_event", line_hash: digest.slice(0, 16) });
          continue;
        }
        seen.add(digest);
        events.push(event);
        const eventType = String(event.event ?? "unknown");
        eventTypes.push(eventType);
        store.writeRunEvent(generation, { type: `agy_${eventType}`, event });
        if (eventType === "init") {
          hasInit = true;
          let conversation = typeof event.conversation_id === "string" ? event.conversation_id : "";
          if (!conversation && isRecord(event.init) && typeof event.init.conversation_id === "string") conversation = event.init.conversation_id;
          if (conversation.trim()) {
            const initId = conversation.trim();
            if (action === "start") {
              if (foundThread == null) {
                foundThread = initId;
                onThread?.(foundThread);
              } else if (foundThread !== initId) {
                mismatchDetected = true;
                mismatchExpected = foundThread;
                mismatchObserved = initId;
              }
            } else if (action === "resume" && threadId && initId !== threadId) {
              mismatchDetected = true;
              mismatchExpected = threadId;
              mismatchObserved = initId;
            }
          } else {
            store.writeRunEvent(generation, { type: "invalid_init_event", event });
            if (action === "start") {
              mismatchDetected = true;
              mismatchExpected = "(valid conversation ID in init)";
              mismatchObserved = "(none)";
            }
          }
        }
        const explicit = explicitConversation(event);
        if (explicit) {
          if (action === "resume" && threadId && explicit !== threadId) {
            mismatchDetected = true;
            mismatchExpected = threadId;
            mismatchObserved = explicit;
          } else if (action === "start" && foundThread && foundThread !== "__MISMATCH__" && explicit !== foundThread) {
            mismatchDetected = true;
            mismatchExpected = foundThread;
            mismatchObserved = explicit;
          }
        }
        if (mismatchDetected) {
          await failMismatch();
          break;
        }
        if (eventType === "result") {
          hasResultEvent = true;
          lastResultEvent = event;
          const payload = isRecord(event.result) ? event.result : null;
          const rawStatus = payload ? payload.status : undefined;
          resultStatus = rawStatus == null ? null : String(rawStatus).toUpperCase();
        }
      }
      let returnCode: number;
      try {
        returnCode = await proc.wait(5);
      } catch {
        returnCode = await abortProcessGroup(pgid, proc);
      }
      await proc.collectStderr(2000);
      const stderr = proc.stderrText();
      try {
        runLog.writeText(String(redact(stderr) ?? ""));
        runLog.chmod(0o600);
      } catch {
        /* logging must not hide the provider result */
      }
      const signalName = proc.signal;
      if (!conversationNotFound && threadId && stderr.toLowerCase().includes(`conversation "${threadId.toLowerCase()}" not found`)) {
        conversationNotFound = true;
        foundThread = "__MISMATCH__";
      }
      if (foundThread === "__MISMATCH__") {
        const detail = conversationNotFound ? `AGY conversation ${threadId} not found` : `AGY conversation mismatch: expected exact thread ${mismatchExpected || threadId}`;
        return providerResult({
          exitCode: returnCode,
          signal: signalName,
          threadId: null,
          eventCount: events.length,
          malformedCount: malformed,
          eventTypes,
          errorKind: ErrorKind.STATE,
          errorDetail: detail,
          runLog: runLog.raw,
        });
      }
      let kind: ErrorKind | null = null;
      let detail: string | null = null;
      let resetAt: number | null = null;
      let resetSource: string | null = null;
      let windows: QuotaWindow[] = [];
      const blocker: string | null = null;
      let aborted = Boolean(stopEvent?.isSet());
      const errorParts = [stderr];
      if (resultStatus === "ERROR" && lastResultEvent && isRecord(lastResultEvent.result)) {
        errorParts.push(String(lastResultEvent.result.error ?? ""));
        errorParts.push(String(lastResultEvent.result.response ?? ""));
      }
      for (const event of events) {
        if (event.type === "error") errorParts.push(String(event.error ?? event.error_message ?? ""));
      }
      const errorText = errorParts.join(" ");
      const lowerError = errorText.toLowerCase();
      const selectedModel = typeof state.model === "string" ? state.model : null;
      const isQuota = ["resource_exhausted", "quota exceeded", "rate limit", "usage limit"].some((token) => lowerError.includes(token)) || /\b429\b/.test(errorText);
      const isAuth = ["unauthorized", "authentication", "invalid token", "login required", "403 forbidden"].some((token) => lowerError.includes(token)) || /\b401\b/.test(errorText);
      const failedTurn = returnCode !== 0 || Boolean(signalName) || (action === "start" && !hasInit) || Boolean(malformed) || !hasResultEvent || resultStatus !== "SUCCESS";
      if (timedOut) {
        kind = ErrorKind.CRASH;
        detail = `AGY print-mode turn timed out after ${watchdogLimit.toFixed(1)}s watchdog`;
        store.writeRunEvent(generation, { type: "agy_watchdog_timeout", watchdog_limit: watchdogLimit, print_timeout: timeoutString });
      } else if (failedTurn && isQuota) {
        try {
          const snap = this.probeQuota(store, new NightPath(repoOfStore(store)), selectedModel);
          windows = snap.windows();
          for (const window of windows) {
            if (window.resetsAt && (resetAt == null || window.resetsAt > resetAt)) {
              resetAt = window.resetsAt;
              resetSource = "agy_usage_probe";
            }
          }
        } catch {
          /* quota probe failure stays fail-closed on the text classification */
        }
        const weekly = lowerError.includes("weekly") || windows.some((window) => window.name === "weekly" && window.exhausted);
        kind = weekly ? ErrorKind.QUOTA_WEEKLY : ErrorKind.QUOTA_5H;
        detail = "AGY quota limit reached";
      } else if (failedTurn && isAuth) {
        kind = ErrorKind.AUTH;
        detail = "AGY authentication failure";
      } else if (aborted) {
        kind = null;
        detail = detail || "AGY turn was stopped";
      } else if (action === "start" && !hasInit) {
        kind = ErrorKind.STATE;
        detail = "AGY start turn missing authoritative init event with conversation_id";
      } else if (malformed) {
        kind = ErrorKind.MALFORMED;
        detail = `AGY stdout contained ${malformed} malformed JSONL event(s)`;
      } else if (hasResultEvent) {
        if (resultStatus === "CANCELED" || resultStatus === "INTERRUPTED") {
          aborted = true;
          kind = null;
          detail = `AGY turn was ${resultStatus.toLowerCase()}`;
        } else if (resultStatus === "WAITING" || resultStatus === "RUNNING") {
          kind = ErrorKind.STATE;
          detail = `AGY emitted non-terminal result status: ${resultStatus}`;
        } else if (resultStatus === "ERROR") {
          kind = ErrorKind.CRASH;
          detail = `AGY result status ERROR (exit code ${returnCode || signalName})`;
        } else if (resultStatus === "SUCCESS") {
          if (returnCode !== 0 || signalName) {
            kind = ErrorKind.CRASH;
            detail = `AGY emitted SUCCESS but exited with ${returnCode || signalName}`;
          }
        } else {
          kind = ErrorKind.STATE;
          detail = `AGY emitted unknown result status: ${resultStatus}`;
        }
      } else if (returnCode !== 0 || signalName) {
        kind = ErrorKind.CRASH;
        detail = `AGY process terminated with ${returnCode || signalName} without result event`;
      } else {
        kind = ErrorKind.STATE;
        detail = "AGY exited 0 without emitting a terminal result event";
      }
      return providerResult({
        exitCode: returnCode,
        signal: signalName,
        threadId: foundThread,
        eventCount: events.length,
        malformedCount: malformed,
        eventTypes,
        errorKind: kind,
        errorDetail: detail,
        resetAt,
        resetSource,
        quotaWindows: windows,
        blocker,
        aborted,
        runLog: runLog.raw,
      });
    } finally {
      proc.close();
    }
  }

  authSanity(binary?: string | null): boolean {
    if (process.env.NIGHTWATCH_SKIP_AUTH_CHECK === "1") return true;
    const binPath = binary || this.resolveBinary();
    const result = spawnSync(binPath, ["--output-format", "stream-json", "-p", "/usage"], { encoding: "utf8", timeout: 30000, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    return !result.error && result.status === 0 && String(result.stdout ?? "").split(/\n/).some((line) => line.includes("command_result"));
  }

  probeQuota(store: NightwatchStore | null = null, _repo: NightPath | null = null, model: string | null = null): QuotaSnapshot {
    const binary = this.resolveBinary();
    const readAt = utcNowIso();
    let target = model;
    if (target == null && store?.exists()) {
      const selected = store.loadState().model;
      if (typeof selected === "string") target = selected;
    }
    if (target == null) target = "gemini-3.8-flash-high";
    let family: string;
    try {
      family = agyModelFamily(target);
    } catch (error) {
      return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, `authoritative quota unavailable: ${(error as Error).message}`);
    }
    const result = spawnSync(binary, ["--output-format", "stream-json", "-p", "/usage"], { encoding: "utf8", timeout: 30000, stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    if (result.error) return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, `AGY quota probe timed out or failed: ${osErrorName(result.error)}`);
    if (result.status !== 0) {
      const errMsg = (result.stderr || result.stdout || "").trim();
      const lower = errMsg.toLowerCase();
      if (["unauthorized", "authentication", "not authenticated", "login", "401", "403"].some((key) => lower.includes(key)) || ` ${lower} `.includes(" auth ")) {
        return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, `AGY /usage authentication failure: ${errMsg}`);
      }
      return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, `AGY /usage command failed with code ${result.status}: ${errMsg}`);
    }
    let parsed: JsonObject | null = null;
    for (const line of String(result.stdout ?? "").split(/\n/)) {
      if (!line.trim()) continue;
      try {
        const item = JSON.parse(line) as JsonObject;
        if (item.event === "command_result" && isRecord(item.command) && item.command.name === "usage" && isRecord(item.command.data)) {
          parsed = item.command.data;
          break;
        }
      } catch {
        continue;
      }
    }
    if (!parsed || !Array.isArray(parsed.groups)) return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, "AGY /usage returned unexpected response format");
    const target5h = family === "gemini" ? "gemini-5h" : "3p-5h";
    const targetWeekly = family === "gemini" ? "gemini-weekly" : "3p-weekly";
    let primary: QuotaWindow | null = null;
    let secondary: QuotaWindow | null = null;
    for (const group of parsed.groups) {
      if (!isRecord(group) || !Array.isArray(group.buckets)) continue;
      for (const bucket of group.buckets) {
        if (!isRecord(bucket)) continue;
        const bucketId = bucket.id;
        if (bucketId !== target5h && bucketId !== targetWeekly) continue;
        const remaining = bucket.remaining_fraction;
        if (typeof remaining === "boolean" || typeof remaining !== "number" || remaining < 0 || remaining > 1) {
          return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, `malformed remaining_fraction in bucket ${String(bucketId)}: ${pyRepr(remaining)}`);
        }
        const used = round1((1 - remaining) * 100);
        const reset = parseIso(bucket.reset_time);
        if (bucketId === target5h || bucket.window === "5h") primary = new QuotaWindow("5h", used, 300, reset);
        else if (bucketId === targetWeekly || bucket.window === "weekly") secondary = new QuotaWindow("weekly", used, 10080, reset);
      }
    }
    if (primary == null) return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, `missing selected-family 5h bucket for family ${pyRepr(family)}`);
    if (secondary == null) return new QuotaSnapshot("AGY_CLI", readAt, null, null, null, `missing selected-family weekly bucket for family ${pyRepr(family)}`);
    return new QuotaSnapshot("AGY_CLI", readAt, primary, secondary, `Google Antigravity (${family})`);
  }

  listModels(): JsonObject[] {
    const result = spawnSync(this.resolveBinary(), ["models"], { encoding: "utf8", timeout: 10000, stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
    if (!result.error && result.status === 0 && result.stdout.trim()) {
      const found: JsonObject[] = [];
      for (const line of result.stdout.split(/\n/)) {
        const parts = line.trim().split(/\s+/);
        const slug = parts[0];
        if (!slug) continue;
        try {
          const valid = this.validateModel(slug);
          found.push({
            slug: valid,
            display_name: parts.slice(1).join(" ") || slug,
            default_reasoning_level: "medium",
            supported_reasoning_levels: ["low", "medium", "high"],
          });
        } catch {
          continue;
        }
      }
      if (found.length) return found;
    }
    return DEFAULT_AGY_MODELS.map((item) => ({ ...item, supported_reasoning_levels: [...(item.supported_reasoning_levels as string[])] }));
  }

  defaultModel(): string {
    return "gemini-3.8-flash-medium";
  }

  validateModel(model: string): string {
    return validateModelName(model);
  }

  validateReasoningEffort(effort: string): string {
    const level = validateReasoningEffort(effort).toLowerCase();
    if (level !== "low" && level !== "medium" && level !== "high") {
      throw new Error(`AGY reasoning effort must be one of low, medium, high (got ${pyRepr(effort)})`);
    }
    return level;
  }

  supportsAutoPool(): boolean {
    return false;
  }

  supportsLiveSteering(): boolean {
    return false;
  }

  steer(_repo: string | NightPath, _threadId: string, _instruction: string): [boolean, string] {
    return [false, "AGY live steering is not supported by the current upstream CLI; no instruction was sent."];
  }

  findActiveProcesses(repo: string | NightPath, excludePid: number | null = null): JsonObject[] {
    if (process.platform !== "linux") return [];
    const target = new NightPath(repoPath(repo)).resolve().raw;
    if (!fs.existsSync("/proc")) return [];
    const found: JsonObject[] = [];
    for (const name of fs.readdirSync("/proc")) {
      if (!/^\d+$/.test(name)) continue;
      const pid = Number(name);
      if (excludePid && pid === excludePid) continue;
      if (!pidAlive(pid)) continue;
      try {
        const cwd = fs.realpathSync(fs.readlinkSync(`/proc/${pid}/cwd`));
        if (cwd !== target) continue;
        const identity = linuxProcessIdentity(pid);
        const executable = identity?.executable ?? fs.readlinkSync(`/proc/${pid}/exe`);
        const cmdline = fs.readFileSync(`/proc/${pid}/cmdline`).toString("utf8").replaceAll("\u0000", " ").toLowerCase();
        const exeName = executable.toLowerCase();
        const isAgy = (exeName.includes("agy") || cmdline.includes("agy ") || exeName.includes("antigravity")) && !exeName.includes("nightwatch") && !cmdline.includes("nightwatch");
        if (isAgy) found.push({ pid, executable, cmdline, cwd });
      } catch {
        continue;
      }
    }
    return found;
  }

  findActiveThreadsForRepo(repo: string | NightPath): JsonObject[] {
    const target = new NightPath(repoPath(repo)).resolve().raw;
    const dbPath = NightPath.home().join(".gemini", "antigravity-cli", "conversation_summaries.db").raw;
    if (!fs.existsSync(dbPath)) return [];
    try {
      const db = new DatabaseSync(dbPath, { readOnly: true });
      try {
        const rows = db.prepare(
          "SELECT conversation_id, title, last_modified_time, workspace_uris, agent_name FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT 50",
        ).all() as Array<Record<string, unknown>>;
        const matching: JsonObject[] = [];
        for (const row of rows) {
          const uris = String(row.workspace_uris ?? "");
          if (!uris.includes(target)) continue;
          matching.push({ id: row.conversation_id, title: row.title, updated_at: row.last_modified_time, workspace_uris: row.workspace_uris, agent_name: row.agent_name });
        }
        return matching;
      } finally {
        db.close();
      }
    } catch {
      return [];
    }
  }

  doctorCheck(repo: NightPath | null = null): JsonObject {
    const resolved = this.resolveBinary();
    const binPath = which(resolved) || (fs.existsSync(resolved) && fs.statSync(resolved).isFile() ? resolved : null);
    let version: string | null = null;
    if (binPath) {
      const result = spawnSync(binPath, ["--version"], { encoding: "utf8", timeout: 5000, stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
      version = !result.error && result.status === 0 ? result.stdout.trim() : null;
    }
    const authOk = binPath ? this.authSanity(binPath) : false;
    const quota: JsonObject = {};
    if (authOk) {
      try {
        quota.gemini = this.probeQuota(null, repo, "gemini-3.8-flash-high").toDict();
      } catch {
        /* doctor keeps the binary status if one family probe throws */
      }
      try {
        quota["3p"] = this.probeQuota(null, repo, "claude-sonnet-4-6").toDict();
      } catch {
        /* same for the third-party family */
      }
    }
    return { binary: binPath, version, auth_ok: authOk, quota, status: binPath && authOk ? "ok" : "fail" };
  }
}

function explicitConversation(event: JsonObject): string | null {
  if (typeof event.conversation_id === "string" && event.conversation_id.trim()) return event.conversation_id.trim();
  if (isRecord(event.step_update) && typeof event.step_update.conversation_id === "string" && event.step_update.conversation_id.trim()) return event.step_update.conversation_id.trim();
  if (isRecord(event.result) && typeof event.result.conversation_id === "string" && event.result.conversation_id.trim()) return event.result.conversation_id.trim();
  return null;
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function sha16(value: string): string {
  return sha256(value).slice(0, 16);
}

function repoOfStore(store: NightwatchStore): string {
  const repo = store.repo as NightPath | string;
  return typeof repo === "string" ? repo : repo.raw;
}

function asNightPath(value: NightPath | string): NightPath {
  return typeof value === "string" ? new NightPath(value) : value;
}

const registry: Record<string, ProviderAdapter> = {
  codex: new CodexProviderAdapter(),
  agy: new AgyProviderAdapter(),
};

export function getProviderAdapter(name = "codex"): ProviderAdapter {
  const normalized = (name || "codex").trim().toLowerCase();
  const adapter = registry[normalized];
  if (!adapter) {
    const supported = Object.keys(registry).sort().map((key) => `'${key}'`).join(", ");
    throw new Error(`unknown provider: ${pyRepr(name)}; supported providers: [${supported}]`);
  }
  return adapter;
}
