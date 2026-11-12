import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { AccountBrokerError } from "./account-errors.js";
import { CodexAuthAdapter } from "./account-broker.js";
import { GitError, repoRoot, snapshot } from "./git.js";
import {
  TERMINAL_STATES,
  validateAgyPrintTimeout,
  validateModelName,
  validateReasoningEffort,
  type JsonObject,
} from "./models.js";
import { getProviderAdapter } from "./providers.js";
import { makeQuotaProvider } from "./quota.js";
import { NightwatchStore, StateIntegrityError, makeRunId, nowIso, redact, repoIdentity } from "./storage.js";
import { Supervisor, findRepoCodexProcesses, SystemExit } from "./supervisor.js";
import { NightPath } from "./path.js";

export { SystemExit };

export const MAX_GOAL_CHARS = 4_000;
export const MAX_INSTRUCTION_CHARS = 4_000;
export const MAX_VERIFY_COMMANDS = 16;

export class ValueError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ValueError";
  }
}

export class RuntimeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "RuntimeError";
  }
}

export function validateHumanText(value: string, name: string, maxChars: number): string {
  if (typeof value !== "string") throw new TypeError(`${name} must be a string`);
  const clean = value.trim();
  if (!clean) throw new ValueError(`${name} cannot be empty`);
  if (clean.length > maxChars) throw new ValueError(`${name} exceeds maximum length (${clean.length} > ${maxChars})`);
  for (const char of clean) {
    const code = char.charCodeAt(0);
    if (code < 32 && char !== "\n" && char !== "\t" && char !== "\r") {
      throw new ValueError(`${name} contains forbidden control characters`);
    }
  }
  return clean;
}

export interface RunSpecInit {
  repo: string | NightPath;
  goal: string;
  model?: string | null;
  reasoningEffort?: string | null;
  verifyCommands?: readonly string[];
  threadId?: string | null;
  service?: boolean;
  accountMode?: string;
  accountSelectors?: readonly string[];
  provider?: string | null;
  agyPrintTimeout?: string | null;
}

export class RunSpec {
  readonly repo: NightPath;
  readonly goal: string;
  readonly model: string | null;
  readonly reasoningEffort: string | null;
  readonly verifyCommands: readonly string[];
  readonly threadId: string | null;
  readonly service: boolean;
  readonly accountMode: string;
  readonly accountSelectors: readonly string[];
  readonly provider: string;
  readonly agyPrintTimeout: string | null;

  constructor(
    repoOrInit: string | NightPath | RunSpecInit,
    goal?: string,
    model: string | null = null,
    reasoningEffort: string | null = null,
    verifyCommands: readonly string[] = [],
    threadId: string | null = null,
    service = true,
    accountMode = "current-only",
    accountSelectors: readonly string[] = [],
    provider: string | null = "codex",
    agyPrintTimeout: string | null = null,
  ) {
    const init = isRunSpecInit(repoOrInit)
      ? repoOrInit
      : {
          repo: repoOrInit,
          goal: goal ?? "",
          model,
          reasoningEffort,
          verifyCommands,
          threadId,
          service,
          accountMode,
          accountSelectors,
          provider,
          agyPrintTimeout,
        };
    this.repo = new NightPath(pathText(init.repo)).expanduser().resolve();
    this.goal = validateHumanText(init.goal, "goal", MAX_INSTRUCTION_CHARS);
    this.model = init.model == null ? null : validateModelName(init.model);
    this.reasoningEffort = init.reasoningEffort == null ? null : validateReasoningEffort(init.reasoningEffort);
    const commands = (init.verifyCommands ?? []).map((item) => validateHumanText(item, "verification command", MAX_INSTRUCTION_CHARS));
    if (commands.length > MAX_VERIFY_COMMANDS) {
      throw new ValueError(`too many verification commands (${commands.length} > ${MAX_VERIFY_COMMANDS})`);
    }
    this.verifyCommands = commands;
    if (init.threadId != null) {
      const thread = validateHumanText(init.threadId, "thread ID", 256);
      if (thread.includes("\n") || thread.includes("\t")) throw new ValueError("thread ID must be a single line");
      this.threadId = thread;
    } else {
      this.threadId = null;
    }
    const mode = (init.accountMode ?? "current-only").replaceAll("-", "_").toUpperCase();
    if (mode !== "CURRENT_ONLY" && mode !== "AUTO_POOL") throw new ValueError("account mode must be current-only or auto-pool");
    const selectors = (init.accountSelectors ?? []).map((item) => validateHumanText(item, "account selector", 512));
    if (mode === "AUTO_POOL" && selectors.length === 0) throw new ValueError("auto-pool requires at least one explicitly selected account");
    if (mode === "CURRENT_ONLY" && selectors.length > 0) throw new ValueError("--account selectors require --account-mode auto-pool");
    if (new Set(selectors).size !== selectors.length) throw new ValueError("account selectors must be unique");
    this.accountMode = mode;
    this.accountSelectors = selectors;
    this.service = init.service ?? true;
    const originalProvider = init.provider;
    const prov = (originalProvider || "codex").trim().toLowerCase();
    if (prov !== "codex" && prov !== "agy") {
      throw new ValueError(`unsupported provider: ${pyRepr(originalProvider ?? "")}; must be 'codex' or 'agy'`);
    }
    if (prov === "agy" && mode === "AUTO_POOL") throw new ValueError("AGY provider does not support auto-pool");
    this.provider = prov;
    if (init.agyPrintTimeout != null) this.agyPrintTimeout = validateAgyPrintTimeout(init.agyPrintTimeout);
    else this.agyPrintTimeout = prov === "agy" ? "60m" : null;
  }
}

