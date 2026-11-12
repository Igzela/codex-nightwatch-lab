import fs from "node:fs";
import path from "node:path";

import { AccountBusy, AccountSchemaError } from "./account-errors.js";
import { canonicalJson } from "./json.js";
import { NightPath, TextFile } from "./path.js";
import { LOCK_EX, LOCK_NB, LOCK_UN, WouldBlock, flock } from "./posix.js";
import { linuxProcessIdentity, processIdentityMatches } from "./process-identity.js";
import { controlPlaneRoot, nowIso } from "./storage.js";
import { crashHook } from "./testing.js";

/** Linux O_CLOEXEC. Node's fs.constants omit it; libuv also sets it on open. */
const O_CLOEXEC = 524288;
const DIRECTORY_FLAGS = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | O_CLOEXEC | fs.constants.O_NOFOLLOW;
const FILE_FLAGS = fs.constants.O_RDWR | fs.constants.O_CREAT | O_CLOEXEC | fs.constants.O_NOFOLLOW;
const METADATA_LIMIT = 64 * 1024;

const sleepSlot = new Int32Array(new SharedArrayBuffer(4));

type Identity = [number, number];

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

function monotonic(): number {
  return Number(process.hrtime.bigint()) / 1e9;
}

function sleepSync(seconds: number): void {
  const ms = Math.ceil(seconds * 1000);
  if (ms > 0) Atomics.wait(sleepSlot, 0, 0, ms);
}

function withCause<T extends Error>(error: T, cause: unknown): T {
  (error as { cause?: unknown }).cause = cause;
  return error;
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

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function deepEqual(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
    return left.every((item, index) => deepEqual(item, right[index]));
  }
  if (isPlainObject(left) && isPlainObject(right)) {
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length) return false;
    return leftKeys.every((key) => deepEqual(left[key], right[key]));
  }
  return false;
}

function keysProperSubset(value: Record<string, unknown>, required: readonly string[]): boolean {
  const keys = Object.keys(value);
  if (keys.length >= required.length) return false;
  const allowed = new Set(required);
  return keys.every((key) => allowed.has(key));
}

function readText(handle: TextFile): string {
  handle.seek(0);
  return handle.read();
}

/**
 * One kernel-held canonical registry lock with auditable ownership.
 * fileno() is the descriptor a trusted child must inherit so the flock
 * survives the parent being killed mid-command.
 */
export class AccountRegistryLock {
  released = false;

  constructor(
    private readonly handle: TextFile,
    private readonly lockFd: number,
    readonly path: NightPath,
    readonly metadata: Record<string, unknown>,
  ) {}

  enter(): this {
    return this;
  }

  fileno(): number {
    return this.lockFd;
  }

  [Symbol.dispose](): void {
    this.release();
  }

  release(): void {
    if (this.released) return;
    try {
      const current = readRegistryMetadata(this.handle);
      if (!deepEqual(current, this.metadata)) {
        throw new AccountSchemaError("canonical registry lock ownership changed unexpectedly");
      }
      this.handle.seek(0);
      this.handle.truncate(0);
      this.handle.flush();
    } finally {
      try {
        crashHook("BEFORE_REGISTRY_UNLOCK");
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
  }

  writeMetadata(value: Record<string, unknown>): void {
    this.handle.seek(0);
    this.handle.truncate(0);
    this.handle.write(`${canonicalJson(value)}\n`);
    this.handle.flush();
  }
}

function readRegistryMetadata(handle: TextFile): Record<string, unknown> | null {
  const text = readText(handle);
  if (Buffer.byteLength(text, "utf8") > METADATA_LIMIT) {
    throw new AccountSchemaError("canonical registry lock metadata is too large");
  }
  if (!text.trim()) return null;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    throw withCause(new AccountSchemaError("canonical registry lock metadata is corrupt"), error);
  }
  if (!isPlainObject(value)) throw new AccountSchemaError("canonical registry lock metadata is not an object");
  return value;
}

/**
 * Fail-closed filesystem lock for canonical codex-auth registry work.
 * The kernel lock is held on the parent directory inode, not the replaceable
 * metadata pathname.
 */
export class AccountRegistryLockBroker {
  readonly root: NightPath;
  readonly path: NightPath;
  readonly timeout: number;
  private readonly rootIdentity: Identity;
  private readonly anchor: NightPath;
  private readonly anchorIdentity: Identity;
  private pathIdentity: Identity | null;

