import { spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import koffi from "koffi";

import { canonicalJson } from "./json.js";
import {
  type JsonObject,
  State,
  crossAccountThreadModeForVersion,
  emptyState,
  validateAgyPrintTimeout,
  validatePlan,
  validateState,
} from "./models.js";
import { NightPath } from "./path.js";
import { LOCK_EX, LOCK_NB, LOCK_UN, WouldBlock, flock } from "./posix.js";
import { crashHook } from "./testing.js";

/** Python package version imported by `storage.py` (`nightwatch.__version__`). */
const NIGHTWATCH_VERSION = "0.7.0";

export const MAX_EVENT_BYTES = 1_000_000;
export const DEFAULT_MAX_SEGMENT_BYTES = 500_000;
export const DEFAULT_MAX_SEGMENT_EVENTS = 2_000;

const CLOCK_REALTIME = 0;
const libc = koffi.load("libc.so.6");
const Timespec = koffi.struct("timespec", { tv_sec: "int64", tv_nsec: "int64" });
const clockGettime = libc.func("int clock_gettime(int clk, _Out_ timespec *tp)");

export class StateIntegrityError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "StateIntegrityError";
  }
}

export class SupervisorAlreadyRunning extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "SupervisorAlreadyRunning";
  }
}

class AttributeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AttributeError";
  }
}

/** Integer-valued JSON numbers that Python's json loader keeps as float (for example `1.0`). */
class PyFloat {
  constructor(readonly value: number) {}

  valueOf(): number {
    return this.value;
  }
}

type Identity = [number, number];

export class TrustedRunHome {
  constructor(
    readonly path: NightPath,
    readonly root: NightPath,
    readonly repoDirName: string,
    readonly runtimeIdentity: Identity,
    readonly homeIdentity: Identity,
  ) {}

  toString(): string {
    return this.path.toString();
  }

  /** Python `trusted_home / child`. */
  join(child: string): NightPath {
    return this.path.join(child);
  }

  verify(): void {
    verifyTrustedCodexHomeChain(this.root, this.repoDirName, this.runtimeIdentity, this.homeIdentity);
  }
}

export const ALLOWED_TRANSITIONS: Record<State, ReadonlySet<State>> = {
  [State.NEW]: new Set([State.NEW, State.PREFLIGHT, State.STOPPED, State.FAILED, State.BLOCKED]),
  [State.PREFLIGHT]: new Set([State.PREFLIGHT, State.RUNNING, State.WAIT_QUOTA, State.FAILED, State.BLOCKED, State.STOPPED]),
  [State.RUNNING]: new Set([
    State.RUNNING,
    State.WAIT_QUOTA,
    State.RETRY_BACKOFF,
    State.RECOVERING,
    State.VERIFYING,
    State.BLOCKED,
    State.FAILED,
    State.STOPPED,
  ]),
  [State.WAIT_QUOTA]: new Set([State.WAIT_QUOTA, State.RECOVERING, State.BLOCKED, State.STOPPED, State.FAILED]),
  [State.RETRY_BACKOFF]: new Set([State.RETRY_BACKOFF, State.RECOVERING, State.RUNNING, State.BLOCKED, State.FAILED, State.STOPPED]),
  [State.RECOVERING]: new Set([
    State.RECOVERING,
    State.RUNNING,
    State.WAIT_QUOTA,
    State.RETRY_BACKOFF,
    State.VERIFYING,
    State.BLOCKED,
    State.FAILED,
    State.STOPPED,
  ]),
  [State.VERIFYING]: new Set([
    State.VERIFYING,
    State.DONE,
    State.AWAITING_ACCEPTANCE,
    State.RUNNING,
    State.BLOCKED,
    State.FAILED,
    State.STOPPED,
  ]),
  [State.AWAITING_ACCEPTANCE]: new Set([State.AWAITING_ACCEPTANCE, State.RECOVERING, State.STOPPED]),
  [State.BLOCKED]: new Set([State.BLOCKED, State.RECOVERING, State.STOPPED]),
  [State.STOPPED]: new Set([State.STOPPED, State.RECOVERING, State.RUNNING]),
  [State.DONE]: new Set([State.DONE]),
  [State.FAILED]: new Set([State.FAILED, State.RECOVERING, State.RUNNING]),
};

const STATE_VALUES = new Set<string>(Object.values(State));
const SECRET_KEY = /(?:token|secret|password|api[_-]?key|authorization|cookie|credential)/i;

function errnoCode(error: unknown): string | undefined {
  return error && typeof error === "object" && "code" in error ? String((error as NodeJS.ErrnoException).code) : undefined;
}

function isErrno(error: unknown): boolean {
  return typeof errnoCode(error) === "string" && errnoCode(error) !== "undefined";
}

function closeQuiet(fd: number | null | undefined): void {
  if (fd === null || fd === undefined || fd < 0) return;
  try {
    fs.closeSync(fd);
  } catch {
    /* The Python paths ignore close failures while releasing descriptors. */
  }
}

function pathExists(target: NightPath): boolean {
  try {
    fs.statSync(target.raw);
    return true;
  } catch {
    return false;
  }
}

function isPlainObject(value: unknown): value is JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function getOrNull(record: JsonObject, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(record, key) ? record[key] : null;
}

function procPath(dirfd: number, name: string): string {
  return `/proc/self/fd/${dirfd}/${name}`;
}

function appendMissing(current: string, parts: string[]): string {
  let acc = current || "/";
  for (const part of parts) {
    if (part === "" || part === ".") continue;
    if (part === "..") acc = acc === "/" ? "/" : path.dirname(acc);
    else acc = acc === "/" ? `/${part}` : `${acc}/${part}`;
  }
  return acc;
}

/** pathlib.Path.resolve(): absolute, symlink-resolved, missing tail preserved. */
function resolvePath(input: NightPath, seen?: Set<string>): NightPath {
  const trail = seen ?? new Set<string>();
  let raw = input.expanduser().raw;
  if (raw === "" || raw === ".") raw = process.cwd();
  else if (!raw.startsWith("/")) raw = `${process.cwd().replace(/\/$/, "")}/${raw}`;
  if (trail.has(raw)) throw new Error(`Symlink loop from '${raw}'`);
  trail.add(raw);
  const parts = raw.split("/");
  let current = "/";
  for (let index = 1; index < parts.length; index += 1) {
    const part = parts[index] ?? "";
    if (part === "" || part === ".") continue;
    if (part === "..") {
      current = current === "/" ? "/" : path.dirname(current);
      continue;
    }
    const next = current === "/" ? `/${part}` : `${current}/${part}`;
    let info: fs.Stats;
    try {
      info = fs.lstatSync(next);
    } catch (error) {
      if (errnoCode(error) !== "ENOENT") throw error;
      return new NightPath(appendMissing(current, parts.slice(index)));
    }
    if (info.isSymbolicLink()) {
      const link = fs.readlinkSync(next);
      const linked = link.startsWith("/") ? link : `${current === "/" ? "" : current}/${link}`;
      const resolved = resolvePath(new NightPath(linked), trail);
      const remainder = parts.slice(index + 1);
      if (remainder.every((item) => item === "" || item === ".")) return resolved;
      return resolvePath(new NightPath(`${resolved.raw}/${remainder.join("/")}`), trail);
    }
    current = next;
  }
  return new NightPath(current);
}

function isLexicalRelative(child: string, parent: string): boolean {
  if (child === parent) return true;
  const prefix = parent.endsWith("/") ? parent : `${parent}/`;
  return child.startsWith(prefix);
}

function pythonPathName(value: string): string {
  const parts: string[] = [];
  for (const part of value.split("/")) {
    if (part === "" || part === ".") continue;
    parts.push(part);
  }
  return parts.length ? parts[parts.length - 1]! : "";
}

function pythonStringRepr(value: string): string {
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let out = quote;
  for (let index = 0; index < value.length; ) {
    const code = value.codePointAt(index)!;
    const ch = String.fromCodePoint(code);
    index += code > 0xffff ? 2 : 1;
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += `\\${quote}`;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (code < 32 || code === 127) out += `\\x${code.toString(16).padStart(2, "0")}`;
    else out += ch;
  }
  return `${out}${quote}`;
}

function pyRepr(value: unknown): string {
  if (typeof value === "string") return pythonStringRepr(value);
  if (value === null || value === undefined) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "bigint") return value.toString();
  if (typeof value === "number") return Object.is(value, -0) ? "-0.0" : String(value);
  if (value instanceof PyFloat) return formatPythonFloat(value.value);
  return String(value);
}

function finiteNumber(value: unknown): number {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (value instanceof PyFloat && Number.isFinite(value.value)) return value.value;
  throw new TypeError("unsupported operand type(s) for +");
}

function asIndex(value: unknown): number | null {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isInteger(value)) return value;
  if (typeof value === "bigint" && value <= BigInt(Number.MAX_SAFE_INTEGER) && value >= BigInt(Number.MIN_SAFE_INTEGER)) return Number(value);
  return null;
}

function pyEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  const leftIndex = asIndex(left);
  const rightIndex = asIndex(right);
  if (leftIndex !== null && rightIndex !== null) return leftIndex === rightIndex;
  const leftNumber = left instanceof PyFloat ? left.value : typeof left === "number" ? left : null;
  const rightNumber = right instanceof PyFloat ? right.value : typeof right === "number" ? right : null;
  if (leftNumber !== null && rightNumber !== null) return leftNumber === rightNumber;
  if (typeof left === "bigint" && typeof right === "bigint") return left === right;
  return false;
}

function positiveExactInt(value: unknown): number | null {
  if (typeof value === "boolean" || value instanceof PyFloat) return null;
  if (typeof value === "bigint") {
    if (value <= 0n || value > BigInt(Number.MAX_SAFE_INTEGER)) return null;
    return Number(value);
  }
  if (typeof value === "number" && Number.isInteger(value) && value > 0) return value;
  return null;
}

function takeCodePoints(text: string, count: number): string {
  if (text.length <= count) return text;
  let taken = 0;
  for (let index = 0; index < text.length; ) {
    if (taken === count) return text.slice(0, index);
    const code = text.codePointAt(index)!;
    index += code > 0xffff ? 2 : 1;
    taken += 1;
  }
  return text;
}

function decodeUtf8(buffer: Buffer, fatal: boolean): string {
  return new TextDecoder("utf-8", { fatal }).decode(buffer);
}