export class ActionResult {
  constructor(
    readonly ok: boolean,
    readonly message: string,
  ) {}
}

export function serviceName(serviceRoot: string | NightPath): string {
  return `nightwatch-${repoIdentity(pathText(serviceRoot))}.service`;
}

export function installPaths(serviceRoot?: string | NightPath | null): [string, string] {
  const home = os.homedir();
  const unit = serviceRoot ? serviceName(serviceRoot) : "nightwatch.service";
  return [path.join(home, ".local", "bin", "nightwatch"), path.join(home, ".config", "systemd", "user", unit)];
}

export function systemdQuote(value: string): string {
  return `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"').replaceAll("%", "%%")}"`;
}

export function serviceText(serviceRoot: string | NightPath): string {
  // systemd parses ExecStart itself (not through a shell), so quote the repo
  // there as one argument. WorkingDirectory is a unit path setting: quotes
  // are literal for that setting, therefore it must remain an absolute raw path.
  const template = fs.readFileSync(serviceTemplatePath(), "utf8");
  const root = pathText(serviceRoot instanceof NightPath ? serviceRoot : new NightPath(serviceRoot).resolve());
  const unitRoot = systemdQuote(root);
  const directoryRoot = root.replaceAll("%", "%%");
  const rendered = template.replace(
    "ExecStart=%h/.local/bin/nightwatch resume",
    `WorkingDirectory=${directoryRoot}\nExecStart=%h/.local/bin/nightwatch resume --repo ${unitRoot}`,
  );
  return `# nightwatch-install\n${rendered}`;
}

/** Keep an existing nightwatch launcher: install.sh marker or pip console script. */
export function launcherIsReusable(text: string): boolean {
  return text.includes("nightwatch-install:") || text.includes("from nightwatch.cli import main");
}

export function validateInstallTargets(serviceRoot?: string | NightPath | null): void {
  const [launcher, service] = installPaths(serviceRoot);
  if (existsOrLink(launcher)) {
    const existing = readTextReplace(launcher);
    if (!launcherIsReusable(existing)) throw new SystemExit(`nightwatch: refusing to overwrite existing ${launcher}`);
  }
  if (serviceRoot == null || !existsOrLink(service)) return;
  const existing = readTextReplace(service);
  if (!existing.includes("# nightwatch-install")) throw new SystemExit(`nightwatch: refusing to overwrite existing ${service}`);
  const existingRoot = existing
    .split(/\r?\n/)
    .filter((line) => line.startsWith("WorkingDirectory="))
    .map((line) => line.slice("WorkingDirectory=".length).replace(/^"+|"+$/g, ""))[0];
  if (existingRoot !== undefined) {
    const bound = new NightPath(existingRoot.replaceAll("%%", "%")).resolve().raw;
    const wanted = new NightPath(pathText(serviceRoot)).resolve().raw;
    if (bound !== wanted) {
      throw new SystemExit(`nightwatch: existing user service is bound to ${existingRoot}; refusing to repoint it to ${pathText(serviceRoot)}`);
    }
  }
}

