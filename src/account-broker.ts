import { AsyncLocalStorage } from "node:async_hooks";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

import { AccountBrokerError, AccountBusy, AccountSchemaError, AccountUnavailable } from "./account-errors.js";
import { AccountRegistryLock, AccountRegistryLockBroker } from "./account-locks.js";
import { canonicalJson, sortValue } from "./json.js";
import { QuotaSnapshot } from "./models.js";
import { NightPath, TextFile } from "./path.js";
import { LOCK_EX, LOCK_NB, LOCK_UN, WouldBlock, flock } from "./posix.js";
import { linuxProcessIdentity, processIdentityMatches } from "./process-identity.js";
import { controlPlaneRoot, nowIso } from "./storage.js";
import { crashHook } from "./testing.js";

const require = createRequire(import.meta.url);

export const SUPPORTED_JSON_SCHEMA = 1;
export const MAX_JSON_BYTES = 1_000_000;

const O_CLOEXEC = 524288;
const DIRECTORY_FLAGS = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | O_CLOEXEC | fs.constants.O_NOFOLLOW;
const FILE_FLAGS = fs.constants.O_RDWR | fs.constants.O_CREAT | O_CLOEXEC | fs.constants.O_NOFOLLOW;

type Identity = [number, number];
type JsonObject = Record<string, unknown>;

interface ProcessIdentity {
  pid: number;
  starttime: string;
  executable: string;
}

interface TrustedHome {
  verify(): void;
  path: string | NightPath;
}

export interface CapsuleLike {
  codexHome?: NightPath | string | null;
  enter?: () => CapsuleLike | void;
  close?: (options?: { discardUnsynced?: boolean }) => void;
  exit?: () => void;
}

export type QuotaFactory = (home: NightPath, fingerprint: string, leaseFd: number) => { read(): QuotaSnapshot };

export interface AccountDirectory {
  listAccounts(options?: { activeOnly?: boolean }): AccountRecord[];
  activeAccount?: () => AccountRecord | null;
}

function asPath(value: string | NightPath): NightPath {
  return value instanceof NightPath ? value : new NightPath(value);
}

function realpathPrefix(absolute: string): NightPath {
  try {
    return new NightPath(fs.realpathSync(absolute));
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if (code !== "ENOENT" && code !== "ENOTDIR") throw error;
    const parent = path.dirname(absolute);
    if (parent === absolute) return new NightPath(absolute);
    return new NightPath(path.join(realpathPrefix(parent).raw, path.basename(absolute)));
  }
}

function resolvePath(value: string | NightPath): NightPath {
  return realpathPrefix(path.resolve(asPath(value).expanduser().raw));
}

function pyExists(target: NightPath): boolean {
  try {
    fs.statSync(target.raw);
    return true;
  } catch {
    return false;
  }
}

function isSymlink(target: NightPath): boolean {
  try {
    return fs.lstatSync(target.raw).isSymbolicLink();
  } catch {
    return false;
  }
}

function withCause<T extends Error>(error: T, cause: unknown): T {
  (error as { cause?: unknown }).cause = cause;
  return error;
}

function isPlainObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

function deepEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((item, index) => deepEqual(item, right[index]));
  }
  if (isPlainObject(left) && isPlainObject(right)) {
    const leftKeys = Object.keys(left);
    if (leftKeys.length !== Object.keys(right).length) return false;
    return leftKeys.every((key) => deepEqual(left[key], right[key]));
  }
  return false;
}

function childPath(dirfd: number, name: string): string {
  if (!name || name.includes("/") || name.includes("\0") || name === "." || name === "..") {
    throw new Error(`refusing unsafe directory-relative name: ${JSON.stringify(name)}`);
  }
  return `/proc/self/fd/${dirfd}/${name}`;
}

function lstatAt(dirfd: number, name: string): fs.Stats {
  return fs.lstatSync(childPath(dirfd, name));
}

function openAt(dirfd: number, name: string, flags: number, mode?: number): number {
  return mode === undefined ? fs.openSync(childPath(dirfd, name), flags) : fs.openSync(childPath(dirfd, name), flags, mode);
}

function identityOf(stats: fs.Stats): Identity {
  return [stats.dev, stats.ino];
}

function identitiesEqual(left: Identity, right: Identity): boolean {
  return left[0] === right[0] && left[1] === right[1];
}

function controlRoot(): NightPath {
  return asPath(controlPlaneRoot() as string | NightPath);
}

function pythonJson(value: unknown): string {
  return spacedJson(sortValue(value));
}

function spacedJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map((item) => spacedJson(item)).join(", ")}]`;
  if (isPlainObject(value)) {
    const entries = Object.entries(value).map(([key, item]) => `${JSON.stringify(key)}: ${spacedJson(item)}`);
    return `{${entries.join(", ")}}`;
  }
  return JSON.stringify(value);
}

function looksLikeAuthFailure(value: unknown): boolean {
  const text = String(value instanceof Error ? value.message : value).toLowerCase();
  return ["unauthorized", "authentication", "login required", "invalid api", "401", "403"].some((marker) => text.includes(marker));
}

function optionalText(value: unknown): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  return value.split(/\s+/).filter(Boolean).join(" ").slice(0, 160);
}

function validAccountKey(value: unknown): value is string {
  return typeof value === "string" && Boolean(value.trim()) && [...value].every((char) => char.codePointAt(0)! >= 32);
}

function requireAccountKey(accountKey: string): void {
  if (typeof accountKey !== "string" || !accountKey.trim()) throw new Error("account_key must be a non-empty string");
}

function which(command: string): string | null {
  if (!command || command.includes("\0")) return null;
  for (const directory of (process.env.PATH ?? "").split(path.delimiter)) {
    const candidate = path.join(directory || ".", command);
    try {
      if (!fs.statSync(candidate).isFile()) continue;
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      continue;
    }
  }
  return null;
}

function spawnErrorName(error: Error): string {
  const code = (error as NodeJS.ErrnoException).code;
  if (code === "ETIMEDOUT") return "TimeoutExpired";
  if (code === "ENOENT") return "FileNotFoundError";
  if (code === "EACCES" || code === "EPERM") return "PermissionError";
  return "OSError";
}

function decodeStdout(value: unknown): string {
  const bytes = Buffer.isBuffer(value) ? value : Buffer.from(String(value ?? ""), "utf8");
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
}

function pythonRepr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (typeof value === "string") return `'${value.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return String(value);
  return JSON.stringify(value);
}

function hasVerify(value: unknown): value is TrustedHome {
  return isPlainObject(value) && typeof value.verify === "function" && (typeof value.path === "string" || value.path instanceof NightPath);
}

function readHandle(handle: TextFile): string {
  handle.seek(0);
  return handle.read();
}

function glob(dir: NightPath, pattern: string): NightPath[] {
  let names: string[];
  try {
    names = fs.readdirSync(dir.raw);
  } catch {
    return [];
  }
  const suffix = pattern.startsWith("*") ? pattern.slice(1) : null;
  return names
    .filter((name) => !name.startsWith(".") && (suffix === null ? name === pattern : name.endsWith(suffix)))
    .map((name) => dir.join(name));
}