function stripAscii(buffer: Buffer): Buffer {
  let start = 0;
  let end = buffer.length;
  const whitespace = (byte: number) => byte === 9 || byte === 10 || byte === 11 || byte === 12 || byte === 13 || byte === 32;
  while (start < end && whitespace(buffer[start]!)) start += 1;
  while (end > start && whitespace(buffer[end - 1]!)) end -= 1;
  return buffer.subarray(start, end);
}

function bufferSplitLines(buffer: Buffer): Buffer[] {
  const lines: Buffer[] = [];
  let start = 0;
  for (let index = 0; index < buffer.length; index += 1) {
    const byte = buffer[index]!;
    const isBreak = byte === 10 || byte === 13 || byte === 11 || byte === 12 || byte === 0x1c || byte === 0x1d || byte === 0x1e || byte === 0x85;
    if (!isBreak) continue;
    lines.push(buffer.subarray(start, index));
    if (byte === 13 && buffer[index + 1] === 10) index += 1;
    start = index + 1;
  }
  if (start < buffer.length) lines.push(buffer.subarray(start));
  return lines;
}

function pythonSplitLines(text: string): string[] {
  const lines: string[] = [];
  let start = 0;
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    const isBreak =
      code === 10 || code === 13 || code === 11 || code === 12 || code === 0x1c || code === 0x1d || code === 0x1e || code === 0x85 || code === 0x2028 || code === 0x2029;
    if (!isBreak) continue;
    lines.push(text.slice(start, index));
    if (code === 13 && text.charCodeAt(index + 1) === 10) index += 1;
    start = index + 1;
  }
  if (start < text.length) lines.push(text.slice(start));
  return lines;
}

function parsePythonJson(input: string): unknown {
  const source = input;
  let index = 0;
  const skip = () => {
    while (index < source.length) {
      const ch = source[index];
      if (ch !== " " && ch !== "\t" && ch !== "\n" && ch !== "\r") break;
      index += 1;
    }
  };
  const parseValue = (): unknown => {
    skip();
    if (index >= source.length) throw new SyntaxError("Expecting value");
    const ch = source[index]!;
    if (ch === "{") return parseObject();
    if (ch === "[") return parseArray();
    if (ch === '"') return parseString();
    if (ch === "t") return consumeLiteral("true", true);
    if (ch === "f") return consumeLiteral("false", false);
    if (ch === "n") return consumeLiteral("null", null);
    if (ch === "-" || (ch >= "0" && ch <= "9")) return parseNumber();
    throw new SyntaxError("Expecting value");
  };
  const consumeLiteral = (literal: string, value: unknown): unknown => {
    if (source.slice(index, index + literal.length) !== literal) throw new SyntaxError("Expecting value");
    index += literal.length;
    return value;
  };
  const parseString = (): string => {
    const start = index;
    index += 1;
    while (index < source.length) {
      const ch = source[index]!;
      if (ch === "\\") {
        index += 2;
        continue;
      }
      if (ch === '"') {
        index += 1;
        return JSON.parse(source.slice(start, index)) as string;
      }
      index += 1;
    }
    throw new SyntaxError("Unterminated string");
  };
  const parseNumber = (): number | bigint | PyFloat => {
    const start = index;
    if (source[index] === "-") index += 1;
    if (source[index] === "0") index += 1;
    else if (source[index]! >= "0" && source[index]! <= "9") while (source[index]! >= "0" && source[index]! <= "9") index += 1;
    else throw new SyntaxError("Invalid number");
    let isFloat = false;
    if (source[index] === ".") {
      isFloat = true;
      index += 1;
      if (!(source[index]! >= "0" && source[index]! <= "9")) throw new SyntaxError("Invalid number");
      while (source[index]! >= "0" && source[index]! <= "9") index += 1;
    }
    if (source[index] === "e" || source[index] === "E") {
      isFloat = true;
      index += 1;
      if (source[index] === "+" || source[index] === "-") index += 1;
      if (!(source[index]! >= "0" && source[index]! <= "9")) throw new SyntaxError("Invalid number");
      while (source[index]! >= "0" && source[index]! <= "9") index += 1;
    }
    const token = source.slice(start, index);
    if (!isFloat) {
      const integer = BigInt(token);
      if (integer <= BigInt(Number.MAX_SAFE_INTEGER) && integer >= BigInt(Number.MIN_SAFE_INTEGER)) return Number(integer);
      return integer;
    }
    const value = Number(token);
    return new PyFloat(value);
  };
  const parseArray = (): unknown[] => {
    index += 1;
    skip();
    const items: unknown[] = [];
    if (source[index] === "]") {
      index += 1;
      return items;
    }
    while (true) {
      items.push(parseValue());
      skip();
      if (source[index] === "]") {
        index += 1;
        return items;
      }
      if (source[index] !== ",") throw new SyntaxError("Expecting ',' delimiter");
      index += 1;
    }
  };
  const parseObject = (): JsonObject => {
    index += 1;
    skip();
    const record: JsonObject = {};
    if (source[index] === "}") {
      index += 1;
      return record;
    }
    while (true) {
      skip();
      if (source[index] !== '"') throw new SyntaxError("Expecting property name enclosed in double quotes");
      const key = parseString();
      skip();
      if (source[index] !== ":") throw new SyntaxError("Expecting ':' delimiter");
      index += 1;
      record[key] = parseValue();
      skip();
      if (source[index] === "}") {
        index += 1;
        return record;
      }
      if (source[index] !== ",") throw new SyntaxError("Expecting ',' delimiter");
      index += 1;
    }
  };
  const value = parseValue();
  skip();
  if (index !== source.length) throw new SyntaxError("Extra data");
  return value;
}

function formatPythonFloat(value: number): string {
  if (!Number.isFinite(value)) throw new Error("Out of range float values are not JSON compliant");
  if (Object.is(value, -0)) return "-0.0";
  const negative = value < 0;
  const abs = Math.abs(value);
  let body: string;
  if (abs === 0) body = "0.0";
  else if (Number.isInteger(abs) && abs < 1e16) body = `${abs.toString()}.0`;
  else body = pythonizeJsNumber(abs);
  return negative ? `-${body}` : body;
}

function pythonizeJsNumber(abs: number): string {
  const rendered = JSON.stringify(abs);
  const scientific = /^(\d+(?:\.\d+)?)e([+-])(\d+)$/.exec(rendered);
  if (scientific) {
    const sign = scientific[2] === "-" ? "-" : "+";
    return `${scientific[1]}e${sign}${scientific[3]!.padStart(2, "0")}`;
  }
  if (/^\d+$/.test(rendered)) return fixedIntegerToScientific(rendered);
  return rendered;
}

function fixedIntegerToScientific(digits: string): string {
  const trimmed = digits.replace(/^0+/, "") || "0";
  const exponent = trimmed.length - 1;
  const fraction = trimmed.slice(1).replace(/0+$/, "");
  const mantissa = fraction.length ? `${trimmed[0]}.${fraction}` : trimmed[0]!;
  return `${mantissa}e+${String(exponent).padStart(2, "0")}`;
}

function compareKeys(left: string, right: string): number {
  const leftCodes = [...left];
  const rightCodes = [...right];
  const length = Math.min(leftCodes.length, rightCodes.length);
  for (let index = 0; index < length; index += 1) {
    const delta = leftCodes[index]!.codePointAt(0)! - rightCodes[index]!.codePointAt(0)!;
    if (delta) return delta;
  }
  return leftCodes.length - rightCodes.length;
}

function encodeString(value: string): string {
  let out = '"';
  for (let index = 0; index < value.length; ) {
    const code = value.codePointAt(index)!;
    const ch = String.fromCodePoint(code);
    index += code > 0xffff ? 2 : 1;
    if (ch === "\\") out += "\\\\";
    else if (ch === '"') out += '\\"';
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (code < 0x20) out += `\\u${code.toString(16).padStart(4, "0")}`;
    else out += ch;
  }
  return `${out}"`;
}

function dumps(value: unknown, indent: number | null = null): string {
  const encode = (current: unknown, level: number): string => {
    if (current === null) return "null";
    if (typeof current === "boolean") return current ? "true" : "false";
    if (typeof current === "bigint") return current.toString();
    if (current instanceof PyFloat) return formatPythonFloat(current.value);
    if (typeof current === "number") {
      if (!Number.isFinite(current)) throw new Error("Out of range float values are not JSON compliant");
      if (Number.isInteger(current)) {
        if (!Number.isSafeInteger(current)) throw new Error("integer is not a safe JSON integer");
        return Object.is(current, -0) ? "0" : String(current);
      }
      return formatPythonFloat(current);
    }
    if (typeof current === "string") return encodeString(current);
    if (Array.isArray(current)) {
      if (!current.length) return "[]";
      if (indent === null) return `[${current.map((item) => encode(item, level)).join(", ")}]`;
      const pad = " ".repeat(indent * (level + 1));
      const inner = current.map((item) => `${pad}${encode(item, level + 1)}`).join(",\n");
      return `[\n${inner}\n${" ".repeat(indent * level)}]`;
    }
    if (isPlainObject(current)) {
      const keys = Object.keys(current).sort(compareKeys);
      if (!keys.length) return "{}";
      if (indent === null) return `{${keys.map((key) => `${encodeString(key)}: ${encode(current[key], level)}`).join(", ")}}`;
      const pad = " ".repeat(indent * (level + 1));
      const inner = keys.map((key) => `${pad}${encodeString(key)}: ${encode(current[key], level + 1)}`).join(",\n");
      return `{\n${inner}\n${" ".repeat(indent * level)}}`;
    }
    throw new TypeError(`Object of type ${typeof current} is not JSON serializable`);
  };
  return encode(value, 0);
}

function writeAll(fd: number, text: string): void {
  const buffer = Buffer.from(text, "utf8");
  let offset = 0;
  while (offset < buffer.length) {
    const wrote = fs.writeSync(fd, buffer, offset, buffer.length - offset);
    if (wrote <= 0) throw new Error("short write");
    offset += wrote;
  }
}

function sha256Hex(buffer: Buffer | string): string {
  return createHash("sha256").update(buffer).digest("hex");
}

