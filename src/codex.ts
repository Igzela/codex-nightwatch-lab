import { createHash } from "node:crypto";
import { Worker } from "node:worker_threads";
import koffi from "koffi";
import { constants as osConstants } from "node:os";

import { trustedEnvironment } from "./milestones.js";
import { ErrorKind, QuotaWindow, validateModelName, validateReasoningEffort, type JsonObject, type ProviderResult } from "./models.js";
import { NightPath } from "./path.js";
import { NightwatchStore, redact } from "./storage.js";

export interface StopFlag {
  isSet(): boolean;
}

export class ProcessTimeoutError extends Error {
  constructor() {
    super("process wait timed out");
    this.name = "TimeoutExpired";
  }
}

let libcReady = false;
let pipe2: (fds: unknown, flags: number) => number;
let closeFd: (fd: number) => number;
let pollFd: (fds: Array<{ fd: number; events: number; revents: number }>, nfds: number, timeout: number) => number;
let readFd: (fd: number, buffer: Buffer, count: number) => number;
let fcntl: (fd: number, cmd: number, arg: number) => number;
let getpgidNative: (pid: number) => number;
let errnoValue: () => number;

function ensureLibc(): void {
  if (libcReady) return;
  const libc = koffi.load("libc.so.6");
  koffi.struct("pollfd", { fd: "int", events: "short", revents: "short" });
  pipe2 = libc.func("int pipe2(void *fds, int flags)") as typeof pipe2;
  closeFd = libc.func("int close(int fd)") as typeof closeFd;
  pollFd = libc.func("int poll(_Inout_ pollfd *fds, uint64 nfds, int timeout)") as typeof pollFd;
  readFd = libc.func("long read(int fd, void *buf, uintptr_t count)") as typeof readFd;
  fcntl = libc.func("int fcntl(int fd, int cmd, int arg)") as typeof fcntl;
  getpgidNative = libc.func("int getpgid(int pid)") as typeof getpgidNative;
  const errnoLocation = libc.func("int * __errno_location()") as () => unknown;
  errnoValue = () => koffi.decode(errnoLocation(), "int") as number;
  libcReady = true;
}

const O_CLOEXEC = 0x80000;
const O_NONBLOCK = 2048;
const F_GETFL = 3;
const F_SETFL = 4;
const POLLIN = 1;
const EAGAIN = 11;

export function posixGetpgid(pid: number): number {
  ensureLibc();
  const value = getpgidNative(pid);
  if (value < 0) {
    const error = new Error("getpgid failed") as NodeJS.ErrnoException;
    error.code = "ESRCH";
    throw error;
  }
  return value;
}

function osErrorName(error: unknown): string {
  const code = (error as NodeJS.ErrnoException)?.code;
  if (code === "ENOENT") return "FileNotFoundError";
  if (code === "EACCES" || code === "EPERM") return "PermissionError";
  if (code === "ETIMEDOUT") return "TimeoutExpired";
  return code || (error as Error)?.name || "OSError";
}

interface SpawnOptions {
  argv: string[];
  cwd?: string;
  env: Record<string, string>;
  detached?: boolean;
  input: string | null;
  leaseFd?: number | null;
}