function rglob(root: NightPath, pattern: string): NightPath[] {
  if (!pyExists(root) || isSymlink(root)) return [];
  let rootStat: fs.Stats;
  try {
    rootStat = fs.lstatSync(root.raw);
  } catch {
    return [];
  }
  if (!rootStat.isDirectory()) return [];
  const suffix = pattern.startsWith("*") ? pattern.slice(1) : null;
  const matches = (name: string) => !name.startsWith(".") && (suffix === null ? name === pattern : name.endsWith(suffix));
  const found: NightPath[] = [];
  const stack = [root];
  while (stack.length) {
    const current = stack.pop()!;
    let names: string[];
    try {
      names = fs.readdirSync(current.raw);
    } catch {
      continue;
    }
    for (const name of names) {
      const child = current.join(name);
      let info: fs.Stats;
      try {
        info = fs.lstatSync(child.raw);
      } catch {
        continue;
      }
      if (info.isSymbolicLink()) {
        if (matches(name)) found.push(child);
        continue;
      }
      if (info.isDirectory()) stack.push(child);
      else if (matches(name)) found.push(child);
    }
  }
  return found;
}

function closeCapsule(capsule: CapsuleLike, discard: boolean): void {
  if (typeof capsule.close === "function") {
    capsule.close({ discardUnsynced: discard });
    return;
  }
  if (typeof capsule.exit === "function") {
    capsule.exit();
    return;
  }
  const dispose = (capsule as { [Symbol.dispose]?: () => void })[Symbol.dispose];
  if (typeof dispose === "function") dispose.call(capsule);
}

function decodeBase64(value: string, encoding: BufferEncoding): string | null {
  const alphabet = encoding === "base64url" ? /^[A-Za-z0-9_-]*={0,2}$/ : /^[A-Za-z0-9+/]*={0,2}$/;
  if (!alphabet.test(value) || value.length % 4 !== 0) return null;
  try {
    return Buffer.from(value, encoding).toString("utf8");
  } catch {
    return null;
  }
}

export function accountFingerprint(accountKey: string): string {
  if (typeof accountKey !== "string" || !accountKey.trim()) throw new Error("account_key must be a non-empty string");
  return `acct-${createHash("sha256").update(accountKey, "utf8").digest("hex").slice(0, 12)}`;
}

export class AccountRecord {
  constructor(
    readonly accountKey: string,
    readonly alias: string | null = null,
    readonly accountName: string | null = null,
    readonly plan: string | null = null,
    readonly authMode: string | null = null,
    readonly active = false,
  ) {}

  get fingerprint(): string {
    return accountFingerprint(this.accountKey);
  }

  get displayName(): string {
    for (const value of [this.alias, this.accountName]) {
      if (typeof value === "string" && value.trim()) return value.split(/\s+/).filter(Boolean).join(" ").slice(0, 80);
    }
    return this.fingerprint;
  }
}

export class AccountCandidate {
  constructor(
    readonly accountKey: string,
    readonly quota: QuotaSnapshot | null,
    readonly leased = false,
    readonly authError = false,
  ) {}

  get fingerprint(): string {
    return accountFingerprint(this.accountKey);
  }
}

export class CodexAuthAdapter {
  readonly binary: string;
  readonly codexHome: NightPath;
  readonly timeout: number;
  readonly canonicalRegistry: boolean;
  registryLock: AccountRegistryLockBroker | null;
  private readonly registryContext = new AsyncLocalStorage<AccountRegistryLock>();

  constructor(
    binary?: string | null,
    codexHome?: string | NightPath | null,
    timeout = 10,
    registryLock: AccountRegistryLockBroker | null = null,
    canonicalRegistry = true,
  ) {
    this.binary = binary || process.env.NIGHTWATCH_CODEX_AUTH_BIN || "codex-auth";
    const home = codexHome || process.env.CODEX_HOME || NightPath.home().join(".codex");
    this.codexHome = resolvePath(home);
    this.timeout = timeout;
    this.registryLock = registryLock;
    this.canonicalRegistry = canonicalRegistry;
  }

  private resolvedBinary(): string | null {
    if (this.binary.includes(path.sep)) {
      try {
        if (!fs.statSync(this.binary).isFile()) return null;
        fs.accessSync(this.binary, fs.constants.X_OK);
        return this.binary;
      } catch {
        return null;
      }
    }
    return which(this.binary);
  }

  private environment(): NodeJS.ProcessEnv {
    const environment: NodeJS.ProcessEnv = {};
    for (const [key, value] of Object.entries(process.env)) {
      if (value !== undefined) environment[key] = value;
    }
    environment.CODEX_HOME = this.codexHome.raw;
    return environment;
  }

  available(): boolean {
    if (this.resolvedBinary() === null) return false;
    try {
      this.listAccounts();
    } catch (error) {
      if (error instanceof AccountBrokerError) return false;
      throw error;
    }
    return true;
  }

  listAccounts(options: { activeOnly?: boolean } = {}): AccountRecord[] {
    const argv = ["list"];
    if (options.activeOnly) argv.push("--active");
    argv.push("--skip-api", "--json");
    const document = this.runJson(argv);
    const accounts = document.accounts;
    if (!Array.isArray(accounts)) throw new AccountSchemaError("codex-auth list JSON has no accounts array");
    const result: AccountRecord[] = [];
    for (const item of accounts) {
      if (!isPlainObject(item)) throw new AccountSchemaError("codex-auth account entry is not an object");
      const key = item.account_key;
      if (!validAccountKey(key)) throw new AccountSchemaError("codex-auth account entry has no stable account_key");
      result.push(new AccountRecord(key, optionalText(item.alias), optionalText(item.account_name), optionalText(item.plan), optionalText(item.auth_mode), item.active === true));
    }
    return result;
  }

  activeAccount(): AccountRecord | null {
    const accounts = this.listAccounts({ activeOnly: true });
    const active = accounts.filter((item) => item.active);
    if (active.length > 1) throw new AccountSchemaError("codex-auth reported multiple active accounts");
    return active[0] ?? (accounts.length === 1 ? accounts[0] : null);
  }

  switch(accountKey: string): AccountRecord {
    requireAccountKey(accountKey);
    const document = this.runJson(["switch", accountKey, "--json"]);
    const switched = document.switched_to;
    if (!isPlainObject(switched)) throw new AccountSchemaError("codex-auth switch JSON has no switched_to account");
    if (switched.account_key !== accountKey) throw new AccountSchemaError("codex-auth switched to an unexpected account");
    return new AccountRecord(accountKey, optionalText(switched.alias), optionalText(switched.account_name), optionalText(switched.plan), optionalText(switched.auth_mode), true);
  }

  exportAccounts(destination: string | NightPath): void {
    this.runCommand(["export", resolvePath(destination).raw], false);
  }

  importAccounts(source: string | NightPath): void {
    this.runCommand(["import", resolvePath(source).raw], false);
  }

  remove(accountKey: string): void {
    requireAccountKey(accountKey);
    const document = this.runJson(["remove", accountKey, "--json"]);
    const removed = document.removed;
    if (!Array.isArray(removed)) throw new AccountSchemaError("codex-auth remove result is not an account-object array");
    let matched = false;
    for (const entry of removed) {
      if (!isPlainObject(entry)) throw new AccountSchemaError("codex-auth remove entry is not an account object");
      const removedKey = entry.account_key;
      if (!validAccountKey(removedKey)) throw new AccountSchemaError("codex-auth remove entry has no stable account_key");
      matched = matched || removedKey === accountKey;
    }
    if (!matched) throw new AccountSchemaError("codex-auth remove did not confirm the requested account");
  }