function pythonInt(value: string): number {
  const trimmed = value.trim();
  if (!/^[+-]?\d+$/.test(trimmed)) throw new Error(`invalid literal for int() with base 10: ${JSON.stringify(value)}`);
  const negative = trimmed.startsWith("-");
  const digits = trimmed.replace(/^[+-]/, "");
  const signed = negative ? -BigInt(digits) : BigInt(digits);
  if (signed > BigInt(Number.MAX_SAFE_INTEGER) || signed < BigInt(Number.MIN_SAFE_INTEGER)) throw new Error("int out of range");
  return Number(signed);
}

function envInt(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined) return fallback;
  return pythonInt(raw);
}

function pythonRstrip(value: string): string {
  return value.replace(/[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$/u, "");
}

function coerceState(value: unknown): State {
  if (typeof value !== "string" || !STATE_VALUES.has(value)) {
    throw new Error(`${pyRepr(value)} is not a valid State`);
  }
  return value as State;
}

function openDirDescriptor(nameOrPath: string, dirFd: number | null = null, mode = 0o700, create = false): [number, Identity] {
  const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW;
  const target = dirFd === null ? nameOrPath : procPath(dirFd, nameOrPath);
  let before: fs.Stats | null;
  try {
    before = fs.lstatSync(target);
  } catch (error) {
    if (errnoCode(error) === "ENOENT") before = null;
    else throw new StateIntegrityError(`cannot inspect entry ${nameOrPath}`, { cause: error });
  }
  if (before) {
    if (before.isSymbolicLink()) throw new StateIntegrityError(`entry ${nameOrPath} must be a real directory and not a symlink`);
    if (!before.isDirectory()) throw new StateIntegrityError(`entry ${nameOrPath} must be a directory`);
  }
  if (create && before === null) {
    try {
      if (dirFd !== null) fs.mkdirSync(target, { mode });
      else {
        const targetPath = new NightPath(nameOrPath);
        if (!pathExists(targetPath.parent)) fs.mkdirSync(targetPath.parent.raw, { recursive: true, mode });
        fs.mkdirSync(nameOrPath, { mode });
      }
    } catch (error) {
      if (errnoCode(error) !== "EEXIST") throw new StateIntegrityError(`cannot create directory ${nameOrPath}`, { cause: error });
    }
  }
  let fd = -1;
  try {
    fd = fs.openSync(target, flags);
  } catch (error) {
    throw new StateIntegrityError(`cannot securely open directory ${nameOrPath}: must be real directory and not a symlink`, { cause: error });
  }
  try {
    const info = fs.fstatSync(fd);
    if (!info.isDirectory()) throw new StateIntegrityError(`descriptor ${nameOrPath} is not a directory`);
    if (create) {
      try {
        fs.fchmodSync(fd, mode);
      } catch {
        /* Python ignores fchmod failures after a successful open. */
      }
    }
    return [fd, [info.dev, info.ino]];
  } catch (error) {
    closeQuiet(fd);
    throw error;
  }
}

export function establishTrustedCodexHome(
  root: NightPath,
  repoDirectoryName: string,
  create = true,
  expectedRuntimeIdentity: Identity | null = null,
  expectedHomeIdentity: Identity | null = null,
): [NightPath, Identity, Identity] {
  let rootFd: number | null = null;
  let dirFd: number | null = null;
  let runtimeFd: number | null = null;
  let homeFd: number | null = null;
  try {
    const rootOpened = openDirDescriptor(root.raw, null, 0o700, create);
    rootFd = rootOpened[0];
    const dirOpened = openDirDescriptor(repoDirectoryName, rootFd, 0o700, create);
    dirFd = dirOpened[0];
    const runtimeOpened = openDirDescriptor("codex-runtime", dirFd, 0o700, create);
    runtimeFd = runtimeOpened[0];
    if (expectedRuntimeIdentity && (runtimeOpened[1][0] !== expectedRuntimeIdentity[0] || runtimeOpened[1][1] !== expectedRuntimeIdentity[1])) {
      throw new StateIntegrityError("codex-runtime directory inode was replaced");
    }
    const homeOpened = openDirDescriptor("codex-home", runtimeFd, 0o700, create);
    homeFd = homeOpened[0];
    if (expectedHomeIdentity && (homeOpened[1][0] !== expectedHomeIdentity[0] || homeOpened[1][1] !== expectedHomeIdentity[1])) {
      throw new StateIntegrityError("codex-home directory inode was replaced");
    }
    return [root.join(repoDirectoryName, "codex-runtime", "codex-home"), runtimeOpened[1], homeOpened[1]];
  } finally {
    closeQuiet(homeFd);
    closeQuiet(runtimeFd);
    closeQuiet(dirFd);
    closeQuiet(rootFd);
  }
}

export function verifyTrustedCodexHomeChain(
  root: NightPath,
  repoDirectoryName: string,
  expectedRuntimeIdentity: Identity,
  expectedHomeIdentity: Identity,
): [NightPath, Identity, Identity] {
  return establishTrustedCodexHome(root, repoDirectoryName, false, expectedRuntimeIdentity, expectedHomeIdentity);
}

export function nowIso(): string {
  const spec: { tv_sec?: number | bigint; tv_nsec?: number | bigint } = {};
  const rc = clockGettime(CLOCK_REALTIME, spec) as number;
  if (rc !== 0) return new Date().toISOString();
  const seconds = Number(spec.tv_sec ?? 0);
  const micros = Math.floor(Number(spec.tv_nsec ?? 0) / 1000);
  const base = new Date(seconds * 1000).toISOString().slice(0, 19);
  if (micros === 0) return `${base}Z`;
  return `${base}.${String(micros).padStart(6, "0")}Z`;
}

export function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => redact(item));
  if (isPlainObject(value)) {
    const redacted: JsonObject = {};
    for (const key of Object.keys(value)) redacted[key] = SECRET_KEY.test(key) ? "[REDACTED]" : redact(value[key]);
    return redacted;
  }
  if (typeof value === "string") {
    let text = value.replace(/bearer\s+[A-Za-z0-9._~+/=-]+/giu, "Bearer [REDACTED]");
    text = text.replace(/\b(?:sk[-_]|sess[-_]|chatcmpl[-_]|ghp_|github_pat_|xox[baprs]-)[-_A-Za-z0-9.]{12,}/giu, "[REDACTED]");
    text = text.replace(/(?:aws_access_key_id|aws_secret_access_key|api[_-]?key|access[_-]?token|authorization|cookie)\s*[=:]\s*\S+/giu, "[REDACTED]");
    text = text.replace(/(https?:\/\/)[^\s/@:]+:[^\s/@]+@/giu, "$1[REDACTED]@");
    return takeCodePoints(text, 4000);
  }
  return value;
}

export function safeSlug(value: string): string {
  const slug = takeCodePoints(value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, ""), 80);
  return slug || "nightwatch";
}

export function controlPlaneRoot(): NightPath {
  const base = process.env.NIGHTWATCH_STATE_HOME;
  if (base) return resolvePath(new NightPath(base).expanduser());
  const xdg = process.env.XDG_STATE_HOME;
  const root = xdg ? new NightPath(xdg).expanduser() : NightPath.home().join(".local", "state");
  return root.join("codex-nightwatch");
}

function gitIdentity(repo: NightPath): string {
  try {
    const result = spawnSync("git", ["config", "--get", "remote.origin.url"], {
      cwd: repo.raw,
      encoding: "utf8",
      timeout: 3000,
      stdio: ["ignore", "pipe", "ignore"],
    });
    if (result.error || result.status !== 0) return "";
    return (result.stdout ?? "").trim();
  } catch {
    return "";
  }
}

export function repoIdentity(repo: string | NightPath): string {
  const resolved = resolvePath(repo instanceof NightPath ? repo : new NightPath(repo));
  const digest = sha256Hex(`${resolved.raw}\0${gitIdentity(resolved)}`).slice(0, 16);
  return `${safeSlug(resolved.name)}-${digest}`;
}

export function policyHash(policy: unknown): string {
  return sha256Hex(canonicalJson(policy));
}

export function sufficientVerificationPolicy(commands: string[]): boolean {
  return commands.some((command) => command.trim() !== "git diff --check");
}

function readIdentityPair(raw: unknown): Identity | null {
  if (!Array.isArray(raw) || raw.length !== 2) return null;
  const first = positiveExactInt(raw[0]);
  const second = positiveExactInt(raw[1]);
  if (first === null || second === null) return null;
  return [first, second];
}

function identitiesAreWellFormed(rawRuntime: unknown, rawHome: unknown): boolean {
  return readIdentityPair(rawRuntime) !== null && readIdentityPair(rawHome) !== null;
}

function pySetRepr(values: Set<string>): string {
  return `{${[...values].sort().map((value) => pythonStringRepr(value)).join(", ")}}`;
}

function setsEqual(left: Set<string>, right: Set<string>): boolean {
  if (left.size !== right.size) return false;
  for (const value of left) if (!right.has(value)) return false;
  return true;
}

function wrapTrustedRead(error: unknown, missing: string, invalid: string): never {
  if (error instanceof StateIntegrityError) throw error;
  if (errnoCode(error) === "ENOENT") throw new StateIntegrityError(missing, { cause: error });
  if (isErrno(error) || error instanceof SyntaxError || error instanceof TypeError || (error instanceof Error && error.name === "Error")) {
    throw new StateIntegrityError(invalid, { cause: error });
  }
  throw error;
}

function readJsonRegular(target: NightPath): unknown {
  const info = fs.lstatSync(target.raw);
  let regular = false;
  try {
    regular = fs.statSync(target.raw).isFile();
  } catch {
    regular = false;
  }
  if (!regular || info.isSymbolicLink() || info.size > MAX_EVENT_BYTES) {
    throw new StateIntegrityError(`unsafe trusted file: ${target.name}`);
  }
  return parsePythonJson(decodeUtf8(fs.readFileSync(target.raw), true));
}

export class NightwatchStore {
  static readonly MAILBOX_RUN_OUTPUTS = ["proposed-plan.json", "progress.json", "blocker.json"] as const;

