/** Localhost JSON control plane for Nightwatch (TypeScript port of web.py). */

import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { GitError, repoRoot } from "./git.js";
import { State, TERMINAL_STATES, planProgress, type JsonObject } from "./models.js";
import {
  RunSpec,
  SystemExit,
  ValueError,
  doctorSnapshot,
  queueSteer,
  startRun,
  stopRun,
} from "./operations.js";
import { NightPath } from "./path.js";
import { RunCatalog, RunRecord } from "./run-catalog.js";
import { NightwatchStore, StateIntegrityError } from "./storage.js";

const require = createRequire(import.meta.url);
const NIGHTWATCH_VERSION = String(require("../package.json").version);

export const MAX_BODY_BYTES = 64 * 1024;
export const LOG_TAIL_LINES = 80;

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1"]);
const TERMINAL_VALUES = new Set<string>([...TERMINAL_STATES]);
const RUN_ACTIONS = new Set(["stop", "steer", "resume"]);
const FINISHED_MESSAGE = "This run is already finished. Start a new goal.";
const CLOSED_STATES = new Set<string>([State.DONE, State.AWAITING_ACCEPTANCE]);

const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));

function resolveIndexPath(): string {
  const candidates = [
    path.join(MODULE_DIR, "web_static", "index.html"),
    path.join(MODULE_DIR, "..", "src", "web_static", "index.html"),
    path.join(MODULE_DIR, "..", "dist", "web_static", "index.html"),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return candidates[0]!;
}

export function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(host);
}

export function listenUrl(host: string, port: number): string {
  if (host.includes(":")) return `http://[${host}]:${port}/`;
  return `http://${host}:${port}/`;
}

function resumeArgv(root: string): string[] {
  const packageRoot = path.resolve(MODULE_DIR, "..");
  const distCli = path.join(packageRoot, "dist", "cli.js");
  const srcCli = path.join(packageRoot, "src", "cli.ts");
  if (fs.existsSync(distCli)) {
    return [process.execPath, distCli, "resume", "--no-inhibit", "--repo", root];
  }
  return [process.execPath, "--import", "tsx", srcCli, "resume", "--no-inhibit", "--repo", root];
}

/** Bind a loopback HTTP server. Resolves once the socket is listening. */
export function makeServer(host: string, port: number): Promise<http.Server> {
  if (!isLoopbackHost(host)) {
    throw new ValueError(`refusing to bind non-loopback host '${host}'`);
  }
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      void dispatch(req, res).catch((error) => {
        console.error(error);
        if (!res.headersSent) sendJson(res, 500, { ok: false, message: "internal error" });
      });
    });
    const onError = (error: Error) => {
      server.close();
      reject(error);
    };
    server.once("error", onError);
    server.listen({ port, host, ipv6Only: host === "::1" }, () => {
      server.off("error", onError);
      resolve(server);
    });
  });
}

export interface ServeOptions {
  host?: string;
  port?: number;
  openBrowser?: boolean;
}

export async function serve(options?: ServeOptions): Promise<number>;
export async function serve(host?: string, port?: number, openBrowser?: boolean): Promise<number>;
export async function serve(
  hostOrOptions: string | ServeOptions = "127.0.0.1",
  port = 8765,
  openBrowser = false,
): Promise<number> {
  let host = "127.0.0.1";
  let bindPort = 8765;
  let shouldOpen = false;
  if (hostOrOptions !== null && typeof hostOrOptions === "object") {
    host = hostOrOptions.host ?? "127.0.0.1";
    bindPort = hostOrOptions.port ?? 8765;
    shouldOpen = hostOrOptions.openBrowser ?? false;
  } else {
    host = hostOrOptions ?? "127.0.0.1";
    bindPort = port;
    shouldOpen = openBrowser;
  }
  if (!isLoopbackHost(host)) {
    console.error(
      `nightwatch: refusing to bind ${host}; the web control plane listens only on 127.0.0.1 or ::1`,
    );
    return 2;
  }
  const server = await makeServer(host, bindPort);
  const address = server.address() as AddressInfo;
  const url = listenUrl(host, address.port);
  console.log(`Nightwatch web control plane: ${url}`);
  if (shouldOpen) {
    try {
      openBrowserUrl(url);
    } catch {
      console.error("nightwatch: could not open a browser");
    }
  }
  await new Promise<void>((resolve) => {
    const shutdown = () => {
      server.close(() => resolve());
    };
    process.once("SIGINT", shutdown);
    process.once("SIGTERM", shutdown);
  });
  return 0;
}

