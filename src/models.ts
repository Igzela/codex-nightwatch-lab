import { execFileSync } from "node:child_process";

export const State = {
  NEW: "NEW",
  PREFLIGHT: "PREFLIGHT",
  RUNNING: "RUNNING",
  WAIT_QUOTA: "WAIT_QUOTA",
  RETRY_BACKOFF: "RETRY_BACKOFF",
  RECOVERING: "RECOVERING",
  VERIFYING: "VERIFYING",
  AWAITING_ACCEPTANCE: "AWAITING_ACCEPTANCE",
  BLOCKED: "BLOCKED",
  STOPPED: "STOPPED",
  DONE: "DONE",
  FAILED: "FAILED",
} as const;

export type State = (typeof State)[keyof typeof State];

export const ErrorKind = {
  QUOTA_5H: "usage_limit_5h",
  QUOTA_WEEKLY: "usage_limit_weekly",
  TEMPORARY_429: "temporary_429",
  CAPACITY: "capacity_overload",
  NETWORK: "network_error",
  AUTH: "auth_error",
  CRASH: "codex_crash",
  BLOCKER: "task_blocker",
  MALFORMED: "malformed_jsonl",
  STATE: "state_integrity",
  GIT: "git_conflict",
  UNKNOWN: "unknown_provider_error",
} as const;

export type ErrorKind = (typeof ErrorKind)[keyof typeof ErrorKind];

export const ACCOUNT_MODES = new Set(["CURRENT_ONLY", "AUTO_POOL"]);
export const PROVEN_CROSS_ACCOUNT_CODEX_VERSIONS = new Set(["0.152.1"]);
export const TERMINAL_STATES = new Set<State>([
  State.DONE,
  State.FAILED,
  State.BLOCKED,
  State.STOPPED,
  State.AWAITING_ACCEPTANCE,
]);

const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const REASONING_EFFORT = /^[A-Za-z][A-Za-z0-9_-]{0,31}$/;
const AGY_PRINT_TIMEOUT = /^([1-9][0-9]*)(s|m|h)$/;

export function installedCodexVersion(binary?: string | null): string | null {
  const binPath = binary || process.env.NIGHTWATCH_CODEX_BIN || "codex";
  try {
    return execFileSync(binPath, ["--version"], { encoding: "utf8", timeout: 8000, stdio: ["ignore", "pipe", "ignore"] }).trim() || null;
  } catch {
    return null;
  }
}

export function crossAccountThreadModeForVersion(version: string | null | undefined): string {
  if (process.env.NIGHTWATCH_UNSAFE_FORCE_CROSS_ACCOUNT_PROVEN === "1") return "PROVEN";
  let baseMode = "INCONCLUSIVE";
  if (version) {
    const clean = version.split(/\s+/).at(-1)?.trim() ?? "";
    if (PROVEN_CROSS_ACCOUNT_CODEX_VERSIONS.has(clean) || PROVEN_CROSS_ACCOUNT_CODEX_VERSIONS.has(version)) baseMode = "PROVEN";
  }
  const override = process.env.NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE;
  if (override) {
    if (override === "UNSUPPORTED" || override === "INCONCLUSIVE") return override;
    if (override === "PROVEN") return baseMode === "PROVEN" ? "PROVEN" : "INCONCLUSIVE";
  }
  return baseMode;
}

export function validateModelName(value: string): string {
  if (!MODEL_NAME.test(value)) throw new Error("model must be a visible model slug (letters, digits, '.', '_', ':', '/', or '-')");
  return value;
}

export function validateReasoningEffort(value: string): string {
  if (!REASONING_EFFORT.test(value)) throw new Error("reasoning effort must be a visible level name");
  return value;
}

export function validateAgyPrintTimeout(value: string): string {
  if (typeof value !== "string") throw new Error("agy_print_timeout must be a string");
  const match = AGY_PRINT_TIMEOUT.exec(value.trim());
  if (!match) throw new Error(`invalid agy_print_timeout: ${JSON.stringify(value)}; must match [1-9][0-9]*(s|m|h)`);
  const count = Number(match[1]);
  const seconds = match[2] === "s" ? count : match[2] === "m" ? count * 60 : count * 3600;
  if (seconds < 5) throw new Error(`agy_print_timeout too short: ${JSON.stringify(value)} (${seconds}s < 5s)`);
  if (seconds > 86400) throw new Error(`agy_print_timeout too long: ${JSON.stringify(value)} (${seconds}s > 24h)`);
  return value.trim();
}