  private runJson(args: string[]): JsonObject {
    const value = this.runCommand(args, true);
    if (!isPlainObject(value)) throw new AccountSchemaError("codex-auth JSON document is not an object");
    if (value.schema_version !== SUPPORTED_JSON_SCHEMA) {
      throw new AccountSchemaError(`unsupported codex-auth JSON schema: ${pythonRepr(value.schema_version)}`);
    }
    if (value.error != null) {
      const error = value.error;
      const code = isPlainObject(error) ? error.code : null;
      const label = typeof code === "string" || typeof code === "number" ? String(code) : "";
      throw new AccountUnavailable(`codex-auth operation failed: ${label || "unknown_error"}`);
    }
    return value;
  }

  private runCommand(args: string[], requireJson: boolean): JsonObject | null {
    const binary = this.resolvedBinary();
    if (binary === null) throw new AccountUnavailable("codex-auth binary is unavailable");
    const result = this.withRegistry(args, (registryLock) => {
      const stdio: Array<"ignore" | "pipe" | number> = ["ignore", "pipe", "pipe"];
      if (registryLock !== null) stdio.push(registryLock.fileno());
      let spawned: ReturnType<typeof spawnSync>;
      try {
        spawned = spawnSync(binary, args, {
          env: this.environment(),
          stdio,
          timeout: Math.max(1, Math.ceil(this.timeout * 1000)),
          maxBuffer: 8 * 1024 * 1024,
          windowsHide: true,
        });
      } catch (error) {
        const name = error instanceof Error ? spawnErrorName(error) : "OSError";
        throw withCause(new AccountUnavailable(`codex-auth command unavailable: ${name}`), error);
      }
      if (spawned.error) {
        const code = (spawned.error as NodeJS.ErrnoException).code;
        if (requireJson && (code === "ERR_CHILD_PROCESS_STDIO_MAXBUFFER" || code === "ENOBUFS")) {
          throw new AccountSchemaError("codex-auth JSON document is too large");
        }
        throw withCause(new AccountUnavailable(`codex-auth command unavailable: ${spawnErrorName(spawned.error)}`), spawned.error);
      }
      return { status: spawned.status, stdout: spawned.stdout };
    });
    const stdout = decodeStdout(result.stdout);
    if (!requireJson) {
      if (result.status !== 0) throw new AccountUnavailable("codex-auth command failed");
      return null;
    }
    if (Buffer.byteLength(stdout, "utf8") > MAX_JSON_BYTES) throw new AccountSchemaError("codex-auth JSON document is too large");
    let value: unknown;
    try {
      value = JSON.parse(stdout);
    } catch (error) {
      throw withCause(new AccountSchemaError("codex-auth stdout is not valid JSON"), error);
    }
    if (result.status !== 0 && result.status !== 1 && result.status !== 2) {
      throw new AccountUnavailable("codex-auth returned an unexpected exit status");
    }
    return isPlainObject(value) || value === null ? (value as JsonObject | null) : (value as JsonObject);
  }

  registryTransaction<T>(body: (lock: AccountRegistryLock | null) => T, operation = "registry"): T {
    if (!this.canonicalRegistry) return body(null);
    const current = this.registryContext.getStore();
    if (current) return body(current);
    return this.withRegistry([operation], (lock) => {
      if (lock === null) return body(null);
      return this.registryContext.run(lock, () => body(lock));
    });
  }

  private withRegistry<T>(args: string[], body: (lock: AccountRegistryLock | null) => T): T {
    if (!this.canonicalRegistry) return body(null);
    const current = this.registryContext.getStore();
    if (current) return body(current);
    if (this.registryLock === null) this.registryLock = new AccountRegistryLockBroker();
    const operation = args.length > 0 && typeof args[0] === "string" ? args[0] : "registry";
    const lock = this.registryLock.acquire({ operation });
    try {
      return body(lock);
    } finally {
      lock.release();
    }
  }
}

export class PoolDecision {
  constructor(
    readonly candidates: AccountCandidate[],
    readonly selected: AccountCandidate | null,
    readonly earliestReset: number | null,
  ) {}
}

export class AccountRuntime {
  lease: AccountLease | null = null;
  capsule: CapsuleLike | null = null;
  codexHome: NightPath | null;

  constructor(
    readonly coordinator: AccountPoolCoordinator,
    readonly accountKey: string,
    readonly runId: string,
    readonly repo: string | NightPath,
    readonly generation: number,
    codexHome: NightPath | null = null,
  ) {
    this.codexHome = codexHome;
  }

  enter(): this {
    this.lease = this.coordinator.leaseBroker.acquire(this.accountKey, this.runId, this.repo, { phase: "switch_prepared" });
    try {
      const targetHome = this.codexHome ?? this.coordinator.runCodexHome;
      const created = this.coordinator.capsuleFactory(
        this.coordinator.auth as CodexAuthAdapter,
        this.accountKey,
        this.runId,
        this.generation,
        this.coordinator.capsuleRoot,
        targetHome,
      );
      const opened = typeof created.enter === "function" ? (created.enter() ?? created) : created;
      this.capsule = opened;
      if (opened.codexHome == null) throw new Error("account capsule is missing codexHome");
      this.codexHome = resolvePath(opened.codexHome);
      this.lease.setPhase("switched");
      return this;
    } catch (error) {
      this.closeResources(true);
      throw error;
    }
  }

  close(): void {
    this.closeResources(false);
  }

  [Symbol.dispose](): void {
    this.close();
  }

  private closeResources(discard: boolean): void {
    let failure: unknown;
    if (this.capsule !== null) {
      try {
        closeCapsule(this.capsule, discard);
      } catch (error) {
        failure = error;
      }
      this.capsule = null;
    }
    if (this.lease !== null) {
      try {
        this.lease.release();
      } catch (error) {
        failure = failure ?? error;
      }
      this.lease = null;
    }
    if (failure !== undefined && !discard) throw failure;
  }
}

export type CapsuleFactory = (
  adapter: CodexAuthAdapter,
  accountKey: string,
  runId: string,
  generation: number,
  root?: string | NightPath | null,
  codexHome?: string | NightPath | TrustedHome | null,
) => CapsuleLike;

function defaultQuotaFactory(home: NightPath, fingerprint: string, leaseFd: number): { read(): QuotaSnapshot } {
  const loaded = require("./quota.js") as {
    AppServerQuotaProvider: new (
      binary?: string | null,
      timeout?: number,
      codexHome?: NightPath | string | null,
      accountFingerprint?: string | null,
      leaseFd?: number | null,
    ) => { read(): QuotaSnapshot };
  };
  return new loaded.AppServerQuotaProvider(undefined, undefined, home, fingerprint, leaseFd);
}

export class AccountPoolCoordinator {
  readonly auth: AccountDirectory;
  readonly leaseBroker: AccountLeaseBroker;
  readonly capsuleFactory: CapsuleFactory;
  readonly capsuleRoot: NightPath | null;
  readonly runCodexHome: NightPath | null;
  readonly quotaFactory: QuotaFactory;

