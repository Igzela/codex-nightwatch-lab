import { createRequire } from "node:module";
import { spawnSync, type StdioOptions } from "node:child_process";

import { NightPath } from "./path.js";
import { redact } from "./storage.js";

const require = createRequire(import.meta.url);
const NIGHTWATCH_VERSION = String(require("../package.json").version);

export class AppServerProtocolError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "AppServerProtocolError";
  }
}

function osErrorName(error: NodeJS.ErrnoException | undefined): string {
  if (error?.code === "ENOENT") return "FileNotFoundError";
  if (error?.code === "EACCES" || error?.code === "EPERM") return "PermissionError";
  if (error?.code === "ETIMEDOUT") return "TimeoutExpired";
  return error?.code || error?.name || "OSError";
}

function resolveCodexHome(value?: string | NightPath | null): string {
  const selected = value || process.env.CODEX_HOME || NightPath.home().join(".codex").raw;
  const raw = typeof selected === "string" ? selected : selected.raw;
  return new NightPath(raw).expanduser().resolve().raw;
}

function stdioWithLease(leaseFd: number | null): StdioOptions | undefined {
  if (leaseFd == null || leaseFd < 3) return undefined;
  const stdio: Array<"pipe" | "ignore" | number> = ["pipe", "pipe", "pipe"];
  while (stdio.length < leaseFd) stdio.push("ignore");
  stdio.push(leaseFd);
  return stdio;
}

function formatRedacted(value: unknown): string {
  const redacted = redact(value);
  if (typeof redacted === "string") return redacted;
  try {
    return JSON.stringify(redacted);
  } catch {
    return String(redacted);
  }
}

/**
 * Small JSON-RPC client for the current Codex app-server stdio contract.
 * The prompt-free handshake is written as stdin, then stdout is scanned.
 * `account/rateLimits/read` omits params: Codex 0.150.1 defines them as null.
 */
export class AppServerClient {
  readonly binary: string;
  readonly timeout: number;
  readonly codexHome: NightPath;
  readonly leaseFd: number | null;
  readonly trace: Array<Record<string, unknown>> = [];

  constructor(binary?: string | null, timeout = 8, codexHome?: string | NightPath | null, leaseFd: number | null = null) {
    this.binary = binary || process.env.NIGHTWATCH_CODEX_BIN || "codex";
    this.timeout = timeout;
    this.codexHome = new NightPath(resolveCodexHome(codexHome));
    this.leaseFd = typeof leaseFd === "number" && Number.isInteger(leaseFd) && leaseFd >= 0 ? leaseFd : null;
  }

  rateLimits(): unknown {
    const messages = [
      {
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          clientInfo: { name: "nightwatch", version: NIGHTWATCH_VERSION },
          capabilities: { experimentalApi: false },
        },
      },
      { jsonrpc: "2.0", method: "initialized" },
      { jsonrpc: "2.0", id: 2, method: "account/rateLimits/read" },
    ];
    const input = messages.map((message) => JSON.stringify(message) + "\n").join("");
    let result: ReturnType<typeof spawnSync>;
    try {
      result = spawnSync(this.binary, ["app-server", "--stdio"], {
        input,
        encoding: "utf8",
        timeout: Math.max(1, Math.round(this.timeout * 1000)),
        env: { ...process.env, CODEX_HOME: this.codexHome.raw },
        stdio: stdioWithLease(this.leaseFd),
        maxBuffer: 8 * 1024 * 1024,
        windowsHide: true,
      });
    } catch (error) {
      throw new AppServerProtocolError(`app-server spawn failed: ${osErrorName(error as NodeJS.ErrnoException)}`, { cause: error });
    }
    const failure = result.error as NodeJS.ErrnoException | undefined;
    if (failure && failure.code !== "ETIMEDOUT" && result.status == null && !result.stdout) {
      throw new AppServerProtocolError(`app-server spawn failed: ${osErrorName(failure)}`, { cause: failure });
    }
    for (const message of messages) {
      this.trace.push({ direction: "send", id: "id" in message ? message.id : undefined, method: message.method });
    }
    const lines = String(result.stdout ?? "").split(/\n/);
    // A trailing split element is not a protocol message.
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    const timedOut = failure?.code === "ETIMEDOUT";
    const initialize = this.takeResponse(lines, 1, timedOut);
    if (initialize == null) {
      throw new AppServerProtocolError(timedOut ? "app-server response id 1 timed out" : "app-server exited before response id 1");
    }
    const response = this.takeResponse(lines, 2, timedOut);
    if (response == null) {
      throw new AppServerProtocolError(timedOut ? "app-server response id 2 timed out" : "app-server exited before response id 2");
    }
    return response.result;
  }

  private takeResponse(lines: string[], requestId: number, timedOut: boolean): Record<string, unknown> | null {
    while (lines.length) {
      const line = lines.shift() ?? "";
      let message: unknown;
      try {
        message = JSON.parse(line);
      } catch {
        this.trace.push({ direction: "recv", kind: "malformed" });
        continue;
      }
      if (!message || typeof message !== "object" || Array.isArray(message)) {
        this.trace.push({ direction: "recv", kind: "non_object" });
        continue;
      }
      const record = message as Record<string, unknown>;
      if ("method" in record && !("id" in record)) {
        this.trace.push({ direction: "recv", kind: "notification", method: record.method });
        continue;
      }
      if (record.id !== requestId) {
        this.trace.push({ direction: "recv", kind: "other_response", id: record.id });
        continue;
      }
      this.trace.push({ direction: "recv", kind: "response", id: requestId });
      if (record.error != null) {
        throw new AppServerProtocolError(`app-server response id ${requestId} returned error: ${formatRedacted(record.error)}`);
      }
      if (!("result" in record)) throw new AppServerProtocolError(`app-server response id ${requestId} has no result`);
      return record;
    }
    if (timedOut) throw new AppServerProtocolError(`app-server response id ${requestId} timed out`);
    return null;
  }
}