function openBrowserUrl(url: string): void {
  const platform = process.platform;
  if (platform === "darwin") {
    spawn("open", [url], { detached: true, stdio: "ignore" }).unref();
    return;
  }
  if (platform === "win32") {
    spawn("cmd", ["/c", "start", "", url], { detached: true, stdio: "ignore" }).unref();
    return;
  }
  spawn("xdg-open", [url], { detached: true, stdio: "ignore" }).unref();
}

export type SpawnSupervisor = (repo: string | NightPath) => number | null | Promise<number | null>;

function defaultSpawnSupervisor(repo: string | NightPath): number | null {
  const root = new NightPath(typeof repo === "string" ? repo : repo.raw).expanduser().resolve().raw;
  const store = new NightwatchStore(root);
  const logPath = store.logPath.raw;
  let logFd: number;
  try {
    fs.mkdirSync(path.dirname(logPath), { recursive: true, mode: 0o700 });
    logFd = fs.openSync(logPath, "a");
  } catch {
    return null;
  }
  try {
    fs.chmodSync(logPath, 0o600);
    const argv = resumeArgv(root);
    const child = spawn(argv[0]!, argv.slice(1), {
      cwd: root,
      detached: true,
      stdio: ["ignore", logFd, logFd],
      windowsHide: true,
    });
    child.unref();
    return child.pid ?? null;
  } catch {
    return null;
  } finally {
    try {
      fs.closeSync(logFd);
    } catch {
      /* parent fd closed after spawn dup */
    }
  }
}

export function spawnSupervisor(repo: string | NightPath): number | null {
  return defaultSpawnSupervisor(repo);
}

let spawnSupervisorFn: SpawnSupervisor = defaultSpawnSupervisor;

/** Test-only hook; pass null to restore the production supervisor spawn. */
export function setSpawnSupervisorForTests(fn: SpawnSupervisor | null): void {
  spawnSupervisorFn = fn ?? defaultSpawnSupervisor;
}

export function doctorPayload(): JsonObject {
  const report = { ...doctorSnapshot(null) };
  const checks = doctorChecks(report);
  report.summary = doctorSummary(checks);
  report.checks = checks;
  return report;
}

export function doctorChecks(report: JsonObject): Array<Record<string, string>> {
  const codexPresent = Boolean(report.codex_binary);
  const version = report.codex_version;
  const codex = codexPresent
    ? {
        id: "codex",
        level: "ok",
        title: "Codex CLI",
        detail: `Codex is installed (${version || String(report.codex_binary)}).`,
      }
    : {
        id: "codex",
        level: "fail",
        title: "Codex CLI",
        detail: "The codex binary was not found. Install Codex before starting a Codex run.",
      };

  const authStatus = authField(report).status;
  const auth =
    authStatus === "ok"
      ? {
          id: "auth",
          level: "ok",
          title: "Codex authentication",
          detail: "Codex login status succeeded.",
        }
      : {
          id: "auth",
          level: "fail",
          title: "Codex authentication",
          detail: "Codex is not signed in. Run codex login on this machine, then refresh.",
        };

  const quotaStatus = quotaField(report).status;
  let quota: Record<string, string>;
  if (quotaStatus === "ok") {
    const authority = quotaField(report).authority || "the quota provider";
    quota = {
      id: "quota",
      level: "ok",
      title: "Codex quota",
      detail: `Quota was read from ${authority}.`,
    };
  } else if (codexPresent) {
    quota = {
      id: "quota",
      level: "warn",
      title: "Codex quota",
      detail:
        "Quota could not be read. You can still start a run; Nightwatch retries the reading before waiting on quota.",
    };
  } else {
    quota = {
      id: "quota",
      level: "fail",
      title: "Codex quota",
      detail: "Quota is unavailable because the codex binary is missing.",
    };
  }

  const systemd = report.systemd_inhibit
    ? {
        id: "systemd",
        level: "ok",
        title: "systemd",
        detail: "systemd-inhibit is usable, so a foreground supervisor can block sleep.",
      }
    : {
        id: "systemd",
        level: "warn",
        title: "systemd",
        detail: "systemd is unavailable. Background runs still work.",
      };

  const agyRaw = report.agy;
  const agy = agyRaw !== null && typeof agyRaw === "object" && !Array.isArray(agyRaw) ? (agyRaw as JsonObject) : {};
  const agyCheck =
    agy.status === "ok" && agy.binary
      ? {
          id: "agy",
          level: "ok",
          title: "AGY CLI",
          detail: `AGY is installed (${agy.version || agy.binary}) and signed in.`,
        }
      : {
          id: "agy",
          level: "warn",
          title: "AGY CLI",
          detail: "AGY is missing or not signed in. It is optional; Codex runs do not need it.",
        };

  return [codex, auth, quota, systemd, agyCheck];
}

