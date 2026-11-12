import fs from "node:fs";

import { AppServerClient, AppServerProtocolError } from "./app-server.js";
import { extractQuotaWindows, parseIso } from "./codex.js";
import { QuotaSnapshot, QuotaWindow } from "./models.js";
import { NightPath } from "./path.js";

export class QuotaError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "QuotaError";
  }
}

export function isoNow(): string {
  return new Date().toISOString();
}

function pyNumber(value: unknown): number | null {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return null;
}

function present(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) if (Object.hasOwn(record, key)) return record[key];
  return undefined;
}

function windowFrom(raw: unknown, name: string): QuotaWindow | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const record = raw as Record<string, unknown>;
  const used = present(record, ["usedPercent", "used_percent"]);
  const duration = present(record, ["windowDurationMins", "window_duration_mins", "windowMinutes", "window_minutes"]);
  const reset = present(record, ["resetsAt", "resets_at"]);
  const usedValue = pyNumber(used);
  const durationNumber = pyNumber(duration);
  const durationValue = durationNumber == null ? null : Math.trunc(durationNumber);
  const resetNumber = pyNumber(reset);
  const resetValue = resetNumber == null ? null : resetNumber > 10_000_000_000 ? Math.trunc(resetNumber / 1000) : Math.trunc(resetNumber);
  return new QuotaWindow(name, usedValue, durationValue, resetValue);
}

function pick(record: Record<string, unknown>, key: string, fallback: unknown): unknown {
  return Object.hasOwn(record, key) ? record[key] : fallback;
}

export function parseQuotaResult(result: unknown, source = "app_server", accountFingerprint: string | null = null): QuotaSnapshot {
  if (!result || typeof result !== "object" || Array.isArray(result)) throw new QuotaError("quota response result is not an object");
  const record = result as Record<string, unknown>;
  const raw = pick(record, "rateLimits", pick(record, "rate_limits", record));
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new QuotaError("quota response has no rateLimits object");
  const limits = raw as Record<string, unknown>;
  const primary = windowFrom(limits.primary, "5h") ?? windowFrom(limits.short, "5h");
  const secondary = windowFrom(limits.secondary, "weekly") ?? windowFrom(limits.long, "weekly");
  if (primary == null && secondary == null) throw new QuotaError("quota response has no primary or secondary window");
  const plan = pick(limits, "planType", limits.plan_type);
  return new QuotaSnapshot(source, isoNow(), primary, secondary, plan ? String(plan) : null, null, accountFingerprint);
}

function resolveHome(value?: string | NightPath | null): string {
  const selected = value || process.env.CODEX_HOME || NightPath.home().join(".codex").raw;
  const raw = typeof selected === "string" ? selected : selected.raw;
  return new NightPath(raw).expanduser().resolve().raw;
}

export class AppServerQuotaProvider {
  readonly binary: string;
  readonly timeout: number;
  readonly codexHome: NightPath;
  readonly accountFingerprint: string | null;
  readonly leaseFd: number | null;

  constructor(binary?: string | null, timeout = 8, codexHome?: string | NightPath | null, accountFingerprint: string | null = null, leaseFd: number | null = null) {
    this.binary = binary || process.env.NIGHTWATCH_CODEX_BIN || "codex";
    this.timeout = timeout;
    this.codexHome = new NightPath(resolveHome(codexHome));
    this.accountFingerprint = accountFingerprint;
    this.leaseFd = leaseFd;
  }

  read(): QuotaSnapshot {
    try {
      const result = new AppServerClient(this.binary, this.timeout, this.codexHome, this.leaseFd).rateLimits();
      return parseQuotaResult(result, "live_app_server", this.accountFingerprint);
    } catch (error) {
      if (error instanceof AppServerProtocolError) throw new QuotaError(error.message, { cause: error });
      throw error;
    }
  }
}

function walkRollouts(root: NightPath): Array<{ path: NightPath; mtime: number }> {
  const found: Array<{ path: NightPath; mtime: number }> = [];
  const visit = (dir: NightPath): void => {
    let entries: NightPath[] = [];
    try {
      entries = dir.iterdir();
    } catch {
      return;
    }
    for (const entry of entries) {
      let info: fs.Stats;
      try {
        info = entry.lstat();
      } catch {
        continue;
      }
      if (info.isSymbolicLink()) continue;
      if (info.isDirectory()) {
        visit(entry);
        continue;
      }
      if (entry.name.startsWith("rollout-") && entry.name.endsWith(".jsonl")) {
        try {
          found.push({ path: entry, mtime: entry.stat().mtimeMs / 1000 });
        } catch {
          found.push({ path: entry, mtime: 0 });
        }
      }
    }
  };
  if (root.exists()) visit(root);
  return found;
}