export function parseAgyDurationSeconds(value: string): number {
  const valid = validateAgyPrintTimeout(value);
  const match = AGY_PRINT_TIMEOUT.exec(valid);
  if (!match) throw new Error("invalid agy_print_timeout");
  const count = Number(match[1]);
  return match[2] === "s" ? count : match[2] === "m" ? count * 60 : count * 3600;
}

export class QuotaWindow {
  constructor(
    readonly name: string,
    readonly usedPercent: number | null = null,
    readonly windowDurationMins: number | null = null,
    readonly resetsAt: number | null = null,
  ) {}

  get exhausted(): boolean {
    return this.usedPercent !== null && this.usedPercent >= 100;
  }

  toDict(): Record<string, unknown> {
    return {
      name: this.name,
      used_percent: this.usedPercent,
      window_duration_mins: this.windowDurationMins,
      resets_at: this.resetsAt,
    };
  }
}

export class QuotaSnapshot {
  constructor(
    readonly source: string,
    readonly readAt: string,
    readonly primary: QuotaWindow | null = null,
    readonly secondary: QuotaWindow | null = null,
    readonly planType: string | null = null,
    readonly error: string | null = null,
    readonly accountFingerprint: string | null = null,
  ) {}

  windows(): QuotaWindow[] {
    return [this.primary, this.secondary].filter((item): item is QuotaWindow => item !== null);
  }

  exhaustedWindows(): QuotaWindow[] {
    return this.windows().filter((item) => item.exhausted);
  }

  recovered(governingNames?: Set<string> | null): boolean {
    let windows = this.windows();
    if (governingNames) windows = windows.filter((item) => governingNames.has(item.name));
    return windows.length > 0 && windows.every((item) => !item.exhausted);
  }

  toDict(): Record<string, unknown> {
    return {
      source: this.source,
      read_at: this.readAt,
      primary: this.primary ? this.primary.toDict() : null,
      secondary: this.secondary ? this.secondary.toDict() : null,
      plan_type: this.planType,
      error: this.error,
      account_fingerprint: this.accountFingerprint,
    };
  }
}

export interface ProviderResult {
  exitCode: number | null;
  signal: string | null;
  threadId: string | null;
  eventCount: number;
  malformedCount: number;
  eventTypes: string[];
  errorKind: ErrorKind | null;
  errorDetail: string | null;
  resetAt: number | null;
  resetSource: string | null;
  quotaWindows: QuotaWindow[];
  blocker: string | null;
  aborted: boolean;
  runLog: string | null;
}

export type JsonObject = Record<string, unknown>;

export function emptyState(
  runId: string,
  goal: string,
  repo: string,
  repoId: string,
  now: string,
  model: string | null = null,
  reasoningEffort: string | null = null,
  provider = "codex",
  agyPrintTimeout: string | null = null,
): JsonObject {
  if (provider === "agy" && agyPrintTimeout === null) agyPrintTimeout = "60m";
  return {
    schema_version: 2,
    run_id: runId,
    goal,
    repo,
    repo_id: repoId,
    provider,
    agy_print_timeout: agyPrintTimeout,
    thread_id: null,
    model,
    reasoning_effort: reasoningEffort,
    state: State.NEW,
    generation: 1,
    created_at: now,
    updated_at: now,
    next_resume_at: null,
    quota_source: null,
    quota: null,
    quota_windows: [],
    last_verified_commit: null,
    last_error: null,
    error_kind: null,
    blocker: null,
    active_process: null,
    supervisor_owner: null,
    resume_claim: null,
    retry_attempt: 0,
    crash_attempt: 0,
    recoveries: 0,
    quota_cycles: 0,
    recovery_failures: 0,
    pool_probe_failures: 0,
    last_event: "run_created",
    last_provider_exit: null,
    last_provider_signal: null,
    last_git_head: null,
    final_verification_passed: false,
    plan_ready: false,
    verification_attempts: 0,
    last_verification: null,
    inhibit_requested: false,
    acceptance_ready: false,
    account_mode: "CURRENT_ONLY",
    authorized_accounts: [],
    current_account_key: null,
    current_account_fingerprint: null,
    active_account_fingerprint: null,
    account_generation: 0,
    account_lease: null,
    account_claim: null,
    account_reselect: false,
    account_snapshots: {},
    account_reset_times: {},
    account_errors: {},
    last_switch_reason: null,
    thread_handoff: null,
    cross_account_thread_mode: "INCONCLUSIVE",
    initialized: false,
  };
}

function isNonNegativeInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function positiveIntPair(value: unknown): value is [number, number] {
  return Array.isArray(value) && value.length === 2 && value.every((item) => typeof item === "number" && Number.isInteger(item) && item > 0);
}

export function validateState(state: JsonObject): void {
  const required = [
    "schema_version", "run_id", "goal", "repo", "repo_id", "thread_id", "state",
    "generation", "created_at", "updated_at", "next_resume_at", "quota_source",
    "last_verified_commit", "last_error", "last_event",
  ];
  const missing = required.filter((key) => !(key in state)).sort();
  if (missing.length) throw new Error(`state missing fields: ${missing.join(", ")}`);
  if (state.schema_version !== 2) throw new Error(`unsupported state schema: ${JSON.stringify(state.schema_version)}`);
  const provider = (state.provider as string | undefined) ?? "codex";
  if (provider !== "codex" && provider !== "agy") throw new Error(`unknown provider: ${JSON.stringify(provider)}`);
  if (provider === "agy") {
    if (state.account_mode === "AUTO_POOL") throw new Error("AGY provider does not support AUTO_POOL");
    const timeout = state.agy_print_timeout;
    if (typeof timeout !== "string") throw new Error("state.agy_print_timeout must be a string for AGY provider");
    validateAgyPrintTimeout(timeout);
  } else if (state.agy_print_timeout != null) {
    throw new Error("Codex provider state must not set agy_print_timeout");
  }
  if (!Object.values(State).includes(state.state as State)) throw new Error(`unknown state: ${JSON.stringify(state.state)}`);
  if (typeof state.generation !== "number" || !Number.isInteger(state.generation) || state.generation < 1) {
    throw new Error("state.generation must be a positive integer");
  }
  for (const field of ["recoveries", "quota_cycles", "recovery_failures", "pool_probe_failures"]) {
    const value = state[field] ?? 0;
    if (!isNonNegativeInt(value)) throw new Error(`state.${field} must be a non-negative integer`);
  }
  if (state.thread_id !== null && typeof state.thread_id !== "string") throw new Error("state.thread_id must be a string or null");
  if (state.model != null) validateModelName(String(state.model));
  if (state.reasoning_effort != null) validateReasoningEffort(String(state.reasoning_effort));
  const accountMode = (state.account_mode as string | undefined) ?? "CURRENT_ONLY";
  if (!ACCOUNT_MODES.has(accountMode)) throw new Error("state.account_mode is invalid");
  const authorized = (state.authorized_accounts as unknown) ?? [];
  if (!Array.isArray(authorized) || new Set(authorized).size !== authorized.length || authorized.some((item) => typeof item !== "string" || !item.trim())) {
    throw new Error("state.authorized_accounts must contain unique stable account keys");
  }
  if (state.current_account_key != null && (typeof state.current_account_key !== "string" || !state.current_account_key.trim())) {
    throw new Error("state.current_account_key must be a string or null");
  }
  for (const field of ["current_account_fingerprint", "active_account_fingerprint"]) {
    const value = state[field];
    if (value != null && (typeof value !== "string" || !value.trim())) throw new Error(`state.${field} must be a string or null`);
  }
  const capability = (state.cross_account_thread_mode as string | undefined) ?? "INCONCLUSIVE";
  if (!["PROVEN", "UNSUPPORTED", "INCONCLUSIVE"].includes(capability)) throw new Error("state.cross_account_thread_mode is invalid");
  if (state.thread_handoff != null && typeof state.thread_handoff !== "object") throw new Error("state.thread_handoff must be an object or null");
  if (state.account_lease != null && typeof state.account_lease !== "object") throw new Error("state.account_lease must be an object or null");
  if (state.account_claim != null && typeof state.account_claim !== "object") throw new Error("state.account_claim must be an object or null");
  if (state.resume_claim != null) {
    const claim = state.resume_claim as JsonObject;
    if (typeof claim !== "object" || typeof claim.generation !== "number") throw new Error("invalid resume_claim");
  }
  if (state.active_process != null) {
    const active = state.active_process as JsonObject;
    if (typeof active !== "object" || typeof active.pid !== "number" || typeof active.starttime !== "string") throw new Error("invalid active_process");
    if (active.pgid != null && (typeof active.pgid !== "number" || !Number.isInteger(active.pgid) || active.pgid <= 1)) {
      throw new Error("invalid active_process pgid");
    }
  }
  const runtimeId = state.codex_runtime_identity;
  const homeId = state.codex_home_identity;
  if (runtimeId != null || homeId != null) {
    if (runtimeId == null || homeId == null) throw new Error("both codex_runtime_identity and codex_home_identity must be present together");
    if (!positiveIntPair(runtimeId)) throw new Error("state.codex_runtime_identity must be a list of two positive integers [st_dev, st_ino]");
    if (!positiveIntPair(homeId)) throw new Error("state.codex_home_identity must be a list of two positive integers [st_dev, st_ino]");
  } else if (state.account_mode === "AUTO_POOL" && state.initialized) {
    if (!state.legacy_pre_identity_migration) throw new Error("AUTO_POOL state requires codex_runtime_identity and codex_home_identity");
  }
}