const WORKER_SOURCE = `
import { workerData } from "node:worker_threads";
import { spawn } from "node:child_process";
import fs from "node:fs";

const control = new Int32Array(workerData.controlSab);
function send(message) {
  try {
    fs.writeSync(workerData.writeFd, Buffer.from(JSON.stringify(message) + "\\n"));
  } catch {
    process.exit(0);
  }
}
const stdio = [workerData.input == null ? "ignore" : "pipe", "pipe", "pipe"];
if (Number.isInteger(workerData.leaseFd) && workerData.leaseFd >= 3) {
  while (stdio.length < workerData.leaseFd) stdio.push("ignore");
  stdio.push(workerData.leaseFd);
}
let child;
try {
  child = spawn(workerData.argv[0], workerData.argv.slice(1), {
    cwd: workerData.cwd,
    env: workerData.env,
    detached: !!workerData.detached,
    stdio,
    windowsHide: true,
  });
} catch (error) {
  send({ kind: "error", code: (error && error.code) || "EIO", message: String((error && error.message) || error) });
  process.exit(0);
}
if (!child.pid) {
  send({ kind: "error", code: "ENOENT", message: "spawn failed" });
  process.exit(0);
}
send({ kind: "spawned", pid: child.pid });
function pipeLines(stream, kind) {
  if (!stream) {
    send({ kind: kind + "-eof" });
    return;
  }
  let pending = Buffer.alloc(0);
  stream.on("data", (chunk) => {
    pending = Buffer.concat([pending, Buffer.from(chunk)]);
    while (true) {
      const index = pending.indexOf(10);
      if (index < 0) break;
      const line = pending.subarray(0, index + 1).toString("utf8");
      pending = pending.subarray(index + 1);
      send({ kind, line });
    }
  });
  stream.on("end", () => {
    if (pending.length) send({ kind, line: pending.toString("utf8") });
    send({ kind: kind + "-eof" });
  });
  stream.on("error", () => send({ kind: kind + "-eof" }));
}
pipeLines(child.stdout, "stdout");
pipeLines(child.stderr, "stderr");
child.on("error", (error) => send({ kind: "error", code: error.code || "EIO", message: String(error.message || error) }));
child.on("exit", (code, signal) => send({ kind: "exit", code, signal }));
if (workerData.input != null && child.stdin) {
  child.stdin.on("error", () => {});
  const timer = setInterval(() => {
    const flag = Atomics.load(control, 0);
    if (flag === 0) return;
    clearInterval(timer);
    if (flag !== 1) return;
    try { child.stdin.end(workerData.input); } catch { /* the process can exit first */ }
  }, 10);
}
`;

interface WorkerMessage {
  kind: string;
  pid?: number;
  line?: string;
  code?: number | string | null;
  signal?: string | null;
  message?: string;
}

export class ManagedProcess {
  pid = 0;
  private exited = false;
  private exitCode: number | null = null;
  private exitSignal: string | null = null;
  private readonly stdoutQueue: string[] = [];
  private readonly stderrQueue: string[] = [];
  private stdoutEof = false;
  private stderrEof = false;
  private spawnError: NodeJS.ErrnoException | null = null;
  private raw = Buffer.alloc(0);
  private readonly readBuffer = Buffer.alloc(65536);
  private readonly readEnd: number;
  private readonly writeEnd: number;
  private readonly worker: Worker;
  private readonly control: Int32Array;
  private closed = false;
  private readClosed = false;

  private constructor(readEnd: number, writeEnd: number, worker: Worker, control: Int32Array) {
    this.readEnd = readEnd;
    this.writeEnd = writeEnd;
    this.worker = worker;
    this.control = control;
  }

  static start(options: SpawnOptions): ManagedProcess {
    ensureLibc();
    const allocated = koffi.alloc("int", 2);
    if (pipe2(allocated, O_CLOEXEC) !== 0) {
      const error = new Error("pipe2 failed") as NodeJS.ErrnoException;
      error.code = "EIO";
      throw error;
    }
    const decoded = koffi.decode(allocated, "int", 2) as Int32Array;
    const readEnd = decoded[0] ?? 0;
    const writeEnd = decoded[1] ?? 0;
    fcntl(readEnd, F_SETFL, fcntl(readEnd, F_GETFL, 0) | O_NONBLOCK);
    const controlSab = new SharedArrayBuffer(4);
    const control = new Int32Array(controlSab);
    const worker = new Worker(WORKER_SOURCE, {
      eval: true,
      workerData: {
        writeFd: writeEnd,
        controlSab,
        argv: options.argv,
        cwd: options.cwd,
        env: options.env,
        detached: !!options.detached,
        input: options.input,
        leaseFd: options.leaseFd ?? null,
      },
    });
    const process = new ManagedProcess(readEnd, writeEnd, worker, control);
    try {
      process.waitUntilSpawned();
      return process;
    } catch (error) {
      process.close();
      throw error;
    }
  }

  releaseStdin(): void {
    Atomics.store(this.control, 0, 1);
    Atomics.notify(this.control, 0);
  }

  get signal(): string | null {
    return this.exitSignal;
  }

  poll(): number | null {
    this.ingest(0);
    if (!this.exited) return null;
    return this.exitCode;
  }

