import { spawnSync } from "node:child_process";

import { diffCheck, snapshot, type GitSnapshot } from "./git.js";
import { planProgress, validatePlan, type JsonObject } from "./models.js";
import { NightPath } from "./path.js";
import { NightwatchStore, StateIntegrityError, redact } from "./storage.js";

const MAX_MAILBOX_BYTES = 1_000_000;
const MAX_MILESTONES = 100;
const MAX_TITLE = 500;
const MAX_DEPTH = 12;

const TRUSTED_ENV = new Set(["PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "CODEX_HOME"]);

function nowIso(): string {
  return new Date().toISOString();
}

function isRecord(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function jsonDepth(value: unknown, depth = 0): number {
  if (depth > MAX_DEPTH) throw new Error("mailbox JSON is too deeply nested");
  if (isRecord(value)) {
    let max = depth;
    for (const item of Object.values(value)) max = Math.max(max, jsonDepth(item, depth + 1));
    return max;
  }
  if (Array.isArray(value)) {
    let max = depth;
    for (const item of value) max = Math.max(max, jsonDepth(item, depth + 1));
    return max;
  }
  return depth;
}

function repoPath(store: NightwatchStore): string {
  const repo = store.repo as NightPath | string;
  return typeof repo === "string" ? repo : repo.raw;
}

function isPyNumber(value: unknown): value is number | boolean {
  return (typeof value === "number" && Number.isFinite(value)) || typeof value === "boolean";
}

export function readMailboxJson(store: NightwatchStore, name: string): unknown {
  let raw: Buffer | null;
  try {
    const file = store.readMailboxFile(name);
    raw = file == null ? null : Buffer.isBuffer(file) ? file : Buffer.from(file);
  } catch (error) {
    if (error instanceof StateIntegrityError) throw new Error("mailbox JSON is invalid", { cause: error });
    throw error;
  }
  if (raw == null) return null;
  if (raw.length > MAX_MAILBOX_BYTES) throw new Error("mailbox entry is too large");
  try {
    const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(raw)) as unknown;
    jsonDepth(value);
    return value;
  } catch (error) {
    if (error instanceof Error && error.message === "mailbox JSON is too deeply nested") throw error;
    throw new Error("mailbox JSON is invalid", { cause: error });
  }
}

function rejectionName(error: unknown): string {
  if (error instanceof StateIntegrityError) return "StateIntegrityError";
  if (error instanceof TypeError) return "TypeError";
  return "ValueError";
}

function proposalToPlan(store: NightwatchStore, proposed: unknown): JsonObject {
  if (!isRecord(proposed) || Object.keys(proposed).some((key) => key !== "goal_hash" && key !== "milestones")) {
    throw new Error("proposal has unsupported fields");
  }
  const acceptance = store.loadAcceptance();
  if (proposed.goal_hash !== acceptance.goal_hash) throw new Error("proposal is not bound to this goal");
  const rows = proposed.milestones;
  if (!Array.isArray(rows) || !rows.length || rows.length > MAX_MILESTONES) throw new Error("proposal milestone count is invalid");
  const policy = store.loadPolicy();
  const commands = policy.final_commands;
  const profile = Array.isArray(commands) && commands.length ? "default" : "none";
  const milestones: JsonObject[] = [];
  const seen = new Set<string>();
  for (const item of rows) {
    if (!isRecord(item) || Object.keys(item).some((key) => key !== "id" && key !== "title" && key !== "weight")) {
      throw new Error("proposal contains untrusted authority fields");
    }
    const ident = item.id;
    const title = item.title;
    const weight = Object.hasOwn(item, "weight") ? item.weight : 1;
    if (typeof ident !== "string" || !ident || ident.length > 80 || seen.has(ident)) throw new Error("proposal milestone id is invalid");
    if (typeof title !== "string" || !title.trim() || title.length > MAX_TITLE) throw new Error("proposal milestone title is invalid");
    if (!isPyNumber(weight) || !(0 < Number(weight) && Number(weight) <= 10_000)) throw new Error("proposal milestone weight is invalid");
    milestones.push({
      id: ident,
      title: title.trim(),
      weight: Number(weight),
      required: true,
      status: "pending",
      verification_profile: profile,
      evidence: [],
    });
    seen.add(ident);
  }
  return { schema_version: 2, authority: "nightwatch", policy_hash: policy.policy_hash, milestones };
}

function deepEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) && Array.isArray(right)) return left.length === right.length && left.every((item, index) => deepEqual(item, right[index]));
  if (isRecord(left) && isRecord(right)) {
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length && keys.every((key) => deepEqual(left[key], right[key]));
  }
  return false;
}