export function atomicWrite(filePath: string, content: string, mode: number): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const temporary = path.join(path.dirname(filePath), `.${path.basename(filePath)}.${process.pid}.tmp`);
  fs.writeFileSync(temporary, content, "utf8");
  fs.chmodSync(temporary, mode);
  fs.renameSync(temporary, filePath);
}

export function backupMarkedInstall(filePath: string): void {
  if (!existsOrLink(filePath)) return;
  const text = readTextReplace(filePath);
  if (!text.includes("nightwatch-install")) throw new SystemExit(`nightwatch: refusing to overwrite existing ${filePath}`);
  const stamp = nowIso().replaceAll(":", "-").replaceAll(".", "-");
  const backupRoot = path.join(os.homedir(), ".local", "state", "codex-nightwatch", "install-backups", stamp);
  fs.mkdirSync(path.dirname(backupRoot), { recursive: true });
  fs.mkdirSync(backupRoot, { mode: 0o700 });
  fs.chmodSync(backupRoot, 0o700);
  atomicWrite(path.join(backupRoot, path.basename(filePath)), text, 0o600);
}

export function installUserFiles(serviceRoot?: string | NightPath | null): [string, string | null] {
  validateInstallTargets(serviceRoot);
  const sourceRoot = packageRoot();
  const [launcher, service] = installPaths(serviceRoot);
  const launcherText = `#!/bin/sh\n# nightwatch-install: ${sourceRoot}\nexec python3 ${path.join(sourceRoot, "bin", "nightwatch")} "$@"\n`;
  const existingLauncher = existsOrLink(launcher) ? readTextReplace(launcher) : "";
  if (!(existingLauncher && launcherIsReusable(existingLauncher))) {
    if (existingLauncher) backupMarkedInstall(launcher);
    atomicWrite(launcher, launcherText, 0o755);
  }
  if (serviceRoot != null) {
    if (existsOrLink(service) && readTextReplace(service).includes("nightwatch-install")) backupMarkedInstall(service);
    else if (existsOrLink(service)) throw new SystemExit(`nightwatch: refusing to overwrite existing ${service}`);
    atomicWrite(service, serviceText(serviceRoot), 0o600);
    return [launcher, service];
  }
  return [launcher, null];
}

export function startUserService(serviceUnitName = "nightwatch.service"): void {
  const binary = which("systemctl");
  if (!binary) throw new RuntimeError("systemctl is not installed");
  for (const args of [
    [binary, "--user", "daemon-reload"],
    [binary, "--user", "enable", "--now", serviceUnitName],
  ]) {
    let result: ReturnType<typeof spawnSync>;
    try {
      result = spawnSync(args[0], args.slice(1), { timeout: 15_000, stdio: "ignore" });
    } catch (error) {
      throw new RuntimeError("systemctl --user is unavailable");
    }
    if (result.error || result.status !== 0) {
      if (result.error) throw new RuntimeError("systemctl --user is unavailable");
      throw new RuntimeError("systemctl --user could not contact or start the user service");
    }
  }
}

export function resumeService(repo: string | NightPath): ActionResult {
  const root = repoRoot(pathText(repo));
  const store = new NightwatchStore(root.raw);
  const providerName = store.exists() ? String(store.loadState().provider ?? "codex") : "codex";
  const live = providerName === "agy" ? providerOf("agy").findActiveProcesses(root.raw) : findRepoCodexProcesses(root.raw);
  if (live.length) {
    const pids = live.map((item) => String(item.pid)).join(", ");
    return new ActionResult(false, `Interactive ${capitalize(providerName)} still running (PID ${pids}); close it before /resume`);
  }
  try {
    validateInstallTargets(root.raw);
    const [, service] = installUserFiles(root.raw);
    const name = serviceName(root.raw);
    startUserService(name);
    return new ActionResult(true, `Resume service started: ${service ? path.basename(service) : name}`);
  } catch (error) {
    if (isServiceFailure(error)) return new ActionResult(false, `Resume service was not started: ${errorText(error)}`);
    throw error;
  }
}