export function validatePlan(plan: JsonObject): void {
  const milestones = plan.milestones;
  if (!Array.isArray(milestones)) throw new Error("plan must contain a milestones array");
  if (!milestones.length) throw new Error("plan must contain at least one milestone");
  if (plan.schema_version !== 2 || plan.authority !== "nightwatch") throw new Error("plan is not a trusted schema 2 Nightwatch plan");
  const allowed = new Set(["pending", "working", "implemented", "verified", "blocked"]);
  const seen = new Set<string>();
  let total = 0;
  for (const item of milestones) {
    if (!item || typeof item !== "object") throw new Error("milestone must be an object");
    const milestone = item as JsonObject;
    const ident = milestone.id;
    const title = milestone.title;
    const weight = milestone.weight;
    if (typeof ident !== "string" || !ident || seen.has(ident)) throw new Error("milestone ids must be unique non-empty strings");
    if (typeof title !== "string" || !title.trim()) throw new Error(`milestone ${JSON.stringify(ident)} has no title`);
    if (typeof weight !== "number" || weight <= 0) throw new Error(`milestone ${JSON.stringify(ident)} has invalid weight`);
    const status = (milestone.status as string | undefined) ?? "pending";
    if (!allowed.has(status)) throw new Error(`milestone ${JSON.stringify(ident)} has invalid status`);
    const profile = milestone.verification_profile;
    if (profile !== "default" && profile !== "none") throw new Error(`milestone ${JSON.stringify(ident)} has invalid verification_profile`);
    if ("verification_commands" in milestone) throw new Error("model-controlled verification_commands are forbidden");
    if (milestone.evidence != null && !Array.isArray(milestone.evidence)) throw new Error(`milestone ${JSON.stringify(ident)} has invalid evidence`);
    seen.add(ident);
    total += Number(weight);
  }
  if (total <= 0) throw new Error("plan weight must be positive");
}

export function planProgress(plan: JsonObject): JsonObject {
  validatePlan(plan);
  const milestones = plan.milestones as JsonObject[];
  const implemented = milestones.filter((item) => item.status === "implemented" || item.status === "verified");
  const verified = milestones.filter((item) => item.status === "verified");
  const total = milestones.reduce((sum, item) => sum + Number(item.weight), 0);
  const implementedWeight = implemented.reduce((sum, item) => sum + Number(item.weight), 0);
  const verifiedWeight = verified.reduce((sum, item) => sum + Number(item.weight), 0);
  const round = (value: number) => Math.round(value * 100) / 100;
  return {
    implemented_count: implemented.length,
    verified_count: verified.length,
    total_count: milestones.length,
    implemented_weight: implementedWeight,
    verified_weight: verifiedWeight,
    total_weight: total,
    implemented_percent: round((implementedWeight / total) * 100),
    verified_percent: round((verifiedWeight / total) * 100),
  };
}