  constructor(
    auth: AccountDirectory,
    leaseBroker: AccountLeaseBroker,
    options: {
      quotaFactory?: QuotaFactory | null;
      capsuleFactory?: CapsuleFactory | null;
      capsuleRoot?: string | NightPath | null;
      runCodexHome?: string | NightPath | null;
    } = {},
  ) {
    this.auth = auth;
    this.leaseBroker = leaseBroker;
    this.capsuleFactory = options.capsuleFactory ?? ((adapter, accountKey, runId, generation, root, codexHome) => AccountCapsule.create(adapter, accountKey, runId, generation, root, codexHome));
    this.capsuleRoot = options.capsuleRoot ? resolvePath(options.capsuleRoot) : null;
    this.runCodexHome = options.runCodexHome ? resolvePath(options.runCodexHome) : null;
    this.quotaFactory = options.quotaFactory ?? defaultQuotaFactory;
  }

  session(accountKey: string, runId: string, repo: string | NightPath, generation: number, codexHome?: string | NightPath | null): AccountRuntime {
    const targetHome = codexHome ? resolvePath(codexHome) : this.runCodexHome;
    return new AccountRuntime(this, accountKey, runId, repo, generation, targetHome);
  }

  activeAccount(): AccountRecord | null {
    if (typeof this.auth.activeAccount !== "function") return null;
    return this.auth.activeAccount();
  }

  probe(accountKeys: string[], runId: string, repo: string | NightPath, generation: number, excluded?: Set<string> | null): PoolDecision {
    const records = this.auth.listAccounts();
    const byKey = new Map(records.map((record) => [record.accountKey, record]));
    const missing = accountKeys.filter((key) => !byKey.has(key));
    if (missing.length) throw new AccountSchemaError("authorized account is no longer present in codex-auth registry");
    const candidates: AccountCandidate[] = [];
    const excludedKeys = excluded && excluded.size ? excluded : new Set<string>();
    for (const key of accountKeys) {
      if (excludedKeys.has(key)) {
        candidates.push(new AccountCandidate(key, null, false, true));
        continue;
      }
      const runtime = this.session(key, runId, repo, generation);
      let entered = false;
      try {
        try {
          runtime.enter();
          entered = true;
          if (runtime.lease === null || runtime.codexHome === null) throw new Error("account runtime is incomplete");
          const quota = this.quotaFactory(runtime.codexHome, accountFingerprint(key), runtime.lease.fd).read();
          runtime.lease.setPhase("quota_verified");
          candidates.push(new AccountCandidate(key, quota));
        } finally {
          if (entered) runtime.close();
        }
      } catch (error) {
        if (error instanceof AccountBusy) candidates.push(new AccountCandidate(key, null, true, false));
        else if (error instanceof AccountUnavailable) candidates.push(new AccountCandidate(key, null, false, true));
        else if (error instanceof AccountBrokerError) candidates.push(new AccountCandidate(key, null));
        else candidates.push(new AccountCandidate(key, null, false, looksLikeAuthFailure(error)));
      }
    }
    return new PoolDecision(candidates, selectBestAccount(candidates), earliestRelevantReset(candidates));
  }
}

function windowByName(snapshot: QuotaSnapshot | null, name: string) {
  if (snapshot === null) return null;
  return snapshot.windows().find((item) => item.name === name) ?? null;
}

function remaining(window: { usedPercent: number | null } | null): number | null {
  if (window === null || window.usedPercent === null) return null;
  return Math.max(0, Math.min(100, 100 - Number(window.usedPercent)));
}

function authoritative(snapshot: QuotaSnapshot | null): snapshot is QuotaSnapshot {
  return snapshot !== null && (snapshot.source === "live_app_server" || snapshot.source === "fake_file") && !snapshot.error;
}

const NO_RESET = Number.MAX_SAFE_INTEGER;

function capacity(candidate: AccountCandidate): [number, number, number, number, string] | null {
  if (candidate.leased || candidate.authError || !authoritative(candidate.quota)) return null;
  const short = remaining(windowByName(candidate.quota, "5h"));
  const weekly = remaining(windowByName(candidate.quota, "weekly"));
  if (short === null || weekly === null || short <= 0 || weekly <= 0) return null;
  const resets = candidate.quota.windows().map((item) => item.resetsAt).filter((item): item is number => item !== null);
  const nextReset = resets.length ? Math.min(...resets) : NO_RESET;
  return [Math.min(short, weekly), short, weekly, nextReset, candidate.fingerprint];
}

export function selectBestAccount(candidates: AccountCandidate[]): AccountCandidate | null {
  const ranked: Array<[[number, number, number, number, string], AccountCandidate]> = [];
  for (const candidate of candidates) {
    const key = capacity(candidate);
    if (key !== null) ranked.push([key, candidate]);
  }
  if (!ranked.length) return null;
  ranked.sort((left, right) => {
    const a = left[0];
    const b = right[0];
    if (a[0] !== b[0]) return b[0] - a[0];
    if (a[1] !== b[1]) return b[1] - a[1];
    if (a[2] !== b[2]) return b[2] - a[2];
    if (a[3] !== b[3]) return a[3] < b[3] ? -1 : 1;
    if (a[4] !== b[4]) return a[4] < b[4] ? -1 : 1;
    return 0;
  });
  return ranked[0][1];
}

export function earliestRelevantReset(candidates: AccountCandidate[]): number | null {
  const resets: number[] = [];
  for (const candidate of candidates) {
    if (!authoritative(candidate.quota)) continue;
    for (const window of candidate.quota.exhaustedWindows()) {
      if (window.resetsAt !== null) resets.push(window.resetsAt);
    }
  }
  return resets.length ? Math.min(...resets) : null;
}

export class AccountLease {
  released = false;

  constructor(
    private readonly handle: TextFile,
    private readonly lockFd: number,
    readonly path: NightPath,
    readonly metadata: JsonObject,
    private readonly pathIdentity: Identity,
  ) {}

  setPhase(phase: string): void {
    if (this.released || typeof phase !== "string" || !phase.trim()) throw new AccountBrokerError("account lease is not active");
    this.metadata.phase = phase;
    this.writeMetadata(this.metadata);
  }

  get fd(): number {
    if (this.released) throw new AccountBrokerError("account lease is not active");
    return this.lockFd;
  }

  fileno(): number {
    return this.fd;
  }

  release(): void {
    if (this.released) return;
    try {
      this.assertMetadataPath();
      const current = readLeaseMetadata(this.handle);
      if (!deepEqual(current, this.metadata)) throw new AccountSchemaError("account lease ownership changed unexpectedly");
      this.handle.seek(0);
      this.handle.truncate(0);
      this.handle.flush();
    } finally {
      try {
        flock(this.lockFd, LOCK_UN);
      } finally {
        try {
          this.handle.close();
        } finally {
          fs.closeSync(this.lockFd);
          this.released = true;
        }
      }
    }
  }

  [Symbol.dispose](): void {
    this.release();
  }

  writeMetadata(value: JsonObject): void {
    this.assertMetadataPath();
    this.handle.seek(0);
    this.handle.truncate(0);
    this.handle.write(`${canonicalJson(value)}\n`);
    this.handle.flush();
  }