  readonly repo: NightPath;
  readonly repoId: string;
  readonly root: NightPath;
  readonly directory: NightPath;
  readonly legacyDirectory: NightPath;
  readonly mailboxDirectory: NightPath;
  readonly statePath: NightPath;
  readonly goalPath: NightPath;
  readonly planPath: NightPath;
  readonly policyPath: NightPath;
  readonly acceptancePath: NightPath;
  readonly metadataPath: NightPath;
  readonly checkpointPath: NightPath;
  readonly legacyEventsPath: NightPath;
  readonly eventsDir: NightPath;
  readonly eventsManifestPath: NightPath;
  readonly logPath: NightPath;
  readonly runsPath: NightPath;
  readonly reportsPath: NightPath;
  readonly lockPath: NightPath;
  readonly supervisorLockPath: NightPath;
  readonly codexRuntimePath: NightPath;
  readonly codexHomePath: NightPath;
  private codexRuntimeIdentity: Identity | null = null;
  private codexHomeIdentity: Identity | null = null;

  constructor(repo: string | NightPath, stateHome?: string | NightPath | null) {
    this.repo = resolvePath(repo instanceof NightPath ? repo : new NightPath(repo));
    this.repoId = repoIdentity(this.repo);
    const requested = stateHome ? resolvePath(stateHome instanceof NightPath ? stateHome : new NightPath(stateHome).expanduser()) : resolvePath(controlPlaneRoot());
    this.root = requested;
    this.assertPathOutsideWorkspace(this.root);
    this.directory = this.root.join(this.repoId);
    const resolvedDirectory = resolvePath(this.directory);
    if (resolvedDirectory.raw !== this.directory.raw) {
      throw new StateIntegrityError("trusted control plane directory must not be a symlink");
    }
    this.assertPathOutsideWorkspace(resolvedDirectory);
    this.legacyDirectory = this.repo.join(".nightwatch");
    this.mailboxDirectory = this.repo.join(".nightwatch-agent");
    this.statePath = this.directory.join("state.json");
    this.goalPath = this.directory.join("goal.md");
    this.planPath = this.directory.join("plan.json");
    this.policyPath = this.directory.join("verification-policy.json");
    this.acceptancePath = this.directory.join("acceptance.json");
    this.metadataPath = this.directory.join("metadata.json");
    this.checkpointPath = this.directory.join("checkpoint.md");
    this.legacyEventsPath = this.directory.join("events.jsonl");
    this.eventsDir = this.directory.join("events");
    this.eventsManifestPath = this.eventsDir.join("manifest.json");
    this.logPath = this.directory.join("supervisor.log");
    this.runsPath = this.directory.join("runs");
    this.reportsPath = this.directory.join("reports");
    this.lockPath = this.directory.join("state.lock");
    this.supervisorLockPath = this.directory.join("supervisor.lock");
    this.codexRuntimePath = this.directory.join("codex-runtime");
    this.codexHomePath = this.codexRuntimePath.join("codex-home");
  }

  get eventsPath(): NightPath {
    if (this.hasLegacyEventsOnly()) return this.legacyEventsPath;
    return this.activeSegmentPath();
  }

  get codexHome(): NightPath {
    return this.codexHomePath;
  }

  get trustedCodexHome(): TrustedRunHome {
    if (this.codexRuntimeIdentity === null || this.codexHomeIdentity === null) this.ensureCodexHome();
    return new TrustedRunHome(this.codexHomePath, this.root, this.directory.name, this.codexRuntimeIdentity!, this.codexHomeIdentity!);
  }

  ensureCodexHome(): NightPath {
    this.assertControlPlaneOutsideWorkspace();
    this.assertPathOutsideWorkspace(this.directory);
    let expectedRuntime = this.codexRuntimeIdentity;
    let expectedHome = this.codexHomeIdentity;
    if ((expectedRuntime === null || expectedHome === null) && this.exists()) {
      const state = this.loadState();
      const rawRuntime = getOrNull(state, "codex_runtime_identity");
      const rawHome = getOrNull(state, "codex_home_identity");
      if (rawRuntime !== null || rawHome !== null) {
        if (rawRuntime === null || rawHome === null) {
          throw new StateIntegrityError("both codex_runtime_identity and codex_home_identity must be present in trusted state");
        }
        if (!identitiesAreWellFormed(rawRuntime, rawHome)) throw new StateIntegrityError("malformed codex home identity in trusted state");
        expectedRuntime = readIdentityPair(rawRuntime);
        expectedHome = readIdentityPair(rawHome);
      } else if (pathExists(this.codexRuntimePath) || pathExists(this.codexHomePath)) {
        if (state.legacy_pre_identity_migration !== true) {
          throw new StateIntegrityError("persistent codex-home exists but durable identity is absent in trusted state; failing closed");
        }
      }
    }
    const established = establishTrustedCodexHome(this.root, this.directory.name, true, expectedRuntime, expectedHome);
    this.codexRuntimeIdentity = established[1];
    this.codexHomeIdentity = established[2];
    return this.codexHomePath;
  }

  verifyCodexHome(): void {
    this.assertControlPlaneOutsideWorkspace();
    this.assertPathOutsideWorkspace(this.directory);
    let expectedRuntime = this.codexRuntimeIdentity;
    let expectedHome = this.codexHomeIdentity;
    if ((expectedRuntime === null || expectedHome === null) && this.exists()) {
      const state = this.loadState();
      const rawRuntime = getOrNull(state, "codex_runtime_identity");
      const rawHome = getOrNull(state, "codex_home_identity");
      if (rawRuntime === null || rawHome === null) {
        throw new StateIntegrityError("cannot verify codex home: missing durable identity in trusted state");
      }
      if (!identitiesAreWellFormed(rawRuntime, rawHome)) throw new StateIntegrityError("malformed codex home identity in trusted state");
      expectedRuntime = readIdentityPair(rawRuntime);
      expectedHome = readIdentityPair(rawHome);
    }
    if (expectedRuntime === null || expectedHome === null) throw new StateIntegrityError("cannot verify codex home without established identities");
    verifyTrustedCodexHomeChain(this.root, this.directory.name, expectedRuntime, expectedHome);
  }

  exists(): boolean {
    return pathExists(this.statePath);
  }

  withLock<T>(body: () => T): T {
    this.ensureDirectory();
    const handle = this.lockPath.open("a+");
    try {
      fs.chmodSync(this.lockPath.raw, 0o600);
      flock(handle.fileno(), LOCK_EX);
      return body();
    } finally {
      try {
        flock(handle.fileno(), LOCK_UN);
      } finally {
        handle.close();
      }
    }
  }

  withSupervisorLease<T>(body: () => T): T {
    this.ensureDirectory();
    const handle = this.supervisorLockPath.open("a+");
    try {
      fs.chmodSync(this.supervisorLockPath.raw, 0o600);
      try {
        flock(handle.fileno(), LOCK_EX | LOCK_NB);
      } catch (error) {
        if (error instanceof WouldBlock) throw new SupervisorAlreadyRunning("another Nightwatch supervisor owns this run", { cause: error });
        throw error;
      }
      return body();
    } finally {
      try {
        flock(handle.fileno(), LOCK_UN);
      } finally {
        handle.close();
      }
    }
  }

  initialize(
    runId: string,
    goal: string,
    repo: string,
    timestamp: string | null = null,
    verifyCommands: string[] | null = null,
    threadId: string | null = null,
    model: string | null = null,
    reasoningEffort: string | null = null,
    accountMode = "CURRENT_ONLY",
    authorizedAccounts: string[] | null = null,
    provider = "codex",
    agyPrintTimeout: string | null = null,
  ): JsonObject {
    const createdAt = timestamp || nowIso();
    const commands = [...(verifyCommands ?? [])];
    const policyCore = { schema_version: 1, source: "cli", final_commands: commands };
    const policy = { ...policyCore, policy_hash: policyHash(policyCore) };
    const acceptance: JsonObject = {
      schema_version: 1,
      goal_hash: sha256Hex(Buffer.from(goal, "utf8")),
      verification_policy_hash: policy.policy_hash,
      required_final_checks: commands,
      plan_minimum: { milestones: 1 },
      baseline_repo: this.repo.raw,
      created_at: createdAt,
    };
    let timeout = agyPrintTimeout;
    if (timeout !== null) {
      if (provider === "codex") throw new StateIntegrityError("Codex provider does not accept agy_print_timeout");
      timeout = validateAgyPrintTimeout(timeout);
    } else if (provider === "agy") timeout = "60m";
    const state = emptyState(runId, goal, resolvePath(new NightPath(repo)).raw, this.repoId, createdAt, model, reasoningEffort, provider, timeout);
    state.acceptance_ready = sufficientVerificationPolicy(commands);
    if (accountMode !== "CURRENT_ONLY" && accountMode !== "AUTO_POOL") throw new StateIntegrityError(`unsupported account mode: ${accountMode}`);
    if (provider === "agy" && accountMode === "AUTO_POOL") throw new StateIntegrityError("AGY provider does not support AUTO_POOL");
    state.account_mode = accountMode;
    state.authorized_accounts = [...(authorizedAccounts ?? [])];
    if (accountMode === "AUTO_POOL") {
      const threadMode = crossAccountThreadModeForVersion(null);
      state.cross_account_thread_mode = threadMode;
      state.cross_account_thread_capability = { codex_version: null, mode: threadMode };
    }
    if (threadId) state.thread_id = threadId;
    const profile = commands.length ? "default" : "none";
    const plan: JsonObject = {
      schema_version: 2,
      authority: "nightwatch",
      policy_hash: policy.policy_hash,
      milestones: [{ id: "M1", title: "Complete the goal", weight: 100, required: true, status: "pending", verification_profile: profile, evidence: [] }],
    };
    const metadata: JsonObject = {
      schema_version: 2,
      repo_id: this.repoId,
      repo_path: this.repo.raw,
      repo_remote: gitIdentity(this.repo),
      created_at: createdAt,
    };
    validateState(state);
    validatePlan(plan);
    this.withLock(() => {
      if (pathExists(this.statePath)) throw new StateIntegrityError("a Nightwatch run already exists in this repository");
      const mailboxFd = this.openMailboxDirectory(true);
      try {
        fs.mkdirSync(this.runsPath.raw, { mode: 0o700 });
        fs.mkdirSync(this.reportsPath.raw, { mode: 0o700 });
        fs.mkdirSync(this.eventsDir.raw, { recursive: true, mode: 0o700 });
        if (provider === "codex") {
          this.ensureCodexHome();
          if (this.codexRuntimeIdentity) state.codex_runtime_identity = [...this.codexRuntimeIdentity];
          if (this.codexHomeIdentity) state.codex_home_identity = [...this.codexHomeIdentity];
        }
        state.initialized = true;
        validateState(state);
        NightwatchStore.clearMailboxRunOutputsAt(mailboxFd);
        NightwatchStore.writeJsonAt(mailboxFd, "context.json", { goal_hash: acceptance.goal_hash, mailbox_contract: "untrusted-input-only" });
      } finally {
        fs.closeSync(mailboxFd);
      }
      NightwatchStore.writeText(this.goalPath, `${pythonRstrip(goal)}\n`);
      this.writeJson(this.policyPath, policy);
      this.writeJson(this.acceptancePath, acceptance);
      this.writeJson(this.metadataPath, metadata);
      this.writeJson(this.planPath, plan);
      NightwatchStore.writeText(this.checkpointPath, `# Nightwatch checkpoint\n\nCreated: ${createdAt}\n`);
      this.writeJson(this.statePath, state);
      this.appendEventUnlocked(state, "run_created", "trusted control plane initialized");
      this.logUnlocked("NEW: trusted control plane initialized");
    });
    return state;
  }