export function adoptRun(spec: RunSpec): ActionResult {
  if ((spec.accountMode || "").replaceAll("-", "_").toUpperCase() === "AUTO_POOL") {
    return new ActionResult(false, "AUTO_POOL adoption of existing interactive threads is not supported; use CURRENT_ONLY to adopt interactive threads.");
  }
  const bound = new RunSpec({
    repo: spec.repo,
    goal: spec.goal,
    model: spec.model,
    reasoningEffort: spec.reasoningEffort,
    verifyCommands: spec.verifyCommands,
    threadId: spec.threadId,
    service: false,
    accountMode: spec.accountMode,
    accountSelectors: spec.accountSelectors,
    provider: spec.provider,
    agyPrintTimeout: spec.agyPrintTimeout,
  });
  const result = startRun(bound, false);
  if (!result.ok) return result;
  const live = spec.provider === "agy" ? providerOf("agy").findActiveProcesses(bound.repo.raw) : findRepoCodexProcesses(bound.repo.raw);
  let suffix = " Use /resume to start unattended supervision.";
  if (live.length) {
    const pids = live.map((item) => String(item.pid)).join(", ");
    suffix = ` Interactive ${capitalize(spec.provider)} still running (PID ${pids}); /resume after it exits.`;
  }
  return new ActionResult(true, `Adopted thread ${bound.threadId ?? "None"} as NEW.${suffix}`);
}

export function stopRun(repo: string | NightPath): ActionResult {
  const root = repoRoot(pathText(repo));
  const store = new NightwatchStore(root.raw);
  if (!store.exists()) return new ActionResult(false, `No Nightwatch run found in ${root.raw}`);
  const state = store.loadState();
  if (state.state === "DONE" || state.state === "STOPPED" || state.state === "AWAITING_ACCEPTANCE") {
    return new ActionResult(true, `Nightwatch ${String(state.state)}`);
  }
  const supervisor = new Supervisor(store);
  supervisor.requestStop();
  supervisor.store.transition("STOPPED", "manual_stop", "user requested stop; no automatic recovery will continue");
  return new ActionResult(true, "Nightwatch STOPPED; durable state preserved");
}