  private assertMetadataPath(): void {
    let info: fs.Stats;
    try {
      info = fs.lstatSync(this.path.raw);
    } catch (error) {
      throw withCause(new AccountSchemaError("account lease metadata path disappeared"), error);
    }
    if (info.isSymbolicLink() || !info.isFile() || !identitiesEqual(identityOf(info), this.pathIdentity)) {
      throw new AccountSchemaError("account lease metadata path was replaced");
    }
  }
}

function readLeaseMetadata(handle: TextFile): JsonObject | null {
  const text = readHandle(handle);
  if (!text.trim()) return null;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw withCause(new AccountSchemaError("account lease metadata is corrupt"), error);
  }
  if (!isPlainObject(value)) throw new AccountSchemaError("account lease metadata is not an object");
  return value;
}

export class AccountLeaseBroker {
  readonly root: NightPath;
  readonly lockRoot: NightPath;
  private readonly rootIdentity: Identity;
  readonly lockRootIdentity: Identity;

  constructor(root?: string | NightPath | null) {
    const candidate = asPath(root == null || root === "" ? controlRoot().join("account-leases") : root).expanduser();
    if (pyExists(candidate) || isSymlink(candidate)) {
      const info = fs.lstatSync(candidate.raw);
      if (info.isSymbolicLink() || !info.isDirectory()) throw new AccountSchemaError("account lease root must be a real directory");
    }
    fs.mkdirSync(candidate.raw, { recursive: true, mode: 0o700 });
    const info = fs.lstatSync(candidate.raw);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new AccountSchemaError("account lease root is unsafe");
    this.root = resolvePath(candidate);
    this.rootIdentity = identityOf(info);
    this.lockRoot = this.root.parent.join(`.${this.root.name}.account-locks`);
    if (pyExists(this.lockRoot) || isSymlink(this.lockRoot)) {
      const lockInfo = fs.lstatSync(this.lockRoot.raw);
      if (lockInfo.isSymbolicLink() || !lockInfo.isDirectory()) throw new AccountSchemaError("account lease lock root must be a real directory");
    }
    fs.mkdirSync(this.lockRoot.raw, { recursive: true, mode: 0o700 });
    const lockInfo = fs.lstatSync(this.lockRoot.raw);
    if (lockInfo.isSymbolicLink() || !lockInfo.isDirectory()) throw new AccountSchemaError("account lease lock root is unsafe");
    this.lockRootIdentity = identityOf(lockInfo);
    let rootDescriptor: number | null = null;
    let lockRootDescriptor: number | null = null;
    try {
      try {
        rootDescriptor = fs.openSync(this.root.raw, DIRECTORY_FLAGS);
        const rootInfo = fs.fstatSync(rootDescriptor);
        if (!rootInfo.isDirectory() || !identitiesEqual(identityOf(rootInfo), this.rootIdentity)) {
          throw new AccountSchemaError("account lease root is unsafe");
        }
        fs.fchmodSync(rootDescriptor, 0o700);
        lockRootDescriptor = fs.openSync(this.lockRoot.raw, DIRECTORY_FLAGS);
        const openedLockRoot = fs.fstatSync(lockRootDescriptor);
        if (!openedLockRoot.isDirectory() || !identitiesEqual(identityOf(openedLockRoot), this.lockRootIdentity)) {
          throw new AccountSchemaError("account lease lock root is unsafe");
        }
        fs.fchmodSync(lockRootDescriptor, 0o700);
      } catch (error) {
        if (error instanceof AccountSchemaError) throw error;
        throw withCause(new AccountSchemaError("account lease root cannot be opened safely"), error);
      }
    } finally {
      if (rootDescriptor !== null) fs.closeSync(rootDescriptor);
      if (lockRootDescriptor !== null) fs.closeSync(lockRootDescriptor);
    }
  }

  private assertRoot(): void {
    let info: fs.Stats;
    try {
      info = fs.lstatSync(this.root.raw);
    } catch (error) {
      throw withCause(new AccountSchemaError("account lease root disappeared"), error);
    }
    if (info.isSymbolicLink() || !info.isDirectory() || !identitiesEqual(identityOf(info), this.rootIdentity)) {
      throw new AccountSchemaError("account lease root was replaced");
    }
  }

  private assertLockRoot(): void {
    let info: fs.Stats;
    try {
      info = fs.lstatSync(this.lockRoot.raw);
    } catch (error) {
      throw withCause(new AccountSchemaError("account lease lock root disappeared"), error);
    }
    if (info.isSymbolicLink() || !info.isDirectory() || !identitiesEqual(identityOf(info), this.lockRootIdentity)) {
      throw new AccountSchemaError("account lease lock root was replaced");
    }
  }

  leasePath(accountKey: string): NightPath {
    return this.root.join(`${accountFingerprint(accountKey)}.lock`);
  }