export function adoptProposedPlan(store: NightwatchStore): boolean {
  let plan: JsonObject;
  try {
    const proposed = readMailboxJson(store, "proposed-plan.json");
    if (proposed == null) return false;
    plan = proposalToPlan(store, proposed);
    validatePlan(plan);
  } catch (error) {
    if (error instanceof StateIntegrityError || error instanceof TypeError || error instanceof Error) {
      store.appendEvent("plan_rejected", "untrusted mailbox plan rejected", { error: rejectionName(error) });
      return false;
    }
    throw error;
  }
  const current = store.loadPlan();
  const commands = store.loadPolicy().final_commands;
  const fresh = [
    {
      id: "M1",
      title: "Complete the goal",
      weight: 100,
      required: true,
      status: "pending",
      verification_profile: Array.isArray(commands) && commands.length ? "default" : "none",
      evidence: [],
    },
  ];
  if (current.authority === "nightwatch" && !deepEqual(current.milestones, fresh)) {
    store.appendEvent("plan_change_ignored", "later untrusted plan replacement ignored");
    return false;
  }
  store.savePlan(plan);
  store.mutate("plan_adopted", "validated untrusted milestone structure adopted", (state) => ({ ...state, plan_ready: true }));
  return true;
}

export function ingestProgress(store: NightwatchStore): boolean {
  let raw: unknown;
  try {
    raw = readMailboxJson(store, "progress.json");
  } catch (error) {
    if (error instanceof Error && !(error instanceof StateIntegrityError)) {
      store.appendEvent("progress_rejected", "untrusted mailbox progress rejected", { error: rejectionName(error) });
      return false;
    }
    throw error;
  }
  if (raw == null) return false;
  const updates = isRecord(raw) && Object.keys(raw).length === 1 && Object.hasOwn(raw, "milestones") ? raw.milestones : null;
  if (!Array.isArray(updates) || updates.length > MAX_MILESTONES) {
    store.appendEvent("progress_rejected", "untrusted mailbox progress shape rejected");
    return false;
  }
  const plan = store.loadPlan();
  const milestones = plan.milestones as JsonObject[];
  const byId = new Map(milestones.map((item) => [String(item.id), item]));
  let changed = false;
  for (const update of updates) {
    if (!isRecord(update) || Object.keys(update).some((key) => key !== "id" && key !== "status" && key !== "reason") || !byId.has(String(update.id))) continue;
    const requested = update.status;
    const item = byId.get(String(update.id));
    if (!item) continue;
    if ((requested === "working" || requested === "implemented" || requested === "blocked") && item.status !== "verified") {
      if (item.status !== requested) {
        item.status = requested;
        changed = true;
      }
      if (requested === "blocked" && typeof update.reason === "string") {
        const evidence = Array.isArray(item.evidence) ? item.evidence : [];
        item.evidence = evidence;
        evidence.push({ at: nowIso(), kind: "untrusted_blocker", detail: redact(update.reason.slice(0, MAX_TITLE)) });
      }
    } else if (requested === "verified") {
      store.appendEvent("model_reported_verified", "model cannot verify a milestone", { id: update.id });
      if (item.status !== "verified") {
        item.status = "implemented";
        changed = true;
      }
    }
  }
  if (changed) {
    store.savePlan(plan);
    store.appendEvent("progress_ingested", "untrusted implementation progress ingested");
  }
  return changed;
}