  loadState(): JsonObject {
    try {
      const parsed = readJsonRegular(this.statePath);
      if (!isPlainObject(parsed)) throw new AttributeError("trusted state is not an object");
      const state = parsed;
      if (!Object.prototype.hasOwnProperty.call(state, "provider")) state.provider = "codex";
      if (state.provider === "agy" && !Object.prototype.hasOwnProperty.call(state, "agy_print_timeout")) {
        state.agy_print_timeout = "60m";
        this.writeJson(this.statePath, state);
      }
      validateState(state);
      if (state.repo !== this.repo.raw || state.repo_id !== this.repoId) {
        throw new StateIntegrityError(
          `trusted state does not belong to this repository identity: state_repo=${pyRepr(state.repo)} self_repo=${pyRepr(this.repo.raw)} state_repo_id=${pyRepr(state.repo_id)} self_repo_id=${pyRepr(this.repoId)}`,
        );
      }
      this.validateEventFrontier();
      return state;
    } catch (error) {
      wrapTrustedRead(error, "trusted state.json is missing", "trusted state.json is unreadable or invalid");
    }
  }

  loadPlan(): JsonObject {
    try {
      const parsed = readJsonRegular(this.planPath);
      if (!isPlainObject(parsed)) throw new AttributeError("trusted plan is not an object");
      validatePlan(parsed);
      if (!pyEqual(parsed.policy_hash, this.loadPolicy().policy_hash)) throw new StateIntegrityError("plan is not bound to the frozen verification policy");
      return parsed;
    } catch (error) {
      wrapTrustedRead(error, "trusted plan.json is missing", "trusted plan.json is unreadable or invalid");
    }
  }

  loadPolicy(): JsonObject {
    try {
      const parsed = readJsonRegular(this.policyPath);
      if (!isPlainObject(parsed)) throw new AttributeError("verification policy is not an object");
      const core = { schema_version: getOrNull(parsed, "schema_version"), source: getOrNull(parsed, "source"), final_commands: getOrNull(parsed, "final_commands") };
      const commands = parsed.final_commands;
      const commandsOk = Array.isArray(commands) && commands.every((item) => typeof item === "string" && item.trim().length > 0);
      if (!pyEqual(parsed.schema_version, 1) || !commandsOk || parsed.policy_hash !== policyHash(core)) {
        throw new StateIntegrityError("frozen verification policy is invalid");
      }
      return parsed;
    } catch (error) {
      wrapTrustedRead(error, "frozen verification policy is missing", "frozen verification policy is unreadable");
    }
  }

  loadAcceptance(): JsonObject {
    try {
      const parsed = readJsonRegular(this.acceptancePath);
      if (!isPlainObject(parsed) || !pyEqual(parsed.schema_version, 1) || !pyEqual(parsed.verification_policy_hash, this.loadPolicy().policy_hash)) {
        throw new StateIntegrityError("trusted acceptance policy is invalid");
      }
      return parsed;
    } catch (error) {
      if (error instanceof StateIntegrityError) throw error;
      if (errnoCode(error) === "ENOENT") throw new StateIntegrityError("trusted acceptance policy is missing", { cause: error });
      throw error;
    }
  }

  savePlan(plan: JsonObject): void {
    validatePlan(plan);
    if (!pyEqual(plan.policy_hash, this.loadPolicy().policy_hash)) throw new StateIntegrityError("cannot save a plan not bound to frozen policy");
    this.withLock(() => {
      this.writeJson(this.planPath, plan);
    });
  }

  transition(target: State | string, event: string, reason: string, changes: JsonObject | null = null, timestamp: string | null = null): JsonObject {
    const targetState = coerceState(target);
    const at = timestamp || nowIso();
    return this.withLock(() => {
      const current = this.loadState();
      const currentState = coerceState(current.state);
      if (!ALLOWED_TRANSITIONS[currentState].has(targetState)) {
        throw new StateIntegrityError(`invalid state transition ${currentState} -> ${targetState}`);
      }
      const nextState: JsonObject = { ...current, ...(changes ?? {}), state: targetState, updated_at: at, last_event: event };
      validateState(nextState);
      this.writeJson(this.statePath, nextState);
      crashHook("AFTER_STATE_WRITE");
      this.appendEventUnlocked(nextState, event, reason);
      this.logUnlocked(`${targetState}: ${reason}`);
      return nextState;
    });
  }

  mutate(event: string, reason: string, mutator: (state: JsonObject) => JsonObject, timestamp: string | null = null): JsonObject {
    const at = timestamp || nowIso();
    return this.withLock(() => {
      const state = this.loadState();
      const nextState: JsonObject = { ...mutator({ ...state }), updated_at: at, last_event: event };
      validateState(nextState);
      this.writeJson(this.statePath, nextState);
      crashHook("AFTER_STATE_WRITE");
      this.appendEventUnlocked(nextState, event, reason);
      this.logUnlocked(`${String(nextState.state)}: ${reason}`);
      return nextState;
    });
  }

  appendEvent(event: string, reason: string, metadata: JsonObject | null = null): void {
    this.withLock(() => {
      const state = this.loadState();
      this.appendEventUnlocked(state, event, reason, metadata);
      this.logUnlocked(`${String(state.state)}: ${reason}`);
    });
  }

  loadEvents(): JsonObject[] {
    this.validateEventFrontier();
    if (this.hasLegacyEventsOnly()) return this.legacyEventItems();
    if (!pathExists(this.eventsManifestPath)) return [];
    const parsed = readJsonRegular(this.eventsManifestPath);
    if (!isPlainObject(parsed)) throw new StateIntegrityError("trusted events manifest is invalid");
    const events: unknown[] = [];
    const segments = Array.isArray(parsed.segments) ? parsed.segments : [];
    for (const segment of segments) {
      if (!isPlainObject(segment)) throw new StateIntegrityError("invalid segment entry in manifest");
      const segmentPath = segment.is_legacy_root ? this.legacyEventsPath : this.eventsDir.join(String(segment.name));
      const raw = this.readSegmentBody(segmentPath);
      if (getOrNull(segment, "sha256") !== null) {
        const digest = sha256Hex(raw);
        if (digest !== segment.sha256) throw new StateIntegrityError(`trusted event segment ${String(segment.name)} digest mismatch`);
      }
      let text: string;
      try {
        text = decodeUtf8(raw, true);
      } catch (error) {
        throw error;
      }
      for (const line of pythonSplitLines(text)) {
        if (!line.trim()) continue;
        try {
          events.push(parsePythonJson(line));
        } catch (error) {
          if (error instanceof SyntaxError) throw new StateIntegrityError(`trusted event segment ${String(segment.name)} is corrupt`, { cause: error });
          throw error;
        }
      }
    }
    let previous = 0;
    for (const item of events) {
      const seq = isPlainObject(item) ? getOrNull(item, "seq") : null;
      const seqIndex = asIndex(seq);
      if (seqIndex === null || seqIndex !== previous + 1) throw new StateIntegrityError("trusted event log sequence is corrupt");
      previous = seqIndex;
    }
    return events.filter(isPlainObject);
  }

  log(message: string): void {
    this.withLock(() => this.logUnlocked(message));
  }

  writeRunEvent(generation: number, value: JsonObject): NightPath {
    fs.mkdirSync(this.runsPath.raw, { recursive: true, mode: 0o700 });
    const target = this.runsPath.join(`generation-${generation}.events.jsonl`);
    this.appendFile(target, `${dumps(redact(value))}\n`);
    return target;
  }

  loadRunEvents(generation: number): unknown[] {
    const target = this.runsPath.join(`generation-${generation}.events.jsonl`);
    if (!pathExists(target)) return [];
    const text = decodeUtf8(fs.readFileSync(target.raw), true);
    const items: unknown[] = [];
    for (const line of pythonSplitLines(text)) {
      const trimmed = line.trim();
      if (trimmed) items.push(parsePythonJson(trimmed));
    }
    return items;
  }

  writeReport(text: string, timestamp: string | null = null): NightPath {
    const stamp = (timestamp || nowIso()).replaceAll(":", "-").replaceAll(".", "-");
    const target = this.reportsPath.join(`report-${stamp}.md`);
    NightwatchStore.writeText(target, text);
    NightwatchStore.writeText(this.reportsPath.join("latest.md"), text);
    return target;
  }

  readMailboxFile(name: string): Buffer | null {
    const safe = NightwatchStore.mailboxName(name);
    let directoryFd: number;
    try {
      directoryFd = this.openMailboxDirectory(false);
    } catch (error) {
      if (error instanceof StateIntegrityError && error.message.includes("is missing")) return null;
      throw error;
    }
    try {
      const flags = fs.constants.O_RDONLY | fs.constants.O_NONBLOCK | fs.constants.O_NOFOLLOW;
      let descriptor: number;
      try {
        descriptor = fs.openSync(procPath(directoryFd, safe), flags);
      } catch (error) {
        if (errnoCode(error) === "ENOENT") return null;
        throw error;
      }
      try {
        const opened = fs.fstatSync(descriptor);
        if (!opened.isFile() || opened.size > MAX_EVENT_BYTES) throw new StateIntegrityError("unsafe Nightwatch mailbox file");
        const limited = Math.min(opened.size, MAX_EVENT_BYTES + 1);
        const buffer = Buffer.alloc(limited);
        let offset = 0;
        while (offset < buffer.length) {
          const count = fs.readSync(descriptor, buffer, offset, buffer.length - offset, offset);
          if (count === 0) break;
          offset += count;
        }
        return buffer.subarray(0, offset);
      } finally {
        closeQuiet(descriptor);
      }
    } catch (error) {
      if (error instanceof StateIntegrityError) throw error;
      if (isErrno(error)) throw new StateIntegrityError("Nightwatch mailbox file is unsafe", { cause: error });
      throw error;
    } finally {
      fs.closeSync(directoryFd);
    }
  }