  acquire(
    accountKey: string,
    runId: string,
    repo: string | NightPath,
    options: { supervisorIdentity?: ProcessIdentity | null; phase?: string } = {},
  ): AccountLease {
    const fingerprint = accountFingerprint(accountKey);
    const leasePath = this.leasePath(accountKey);
    this.assertLockRoot();
    this.assertRoot();
    let lockRootDescriptor: number | null = null;
    let rootDescriptor: number | null = null;
    let lockFd: number | null = null;
    let handle: TextFile | null = null;
    try {
      lockRootDescriptor = fs.openSync(this.lockRoot.raw, DIRECTORY_FLAGS);
      const lockRootInfo = fs.fstatSync(lockRootDescriptor);
      if (!lockRootInfo.isDirectory() || !identitiesEqual(identityOf(lockRootInfo), this.lockRootIdentity)) {
        throw new AccountSchemaError("account lease lock root is unsafe");
      }
      this.assertLockRoot();
      try {
        fs.mkdirSync(childPath(lockRootDescriptor, fingerprint), { mode: 0o700 });
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      }
      const lockDirInfo = lstatAt(lockRootDescriptor, fingerprint);
      if (lockDirInfo.isSymbolicLink() || !lockDirInfo.isDirectory()) throw new AccountSchemaError("account lease lock directory is unsafe");
      lockFd = openAt(lockRootDescriptor, fingerprint, DIRECTORY_FLAGS);
      const openedLockDir = fs.fstatSync(lockFd);
      if (!openedLockDir.isDirectory() || !identitiesEqual(identityOf(openedLockDir), identityOf(lockDirInfo))) {
        throw new AccountSchemaError("account lease lock directory was replaced");
      }
      try {
        flock(lockFd, LOCK_EX | LOCK_NB);
      } catch (error) {
        if (error instanceof WouldBlock) throw withCause(new AccountBusy(`account ${fingerprint} is busy`), error);
        throw error;
      }
      this.assertLockRoot();
      this.assertRoot();
      rootDescriptor = fs.openSync(this.root.raw, DIRECTORY_FLAGS);
      const rootInfo = fs.fstatSync(rootDescriptor);
      if (!rootInfo.isDirectory() || !identitiesEqual(identityOf(rootInfo), this.rootIdentity)) {
        throw new AccountSchemaError("account lease root is unsafe");
      }
      this.assertRoot();
      const leaseName = leasePath.name;
      try {
        const existing = lstatAt(rootDescriptor, leaseName);
        if (existing.isSymbolicLink() || !existing.isFile()) throw new AccountSchemaError("account lease path is not a regular file");
      } catch (error) {
        if (error instanceof AccountSchemaError) throw error;
        if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      }
      let descriptor: number | null = null;
      let opened: fs.Stats;
      try {
        descriptor = openAt(rootDescriptor, leaseName, FILE_FLAGS, 0o600);
        opened = fs.fstatSync(descriptor);
        const named = lstatAt(rootDescriptor, leaseName);
        if (opened.isSymbolicLink() || !opened.isFile() || named.isSymbolicLink() || !named.isFile() || !identitiesEqual(identityOf(opened), identityOf(named))) {
          fs.closeSync(descriptor);
          descriptor = null;
          throw new AccountSchemaError("account lease path is unsafe");
        }
        handle = new TextFile(descriptor, leasePath);
        descriptor = null;
        fs.fchmodSync(handle.fileno(), 0o600);
      } catch (error) {
        if (descriptor !== null) fs.closeSync(descriptor);
        if (error instanceof AccountSchemaError) throw error;
        throw withCause(new AccountUnavailable("account lease path cannot be opened safely"), error);
      }
      const previous = readLeaseMetadata(handle);
      if (previous !== null) {
        const required = ["schema_version", "account_fingerprint", "lock_root", "run_id", "repo", "pid", "starttime", "executable", "phase"];
        const lockRoot = previous.lock_root;
        if (
          !required.every((key) => Object.prototype.hasOwnProperty.call(previous, key)) ||
          previous.schema_version !== 1 ||
          previous.account_fingerprint !== fingerprint ||
          !isPlainObject(lockRoot) ||
          !isInt(lockRoot.device) ||
          !isInt(lockRoot.inode)
        ) {
          throw new AccountSchemaError("account lease metadata is invalid");
        }
        if (lockRoot.device !== this.lockRootIdentity[0] || lockRoot.inode !== this.lockRootIdentity[1]) {
          throw new AccountSchemaError("account lease lock domain changed unexpectedly");
        }
        if (processIdentityMatches(previous)) throw new AccountBusy("account lease metadata identifies a live owner");
      }
      const identity = options.supervisorIdentity ?? linuxProcessIdentity(process.pid);
      if (identity === null) throw new AccountUnavailable("Linux PID identity is unavailable for account lease");
      const metadata: JsonObject = {
        schema_version: 1,
        account_fingerprint: fingerprint,
        lock_root: { device: this.lockRootIdentity[0], inode: this.lockRootIdentity[1] },
        run_id: String(runId),
        repo: resolvePath(repo).raw,
        pid: identity.pid,
        starttime: identity.starttime,
        executable: identity.executable,
        acquired_at: nowIso(),
        phase: options.phase ?? "selected",
      };
      const lease = new AccountLease(handle, lockFd, leasePath, metadata, identityOf(opened));
      lease.writeMetadata(metadata);
      fs.closeSync(lockRootDescriptor);
      lockRootDescriptor = null;
      fs.closeSync(rootDescriptor);
      rootDescriptor = null;
      lockFd = null;
      handle = null;
      return lease;
    } catch (error) {
      try {
        if (lockFd !== null) flock(lockFd, LOCK_UN);
      } finally {
        if (handle !== null) handle.close();
        if (lockFd !== null) fs.closeSync(lockFd);
        if (rootDescriptor !== null) fs.closeSync(rootDescriptor);
        if (lockRootDescriptor !== null) fs.closeSync(lockRootDescriptor);
      }
      throw error;
    }
  }
}

export class AccountCapsule {
  readonly adapter: CodexAuthAdapter;
  readonly accountKey: string;
  readonly root: NightPath;
  readonly allowedRoot: NightPath;
  readonly codexHome: NightPath;
  readonly manifestPath: NightPath;
  readonly exportRoot: NightPath;
  readonly syncRoot: NightPath;
  readonly capsuleAdapter: CodexAuthAdapter;
  closed = false;
  private trustedHome: TrustedHome | null;
  private homeIdentity: Identity | null = null;

  constructor(
    adapter: CodexAuthAdapter,
    accountKey: string,
    root: NightPath,
    allowedRoot: NightPath | null = null,
    codexHome: NightPath | TrustedHome | string | null = null,
    trustedHome: TrustedHome | null = null,
  ) {
    this.adapter = adapter;
    this.accountKey = accountKey;
    this.root = root;
    this.allowedRoot = allowedRoot ?? controlRoot().join("account-capsules");
    this.trustedHome = trustedHome ?? (hasVerify(codexHome) ? codexHome : null);
    this.codexHome = coerceHome(codexHome, this.root.join("codex-home"));
    this.manifestPath = this.root.join("manifest.json");
    this.exportRoot = this.root.join("canonical-export");
    this.syncRoot = this.root.join("synchronized-export");
    this.capsuleAdapter = new CodexAuthAdapter(adapter.binary, this.codexHome, adapter.timeout, null, false);
    this.validatePaths();
  }

  private validatePaths(): void {
    let info: fs.Stats;
    try {
      info = fs.lstatSync(this.root.raw);
    } catch (error) {
      throw withCause(new AccountSchemaError(`account capsule root is missing: ${this.root.raw}`), error);
    }
    if (info.isSymbolicLink() || !info.isDirectory()) {
      throw new AccountSchemaError("account capsule root must be a real directory and not a symlink");
    }
    const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW | O_CLOEXEC;
    let fd: number;
    try {
      fd = fs.openSync(this.codexHome.raw, flags);
    } catch (error) {
      throw withCause(new AccountSchemaError(`account capsule codex-home must be a real directory and not a symlink: ${this.codexHome.raw}`), error);
    }
    try {
      const stats = fs.fstatSync(fd);
      if (!stats.isDirectory()) throw new AccountSchemaError("account capsule codex-home is not a directory");
      this.homeIdentity = identityOf(stats);
    } finally {
      fs.closeSync(fd);
    }
  }

  private verifyHomeIdentity(): void {
    if (this.trustedHome !== null) {
      try {
        this.trustedHome.verify();
      } catch (error) {
        const detail = error instanceof Error ? error.message : String(error);
        throw withCause(new AccountSchemaError(`trusted codex-home verification failed: ${detail}`), error);
      }
      return;
    }
    const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW | O_CLOEXEC;
    let fd: number;
    try {
      fd = fs.openSync(this.codexHome.raw, flags);
    } catch (error) {
      throw withCause(new AccountSchemaError(`cannot securely access codex-home: directory missing or replaced with symlink: ${this.codexHome.raw}`), error);
    }
    try {
      const stats = fs.fstatSync(fd);
      if (!stats.isDirectory()) throw new AccountSchemaError("codex-home is not a directory");
      if (this.homeIdentity !== null && !identitiesEqual(identityOf(stats), this.homeIdentity)) {
        throw new AccountSchemaError("codex-home directory inode was replaced");
      }
    } finally {
      fs.closeSync(fd);
    }
  }

  private static matchesAccountSnapshot(filename: string, accountKey: string, data: JsonObject): boolean {
    if (data.account_key === accountKey) return true;
    const suffix = ".auth.json";
    const stem = filename.endsWith(suffix) ? filename.slice(0, -suffix.length) : filename;
    if (stem === accountKey || stem === accountKey.replaceAll("::", "--")) return true;
    if (![...stem].every((char) => char.charCodeAt(0) < 128)) return false;
    for (const padding of ["", "=", "=="]) {
      if (decodeBase64(stem + padding, "base64") === accountKey) return true;
      if (decodeBase64(stem + padding, "base64url") === accountKey) return true;
    }
    return false;
  }