  constructor(root?: string | NightPath | null, timeout = 10) {
    const candidate = asPath(root == null || root === "" ? (controlPlaneRoot() as string | NightPath) : root).expanduser();
    if (pyExists(candidate) || isSymlink(candidate)) {
      const info = fs.lstatSync(candidate.raw);
      if (info.isSymbolicLink() || !info.isDirectory()) {
        throw new AccountSchemaError("canonical registry lock root must be a real directory");
      }
    }
    fs.mkdirSync(candidate.raw, { recursive: true, mode: 0o700 });
    const info = fs.lstatSync(candidate.raw);
    if (info.isSymbolicLink() || !info.isDirectory()) {
      throw new AccountSchemaError("canonical registry lock root is unsafe");
    }
    this.root = resolvePath(candidate);
    this.rootIdentity = identityOf(info);
    this.anchor = this.root.parent;
    const anchorInfo = fs.lstatSync(this.anchor.raw);
    if (anchorInfo.isSymbolicLink() || !anchorInfo.isDirectory()) {
      throw new AccountSchemaError("canonical registry lock anchor must be a real directory");
    }
    this.anchorIdentity = identityOf(anchorInfo);
    this.path = this.root.join("account-registry.lock");
    try {
      const pathInfo = fs.lstatSync(this.path.raw);
      if (pathInfo.isSymbolicLink() || !pathInfo.isFile()) {
        throw new AccountSchemaError("canonical registry lock path is not a regular file");
      }
      this.pathIdentity = identityOf(pathInfo);
    } catch (error) {
      if (error instanceof AccountSchemaError) throw error;
      if ((error as NodeJS.ErrnoException).code === "ENOENT") this.pathIdentity = null;
      else throw error;
    }
    this.timeout = Math.max(0, Number(timeout));
    let rootDescriptor: number | null = null;
    try {
      rootDescriptor = fs.openSync(this.root.raw, DIRECTORY_FLAGS);
      const rootInfo = fs.fstatSync(rootDescriptor);
      if (!rootInfo.isDirectory() || !identitiesEqual(identityOf(rootInfo), this.rootIdentity)) {
        throw new AccountSchemaError("canonical registry lock root is unsafe");
      }
      fs.fchmodSync(rootDescriptor, 0o700);
    } finally {
      if (rootDescriptor !== null) fs.closeSync(rootDescriptor);
    }
  }

  private assertAnchor(): void {
    let info: fs.Stats;
    try {
      info = fs.lstatSync(this.anchor.raw);
    } catch (error) {
      throw withCause(new AccountSchemaError("canonical registry lock anchor disappeared"), error);
    }
    if (info.isSymbolicLink() || !info.isDirectory() || !identitiesEqual(identityOf(info), this.anchorIdentity)) {
      throw new AccountSchemaError("canonical registry lock anchor was replaced");
    }
  }

  private assertRoot(): void {
    let info: fs.Stats;
    try {
      info = fs.lstatSync(this.root.raw);
    } catch (error) {
      throw withCause(new AccountSchemaError("canonical registry lock root disappeared"), error);
    }
    if (info.isSymbolicLink() || !info.isDirectory() || !identitiesEqual(identityOf(info), this.rootIdentity)) {
      throw new AccountSchemaError("canonical registry lock root was replaced");
    }
  }