export function doctorSummary(checks: Array<Record<string, string>>): string {
  const levels = Object.fromEntries(checks.map((item) => [item.id, item.level]));
  if (levels.codex === "fail") return "Install Codex on this machine before starting a run.";
  if (levels.auth === "fail") {
    return "Run codex login on this machine, then refresh this page before starting a run.";
  }
  if (levels.quota === "fail") return "Install Codex so Nightwatch can read quota before starting a run.";
  if (levels.quota === "warn") {
    return "Codex is signed in, but quota could not be read; you can still start a run from this page.";
  }
  if (levels.systemd === "warn") {
    return "Codex is ready, and background runs still work even though systemd is unavailable.";
  }
  if (levels.agy === "warn") return "Codex is ready to start a run, and AGY remains optional.";
  return "Codex is ready, so start a run from this page.";
}

function authField(report: JsonObject): JsonObject {
  const auth = report.auth;
  return auth !== null && typeof auth === "object" && !Array.isArray(auth) ? (auth as JsonObject) : {};
}

function quotaField(report: JsonObject): JsonObject {
  const quota = report.quota;
  return quota !== null && typeof quota === "object" && !Array.isArray(quota) ? (quota as JsonObject) : {};
}

export function unsafeRunId(runId: string): boolean {
  if (typeof runId !== "string" || !runId || runId === "." || runId === "..") return true;
  if (runId.includes("/") || runId.includes("\\") || runId.includes("..") || runId.includes("\0")) return true;
  return false;
}

export function goalText(record: RunRecord): string {
  const goal = record.state.goal;
  if (typeof goal === "string" && goal.trim()) return goal;
  try {
    return record.store.goalPath.readText().trim();
  } catch {
    return "";
  }
}