  private ensureDirectory(): void {
    this.assertControlPlaneOutsideWorkspace();
    this.assertPathOutsideWorkspace(this.directory);
    let rootFd: number | null = null;
    let dirFd: number | null = null;
    try {
      rootFd = openDirDescriptor(this.root.raw, null, 0o700, true)[0];
      dirFd = openDirDescriptor(this.directory.name, rootFd, 0o700, true)[0];
    } finally {
      closeQuiet(dirFd);
      closeQuiet(rootFd);
    }
  }

  private assertControlPlaneOutsideWorkspace(): void {
    this.assertPathOutsideWorkspace(this.root);
  }

  private assertPathOutsideWorkspace(target: NightPath): void {
    if (target.raw === this.repo.raw || isLexicalRelative(target.raw, this.repo.raw)) {
      throw new StateIntegrityError("trusted control plane must be outside the Codex workspace");
    }
  }

  private openMailboxDirectory(create = false): number {
    const flags = fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW;
    if (create) {
      try {
        fs.mkdirSync(this.mailboxDirectory.raw, { mode: 0o700 });
      } catch (error) {
        if (errnoCode(error) !== "EEXIST") throw new StateIntegrityError("cannot create the Nightwatch mailbox directory", { cause: error });
      }
    }
    let info: fs.Stats;
    try {
      info = fs.lstatSync(this.mailboxDirectory.raw);
    } catch (error) {
      if (errnoCode(error) === "ENOENT") throw new StateIntegrityError("Nightwatch mailbox directory is missing", { cause: error });
      throw new StateIntegrityError("Nightwatch mailbox directory cannot be inspected", { cause: error });
    }
    if (info.isSymbolicLink() || !info.isDirectory()) throw new StateIntegrityError("Nightwatch mailbox directory must be a real directory");
    let descriptor = -1;
    try {
      descriptor = fs.openSync(this.mailboxDirectory.raw, flags);
      const opened = fs.fstatSync(descriptor);
      if (!opened.isDirectory()) {
        closeQuiet(descriptor);
        throw new StateIntegrityError("Nightwatch mailbox directory is not a directory");
      }
      return descriptor;
    } catch (error) {
      if (error instanceof StateIntegrityError) throw error;
      closeQuiet(descriptor);
      throw new StateIntegrityError("Nightwatch mailbox directory is unsafe", { cause: error });
    }
  }

  private static mailboxName(name: string): string {
    if (typeof name !== "string" || !name || pythonPathName(name) !== name) throw new StateIntegrityError("invalid Nightwatch mailbox filename");
    return name;
  }

  private static clearMailboxRunOutputsAt(directoryFd: number): void {
    for (const name of NightwatchStore.MAILBOX_RUN_OUTPUTS) {
      try {
        fs.unlinkSync(procPath(directoryFd, name));
      } catch (error) {
        if (errnoCode(error) === "ENOENT") continue;
        throw new StateIntegrityError(`cannot clear stale Nightwatch mailbox output: ${name}`, { cause: error });
      }
    }
    fs.fsyncSync(directoryFd);
  }

  private static writeJsonAt(directoryFd: number, name: string, value: unknown): void {
    NightwatchStore.writeTextAt(directoryFd, name, `${dumps(redact(value), 2)}\n`);
  }

  private static writeTextAt(directoryFd: number, name: string, text: string): void {
    const temporary = `.${name}.${randomBytes(12).toString("hex")}.tmp`;
    const flags = fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW;
    let descriptor = -1;
    try {
      descriptor = fs.openSync(procPath(directoryFd, temporary), flags, 0o600);
      writeAll(descriptor, text);
      fs.fsyncSync(descriptor);
      closeQuiet(descriptor);
      descriptor = -1;
      fs.renameSync(procPath(directoryFd, temporary), procPath(directoryFd, name));
      fs.fsyncSync(directoryFd);
    } finally {
      closeQuiet(descriptor);
      try {
        fs.unlinkSync(procPath(directoryFd, temporary));
      } catch (error) {
        if (errnoCode(error) !== "ENOENT") throw error;
      }
    }
  }

  private hasLegacyEventsOnly(): boolean {
    return pathExists(this.legacyEventsPath) && !pathExists(this.eventsManifestPath);
  }

  private activeSegmentName(): string {
    if (pathExists(this.eventsManifestPath)) {
      const manifest = readJsonRegular(this.eventsManifestPath);
      if (!isPlainObject(manifest)) throw new StateIntegrityError("trusted events manifest is invalid");
      const current = manifest.current_segment;
      if (typeof current === "string" && current) return current;
      throw new StateIntegrityError("trusted events manifest current_segment is invalid");
    }
    return "segment-000001.jsonl";
  }

  private activeSegmentPath(): NightPath {
    return this.eventsDir.join(this.activeSegmentName());
  }

  private maxSegmentBytes(): number {
    return envInt("NIGHTWATCH_MAX_SEGMENT_BYTES", DEFAULT_MAX_SEGMENT_BYTES);
  }

  private readSegmentBody(target: NightPath): Buffer {
    return fs.readFileSync(target.raw);
  }

  private static createEmptySegment(target: NightPath): void {
    fs.mkdirSync(target.parent.raw, { recursive: true, mode: 0o700 });
    const flags = fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW;
    const fd = fs.openSync(target.raw, flags, 0o600);
    fs.closeSync(fd);
  }

  private static readLastJsonLine(target: NightPath, fileSize: number): unknown | null {
    const readSize = Math.min(fileSize, 4096);
    const fd = fs.openSync(target.raw, "r");
    try {
      const chunk = Buffer.alloc(readSize);
      fs.readSync(fd, chunk, 0, readSize, fileSize - readSize);
      const lines = bufferSplitLines(chunk);
      for (let index = lines.length - 1; index >= 0; index -= 1) {
        const stripped = stripAscii(lines[index]!);
        if (!stripped.length) continue;
        const line = decodeUtf8(stripped, false);
        if (!line) continue;
        try {
          return parsePythonJson(line);
        } catch (error) {
          if (error instanceof SyntaxError) throw new StateIntegrityError("active segment tail is malformed JSON", { cause: error });
          throw error;
        }
      }
      return null;
    } finally {
      fs.closeSync(fd);
    }
  }

  private getOrInitManifestUnlocked(): JsonObject {
    if (pathExists(this.eventsManifestPath)) {
      const manifest = readJsonRegular(this.eventsManifestPath);
      if (!isPlainObject(manifest)) throw new StateIntegrityError("trusted events manifest is invalid");
      return manifest;
    }
    return { schema_version: 1, last_seq: 0, current_segment: "segment-000001.jsonl", segments: [] };
  }

  private migrateLegacyEventsUnlocked(): void {
    const info = fs.lstatSync(this.legacyEventsPath.raw);
    if (info.isSymbolicLink() || !info.isFile()) throw new StateIntegrityError("legacy events.jsonl is not a regular file");
    if (info.size > MAX_EVENT_BYTES) throw new StateIntegrityError("legacy events.jsonl exceeds maximum allowed size");
    const events: unknown[] = [];
    const text = decodeUtf8(fs.readFileSync(this.legacyEventsPath.raw), true);
    for (const line of pythonSplitLines(text)) {
      if (line.trim()) events.push(parsePythonJson(line));
    }
    let previous = 0;
    for (const item of events) {
      const seq = isPlainObject(item) ? getOrNull(item, "seq") : null;
      const seqIndex = asIndex(seq);
      if (seqIndex === null || seqIndex !== previous + 1) throw new StateIntegrityError("legacy events.jsonl sequence is corrupt");
      previous = seqIndex;
    }
    const count = events.length;
    const legacyBytes = this.readSegmentBody(this.legacyEventsPath);
    const legacySegment: JsonObject = {
      name: "events.jsonl",
      is_legacy_root: true,
      seq_start: 1,
      seq_end: count,
      event_count: count,
      byte_size: info.size,
      sha256: sha256Hex(legacyBytes),
      prev_sha256: null,
    };
    const manifest: JsonObject = { schema_version: 1, last_seq: count, current_segment: "events.jsonl", segments: [legacySegment] };
    fs.mkdirSync(this.eventsDir.raw, { recursive: true, mode: 0o700 });
    this.writeJson(this.eventsManifestPath, manifest);
  }