  acquire(options: { operation?: string; timeout?: number | null } = {}): AccountRegistryLock {
    crashHook("BEFORE_REGISTRY_LOCK");
    this.assertAnchor();
    this.assertRoot();
    let anchorDescriptor: number | null = null;
    let rootDescriptor: number | null = null;
    let handle: TextFile | null = null;
    try {
      anchorDescriptor = fs.openSync(this.anchor.raw, DIRECTORY_FLAGS);
      const anchorInfo = fs.fstatSync(anchorDescriptor);
      if (!anchorInfo.isDirectory() || !identitiesEqual(identityOf(anchorInfo), this.anchorIdentity)) {
        throw new AccountSchemaError("canonical registry lock anchor is unsafe");
      }
      this.assertAnchor();
      const limit = options.timeout == null ? this.timeout : Math.max(0, Number(options.timeout));
      const deadline = monotonic() + limit;
      while (true) {
        try {
          flock(anchorDescriptor, LOCK_EX | LOCK_NB);
          break;
        } catch (error) {
          if (!(error instanceof WouldBlock)) throw error;
          if (monotonic() >= deadline) throw withCause(new AccountBusy("canonical registry lock is busy"), error);
          sleepSync(Math.min(0.01, Math.max(0.001, deadline - monotonic())));
        }
      }
      this.assertAnchor();
      this.assertRoot();
      rootDescriptor = fs.openSync(this.root.raw, DIRECTORY_FLAGS);
      const rootInfo = fs.fstatSync(rootDescriptor);
      if (!rootInfo.isDirectory() || !identitiesEqual(identityOf(rootInfo), this.rootIdentity)) {
        throw new AccountSchemaError("canonical registry lock root is unsafe");
      }
      this.assertRoot();
      const lockName = this.path.name;
      let opened: fs.Stats | null = null;
      try {
        const existing = lstatAt(rootDescriptor, lockName);
          if (existing.isSymbolicLink() || !existing.isFile()) {
            throw new AccountSchemaError("canonical registry lock path is not a regular file");
          }
          if (this.pathIdentity !== null && !identitiesEqual(identityOf(existing), this.pathIdentity)) {
            throw new AccountSchemaError("canonical registry lock path was replaced");
          }
        } catch (error) {
          if (error instanceof AccountSchemaError) throw error;
          if ((error as NodeJS.ErrnoException).code === "ENOENT") {
            if (this.pathIdentity !== null) throw new AccountSchemaError("canonical registry lock path disappeared");
          } else {
            throw error;
          }
        }
        let descriptor: number | null = null;
        try {
          descriptor = openAt(rootDescriptor, lockName, FILE_FLAGS, 0o600);
          opened = fs.fstatSync(descriptor);
          const named = lstatAt(rootDescriptor, lockName);
          if (opened.isSymbolicLink() || !opened.isFile() || named.isSymbolicLink() || !named.isFile() || !identitiesEqual(identityOf(opened), identityOf(named))) {
            fs.closeSync(descriptor);
            descriptor = null;
            throw new AccountSchemaError("canonical registry lock path is unsafe");
          }
          handle = new TextFile(descriptor, this.path);
          descriptor = null;
          fs.fchmodSync(handle.fileno(), 0o600);
        } catch (error) {
          if (descriptor !== null) fs.closeSync(descriptor);
          if (error instanceof AccountSchemaError) throw error;
          throw withCause(new AccountSchemaError("canonical registry lock path cannot be opened safely"), error);
        }
      if (handle === null || opened === null) throw new AccountSchemaError("canonical registry lock path cannot be opened safely");
      const previous = readRegistryMetadata(handle);
      if (previous !== null) {
        const required = ["schema_version", "lock_kind", "pid", "starttime", "executable", "operation", "acquired_at"];
        if (keysProperSubset(previous, required) || previous.schema_version !== 1 || previous.lock_kind !== "canonical_registry") {
          throw new AccountSchemaError("canonical registry lock metadata is invalid");
        }
        if (processIdentityMatches(previous)) {
          throw new AccountBusy("canonical registry lock metadata identifies a live owner");
        }
      }
      const identity = linuxProcessIdentity(process.pid);
      if (identity === null) throw new AccountSchemaError("Linux PID identity is unavailable for canonical registry lock");
      const metadata = {
        schema_version: 1,
        lock_kind: "canonical_registry",
        pid: identity.pid,
        starttime: identity.starttime,
        executable: identity.executable,
        operation: String(options.operation ?? "registry").slice(0, 80),
        acquired_at: nowIso(),
      };
      const lock = new AccountRegistryLock(handle, anchorDescriptor, this.path, metadata);
      lock.writeMetadata(metadata);
      crashHook("AFTER_REGISTRY_LOCK");
      if (this.pathIdentity === null) this.pathIdentity = identityOf(opened);
      fs.closeSync(rootDescriptor);
      rootDescriptor = null;
      anchorDescriptor = null;
      handle = null;
      return lock;
    } catch (error) {
      try {
        if (anchorDescriptor !== null) flock(anchorDescriptor, LOCK_UN);
      } finally {
        if (handle !== null) handle.close();
        if (rootDescriptor !== null) fs.closeSync(rootDescriptor);
        if (anchorDescriptor !== null) fs.closeSync(anchorDescriptor);
      }
      throw error;
    }
  }
}