/** Match Python `str.splitlines()` (drops a trailing empty line after a final newline). */
export function logTail(filePath: string | NightPath, limit = LOG_TAIL_LINES): string[] {
  const raw = typeof filePath === "string" ? filePath : filePath.raw;
  try {
    const text = fs.readFileSync(raw, { encoding: "utf8" });
    const lines = text.split(/\r\n|\n|\r/);
    if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
    return lines.slice(-limit);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
}

export function runSummary(record: RunRecord): JsonObject {
  const state = record.state;
  const progress = planProgress(record.plan);
  let provider = state.provider;
  if (typeof provider !== "string" || !provider) provider = record.provider;
  const model = typeof state.model === "string" ? state.model : null;
  const threadId = typeof state.thread_id === "string" ? state.thread_id : null;
  return {
    id: record.store.directory.name,
    run_id: state.run_id ?? null,
    repo: record.repo.raw,
    goal: goalText(record),
    state: state.state ?? null,
    thread_id: threadId,
    provider,
    model,
    verified_percent: progress.verified_percent,
    verified_count: progress.verified_count,
    total_count: progress.total_count,
    updated_at: state.updated_at ?? null,
    terminal: typeof state.state === "string" && TERMINAL_VALUES.has(state.state),
  };
}

export function runDetail(record: RunRecord): JsonObject {
  const payload = runSummary(record);
  const milestones: JsonObject[] = [];
  const rawMilestones = record.plan.milestones;
  if (Array.isArray(rawMilestones)) {
    for (const item of rawMilestones) {
      if (item === null || typeof item !== "object" || Array.isArray(item)) continue;
      const row = item as JsonObject;
      milestones.push({ id: row.id ?? null, title: row.title ?? null, status: row.status ?? null });
    }
  }
  payload.milestones = milestones;
  payload.quota = stateOrNull(record.state.quota);
  payload.last_error = stateOrNull(record.state.last_error);
  payload.progress = planProgress(record.plan);
  payload.log_tail = logTail(record.store.logPath);
  return payload;
}

function stateOrNull(value: unknown): unknown {
  return value === undefined ? null : value;
}

function errorMessage(exc: unknown): string {
  if (exc instanceof Error) return exc.message || exc.name;
  return String(exc);
}

function isErrno(error: unknown): error is NodeJS.ErrnoException {
  return typeof error === "object" && error !== null && typeof (error as NodeJS.ErrnoException).code === "string";
}

export function listPayload(): JsonObject {
  const catalog = new RunCatalog();
  const records = catalog.discover();
  records.sort((a, b) => {
    const left = String(a.state.updated_at ?? "");
    const right = String(b.state.updated_at ?? "");
    return left < right ? 1 : left > right ? -1 : 0;
  });
  return { runs: records.map((item) => runSummary(item)), errors: [...catalog.errors] };
}

export function findRun(runId: string): RunRecord | null {
  if (unsafeRunId(runId)) return null;
  const catalog = new RunCatalog();
  for (const record of catalog.discover()) {
    if (record.store.directory.name === runId) return record;
  }
  return null;
}

export function runSpecFromBody(body: JsonObject): RunSpec {
  if (!Object.prototype.hasOwnProperty.call(body, "repo") || !Object.prototype.hasOwnProperty.call(body, "goal")) {
    throw new ValueError("repo and goal are required");
  }
  const repo = body.repo;
  if (typeof repo !== "string" || !repo.trim()) throw new ValueError("repo must be a path string");
  let verify = body.verify;
  if (verify === null || verify === undefined) verify = [];
  if (!Array.isArray(verify) || !verify.every((item) => typeof item === "string")) {
    throw new ValueError("verify must be a list of strings");
  }
  let model = Object.prototype.hasOwnProperty.call(body, "model") ? body.model : null;
  if (Object.prototype.hasOwnProperty.call(body, "model") && model !== null && typeof model !== "string") {
    throw new ValueError("model must be a string or null");
  }
  let effort = Object.prototype.hasOwnProperty.call(body, "reasoning_effort") ? body.reasoning_effort : null;
  if (
    Object.prototype.hasOwnProperty.call(body, "reasoning_effort") &&
    effort !== null &&
    typeof effort !== "string"
  ) {
    throw new ValueError("reasoning_effort must be a string or null");
  }
  let provider = Object.prototype.hasOwnProperty.call(body, "provider") ? body.provider : "codex";
  if (provider === null || provider === undefined) provider = "codex";
  if (typeof provider !== "string") throw new ValueError("provider must be 'codex' or 'agy'");
  return new RunSpec({
    repo,
    goal: body.goal as string,
    model: model as string | null,
    reasoningEffort: effort as string | null,
    verifyCommands: verify as string[],
    service: false,
    accountMode: "current-only",
    provider,
  });
}

export function indexBytes(): [number, string, Buffer] {
  const indexPath = resolveIndexPath();
  let data: Buffer;
  try {
    data = fs.readFileSync(indexPath);
  } catch {
    return [500, "text/plain; charset=utf-8", Buffer.from("index.html is missing\n")];
  }
  try {
    new TextDecoder("utf-8", { fatal: true }).decode(data);
  } catch {
    return [500, "text/plain; charset=utf-8", Buffer.from("index.html is not valid UTF-8\n")];
  }
  return [200, "text/html; charset=utf-8", data];
}

function unquotePath(value: string): string {
  return value.replace(/%([0-9A-Fa-f]{2})/g, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
}

export function requestPath(raw: string): string {
  const withoutHash = raw.split("#", 1)[0] ?? raw;
  const withoutQuery = withoutHash.split("?", 1)[0] ?? withoutHash;
  let result = unquotePath(withoutQuery);
  if (!result.startsWith("/")) result = `/${result}`;
  return result;
}

export function splitRunPath(pathname: string): [string, string] | null {
  const prefix = "/api/runs/";
  if (!pathname.startsWith(prefix)) return null;
  const rest = pathname.slice(prefix.length);
  if (!rest) return null;
  const parts = rest.split("/");
  if (parts.length === 1) return [parts[0]!, ""];
  if (parts.length === 2 && RUN_ACTIONS.has(parts[1]!)) return [parts[0]!, parts[1]!];
  return null;
}

async function dispatch(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const method = (req.method || "GET").toUpperCase();
  try {
    const pathname = requestPath(req.url || "/");
    if (pathname.includes("\0")) {
      sendJson(res, 404, { ok: false, message: "Not found." });
      return;
    }
    if (pathname === "/" || pathname === "/index.html") {
      if (method !== "GET") {
        sendJson(res, 405, { ok: false, message: "Method not allowed." });
        return;
      }
      const [status, contentType, body] = indexBytes();
      sendBytes(res, status, contentType, body);
      return;
    }
    if (pathname === "/api/doctor") {
      if (method !== "GET") {
        sendJson(res, 405, { ok: false, message: "Method not allowed." });
        return;
      }
      sendJson(res, 200, doctorPayload());
      return;
    }
    if (pathname === "/api/runs") {
      if (method === "GET") {
        sendJson(res, 200, listPayload());
        return;
      }
      if (method === "POST") {
        await postRun(req, res);
        return;
      }
      sendJson(res, 405, { ok: false, message: "Method not allowed." });
      return;
    }
    const matched = splitRunPath(pathname);
    if (matched === null) {
      sendJson(res, 404, { ok: false, message: "Not found." });
      return;
    }
    const [runId, action] = matched;
    const record = findRun(runId);
    if (record === null) {
      sendJson(res, 404, { ok: false, message: "No Nightwatch run matches that id." });
      return;
    }
    if (action === "" && method === "GET") {
      sendJson(res, 200, runDetail(record));
      return;
    }
    if (action === "stop" && method === "POST") {
      await postStop(req, res, record);
      return;
    }
    if (action === "steer" && method === "POST") {
      await postSteer(req, res, record);
      return;
    }
    if (action === "resume" && method === "POST") {
      await postResume(req, res, record);
      return;
    }
    if (action === "" && method !== "GET") {
      sendJson(res, 405, { ok: false, message: "Method not allowed." });
      return;
    }
    sendJson(res, 405, { ok: false, message: "Method not allowed." });
  } catch (error) {
    console.error(error);
    if (!res.headersSent) sendJson(res, 500, { ok: false, message: "internal error" });
  }
}

async function postRun(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  const [body, error] = await jsonObject(req);
  if (error || body === null) {
    sendJson(res, 400, { ok: false, message: error || "request body is not valid JSON" });
    return;
  }
  let root: NightPath;
  let result;
  try {
    const spec = runSpecFromBody(body);
    root = repoRoot(spec.repo.raw);
    result = startRun(spec, false);
  } catch (exc) {
    if (exc instanceof TypeError || exc instanceof ValueError || exc instanceof GitError || exc instanceof SystemExit || isErrno(exc)) {
      sendJson(res, 400, { ok: false, message: errorMessage(exc) });
      return;
    }
    throw exc;
  }
  if (!result.ok) {
    sendJson(res, 400, { ok: false, message: result.message });
    return;
  }
  let state: JsonObject;
  try {
    state = new NightwatchStore(root.raw).loadState();
  } catch (exc) {
    if (exc instanceof StateIntegrityError || isErrno(exc)) {
      sendJson(res, 500, { ok: false, message: errorMessage(exc) });
      return;
    }
    throw exc;
  }
  let pid: number | null;
  try {
    pid = await Promise.resolve(spawnSupervisorFn(root));
  } catch (exc) {
    if (exc instanceof GitError || isErrno(exc)) pid = null;
    else throw exc;
  }
  sendJson(res, 201, {
    ok: true,
    message: result.message,
    run_id: state.run_id ?? null,
    repo: root.raw,
    supervisor_pid: pid,
  });
}

async function postStop(req: http.IncomingMessage, res: http.ServerResponse, record: RunRecord): Promise<void> {
  const [, error] = await bodyBytes(req);
  if (error) {
    sendJson(res, 400, { ok: false, message: error });
    return;
  }
  const result = stopRun(record.repo);
  sendJson(res, 200, { ok: result.ok, message: result.message });
}

async function postSteer(req: http.IncomingMessage, res: http.ServerResponse, record: RunRecord): Promise<void> {
  const [body, error] = await jsonObject(req);
  if (error || body === null) {
    sendJson(res, 400, { ok: false, message: error || "request body is not valid JSON" });
    return;
  }
  const instruction = body.instruction;
  if (typeof instruction !== "string") {
    sendJson(res, 400, { ok: false, message: "instruction must be a string" });
    return;
  }
  try {
    const result = queueSteer(record.store, instruction);
    sendJson(res, 200, { ok: result.ok, message: result.message });
  } catch (exc) {
    if (exc instanceof TypeError || exc instanceof ValueError) {
      sendJson(res, 400, { ok: false, message: errorMessage(exc) });
      return;
    }
    throw exc;
  }
}

async function postResume(req: http.IncomingMessage, res: http.ServerResponse, record: RunRecord): Promise<void> {
  const [, error] = await bodyBytes(req);
  if (error) {
    sendJson(res, 400, { ok: false, message: error });
    return;
  }
  if (typeof record.state.state === "string" && CLOSED_STATES.has(record.state.state)) {
    sendJson(res, 200, { ok: false, message: FINISHED_MESSAGE });
    return;
  }
  let pid: number | null;
  try {
    pid = await Promise.resolve(spawnSupervisorFn(record.repo));
  } catch (exc) {
    if (exc instanceof GitError || isErrno(exc)) pid = null;
    else throw exc;
  }
  if (pid === null) {
    sendJson(res, 200, {
      ok: false,
      message: "The detached supervisor did not start.",
      supervisor_pid: null,
    });
    return;
  }
  sendJson(res, 200, { ok: true, message: "Detached supervisor started.", supervisor_pid: pid });
}

async function jsonObject(req: http.IncomingMessage): Promise<[JsonObject | null, string | null]> {
  const [raw, error] = await bodyBytes(req);
  if (error) return [null, error];
  if (!raw.length) return [null, "request body is not valid JSON"];
  let payload: unknown;
  try {
    payload = JSON.parse(raw.toString("utf8"));
  } catch {
    return [null, "request body is not valid JSON"];
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return [null, "request body must be a JSON object"];
  }
  return [payload as JsonObject, null];
}

async function bodyBytes(req: http.IncomingMessage): Promise<[Buffer, string | null]> {
  const header = req.headers["content-length"];
  if (header === undefined) return [Buffer.alloc(0), null];
  const length = Number.parseInt(Array.isArray(header) ? header[0]! : header, 10);
  if (!Number.isFinite(length) || Number.isNaN(length)) return [Buffer.alloc(0), "Content-Length is invalid"];
  if (length < 0 || length > MAX_BODY_BYTES) return [Buffer.alloc(0), "request body exceeds 64 KiB"];
  try {
    const chunks: Buffer[] = [];
    let received = 0;
    for await (const chunk of req) {
      const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      received += buf.length;
      if (received > length) return [Buffer.alloc(0), "request body exceeds 64 KiB"];
      chunks.push(buf);
    }
    const body = Buffer.concat(chunks);
    if (body.length !== length) {
      // Prefer reading exactly Content-Length bytes when the stream ends early.
      return [body.subarray(0, Math.min(body.length, length)), null];
    }
    return [body, null];
  } catch {
    return [Buffer.alloc(0), "request body could not be read"];
  }
}

function sendJson(res: http.ServerResponse, status: number, payload: JsonObject): void {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  sendBytes(res, status, "application/json; charset=utf-8", body);
}

function sendBytes(res: http.ServerResponse, status: number, contentType: string, body: Buffer): void {
  res.statusCode = status;
  res.setHeader("Content-Type", contentType);
  res.setHeader("Content-Length", String(body.length));
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Server", `NightwatchHTTP/${NIGHTWATCH_VERSION}`);
  res.end(body);
}