export function queueSteer(store: NightwatchStore, instruction: string): ActionResult {
  const text = validateHumanText(instruction, "instruction", MAX_INSTRUCTION_CHARS);
  const state = store.loadState();
  const current = state.state;
  if (typeof current === "string" && TERMINAL_STATES.has(current as never)) {
    return new ActionResult(false, "Instruction was NOT queued because this Nightwatch run is terminal.\nUse /resume or start a new supervised run before steering.");
  }
  const providerName = String(state.provider ?? "codex");
  const adapter = providerOf(providerName);
  if (!adapter.supportsLiveSteering()) {
    const [ok, message] = adapter.steer(store.repo, typeof state.thread_id === "string" ? state.thread_id : "", text);
    return new ActionResult(ok, message);
  }
  const thread = state.thread_id;
  if (typeof thread !== "string" || !thread) return new ActionResult(false, "No exact thread has been captured; steering was not sent.");
  const binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
  const result = spawnSync(binary, ["queue", "--thread", thread, "--message", text], {
    cwd: pathText(store.repo),
    timeout: 15_000,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  if (result.error) return new ActionResult(false, "Codex queue transport was unavailable; no instruction was recorded as delivered.");
  if (result.status !== 0) return new ActionResult(false, "Codex did not accept the queued instruction; inspect the exact thread state.");
  const digest = createHash("sha256").update(text, "utf8").digest("hex");
  store.appendEvent("user_instruction_queued", "confirmed user instruction queued to exact thread", {
    instruction_sha256: digest,
    instruction_chars: text.length,
  });
  return new ActionResult(true, `Instruction queued to exact thread ${thread}.`);
}

const WORKTREE_LABEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export function createWorktree(sourceRepo: string | NightPath, label: string): NightPath {
  const source = repoRoot(pathText(sourceRepo));
  if (!WORKTREE_LABEL.test(label)) throw new ValueError("worktree label must contain only letters, digits, '.', '_', or '-'");
  const worktreesRoot = path.join(source.raw, "..", ".worktrees");
  const layout = path.resolve(worktreesRoot, source.name);
  for (const candidate of [path.resolve(worktreesRoot), layout]) {
    if (isSymlink(candidate)) throw new ValueError(`worktree layout must not contain symlinks: ${candidate}`);
  }
  const target = path.join(layout, label);
  if (existsOrLink(target)) throw new ValueError(`worktree target already exists: ${target}`);
  fs.mkdirSync(layout, { recursive: true });
  if (realOrLexical(layout) !== path.resolve(layout)) throw new ValueError("worktree layout resolved outside its confirmed path");
  const branch = `nightwatch/${label}`;
  let result: ReturnType<typeof spawnSync>;
  try {
    result = spawnSync("git", ["worktree", "add", "-b", branch, target, "HEAD"], {
      cwd: source.raw,
      timeout: 30_000,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    throw new GitError("git worktree add failed");
  }
  if (result.error) throw new GitError("git worktree add failed");
  if (result.status !== 0) throw new GitError("git worktree add failed; branch or target may already exist");
  return new NightPath(fs.realpathSync(target));
}

export function startRun(spec: RunSpec, runInService = true): ActionResult {
  const root = repoRoot(spec.repo.raw);
  const store = new NightwatchStore(root.raw);
  if (store.exists()) {
    try {
      const state = store.loadState();
      return new ActionResult(false, `A run already exists in ${root.raw} (state=${String(state.state)}); use /resume`);
    } catch (error) {
      if (error instanceof StateIntegrityError) return new ActionResult(false, `Refusing to overwrite invalid durable state: ${errorText(error)}`);
      throw error;
    }
  }
  let authorized: string[];
  try {
    authorized = resolveAuthorizedAccounts(spec, root);
  } catch (error) {
    if (error instanceof AccountBrokerError || error instanceof ValueError) {
      return new ActionResult(false, `Account configuration rejected: ${errorText(error)}`);
    }
    throw error;
  }
  const state = store.initialize(
    makeRunId(root.raw),
    spec.goal,
    root.raw,
    null,
    [...spec.verifyCommands],
    spec.threadId,
    spec.model,
    spec.reasoningEffort,
    spec.accountMode,
    authorized,
    spec.provider,
    spec.agyPrintTimeout,
  );
  if (runInService) {
    try {
      validateInstallTargets(root.raw);
      installUserFiles(root.raw);
      startUserService(serviceName(root.raw));
      return new ActionResult(true, `Nightwatch service started: run_id=${String(state.run_id)} repo=${root.raw}`);
    } catch (error) {
      if (isServiceFailure(error)) {
        return new ActionResult(false, `Goal saved as NEW, but user service could not be started; run \`nightwatch resume --repo ${root.raw}\`: ${errorText(error)}`);
      }
      throw error;
    }
  }
  return new ActionResult(true, `Nightwatch goal initialized: run_id=${String(state.run_id)}`);
}

/** Resolve explicit human selectors to stable codex-auth account keys. */
export function resolveAuthorizedAccounts(spec: RunSpec, _root: string | NightPath): string[] {
  if (spec.accountMode === "CURRENT_ONLY") return [];
  const accounts = new CodexAuthAdapter().listAccounts();
  const selected: string[] = [];
  for (const selector of spec.accountSelectors) {
    const exact = accounts.filter((item) => item.accountKey === selector || item.fingerprint === selector);
    const matches = exact.length
      ? exact
      : accounts.filter((item) => {
          const folded = new Set([item.alias, item.accountName].filter((value): value is string => Boolean(value)).map((value) => value.toLowerCase()));
          return folded.has(selector.toLowerCase());
        });
    if (matches.length !== 1) throw new ValueError(`account selector ${pyRepr(selector)} did not resolve to exactly one stored account`);
    const key = matches[0].accountKey;
    if (!selected.includes(key)) selected.push(key);
  }
  if (!selected.length) throw new ValueError("auto-pool requires at least one explicitly selected account");
  return selected;
}

/** Return local account choices for human confirmation surfaces. */
export function listAccountChoices() {
  return new CodexAuthAdapter().listAccounts();
}

export function listModels(provider = "codex"): JsonObject[] {
  if (provider === "agy") return providerOf("agy").listModels();
  const binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
  const result = spawnSync(binary, ["debug", "models"], { timeout: 15_000, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
  if (result.error) throw new RuntimeError("installed Codex model catalog is unavailable");
  if (result.status !== 0) throw new RuntimeError("installed Codex model catalog command failed");
  let rawModels: unknown;
  try {
    rawModels = JSON.parse(result.stdout ?? "").models;
  } catch {
    throw new RuntimeError("installed Codex returned an invalid model catalog");
  }
  if (!Array.isArray(rawModels)) throw new RuntimeError("installed Codex returned an invalid model catalog");
  const catalog: JsonObject[] = [];
  for (const item of rawModels) {
    if (!item || typeof item !== "object") continue;
    const record = item as JsonObject;
    if (record.visibility != null && record.visibility !== "list") continue;
    let slug: string;
    try {
      slug = validateModelName(String(record.slug));
    } catch {
      continue;
    }
    let fallback: string | null = null;
    if (record.default_reasoning_level != null) {
      try {
        fallback = validateReasoningEffort(String(record.default_reasoning_level));
      } catch {
        fallback = null;
      }
    }
    const levels: string[] = [];
    const rawLevels = Array.isArray(record.supported_reasoning_levels) ? record.supported_reasoning_levels : [];
    for (const level of rawLevels) {
      const candidate = level && typeof level === "object" ? (level as JsonObject).effort : null;
      if (typeof candidate !== "string") continue;
      try {
        const valid = validateReasoningEffort(candidate);
        if (!levels.includes(valid)) levels.push(valid);
      } catch {
        continue;
      }
    }
    let displayName = typeof record.display_name === "string" ? record.display_name : slug;
    displayName = displayName.split(/\s+/).filter(Boolean).join(" ").slice(0, 80) || slug;
    catalog.push({
      slug,
      display_name: displayName,
      default_reasoning_level: fallback,
      supported_reasoning_levels: levels,
    });
  }
  if (!catalog.length) throw new RuntimeError("installed Codex model catalog contains no visible models");
  return catalog;
}

export function doctorSnapshot(repo?: string | NightPath | null): JsonObject {
  const binary = process.env.NIGHTWATCH_CODEX_BIN || "codex";
  const inhibitor = which("systemd-inhibit");
  let inhibitOk = false;
  if (inhibitor) {
    const probe = spawnSync(inhibitor, ["--what=sleep", "--mode=block", "--why=Nightwatch probe", "/bin/true"], {
      timeout: 5_000,
      stdio: "ignore",
    });
    inhibitOk = !probe.error && probe.status === 0;
  }
  const report: JsonObject = {
    platform: process.platform,
    python: process.versions.node,
    linux_first: process.platform.startsWith("linux"),
    codex_binary: which(binary) || (isFile(binary) ? binary : null),
    systemd_inhibit: inhibitOk,
    auth: { status: "unknown" },
    quota: { status: "unknown" },
    account_pool: { status: "optional_unavailable", count: 0 },
  };
  try {
    const accounts = new CodexAuthAdapter().listAccounts();
    report.account_pool = { status: "available", count: accounts.length, fingerprints: accounts.map((item) => item.fingerprint) };
  } catch (error) {
    if (error instanceof AccountBrokerError) {
      report.account_pool = { status: "optional_unavailable", count: 0, error: error.name };
    } else {
      throw error;
    }
  }
  const version = spawnSync(binary, ["--version"], { timeout: 8_000, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
  report.codex_version = !version.error && version.status === 0 ? (version.stdout ?? "").trim() : null;
  const auth = spawnSync(binary, ["login", "status"], { timeout: 8_000, stdio: "ignore" });
  report.auth = auth.error ? { status: "fail", credentials_read: false } : { status: auth.status === 0 ? "ok" : "fail", credentials_read: false };
  try {
    const quota = makeQuotaProvider().read();
    report.quota = {
      status: "ok",
      authority: quota.source === "live_app_server" ? "LIVE_APP_SERVER" : "ROLLOUT_SCHEDULE_ONLY",
      recovery_capability: quota.source === "live_app_server" ? "LIVE_REVALIDATION" : "GUARDED_PROBE_ONLY",
      ...quota.toDict(),
    };
  } catch (error) {
    const redacted = redact(error instanceof Error ? error.message : String(error));
    report.quota = {
      status: "unavailable",
      source: "none",
      error: (typeof redacted === "string" && redacted) || (error instanceof Error ? error.name : "Error"),
    };
  }
  if (repo != null) {
    try {
      const root = repoRoot(pathText(repo));
      report.git = snapshot(root.raw).toDict();
      const store = new NightwatchStore(root.raw);
      report.nightwatch_state = store.exists() ? new NightwatchStore(root.raw).loadState().state : "NO_RUN";
    } catch (error) {
      if (error instanceof SystemExit || error instanceof StateIntegrityError || error instanceof GitError) {
        report.git = { status: "unavailable", error: errorText(error) };
      } else {
        throw error;
      }
    }
  }
  try {
    report.agy = providerOf("agy").doctorCheck(repo == null ? null : pathText(repo));
  } catch (error) {
    report.agy = { status: "fail", error: errorText(error) };
  }
  const codexOk = Boolean(report.codex_binary && (report.auth as JsonObject).status === "ok");
  const agy = report.agy as JsonObject | undefined;
  const agyOk = Boolean(agy && agy.status === "ok");
  report.status = codexOk || agyOk ? "ok" : "fail";
  return report;
}

interface ProviderPort {
  resolveBinary(): string;
  findActiveProcesses(repo: unknown, excludePid?: number | null): JsonObject[];
  listModels(): JsonObject[];
  supportsLiveSteering(): boolean;
  steer(repo: unknown, threadId: string, instruction: string): [boolean, string];
  doctorCheck(repo?: string | null): JsonObject;
}

function providerOf(name: string): ProviderPort {
  return getProviderAdapter(name) as unknown as ProviderPort;
}

function isRunSpecInit(value: string | NightPath | RunSpecInit): value is RunSpecInit {
  return Boolean(value) && typeof value === "object" && !(value instanceof NightPath) && "goal" in value && "repo" in value;
}

function pathText(value: string | NightPath | { toString(): string }): string {
  if (typeof value === "string") return value;
  if (value instanceof NightPath) return value.raw;
  return String(value);
}

function pyRepr(value: string): string {
  return `'${value.replaceAll("\\", "\\\\").replaceAll("'", "\\'")}'`;
}

function capitalize(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function isServiceFailure(error: unknown): boolean {
  return error instanceof RuntimeError || error instanceof SystemExit || error instanceof StateIntegrityError || isOsError(error);
}

function isOsError(error: unknown): boolean {
  const code = (error as NodeJS.ErrnoException | undefined)?.code;
  return typeof code === "string" && /^E[A-Z0-9]+$/.test(code);
}

function existsOrLink(filePath: string): boolean {
  try {
    fs.lstatSync(filePath);
    return true;
  } catch {
    return false;
  }
}

function isSymlink(filePath: string): boolean {
  try {
    return fs.lstatSync(filePath).isSymbolicLink();
  } catch {
    return false;
  }
}

function isFile(filePath: string): boolean {
  try {
    return fs.statSync(filePath).isFile();
  } catch {
    return false;
  }
}

function readTextReplace(filePath: string): string {
  try {
    return fs.readFileSync(filePath).toString("utf8");
  } catch {
    return "";
  }
}

function which(command: string): string | null {
  if (command.includes("/")) return isExecutable(command) ? path.resolve(command) : null;
  const pathEnv = process.env.PATH ?? "/usr/bin:/bin";
  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const full = path.join(dir, command);
    if (isExecutable(full)) return full;
  }
  return null;
}

function isExecutable(filePath: string): boolean {
  try {
    const stat = fs.statSync(filePath);
    if (!stat.isFile()) return false;
    fs.accessSync(filePath, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function moduleDir(): string {
  return path.dirname(fileURLToPath(import.meta.url));
}

function packageRoot(): string {
  const candidates = [path.resolve(moduleDir(), "../nightwatch"), path.resolve(moduleDir(), "..")];
  for (const candidate of candidates) {
    if (fs.existsSync(path.join(candidate, "bin", "nightwatch"))) return candidate;
  }
  return candidates[0];
}

function serviceTemplatePath(): string {
  const candidates = [
    path.join(packageRoot(), "nightwatch", "nightwatch.service"),
    path.join(moduleDir(), "nightwatch.service"),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error("nightwatch.service template is missing");
}

function realOrLexical(filePath: string): string {
  const lexical = path.resolve(filePath);
  try {
    return fs.realpathSync(lexical);
  } catch {
    try {
      return path.join(fs.realpathSync(path.dirname(lexical)), path.basename(lexical));
    } catch {
      return lexical;
    }
  }
}