  async wait(timeoutSeconds?: number): Promise<number> {
    const deadline = timeoutSeconds == null ? Number.POSITIVE_INFINITY : performance.now() + timeoutSeconds * 1000;
    while (!this.exited) {
      if (performance.now() > deadline) throw new ProcessTimeoutError();
      this.ingest(0);
      if (this.exited) break;
      const slice = Math.min(20, deadline - performance.now());
      await delay(Number.isFinite(slice) ? Math.max(1, slice) : 20);
    }
    return this.exitCode ?? -1;
  }

  async readStdoutLine(timeoutMs: number): Promise<string | null> {
    const deadline = performance.now() + timeoutMs;
    while (true) {
      this.ingest(0);
      if (this.stdoutQueue.length) return this.stdoutQueue.shift() ?? "";
      if (this.stdoutEof) return "";
      const remaining = deadline - performance.now();
      if (remaining <= 0) return null;
      await delay(Math.min(20, remaining));
    }
  }

  stderrText(): string {
    return this.stderrQueue.join("");
  }

  async collectStderr(timeoutMs: number): Promise<void> {
    const deadline = performance.now() + timeoutMs;
    while (!this.stderrEof && performance.now() < deadline) {
      this.ingest(0);
      if (this.stderrEof) return;
      await delay(Math.max(1, Math.min(20, deadline - performance.now())));
    }
  }

  async drain(timeoutMs: number): Promise<void> {
    const deadline = performance.now() + Math.max(0, timeoutMs);
    while (performance.now() < deadline) {
      this.ingest(0);
      const remaining = deadline - performance.now();
      if (remaining <= 0) break;
      await delay(Math.min(20, remaining));
    }
    this.ingest(0);
  }

  kill(signal: NodeJS.Signals): void {
    if (this.pid <= 0) return;
    try {
      process.kill(this.pid, signal);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") return;
    }
  }

  killGroup(signal: NodeJS.Signals): void {
    if (this.pid <= 1) return;
    try {
      process.kill(-this.pid, signal);
    } catch {
      /* group may already be gone */
    }
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    Atomics.store(this.control, 0, 2);
    Atomics.notify(this.control, 0);
    if (!this.exited && this.pid > 0) {
      try {
        process.kill(this.pid, "SIGKILL");
      } catch {
        /* already gone */
      }
      this.ingest(200);
    }
    void this.worker.terminate();
    try {
      closeFd(this.readEnd);
    } catch {
      /* closed */
    }
    try {
      closeFd(this.writeEnd);
    } catch {
      /* closed */
    }
  }

  private waitUntilSpawned(): void {
    const deadline = performance.now() + 5000;
    while (performance.now() < deadline) {
      this.ingest(50);
      if (this.pid > 0) return;
      if (this.spawnError) throw this.spawnError;
    }
    const error = new Error("spawn timed out") as NodeJS.ErrnoException;
    error.code = "ETIMEDOUT";
    throw error;
  }

  private ingest(timeoutMs: number): void {
    if (this.readClosed) return;
    const polled = [{ fd: this.readEnd, events: POLLIN, revents: 0 }];
    const ready = pollFd(polled, 1, Math.max(0, Math.floor(timeoutMs)));
    if (ready <= 0) return;
    while (true) {
      const count = Number(readFd(this.readEnd, this.readBuffer, this.readBuffer.length));
      if (count < 0) {
        if (errnoValue() === EAGAIN) return;
        return;
      }
      if (count === 0) {
        this.readClosed = true;
        return;
      }
      this.raw = Buffer.concat([this.raw, this.readBuffer.subarray(0, count)]);
      this.consume();
    }
  }

  private consume(): void {
    while (true) {
      const index = this.raw.indexOf(0x0a);
      if (index < 0) return;
      const line = this.raw.subarray(0, index).toString("utf8");
      this.raw = this.raw.subarray(index + 1);
      if (!line) continue;
      let message: WorkerMessage;
      try {
        message = JSON.parse(line) as WorkerMessage;
      } catch {
        continue;
      }
      this.apply(message);
    }
  }

  private apply(message: WorkerMessage): void {
    if (message.kind === "spawned" && typeof message.pid === "number") this.pid = message.pid;
    else if (message.kind === "stdout" && typeof message.line === "string") this.stdoutQueue.push(message.line);
    else if (message.kind === "stdout-eof") this.stdoutEof = true;
    else if (message.kind === "stderr" && typeof message.line === "string") this.stderrQueue.push(message.line);
    else if (message.kind === "stderr-eof") this.stderrEof = true;
    else if (message.kind === "error" && this.pid === 0) {
      const error = new Error(message.message || "spawn failed") as NodeJS.ErrnoException;
      error.code = typeof message.code === "string" ? message.code : "EIO";
      this.spawnError = error;
    } else if (message.kind === "exit") this.noteExit(typeof message.code === "number" ? message.code : null, message.signal ?? null);
  }