export function trustedEnvironment(): Record<string, string> {
  const environment: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (typeof value !== "string") continue;
    if (TRUSTED_ENV.has(key) || key.startsWith("LC_") || key.startsWith("FAKE_CODEX_")) environment[key] = value;
  }
  return environment;
}

function osErrorName(error: NodeJS.ErrnoException): string {
  if (error.code === "ENOENT") return "FileNotFoundError";
  if (error.code === "EACCES" || error.code === "EPERM") return "PermissionError";
  return error.code || error.name || "OSError";
}

function runTrustedCommand(root: string, command: string, timeout = 120): JsonObject {
  const started = nowIso();
  if (command.trim() === "git diff --check") {
    const [ok, output] = diffCheck(root);
    return { command, ok, output: redact(output), started_at: started, finished_at: nowIso() };
  }
  const result = spawnSync("/bin/sh", ["-lc", command], {
    cwd: root,
    env: trustedEnvironment(),
    encoding: "utf8",
    timeout: timeout * 1000,
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 8 * 1024 * 1024,
  });
  const failure = result.error as NodeJS.ErrnoException | undefined;
  if (failure?.code === "ETIMEDOUT") {
    return { command, ok: false, output: "verification timed out", started_at: started, finished_at: nowIso() };
  }
  if (failure && result.status == null) {
    return { command, ok: false, output: `verification failed: ${osErrorName(failure)}`, started_at: started, finished_at: nowIso() };
  }
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  return {
    command,
    ok: result.status === 0,
    returncode: result.status,
    output: redact(output.slice(-4000)),
    started_at: started,
    finished_at: nowIso(),
  };
}

export function verifyMilestones(store: NightwatchStore, git: GitSnapshot | null = null): JsonObject {
  const plan = store.loadPlan();
  const commands = store.loadPolicy().final_commands;
  const commandList = Array.isArray(commands) ? commands.filter((item): item is string => typeof item === "string") : [];
  const milestones = plan.milestones as JsonObject[];
  const results: JsonObject[] = [];
  let changed = false;
  const eligible = milestones.filter((item) => (item.status === "working" || item.status === "implemented") && item.verification_profile === "default" && commandList.length);
  const root = repoPath(store);
  if (eligible.length) {
    const sharedChecks = commandList.map((command) => runTrustedCommand(root, command));
    const allPassed = sharedChecks.every((check) => check.ok === true);
    for (const item of eligible) {
      for (const check of sharedChecks) results.push({ id: item.id, ...check });
      if (allPassed) {
        item.status = "verified";
        const evidence = Array.isArray(item.evidence) ? item.evidence : [];
        item.evidence = evidence;
        evidence.push({
          at: nowIso(),
          kind: "trusted_policy_commands",
          policy_hash: plan.policy_hash,
          checks: sharedChecks,
        });
        changed = true;
        store.appendEvent("milestone_verified", "trusted policy checks passed", { id: item.id });
      }
    }
  }
  if (changed) store.savePlan(plan);
  const finalChecks = commandList.map((command) => runTrustedCommand(root, command));
  const required = milestones.filter((item) => item.required);
  return {
    milestones: results,
    final_checks: finalChecks,
    all_milestones_verified: required.every((item) => item.status === "verified"),
    all_final_checks_passed: finalChecks.length > 0 && finalChecks.every((check) => check.ok === true),
    progress: planProgress(plan),
    git: (git ?? snapshot(root)).toDict(),
  };
}

export function currentMilestone(store: NightwatchStore): JsonObject | null {
  const plan = store.loadPlan();
  const milestones = Array.isArray(plan.milestones) ? (plan.milestones as JsonObject[]) : [];
  return milestones.find((item) => item.status === "working" || item.status === "implemented" || item.status === "pending") ?? null;
}