  static create(
    adapter: CodexAuthAdapter,
    accountKey: string,
    runId: string,
    generation: number,
    root?: string | NightPath | null,
    codexHome?: string | NightPath | TrustedHome | null,
  ): AccountCapsule {
    let trustedHome: TrustedHome | null = null;
    let capsuleRoot: NightPath;
    let allowedBase: NightPath;
    let targetCodexHome: NightPath;
    if (codexHome != null) {
      if (hasVerify(codexHome)) {
        try {
          codexHome.verify();
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error);
          throw withCause(new AccountSchemaError(`trusted codex-home verification failed: ${detail}`), error);
        }
        trustedHome = codexHome;
        targetCodexHome = asPath(codexHome.path);
      } else {
        targetCodexHome = asPath(codexHome as string | NightPath);
        try {
          const info = fs.lstatSync(targetCodexHome.raw);
          if (info.isSymbolicLink() || !info.isDirectory()) {
            throw new AccountSchemaError("target codex-home must be a real directory and not a symlink");
          }
        } catch (error) {
          if (error instanceof AccountSchemaError) throw error;
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        }
      }
      capsuleRoot = targetCodexHome.parent;
      allowedBase = capsuleRoot.parent;
    } else {
      const base = asPath(root == null || root === "" ? controlRoot().join("account-capsules") : root).expanduser();
      if (pyExists(base) || isSymlink(base)) {
        const baseInfo = fs.lstatSync(base.raw);
        if (baseInfo.isSymbolicLink() || !baseInfo.isDirectory()) throw new AccountSchemaError("account capsule root must be a real directory");
      }
      fs.mkdirSync(base.raw, { recursive: true, mode: 0o700 });
      const baseInfo = fs.lstatSync(base.raw);
      if (baseInfo.isSymbolicLink() || !baseInfo.isDirectory()) throw new AccountSchemaError("account capsule root is unsafe");
      allowedBase = base;
      fs.chmodSync(allowedBase.raw, 0o700);
      capsuleRoot = allowedBase.join(String(runId).replaceAll("/", "_"));
      targetCodexHome = capsuleRoot.join("codex-home");
    }
    if (pyExists(capsuleRoot) || isSymlink(capsuleRoot)) {
      const info = fs.lstatSync(capsuleRoot.raw);
      if (info.isSymbolicLink() || !info.isDirectory()) throw new AccountSchemaError("account capsule root must be a real directory");
    }
    fs.mkdirSync(capsuleRoot.raw, { recursive: true, mode: 0o700 });
    fs.chmodSync(capsuleRoot.raw, 0o700);
    if (pyExists(targetCodexHome) || isSymlink(targetCodexHome)) {
      const info = fs.lstatSync(targetCodexHome.raw);
      if (info.isSymbolicLink() || !info.isDirectory()) throw new AccountSchemaError("account capsule codex-home must be a real directory");
    }
    fs.mkdirSync(targetCodexHome.raw, { recursive: true, mode: 0o700 });
    fs.chmodSync(targetCodexHome.raw, 0o700);
    const manifestPath = capsuleRoot.join("manifest.json");
    const authFile = targetCodexHome.join("auth.json");
    const accountsDir = targetCodexHome.join("accounts");
    if (pyExists(manifestPath) || pyExists(authFile) || pyExists(accountsDir)) {
      AccountCapsule.recoverExisting(adapter, accountKey, capsuleRoot, allowedBase, runId, generation, targetCodexHome);
    }
    const capsule = new AccountCapsule(adapter, accountKey, capsuleRoot, allowedBase, targetCodexHome, trustedHome);
    try {
      capsule.writeManifest(runId, generation);
      fs.mkdirSync(capsule.exportRoot.raw, { recursive: true, mode: 0o700 });
      fs.chmodSync(capsule.exportRoot.raw, 0o700);
      capsule.adapter.exportAccounts(capsule.exportRoot);
      hardenTree(capsule.exportRoot);
      crashHook("AFTER_CANONICAL_EXPORT");
      for (const filePath of capsule.exportRoot.iterdir()) {
        if (filePath.isFile() && filePath.name.endsWith(".auth.json")) {
          let recordData: JsonObject = {};
          try {
            recordData = JSON.parse(fs.readFileSync(filePath.raw, "utf8")) as JsonObject;
            if (!isPlainObject(recordData)) recordData = {};
          } catch {
            recordData = {};
          }
          if (!AccountCapsule.matchesAccountSnapshot(filePath.name, accountKey, recordData)) filePath.unlink();
        }
      }
      capsule.verifyHomeIdentity();
      capsule.capsuleAdapter.importAccounts(capsule.exportRoot);
      capsule.cleanRuntimeTmp();
      hardenTree(capsule.codexHome);
      crashHook("AFTER_CAPSULE_IMPORT");
      for (const record of capsule.capsuleAdapter.listAccounts()) {
        if (record.accountKey !== accountKey) capsule.capsuleAdapter.remove(record.accountKey);
      }
      const selected = capsule.capsuleAdapter.switch(accountKey);
      const records = capsule.capsuleAdapter.listAccounts();
      if (records.length !== 1 || records[0].accountKey !== accountKey || !selected.active || !records[0].active) {
        throw new AccountSchemaError("account capsule did not reconcile to the selected active account");
      }
      capsule.removeStaging(capsule.exportRoot);
      crashHook("AFTER_CAPSULE_PRUNE");
      return capsule;
    } catch (error) {
      capsule.close({ discardUnsynced: true });
      throw error;
    }
  }

  private static recoverExisting(
    adapter: CodexAuthAdapter,
    accountKey: string,
    capsuleRoot: NightPath,
    allowedRoot: NightPath,
    _runId: string,
    _generation: number,
    codexHome: NightPath | null = null,
  ): void {
    const info = fs.lstatSync(capsuleRoot.raw);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new AccountSchemaError("existing account capsule is not a real directory");
    const manifestPath = capsuleRoot.join("manifest.json");
    if (pyExists(manifestPath)) {
      const manifestInfo = fs.lstatSync(manifestPath.raw);
      if (manifestInfo.isSymbolicLink() || !manifestInfo.isFile()) throw new AccountSchemaError("existing account capsule manifest is unsafe");
      let manifest: unknown;
      try {
        manifest = JSON.parse(fs.readFileSync(manifestPath.raw, "utf8"));
      } catch (error) {
        throw withCause(new AccountSchemaError("existing account capsule manifest is corrupt"), error);
      }
      if (!isPlainObject(manifest) || manifest.schema_version !== 1) throw new AccountSchemaError("existing account capsule manifest is invalid");
      const owner = manifest.owner;
      if (!isPlainObject(owner) || !isInt(owner.pid) || typeof owner.starttime !== "string" || typeof owner.executable !== "string") {
        throw new AccountSchemaError("existing account capsule owner identity is invalid");
      }
      if (processIdentityMatches(owner) && owner.pid !== process.pid) throw new AccountBusy("existing account capsule still has a live owner");
    }
    const capsule = new AccountCapsule(adapter, accountKey, capsuleRoot, allowedRoot, codexHome);
    capsule.cleanRuntimeTmp();
    if (pyExists(capsule.codexHome)) hardenTree(capsule.codexHome);
    try {
      capsule.synchronize();
    } catch {
      /* A failed sync must not block credential scrubbing. */
    }
    capsule.scrubCredentials();
    capsule.closed = true;
  }

  private cleanRuntimeTmp(): void {
    const runtimeTmp = this.codexHome.join("tmp");
    let info: fs.Stats;
    try {
      info = fs.lstatSync(runtimeTmp.raw);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
      throw error;
    }
    if (info.isSymbolicLink()) throw new AccountSchemaError("account capsule runtime tmp is a symlink");
    if (info.isDirectory()) fs.rmSync(runtimeTmp.raw, { recursive: true, force: false });
  }

  private writeManifest(runId: string, generation: number): void {
    const identity = linuxProcessIdentity(process.pid);
    if (identity === null) throw new AccountUnavailable("Linux PID identity is unavailable for account capsule");
    const value = {
      schema_version: 1,
      account_fingerprint: accountFingerprint(this.accountKey),
      account_key: this.accountKey,
      run_id: String(runId),
      generation,
      owner: identity,
      created_at: nowIso(),
    };
    fs.writeFileSync(this.manifestPath.raw, `${pythonJson(value)}\n`, "utf8");
    fs.chmodSync(this.manifestPath.raw, 0o600);
  }

  synchronize(): void {
    if (this.closed) throw new AccountBrokerError("account capsule is closed");
    this.verifyHomeIdentity();
    const authFile = this.codexHome.join("auth.json");
    const accountsDir = this.codexHome.join("accounts");
    const registryFile = this.codexHome.join("registry.json");
    if (!pyExists(authFile) && !pyExists(accountsDir) && !pyExists(registryFile) && glob(this.codexHome, "*.auth.json").length === 0) return;
    this.adapter.activeAccount();
    this.capsuleAdapter.switch(this.accountKey);
    this.cleanRuntimeTmp();
    hardenTree(this.codexHome);
    fs.mkdirSync(this.syncRoot.raw, { recursive: true, mode: 0o700 });
    fs.chmodSync(this.syncRoot.raw, 0o700);
    crashHook("BEFORE_CAPSULE_EXPORT");
    this.capsuleAdapter.exportAccounts(this.syncRoot);
    hardenTree(this.syncRoot);
    crashHook("AFTER_CAPSULE_EXPORT");
    const exported = this.syncRoot.iterdir().filter((item) => item.isFile() && item.name.endsWith(".auth.json"));
    if (exported.length !== 1) throw new AccountSchemaError("account capsule synchronization did not produce exactly one auth snapshot");
    this.adapter.registryTransaction(() => {
      const canonicalActive = this.adapter.activeAccount();
      crashHook("BEFORE_CANONICAL_IMPORT");
      this.adapter.importAccounts(this.syncRoot);
      crashHook("AFTER_CANONICAL_IMPORT");
      if (canonicalActive !== null && canonicalActive.accountKey !== this.accountKey) this.adapter.switch(canonicalActive.accountKey);
    }, "capsule-sync");
    this.removeStaging(this.syncRoot);
  }

  scrubCredentials(): void {
    this.verifyHomeIdentity();
    const authFile = this.codexHome.join("auth.json");
    if (pyExists(authFile) || isSymlink(authFile)) unlinkMissing(authFile);
    const registryFile = this.codexHome.join("registry.json");
    if (pyExists(registryFile) || isSymlink(registryFile)) unlinkMissing(registryFile);
    const accountsDir = this.codexHome.join("accounts");
    if (pyExists(accountsDir) || isSymlink(accountsDir)) {
      if (isSymlink(accountsDir)) throw new AccountSchemaError("account capsule accounts directory is a symlink");
      fs.rmSync(accountsDir.raw, { recursive: true, force: false });
    }
    for (const authPath of glob(this.codexHome, "*.auth.json")) unlinkMissing(authPath);
    if (pyExists(this.manifestPath) || isSymlink(this.manifestPath)) unlinkMissing(this.manifestPath);
    this.removeStaging(this.exportRoot);
    this.removeStaging(this.syncRoot);
    this.cleanRuntimeTmp();
    if (pyExists(this.codexHome)) hardenTree(this.codexHome);
  }

  private removeStaging(root: NightPath): void {
    let info: fs.Stats;
    try {
      info = fs.lstatSync(root.raw);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
      throw error;
    }
    if (info.isSymbolicLink() || !info.isDirectory()) throw new AccountSchemaError("account capsule staging path is unsafe");
    hardenTree(root);
    fs.rmSync(root.raw, { recursive: true, force: false });
  }

  close(options: { discardUnsynced?: boolean } = {}): void {
    if (this.closed) return;
    const discardUnsynced = options.discardUnsynced ?? false;
    try {
      if (!discardUnsynced) this.synchronize();
    } finally {
      this.scrubCredentials();
      const sessions = this.codexHome.join("sessions");
      const hasThreadStore = pyExists(sessions) || rglob(sessions, "*.jsonl").length > 0;
      if (discardUnsynced && !hasThreadStore) this.removeCapsuleRoot();
      this.closed = true;
    }
  }

  enter(): this {
    return this;
  }

  [Symbol.dispose](): void {
    this.close();
  }

  private removeCapsuleRoot(): void {
    const parent = resolvePath(this.root.parent);
    const allowed = this.allowedRoot;
    if (parent.raw !== allowed.raw && this.root.raw !== allowed.raw && !isRelativeTo(this.root, allowed)) {
      throw new AccountSchemaError("refusing to remove an unsafe account capsule path");
    }
    this.cleanRuntimeTmp();
    hardenTree(this.root);
    fs.rmSync(this.root.raw, { recursive: true, force: false });
  }
}