  private noteExit(code: number | null, signal: string | null): void {
    if (this.exited) return;
    this.exited = true;
    if (signal) {
      this.exitSignal = signal;
      const number = osConstants.signals[signal as keyof typeof osConstants.signals];
      this.exitCode = typeof number === "number" ? -number : -1;
      return;
    }
    this.exitSignal = null;
    this.exitCode = typeof code === "number" ? code : -1;
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

export function utcNow(): Date {
  return new Date();
}

export function parseEpoch(value: unknown): number | null {
  if (typeof value === "boolean" || value == null) return null;
  let number: number;
  if (typeof value === "number") number = value;
  else if (typeof value === "string") {
    const text = value.trim();
    if (!text || !/^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$/.test(text)) return null;
    number = Number(text);
  } else return null;
  if (!Number.isFinite(number) || !number || number < 0) return null;
  if (number > 10_000_000_000) number /= 1000;
  return Math.trunc(number);
}

export function parseIso(value: unknown): number | null {
  if (typeof value !== "string") return null;
  let text = value.trim();
  if (!text) return null;
  text = text.replaceAll("Z", "+00:00").replace(" ", "T");
  if (!/(?:[+-]\d{2}:\d{2}|[+-]\d{4})$/.test(text)) text += "+00:00";
  text = text.replace(/\.(\d+)(?=[+-])/, (_match, fraction: string) => `.${fraction.slice(0, 3).padEnd(3, "0")}`);
  const parsed = Date.parse(text);
  if (Number.isNaN(parsed)) return null;
  return Math.trunc(parsed / 1000);
}

export function parseRelativeSeconds(text: string): number | null {
  const matches = [...text.matchAll(/(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b/gi)];
  if (!matches.length) return null;
  let total = 0;
  for (const match of matches) {
    const value = Number(match[1]);
    const unit = (match[2] ?? "").toLowerCase();
    if (unit.startsWith("h")) total += value * 3600;
    else if (unit.startsWith("m")) total += value * 60;
    else total += value;
  }
  return total || null;
}

export function extractReset(text: string, now: Date | null = null): [number | null, string | null] {
  const current = now ?? utcNow();
  const epoch = /(?:resets?_at|resets?At)["']?\s*[=:]\s*["']?(\d{10,13})/i.exec(text);
  if (epoch) {
    const value = parseEpoch(epoch[1]);
    if (value != null) return [value, "provider_epoch"];
  }
  const iso = /(?:resets?_at|resets?At)["']?\s*[=:]\s*["']?(\d{4}-\d{2}-\d{2}T[^"'\s]+Z)|(?:try again|retry|available)\s+(?:at|on)\s*["']?(\d{4}-\d{2}-\d{2}T[^"'\s]+Z)/i.exec(text);
  if (iso) {
    const value = parseIso(iso[1] || iso[2]);
    if (value != null) return [value, "provider_reset_at"];
  }
  const relative = /(?:try again|reset|retry|available)\s+(?:in|after)\s+([^\n.]+)/i.exec(text);
  if (relative?.[1]) {
    const seconds = parseRelativeSeconds(relative[1]);
    if (seconds != null) return [Math.trunc(current.getTime() / 1000) + seconds, "provider_relative"];
  }
  return [null, null];
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function* walkDicts(value: unknown): Generator<JsonObject> {
  if (isRecord(value)) {
    yield value;
    for (const item of Object.values(value)) yield* walkDicts(item);
  } else if (Array.isArray(value)) {
    for (const item of value) yield* walkDicts(item);
  }
}

function first(value: JsonObject, ...keys: string[]): unknown {
  for (const key of keys) if (Object.hasOwn(value, key)) return value[key];
  return null;
}

function pyNumber(value: unknown): number | null {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

export function extractThreadId(event: JsonObject): string | null {
  const eventType = String(event.type ?? "");
  if (!["thread.started", "thread_started", "session_meta", "session.started"].includes(eventType) && !("thread_id" in event)) return null;
  for (const item of walkDicts(event)) {
    for (const key of ["thread_id", "threadId", "session_id", "sessionId", "conversation_id", "conversationId"]) {
      const candidate = item[key];
      if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
    }
  }
  return null;
}

function limitName(key: string, window: JsonObject): string {
  const minutes = pyNumber(first(window, "window_duration_mins", "windowDurationMins", "window_minutes", "windowMinutes")) ?? 0;
  if (minutes >= 7 * 24 * 60) return "weekly";
  if (minutes > 0 && minutes <= 12 * 60) return "5h";
  return key === "primary" ? "primary" : "secondary";
}

export function extractQuotaWindows(event: unknown): QuotaWindow[] {
  const found: QuotaWindow[] = [];
  for (const item of walkDicts(event)) {
    const limits = first(item, "rate_limits", "rateLimits");
    if (!isRecord(limits)) continue;
    for (const key of ["primary", "secondary"]) {
      const raw = limits[key];
      if (!isRecord(raw)) continue;
      const used = pyNumber(first(raw, "used_percent", "usedPercent"));
      const duration = pyNumber(first(raw, "window_duration_mins", "windowDurationMins", "window_minutes", "windowMinutes"));
      found.push(new QuotaWindow(limitName(key, raw), used, duration == null ? null : Math.trunc(duration), parseEpoch(first(raw, "resets_at", "resetsAt"))));
    }
    if (found.length) break;
  }
  return dedupeWindows(found);
}

function dedupeWindows(windows: QuotaWindow[]): QuotaWindow[] {
  const result = new Map<string, QuotaWindow>();
  for (const window of windows) result.set(window.name, window);
  return [...result.values()];
}

function textFields(value: unknown): string[] {
  const result: string[] = [];
  for (const item of walkDicts(value)) {
    for (const key of ["code", "error", "message", "detail", "reason", "status", "type", "rateLimitReachedType"]) {
      const candidate = item[key];
      if (typeof candidate === "string") result.push(candidate);
    }
  }
  return result;
}

function safeDetail(text: string): string | null {
  const redacted = redact(text);
  const compact = String(redacted ?? "").split(/\s+/).filter(Boolean).join(" ").slice(0, 500);
  return compact || null;
}

export function classifyFailure(
  events: JsonObject[],
  stderr: string,
  exitCode: number | null,
  signalName: string | null,
): [ErrorKind | null, string | null, number | null, string | null, QuotaWindow[], string | null] {
  const texts = events.flatMap((event) => textFields(event));
  texts.push(stderr);
  const combined = texts.join("\n");
  const lower = combined.toLowerCase();
  let windows: QuotaWindow[] = [];
  let resetAt: number | null = null;
  let resetSource: string | null = null;
  for (const event of events) {
    windows.push(...extractQuotaWindows(event));
    for (const item of walkDicts(event)) {
      const rawReset = first(item, "resets_at", "resetsAt");
      const structured = parseEpoch(rawReset) || parseIso(rawReset);
      if (structured && (resetAt == null || structured > resetAt)) {
        resetAt = structured;
        resetSource = typeof rawReset === "number" && typeof rawReset !== "boolean" ? "provider_epoch" : "provider_reset_at";
        break;
      }
    }
  }
  windows = dedupeWindows(windows);
  for (const window of windows) {
    if (window.exhausted && window.resetsAt && (resetAt == null || window.resetsAt > resetAt)) {
      resetAt = window.resetsAt;
      resetSource = "rollout_rate_limits";
    }
  }
  if (resetAt == null) [resetAt, resetSource] = extractReset(combined);
  const structuredCodes = events
    .flatMap((event) => [...walkDicts(event)].filter((value) => typeof value.code === "string").map((value) => String(value.code)))
    .join(" ")
    .toLowerCase();
  const reachedTypes = events.flatMap((event) => [...walkDicts(event)].map((item) => String(item.rateLimitReachedType ?? "").toLowerCase()));
  const isWeekly = reachedTypes.some((value) => ["weekly", "week", "7d", "7_day"].includes(value)) || ["weekly", "7 day", "7-day"].some((token) => lower.includes(token));
  const quotaCode = ["usage_limit", "usage-limit", "quota_exhausted", "rate_limit_reached"].some((token) => structuredCodes.includes(token));
  const quotaText = lower.includes("usage limit") || lower.includes("you've hit your usage") || lower.includes("quota exhausted");
  if (quotaCode || quotaText || windows.some((window) => window.exhausted)) {
    const kind = isWeekly || windows.some((window) => window.name === "weekly" && window.exhausted) ? ErrorKind.QUOTA_WEEKLY : ErrorKind.QUOTA_5H;
    return [kind, safeDetail(combined), resetAt, resetSource, windows, null];
  }
  if (lower.includes("task_blocker") || lower.includes("blocked") || lower.includes("blocker")) {
    const detail = safeDetail(combined);
    return [ErrorKind.BLOCKER, detail, null, null, windows, detail];
  }
  if (["unauthorized", "authentication", "invalid api key", "token expired", "login required", "401", "403 forbidden"].some((token) => lower.includes(token))) {
    return [ErrorKind.AUTH, safeDetail(combined), null, null, windows, null];
  }
  if (["capacity", "overloaded", "temporarily unavailable", "529"].some((token) => lower.includes(token))) {
    return [ErrorKind.CAPACITY, safeDetail(combined), null, null, windows, null];
  }
  if (lower.includes("429") || lower.includes("too many requests") || lower.includes("rate limit")) {
    return [ErrorKind.TEMPORARY_429, safeDetail(combined), null, null, windows, null];
  }
  if (["connection", "network", "timed out", "timeout", "dns", "broken pipe", "stream disconnected", "websocket"].some((token) => lower.includes(token))) {
    return [ErrorKind.NETWORK, safeDetail(combined), null, null, windows, null];
  }
  if ((exitCode != null && exitCode !== 0) || signalName) {
    return [ErrorKind.CRASH, safeDetail(combined) || `Codex exited ${exitCode || signalName}`, null, null, windows, null];
  }
  return [null, null, resetAt, resetSource, windows, null];
}

export function buildCommand(
  _repo: string | NightPath,
  threadId: string | null,
  _prompt: string,
  model: string | null = null,
  reasoningEffort: string | null = null,
): [string[], string] {
  const binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
  // The prompt is sent on stdin so it does not appear in argv or the process list.
  const base = [binary, "exec"];
  if (model) base.push("--model", validateModelName(model));
  if (reasoningEffort) {
    // A JSON string is one TOML scalar and cannot inject extra config keys.
    base.push("--config", `model_reasoning_effort=${JSON.stringify(validateReasoningEffort(reasoningEffort))}`);
  }
  if (threadId) {
    // Resume the persisted session. Never use the recency-based --last path.
    return [[...base, "--json", "resume", threadId, "-"], "resume"];
  }
  return [[...base, "--json", "--sandbox", "workspace-write", "-"], "start"];
}

function sanitizedEvent(event: JsonObject): JsonObject {
  const allowed = new Set(["type", "thread_id", "threadId", "session_id", "sessionId", "turn_id", "turnId", "timestamp", "code", "status", "rate_limits", "rateLimits", "usage", "resets_at", "resetsAt", "error"]);
  const result: JsonObject = {};
  for (const [key, value] of Object.entries(event)) {
    if (allowed.has(key)) result[key] = redact(value) as JsonObject[string];
  }
  if (!("type" in result)) result.type = "unknown";
  return result;
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

function asNightPath(value: NightPath | string): NightPath {
  return typeof value === "string" ? new NightPath(value) : value;
}

export async function runCodex(
  store: NightwatchStore,
  generation: number,
  prompt: string,
  threadId: string | null = null,
  onSpawn: ((pid: number, action: string) => void) | null = null,
  onThread: ((threadId: string) => void) | null = null,
  stopEvent: StopFlag | null = null,
  timeout: number | null = null,
  codexHome: string | NightPath | null = null,
  leaseFd: number | null = null,
  accountFingerprint: string | null = null,
): Promise<ProviderResult> {
  const state = store.loadState();
  const model = typeof state.model === "string" ? state.model : null;
  const reasoning = typeof state.reasoning_effort === "string" ? state.reasoning_effort : null;
  const [args, action] = buildCommand(repoOf(store), threadId, prompt, model, reasoning);
  const runs = asNightPath(store.runsPath as NightPath | string);
  runs.mkdir({ parents: true, existOk: true, mode: 0o700 });
  const runLog = runs.join(`generation-${generation}.stderr.log`);
  store.writeRunEvent(generation, { type: "provider_command", action, argv: args.filter((item) => item !== prompt) });
  const started = performance.now();
  const environment = trustedEnvironment();
  if (codexHome != null) environment.CODEX_HOME = new NightPath(typeof codexHome === "string" ? codexHome : codexHome.raw).expanduser().resolve().raw;
  if (accountFingerprint != null) environment.NIGHTWATCH_ACCOUNT_FINGERPRINT = accountFingerprint;
  let proc: ManagedProcess;
  try {
    proc = ManagedProcess.start({
      argv: args,
      cwd: repoOf(store),
      env: environment,
      detached: false,
      input: prompt,
      leaseFd: typeof leaseFd === "number" && Number.isInteger(leaseFd) && leaseFd >= 0 ? leaseFd : null,
    });
  } catch (error) {
    return providerResult({ threadId, errorKind: ErrorKind.UNKNOWN, errorDetail: `Codex spawn failed: ${osErrorName(error)}`, runLog: runLog.raw });
  }
  try {
    onSpawn?.(proc.pid, action);
    proc.releaseStdin();
    const events: JsonObject[] = [];
    const eventTypes: string[] = [];
    let malformed = 0;
    const seen = new Set<string>();
    let foundThread = threadId;
    while (true) {
      if (stopEvent?.isSet() && proc.poll() == null) proc.kill("SIGINT");
      if (timeout != null && performance.now() - started > timeout * 1000 && proc.poll() == null) proc.kill("SIGKILL");
      const line = await proc.readStdoutLine(100);
      if (line == null) continue;
      if (line === "") break;
      const raw = line.trim();
      if (!raw) continue;
      let event: unknown;
      try {
        event = JSON.parse(raw);
      } catch {
        malformed += 1;
        store.writeRunEvent(generation, { type: "malformed_json", line_hash: createHash("sha256").update(raw, "utf8").digest("hex").slice(0, 16) });
        continue;
      }
      if (!isRecord(event)) {
        malformed += 1;
        continue;
      }
      const digest = createHash("sha256").update(raw, "utf8").digest("hex");
      if (seen.has(digest)) {
        store.writeRunEvent(generation, { type: "duplicate_event", line_hash: digest.slice(0, 16) });
        continue;
      }
      seen.add(digest);
      events.push(event);
      const eventType = String(event.type ?? "unknown");
      eventTypes.push(eventType);
      store.writeRunEvent(generation, sanitizedEvent(event));
      const candidate = extractThreadId(event);
      if (candidate) {
        if (foundThread && candidate !== foundThread) {
          store.writeRunEvent(generation, { type: "thread_id_mismatch", expected: foundThread, observed: candidate });
          foundThread = "__MISMATCH__";
        } else if (!foundThread) {
          foundThread = candidate;
          onThread?.(candidate);
        }
      }
    }
    const returnCode = await proc.wait();
    await proc.collectStderr(2000);
    const stderr = proc.stderrText();
    try {
      runLog.writeText(String(redact(stderr) ?? ""));
      runLog.chmod(0o600);
    } catch {
      /* logging must not hide the provider result */
    }
    const signalName = proc.signal;
    if (foundThread === "__MISMATCH__") {
      return providerResult({
        exitCode: returnCode,
        signal: signalName,
        threadId: null,
        eventCount: events.length,
        malformedCount: malformed,
        eventTypes,
        errorKind: ErrorKind.STATE,
        errorDetail: "Codex emitted a different thread ID than the durable thread",
        runLog: runLog.raw,
      });
    }
    let [kind, detail, resetAt, resetSource, windows, blocker] = classifyFailure(events, stderr, returnCode, signalName);
    if (malformed) {
      kind = ErrorKind.MALFORMED;
      detail = `Codex stdout contained ${malformed} malformed JSONL event(s)`;
    }
    if (!foundThread && action === "start") {
      kind = ErrorKind.STATE;
      detail = "Codex did not emit thread.started with a thread_id";
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
      aborted: Boolean(stopEvent?.isSet()),
      runLog: runLog.raw,
    });
  } finally {
    proc.close();
  }
}

function repoOf(store: NightwatchStore): string {
  const repo = store.repo as NightPath | string;
  return typeof repo === "string" ? repo : repo.raw;
}