  private appendEventUnlocked(state: JsonObject, event: string, reason: string, metadata: JsonObject | null = null): void {
    fs.mkdirSync(this.eventsDir.raw, { recursive: true, mode: 0o700 });
    if (this.hasLegacyEventsOnly()) this.migrateLegacyEventsUnlocked();
    const manifest = this.getOrInitManifestUnlocked();
    const nextSeq = finiteNumber(manifest.last_seq) + 1;
    const item: JsonObject = {
      seq: nextSeq,
      ts: state.updated_at,
      event,
      reason,
      run_id: state.run_id,
      state: state.state,
      generation: getOrNull(state, "generation"),
      thread_id: getOrNull(state, "thread_id"),
      repo: getOrNull(state, "repo"),
      git_head: getOrNull(state, "last_git_head"),
    };
    if (metadata && Object.keys(metadata).length > 0) item.metadata = metadata;
    const lineText = `${dumps(redact(item))}\n`;
    const lineBytes = Buffer.byteLength(lineText, "utf8");
    const segments = manifest.segments;
    if (!Array.isArray(segments)) throw new TypeError("manifest segments are not a list");
    let active = segments.length ? segments[segments.length - 1] : null;
    if (active !== null && !isPlainObject(active)) throw new TypeError("manifest segment is not an object");
    let needRotation = false;
    if (active === null || active.is_legacy_root) needRotation = true;
    else {
      const activePath = this.eventsDir.join(String(active.name));
      const currentSize = pathExists(activePath) ? fs.lstatSync(activePath.raw).size : 0;
      const maxEvents = envInt("NIGHTWATCH_MAX_SEGMENT_EVENTS", DEFAULT_MAX_SEGMENT_EVENTS);
      const count = finiteNumber(Object.prototype.hasOwnProperty.call(active, "event_count") ? active.event_count : 0);
      if (currentSize + lineBytes > this.maxSegmentBytes() || count >= maxEvents) needRotation = true;
    }
    if (needRotation) {
      let prevHash: unknown = null;
      if (active !== null) {
        if (active.is_legacy_root) prevHash = getOrNull(active, "sha256");
        else {
          const activePath = this.eventsDir.join(String(active.name));
          const activeBytes = this.readSegmentBody(activePath);
          prevHash = sha256Hex(activeBytes);
          active.sha256 = prevHash;
          active.byte_size = fs.lstatSync(activePath.raw).size;
        }
      }
      const segNum = segments.filter((segment) => isPlainObject(segment) && !segment.is_legacy_root).length + 1;
      const newName = `segment-${String(segNum).padStart(6, "0")}.jsonl`;
      const newSegment: JsonObject = {
        name: newName,
        seq_start: nextSeq,
        seq_end: nextSeq,
        event_count: 0,
        byte_size: 0,
        sha256: null,
        prev_sha256: prevHash,
      };
      segments.push(newSegment);
      manifest.current_segment = newName;
      active = newSegment;
      NightwatchStore.createEmptySegment(this.eventsDir.join(newName));
      crashHook("AFTER_SEGMENT_CREATE");
    }
    if (!isPlainObject(active)) throw new TypeError("active segment is not an object");
    const activePath = this.eventsDir.join(String(active.name));
    this.appendFile(activePath, lineText);
    crashHook("AFTER_EVENT_APPEND");
    active.seq_end = nextSeq;
    const previousCount = finiteNumber(Object.prototype.hasOwnProperty.call(active, "event_count") ? active.event_count : 0);
    active.event_count = previousCount + 1;
    active.byte_size = fs.lstatSync(activePath.raw).size;
    manifest.last_seq = nextSeq;
    this.writeJson(this.eventsManifestPath, manifest);
    if (needRotation) crashHook("AFTER_ROTATION_MANIFEST_COMMIT");
  }

  private reconcileOrphanSegmentUnlocked(manifest: JsonObject, unexpectedEntries: Set<string>): void {
    if (unexpectedEntries.size !== 1) {
      throw new StateIntegrityError("events directory entries do not match manifest: multiple unexpected entries");
    }
    const orphanName = [...unexpectedEntries][0]!;
    if (typeof orphanName !== "string" || pythonPathName(orphanName) !== orphanName || orphanName.includes("/") || orphanName.includes("\\") || orphanName.startsWith(".")) {
      throw new StateIntegrityError("events directory entries do not match manifest: invalid orphan entry name");
    }
    const segments = manifest.segments;
    if (!Array.isArray(segments) || !segments.length) {
      throw new StateIntegrityError("events directory entries do not match manifest: manifest segments invalid");
    }
    const nextSegNum = segments.filter((segment) => isPlainObject(segment) && !segment.is_legacy_root).length + 1;
    const expectedNextName = `segment-${String(nextSegNum).padStart(6, "0")}.jsonl`;
    if (orphanName !== expectedNextName) {
      throw new StateIntegrityError(`events directory entries do not match manifest: unexpected entry ${orphanName}`);
    }
    const orphanPath = this.eventsDir.join(orphanName);
    let orphanStat: fs.Stats;
    try {
      orphanStat = fs.lstatSync(orphanPath.raw);
    } catch (error) {
      throw new StateIntegrityError(`cannot inspect orphan entry ${orphanName}`, { cause: error });
    }
    if (orphanStat.isSymbolicLink()) throw new StateIntegrityError(`orphan segment ${orphanName} is a symlink`);
    if (!orphanStat.isFile()) throw new StateIntegrityError(`orphan segment ${orphanName} must be a regular file`);
    const prevSeg = segments[segments.length - 1];
    if (!isPlainObject(prevSeg)) throw new StateIntegrityError("events directory entries do not match manifest: manifest segments invalid");
    const prevPath = prevSeg.is_legacy_root ? this.legacyEventsPath : this.eventsDir.join(String(prevSeg.name));
    let prevStat: fs.Stats;
    try {
      prevStat = fs.lstatSync(prevPath.raw);
    } catch (error) {
      throw new StateIntegrityError(`cannot inspect previous active segment ${String(prevSeg.name)}`, { cause: error });
    }
    if (prevStat.isSymbolicLink() || !prevStat.isFile()) {
      throw new StateIntegrityError(`previous active segment ${String(prevSeg.name)} must be an intact regular file`);
    }
    if (!pyEqual(prevStat.size, prevSeg.byte_size)) throw new StateIntegrityError("previous active segment size modified");
    if (prevStat.size > 0) {
      const lastLine = NightwatchStore.readLastJsonLine(prevPath, prevStat.size);
      if (!isPlainObject(lastLine) || !pyEqual(getOrNull(lastLine, "seq"), getOrNull(manifest, "last_seq"))) {
        throw new StateIntegrityError("previous active segment tail does not match manifest last_seq");
      }
    }
    if (getOrNull(prevSeg, "sha256") !== null) {
      const prevBytes = this.readSegmentBody(prevPath);
      if (sha256Hex(prevBytes) !== prevSeg.sha256) throw new StateIntegrityError("previous active segment digest mismatch");
    }
    const orphanSize = orphanStat.size;
    if (orphanSize > this.maxSegmentBytes() + 65_536) throw new StateIntegrityError("orphan segment exceeds maximum segment limit");
    const discard = (message: string) => {
      try {
        fs.unlinkSync(orphanPath.raw);
      } catch (error) {
        throw new StateIntegrityError(message, { cause: error });
      }
    };
    if (orphanSize === 0) {
      discard(`cannot remove empty orphan segment ${orphanName}`);
      return;
    }
    let raw: Buffer;
    try {
      raw = this.readSegmentBody(orphanPath);
    } catch (error) {
      throw new StateIntegrityError(`cannot read orphan segment ${orphanName}`, { cause: error });
    }
    if (!stripAscii(raw).length) {
      discard(`cannot remove empty orphan segment ${orphanName}`);
      return;
    }
    if (raw[raw.length - 1] !== 0x0a) {
      if (raw.includes(0x0a)) throw new StateIntegrityError("orphan segment contains unexpected multiple records or trailing data");
      if (stripAscii(raw)[0] !== 0x7b) throw new StateIntegrityError("orphan segment contains malformed non-JSON data");
      discard(`cannot discard uncommitted orphan segment ${orphanName}`);
      return;
    }
    const lines = bufferSplitLines(raw).filter((line) => stripAscii(line).length > 0);
    if (lines.length !== 1) throw new StateIntegrityError("orphan segment contains multiple records");
    let item: unknown;
    try {
      item = parsePythonJson(decodeUtf8(lines[0]!, true));
    } catch (error) {
      if (error instanceof SyntaxError || error instanceof TypeError) {
        throw new StateIntegrityError("orphan segment contains malformed JSON record", { cause: error });
      }
      throw error;
    }
    if (!isPlainObject(item)) throw new StateIntegrityError("orphan segment event must be a JSON object");
    const eventSeq = asIndex(getOrNull(item, "seq"));
    const expectedSeq = asIndex(manifest.last_seq);
    if (eventSeq === null || expectedSeq === null || eventSeq !== expectedSeq + 1) {
      throw new StateIntegrityError(`orphan segment seq ${pyRepr(getOrNull(item, "seq"))} does not match expected ${(expectedSeq ?? 0) + 1}`);
    }
    const eventName = item.event;
    if (typeof eventName !== "string" || !eventName) throw new StateIntegrityError("orphan segment event missing event type");
    let prevHash: unknown;
    if (getOrNull(prevSeg, "sha256") === null) {
      prevHash = sha256Hex(this.readSegmentBody(prevPath));
      prevSeg.sha256 = prevHash;
      prevSeg.byte_size = prevStat.size;
    } else prevHash = prevSeg.sha256;
    segments.push({
      name: orphanName,
      seq_start: eventSeq,
      seq_end: eventSeq,
      event_count: 1,
      byte_size: orphanSize,
      sha256: null,
      prev_sha256: prevHash,
    });
    manifest.current_segment = orphanName;
    manifest.last_seq = eventSeq;
    this.writeJson(this.eventsManifestPath, manifest);
    crashHook("AFTER_ROTATION_MANIFEST_COMMIT");
  }