function unlinkMissing(target: NightPath): void {
  try {
    fs.unlinkSync(target.raw);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
}

function isRelativeTo(child: NightPath, parent: NightPath): boolean {
  const relative = path.relative(parent.raw, child.raw);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function hardenTree(root: NightPath): void {
  const pending = [root];
  const seen: NightPath[] = [];
  while (pending.length) {
    const current = pending.pop()!;
    seen.push(current);
    let info: fs.Stats;
    try {
      info = fs.lstatSync(current.raw);
    } catch (error) {
      throw error;
    }
    if (!info.isSymbolicLink() && info.isDirectory()) {
      for (const name of fs.readdirSync(current.raw)) pending.push(current.join(name));
    }
  }
  for (const current of seen) {
    const info = fs.lstatSync(current.raw);
    if (info.isSymbolicLink()) throw new AccountSchemaError("account capsule contains a symlink");
    if (info.isDirectory()) fs.chmodSync(current.raw, 0o700);
    else if (info.isFile()) fs.chmodSync(current.raw, 0o600);
    else throw new AccountSchemaError("account capsule contains an unsupported file");
  }
}

function coerceHome(value: NightPath | TrustedHome | string | null, fallback: NightPath): NightPath {
  if (value instanceof NightPath) return value;
  if (typeof value === "string" && value) return new NightPath(value);
  if (hasVerify(value) && value.path) return asPath(value.path);
  if (value && typeof value === "object" && "path" in value) {
    const candidate = (value as { path?: unknown }).path;
    if (candidate instanceof NightPath || (typeof candidate === "string" && candidate)) return asPath(candidate);
  }
  return fallback;
}