/**
 * Read the newest structured rate-limit event from local Codex JSONL.
 * This is a fallback only. A stale rollout is never a fresh recovery.
 */
export class RolloutQuotaProvider {
  readonly codexHome: NightPath;
  readonly maxAgeSeconds: number;

  constructor(codexHome?: string | NightPath | null, maxAgeSeconds = 3600) {
    const selected = codexHome || process.env.CODEX_HOME || NightPath.home().join(".codex").raw;
    const raw = typeof selected === "string" ? selected : selected.raw;
    this.codexHome = new NightPath(raw).expanduser();
    this.maxAgeSeconds = maxAgeSeconds;
  }

  read(): QuotaSnapshot {
    const sessions = this.codexHome.join("sessions");
    const candidates = walkRollouts(sessions).sort((left, right) => right.mtime - left.mtime);
    let newest: { timestamp: number; windows: QuotaWindow[] } | null = null;
    for (const candidate of candidates) {
      try {
        const text = new TextDecoder("utf-8", { fatal: false }).decode(candidate.path.readBytes());
        for (const line of text.split(/\n/)) {
          if (!line.trim()) continue;
          let event: unknown;
          try {
            event = JSON.parse(line);
          } catch {
            continue;
          }
          const windows = extractQuotaWindows(event);
          if (!windows.length) continue;
          let timestamp: number | null = null;
          if (event && typeof event === "object" && !Array.isArray(event)) {
            timestamp = parseIso((event as Record<string, unknown>).timestamp);
          }
          timestamp = timestamp ?? Math.trunc(candidate.mtime);
          if (newest == null || timestamp >= newest.timestamp) newest = { timestamp, windows };
        }
      } catch {
        continue;
      }
    }
    if (newest == null) throw new QuotaError("no structured rate-limit event found in local Codex rollouts");
    if (Math.trunc(Date.now() / 1000) - newest.timestamp > this.maxAgeSeconds) throw new QuotaError("local rollout rate-limit event is stale");
    const primary = newest.windows.find((window) => window.name === "5h") ?? null;
    const secondary = newest.windows.find((window) => window.name === "weekly") ?? null;
    const readAt = new Date(newest.timestamp * 1000).toISOString().replace(".000Z", "Z");
    return new QuotaSnapshot("rollout_jsonl", readAt, primary, secondary);
  }
}

export class FallbackQuotaProvider {
  constructor(
    readonly primary: AppServerQuotaProvider,
    readonly fallback: RolloutQuotaProvider,
  ) {}

  read(): QuotaSnapshot {
    try {
      return this.primary.read();
    } catch (primaryError) {
      if (!(primaryError instanceof QuotaError)) throw primaryError;
      try {
        const snapshot = this.fallback.read();
        return new QuotaSnapshot("rollout_schedule_only", snapshot.readAt, snapshot.primary, snapshot.secondary, snapshot.planType, snapshot.error);
      } catch (fallbackError) {
        if (!(fallbackError instanceof QuotaError)) throw fallbackError;
        throw new QuotaError("App Server and local rollout quota sources unavailable", { cause: fallbackError });
      }
    }
  }
}

/** Deterministic provider used by fault tests and disposable fixtures. */
export class FileQuotaProvider {
  readonly path: NightPath;

  constructor(path: string | NightPath) {
    this.path = typeof path === "string" ? new NightPath(path) : path;
  }

  read(): QuotaSnapshot {
    try {
      const value = JSON.parse(this.path.readText());
      return parseQuotaResult(value, "fake_file");
    } catch (error) {
      if (error instanceof QuotaError) throw new QuotaError("fake quota file is unavailable or invalid", { cause: error });
      throw new QuotaError("fake quota file is unavailable or invalid", { cause: error });
    }
  }
}

export function makeQuotaProvider(): AppServerQuotaProvider | FileQuotaProvider | FallbackQuotaProvider {
  const fake = process.env.NIGHTWATCH_QUOTA_FILE;
  return fake ? new FileQuotaProvider(fake) : new FallbackQuotaProvider(new AppServerQuotaProvider(), new RolloutQuotaProvider());
}

export function quotaRecovered(snapshot: QuotaSnapshot, names: Set<string>): boolean {
  if (snapshot.source.startsWith("rollout_")) return false;
  if (snapshot.error) return false;
  const selected = snapshot.windows().filter((window) => names.has(window.name));
  return selected.length > 0 && selected.every((window) => !window.exhausted);
}