  private validateEventFrontier(): void {
    if (this.hasLegacyEventsOnly()) {
      this.validateLegacyEvents();
      return;
    }
    if (!pathExists(this.eventsManifestPath)) {
      if (this.exists()) throw new StateIntegrityError("trusted event log is missing");
      return;
    }
    let manifest: JsonObject;
    try {
      const info = fs.lstatSync(this.eventsManifestPath.raw);
      if (info.isSymbolicLink() || !info.isFile()) throw new StateIntegrityError("manifest.json is unsafe");
      const parsed = readJsonRegular(this.eventsManifestPath);
      if (!isPlainObject(parsed)) throw new StateIntegrityError("trusted events manifest is unreadable or corrupt");
      manifest = parsed;
    } catch (error) {
      if (error instanceof StateIntegrityError) throw error;
      if (isErrno(error) || error instanceof SyntaxError) throw new StateIntegrityError("trusted events manifest is unreadable or corrupt", { cause: error });
      throw error;
    }
    if (manifest.schema_version !== 1) throw new StateIntegrityError("unsupported event manifest schema");
    let segments = manifest.segments;
    if (!Array.isArray(segments) || !segments.length || !segments.every(isPlainObject)) {
      throw new StateIntegrityError("manifest segments must be a non-empty list");
    }
    let lastSeq = asIndex(manifest.last_seq);
    if (lastSeq === null || lastSeq < 1) throw new StateIntegrityError("invalid manifest last_seq");
    const typed = segments;
    if (manifest.current_segment !== typed[typed.length - 1]!.name) throw new StateIntegrityError("manifest current_segment mismatch");
    let expectedSeq = 1;
    let prevHash: unknown = null;
    for (const segment of typed) {
      const name = segment.name;
      const seqStart = asIndex(segment.seq_start);
      const seqEnd = asIndex(segment.seq_end);
      const count = asIndex(segment.event_count);
      const byteSize = asIndex(segment.byte_size);
      if (typeof name !== "string" || !name) throw new StateIntegrityError("invalid segment name in manifest");
      if (seqStart === null || seqStart !== expectedSeq) throw new StateIntegrityError("segment seq_start broken");
      if (seqEnd === null || seqEnd < seqStart) throw new StateIntegrityError("segment seq_end broken");
      if (count === null || count < 0) throw new StateIntegrityError("segment event_count invalid");
      if (byteSize === null || byteSize < 0) throw new StateIntegrityError("segment byte_size invalid");
      if (!pyEqual(getOrNull(segment, "prev_sha256"), prevHash)) throw new StateIntegrityError("segment digest chain broken");
      prevHash = getOrNull(segment, "sha256");
      expectedSeq = seqEnd + 1;
    }
    if (lastSeq !== asIndex(typed[typed.length - 1]!.seq_end)) {
      throw new StateIntegrityError("manifest last_seq does not match last segment seq_end");
    }
    for (const segment of typed.slice(0, -1)) {
      const segmentPath = segment.is_legacy_root ? this.legacyEventsPath : this.eventsDir.join(String(segment.name));
      let info: fs.Stats;
      try {
        info = fs.lstatSync(segmentPath.raw);
      } catch (error) {
        throw new StateIntegrityError(`cannot inspect sealed segment ${String(segment.name)}`, { cause: error });
      }
      if (info.isSymbolicLink() || !info.isFile()) throw new StateIntegrityError(`sealed segment ${String(segment.name)} must be a regular file`);
      if (info.size !== asIndex(segment.byte_size)) throw new StateIntegrityError(`sealed segment ${String(segment.name)} size modified`);
    }
    let entries: Set<string>;
    try {
      entries = new Set(fs.readdirSync(this.eventsDir.raw).filter((name) => !name.startsWith(".")));
    } catch (error) {
      throw new StateIntegrityError("cannot inspect events directory", { cause: error });
    }
    const expectedEntries = (source: JsonObject[]): Set<string> => new Set(["manifest.json", ...source.filter((segment) => !segment.is_legacy_root).map((segment) => String(segment.name))]);
    let expected = expectedEntries(typed);
    if (!setsEqual(entries, expected)) {
      const missing = new Set([...expected].filter((name) => !entries.has(name)));
      if (missing.size) throw new StateIntegrityError(`events directory entries do not match manifest: missing ${pySetRepr(missing)}`);
      const unexpected = new Set([...entries].filter((name) => !expected.has(name)));
      this.reconcileOrphanSegmentUnlocked(manifest, unexpected);
      try {
        entries = new Set(fs.readdirSync(this.eventsDir.raw).filter((name) => !name.startsWith(".")));
      } catch (error) {
        throw new StateIntegrityError("cannot inspect events directory after reconciliation", { cause: error });
      }
      segments = manifest.segments;
      if (!Array.isArray(segments) || !segments.every(isPlainObject)) throw new StateIntegrityError("manifest segments must be a non-empty list");
      expected = expectedEntries(segments);
      if (!setsEqual(entries, expected)) throw new StateIntegrityError("events directory entries do not match manifest after reconciliation");
      const reconciledLast = asIndex(manifest.last_seq);
      if (reconciledLast === null) throw new StateIntegrityError("invalid manifest last_seq");
      lastSeq = reconciledLast;
    }
    const active = (manifest.segments as JsonObject[])[(manifest.segments as JsonObject[]).length - 1]!;
    const activePath = active.is_legacy_root ? this.legacyEventsPath : this.eventsDir.join(String(active.name));
    let activeStat: fs.Stats;
    try {
      activeStat = fs.lstatSync(activePath.raw);
    } catch (error) {
      throw new StateIntegrityError(`cannot inspect active segment ${String(active.name)}`, { cause: error });
    }
    if (activeStat.isSymbolicLink() || !activeStat.isFile()) throw new StateIntegrityError(`active segment ${String(active.name)} must be a regular file`);
    if (activeStat.size > this.maxSegmentBytes() + 65_536) throw new StateIntegrityError("active segment exceeds maximum segment limit");
    const recordedSize = asIndex(active.byte_size) ?? 0;
    if (activeStat.size > recordedSize) {
      const trailing = fs.readFileSync(activePath.raw).subarray(recordedSize);
      if (trailing.length && trailing[trailing.length - 1] === 0x0a) {
        let trailingItem: unknown;
        try {
          trailingItem = parsePythonJson(decodeUtf8(trailing, true).trim());
        } catch (error) {
          if (error instanceof SyntaxError) throw new StateIntegrityError("trusted event log has corrupt trailing record", { cause: error });
          throw error;
        }
        if (isPlainObject(trailingItem)) {
          const trailingSeq = asIndex(getOrNull(trailingItem, "seq"));
          if (trailingSeq === lastSeq + 1) {
            active.seq_end = trailingSeq;
            active.event_count = (asIndex(active.event_count) ?? 0) + 1;
            active.byte_size = activeStat.size;
            manifest.last_seq = trailingSeq;
            this.writeJson(this.eventsManifestPath, manifest);
            lastSeq = trailingSeq;
          } else throw new StateIntegrityError("trusted event log sequence is corrupt");
        } else throw new StateIntegrityError("trusted event log sequence is corrupt");
      } else {
        try {
          const handle = fs.openSync(activePath.raw, "r+");
          try {
            fs.ftruncateSync(handle, recordedSize);
            fs.fsyncSync(handle);
          } finally {
            fs.closeSync(handle);
          }
        } catch (error) {
          throw new StateIntegrityError("cannot reconcile active segment frontier", { cause: error });
        }
      }
    } else if (activeStat.size < recordedSize) throw new StateIntegrityError("active segment truncated unexpectedly");
    const finalSize = asIndex(active.byte_size) ?? 0;
    if (finalSize > 0) {
      const lastLine = NightwatchStore.readLastJsonLine(activePath, finalSize);
      if (!isPlainObject(lastLine) || !pyEqual(getOrNull(lastLine, "seq"), lastSeq)) {
        throw new StateIntegrityError("active segment frontier tail does not match manifest last_seq");
      }
    }
  }

  private validateLegacyEvents(): void {
    const events = this.legacyEventItems();
    let previous = 0;
    for (const item of events) {
      const seq = asIndex(getOrNull(item, "seq"));
      if (seq === null || seq !== previous + 1) throw new StateIntegrityError("trusted event log sequence is corrupt");
      previous = seq;
    }
  }

  private legacyEventItems(): JsonObject[] {
    try {
      const info = fs.lstatSync(this.legacyEventsPath.raw);
      let regular = false;
      try {
        regular = fs.statSync(this.legacyEventsPath.raw).isFile();
      } catch {
        regular = false;
      }
      if (!regular || info.isSymbolicLink() || info.size > MAX_EVENT_BYTES) throw new StateIntegrityError("trusted event log is unsafe");
      const raw = this.readSegmentBody(this.legacyEventsPath);
      const events: JsonObject[] = [];
      for (const line of pythonSplitLines(decodeUtf8(raw, true))) {
        if (!line.trim()) continue;
        const parsed = parsePythonJson(line);
        if (isPlainObject(parsed)) events.push(parsed);
        else events.push({});
      }
      return events;
    } catch (error) {
      if (errnoCode(error) === "ENOENT") return [];
      if (error instanceof StateIntegrityError) throw error;
      if (isErrno(error) || error instanceof SyntaxError) throw new StateIntegrityError("trusted event log is unreadable", { cause: error });
      throw error;
    }
  }

  private eventItems(): JsonObject[] {
    return this.loadEvents();
  }

  private nextEventSeq(): number {
    if (this.hasLegacyEventsOnly()) {
      const events = this.legacyEventItems();
      return events.length ? asIndex(events[events.length - 1]!.seq)! + 1 : 1;
    }
    if (pathExists(this.eventsManifestPath)) {
      const manifest = readJsonRegular(this.eventsManifestPath);
      if (!isPlainObject(manifest)) throw new StateIntegrityError("trusted events manifest is invalid");
      const last = getOrNull(manifest, "last_seq");
      if (typeof last === "boolean") return (last ? 1 : 0) + 1;
      if (typeof last === "number") return Math.trunc(last) + 1;
      if (last instanceof PyFloat) return Math.trunc(last.value) + 1;
      if (typeof last === "string") return pythonInt(last) + 1;
      if (last === null) return 1;
      throw new Error("cannot convert manifest last_seq to int");
    }
    return 1;
  }

  private validateEventSequence(): void {
    this.validateEventFrontier();
  }

  private logUnlocked(message: string): void {
    const redacted = redact(message);
    this.appendFile(this.logPath, `${nowIso()} nightwatch/${NIGHTWATCH_VERSION} ${typeof redacted === "string" ? redacted : String(redacted)}\n`);
  }

  private appendFile(target: NightPath, text: string): void {
    fs.mkdirSync(target.parent.raw, { recursive: true, mode: 0o700 });
    const fd = fs.openSync(target.raw, "a", 0o666);
    try {
      writeAll(fd, text);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.chmodSync(target.raw, 0o600);
  }

  private static writeText(target: NightPath, text: string): void {
    fs.mkdirSync(target.parent.raw, { recursive: true, mode: 0o700 });
    const temporary = target.parent.join(`.${target.name}.${randomBytes(12).toString("hex")}.tmp`);
    const flags = fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW;
    let fd = -1;
    try {
      fd = fs.openSync(temporary.raw, flags, 0o600);
      writeAll(fd, text);
      fs.fsyncSync(fd);
      closeQuiet(fd);
      fd = -1;
      fs.chmodSync(temporary.raw, 0o600);
      fs.renameSync(temporary.raw, target.raw);
    } finally {
      closeQuiet(fd);
      try {
        fs.unlinkSync(temporary.raw);
      } catch (error) {
        if (errnoCode(error) !== "ENOENT") throw error;
      }
    }
  }

  private writeJson(target: NightPath, value: unknown): void {
    NightwatchStore.writeText(target, `${dumps(redact(value), 2)}\n`);
  }
}

export function makeRunId(repo: string, timestamp: string | null = null): string {
  const stamp = (timestamp || nowIso()).replaceAll("-", "").replaceAll(":", "").replaceAll(".", "").slice(0, 15);
  return `${safeSlug(new NightPath(repo).name)}-${stamp}`;
}
