#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

import { AccountBrokerError } from "./account-errors.js";
import { GitError, repoRoot } from "./git.js";
import {
  State,
  TERMINAL_STATES,
  planProgress,
  validateAgyPrintTimeout,
  validateModelName,
  validateReasoningEffort,
  type JsonObject,
} from "./models.js";
import {
  RunSpec,
  doctorSnapshot,
  installPaths,
  installUserFiles,
  listModels,
  queueSteer,
  resolveAuthorizedAccounts,
  serviceName,
  startUserService,
  validateInstallTargets,
  ValueError,
  RuntimeError,
  SystemExit,
} from "./operations.js";
import { AppServerQuotaProvider } from "./quota.js";
import {
  NightwatchStore,
  StateIntegrityError,
  SupervisorAlreadyRunning,
  makeRunId,
} from "./storage.js";
import {
  PassiveWatcher,
  Supervisor,
  buildReport,
  pidAlive,
  processMatches,
} from "./supervisor.js";
import { launchTui } from "./tui.js";
import { serve } from "./web.js";

const VERSION = readPackageVersion();

type ProviderName = "codex" | "agy";

interface CliArgs {
  command: string;
  goal?: string;
  repo?: string | null;
  thread?: string | null;
  provider?: ProviderName | null;
  noInhibit?: boolean;
  service?: boolean;
  verify?: string[];
  agyPrintTimeout?: string | null;
  model?: string | null;
  reasoningEffort?: string | null;
  accountMode?: string;
  accounts?: string[];
  json?: boolean;
  once?: boolean;
  autoTakeover?: boolean;
  watch?: boolean;
  interval?: number;
  tail?: number;
  instruction?: string;
  host?: string;
  port?: number;
  noBrowser?: boolean;
  ackAmbiguous?: boolean;
  testName?: string | null;
  help?: boolean;
  version?: boolean;
}

function readPackageVersion(): string {
  try {
    const pkgPath = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "package.json");
    const raw = JSON.parse(fs.readFileSync(pkgPath, "utf8")) as { version?: string };
    return raw.version ?? "0.7.0";
  } catch {
    return "0.7.0";
  }
}

function which(command: string): string | null {
  if (command.includes(path.sep) || command.includes("/")) {
    try {
      fs.accessSync(command, fs.constants.X_OK);
      return path.resolve(command);
    } catch {
      return null;
    }
  }
  const pathEnv = process.env.PATH ?? "/usr/bin:/bin";
  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const full = path.join(dir, command);
    try {
      fs.accessSync(full, fs.constants.X_OK);
      if (fs.statSync(full).isFile()) return full;
    } catch {
      /* try next */
    }
  }
  return null;
}

function printHelp(): void {
  console.log(`nightwatch ${VERSION}

Fail-closed multi-thread control plane for OpenAI Codex

Usage: nightwatch <command> [options]

Commands:
  run         start a new supervised goal
  resume      resume the existing exact-thread goal
  recover     record explicit recovery of an ambiguous exact-thread lease
  status      show current durable status
  log         show human-readable supervisor log
  report      write/show a durable report
  stop        stop automatic work and preserve state
  steer       queue an instruction to the active supervised run
  doctor      check Linux, Codex, auth, quota, and local state support
  models      show the installed Codex or AGY model catalog
  install     install a user-local launcher
  uninstall   remove only Nightwatch-owned user-local installation
  ui          open the interactive multi-thread terminal control plane
  web         open the local browser control plane
  test        run non-destructive product checks
  adopt       bind an existing exact thread into Nightwatch control plane
  watch       passively monitor an active Codex session in this repo

Global:
  --version   show version
  --help, -h  show this help
`);
}

function usageError(message: string): never {
  throw new SystemExit(`nightwatch: ${message}`);
}

function parseModel(value: string): string {
  try {
    return validateModelName(value);
  } catch (error) {
    usageError(error instanceof Error ? error.message : String(error));
  }
}

function parseReasoning(value: string): string {
  try {
    return validateReasoningEffort(value);
  } catch (error) {
    usageError(error instanceof Error ? error.message : String(error));
  }
}

function parseAgyTimeout(value: string): string {
  try {
    return validateAgyPrintTimeout(value);
  } catch (error) {
    usageError(error instanceof Error ? error.message : String(error));
  }
}

function parseInterval(value: string): number {
  const interval = Number(value);
  if (!Number.isFinite(interval)) usageError("interval must be a number");
  if (!(interval >= 0.2 && interval <= 60)) usageError("interval must be between 0.2 and 60 seconds");
  return interval;
}

function takeOptionValue(argv: string[], index: number, flag: string): [string, number] {
  const current = argv[index];
  if (current.includes("=")) {
    return [current.slice(current.indexOf("=") + 1), index];
  }
  const next = argv[index + 1];
  if (next == null || next.startsWith("-")) usageError(`option ${flag} requires a value`);
  return [next, index + 1];
}

function parseProvider(value: string): ProviderName {
  if (value === "codex" || value === "agy") return value;
  usageError(`provider must be codex or agy (got ${JSON.stringify(value)})`);
}

function parseCliArgs(argv: string[]): CliArgs {
  if (argv.includes("--version") || argv.includes("-V")) {
    return { command: "__version__" };
  }
  if (argv.length === 0) {
    return { command: "__empty__" };
  }
  if (argv[0] === "--help" || argv[0] === "-h") {
    return { command: "__help__" };
  }

  const command = argv[0];
  if (command.startsWith("-")) usageError(`unknown option ${command}`);
  const rest = argv.slice(1);
  const args: CliArgs = { command, verify: [], accounts: [] };

  const known = new Set([
    "run",
    "resume",
    "recover",
    "status",
    "log",
    "report",
    "stop",
    "steer",
    "doctor",
    "models",
    "install",
    "uninstall",
    "ui",
    "web",
    "test",
    "adopt",
    "watch",
  ]);
  if (!known.has(command)) usageError(`unknown command ${JSON.stringify(command)}`);

  const positionals: string[] = [];
  for (let i = 0; i < rest.length; i += 1) {
    const token = rest[i];
    if (token === "--help" || token === "-h") {
      args.help = true;
      continue;
    }
    if (!token.startsWith("-")) {
      positionals.push(token);
      continue;
    }
    const flag = token.includes("=") ? token.slice(0, token.indexOf("=")) : token;
    switch (flag) {
      case "--repo": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.repo = value;
        i = next;
        break;
      }
      case "--thread": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.thread = value;
        i = next;
        break;
      }
      case "--provider": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.provider = parseProvider(value);
        i = next;
        break;
      }
      case "--model": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.model = parseModel(value);
        i = next;
        break;
      }
      case "--reasoning-effort": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.reasoningEffort = parseReasoning(value);
        i = next;
        break;
      }
      case "--agy-print-timeout": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.agyPrintTimeout = parseAgyTimeout(value);
        i = next;
        break;
      }
      case "--account-mode": {
        const [value, next] = takeOptionValue(rest, i, flag);
        if (value !== "current-only" && value !== "auto-pool") {
          usageError("account-mode must be current-only or auto-pool");
        }
        args.accountMode = value;
        i = next;
        break;
      }
      case "--account": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.accounts = [...(args.accounts ?? []), value];
        i = next;
        break;
      }
      case "--verify": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.verify = [...(args.verify ?? []), value];
        i = next;
        break;
      }
      case "--interval": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.interval = parseInterval(value);
        i = next;
        break;
      }
      case "--tail": {
        const [value, next] = takeOptionValue(rest, i, flag);
        const n = Number.parseInt(value, 10);
        if (!Number.isFinite(n)) usageError("tail must be an integer");
        args.tail = n;
        i = next;
        break;
      }
      case "--host": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.host = value;
        i = next;
        break;
      }
      case "--port": {
        const [value, next] = takeOptionValue(rest, i, flag);
        const n = Number.parseInt(value, 10);
        if (!Number.isFinite(n)) usageError("port must be an integer");
        args.port = n;
        i = next;
        break;
      }
      case "--goal": {
        const [value, next] = takeOptionValue(rest, i, flag);
        args.goal = value;
        i = next;
        break;
      }
      case "--no-inhibit":
        args.noInhibit = true;
        break;
      case "--service":
        args.service = true;
        break;
      case "--json":
        args.json = true;
        break;
      case "--once":
        args.once = true;
        break;
      case "--auto-takeover":
        args.autoTakeover = true;
        break;
      case "--watch":
        args.watch = true;
        break;
      case "--no-browser":
        args.noBrowser = true;
        break;
      case "--ack-ambiguous":
        args.ackAmbiguous = true;
        break;
      default:
        usageError(`unknown option ${flag}`);
    }
  }

  switch (command) {
    case "run":
      if (positionals.length < 1) usageError("run requires a goal");
      args.goal = positionals[0];
      if (positionals.length > 1) usageError("run accepts a single goal argument");
      args.accountMode = args.accountMode ?? "current-only";
      args.verify = args.verify ?? [];
      args.accounts = args.accounts ?? [];
      break;
    case "adopt":
      if (!args.thread) usageError("adopt requires --thread");
      args.goal = positionals[0] ?? "Supervise adopted conversation";
      if (positionals.length > 1) usageError("adopt accepts at most one goal argument");
      args.accountMode = args.accountMode ?? "current-only";
      args.verify = args.verify ?? [];
      args.accounts = args.accounts ?? [];
      break;
    case "steer":
      if (positionals.length < 1) usageError("steer requires an instruction");
      args.instruction = positionals[0];
      if (positionals.length > 1) usageError("steer accepts a single instruction argument");
      break;
    case "test":
      args.testName = positionals[0] ?? null;
      if (positionals.length > 1) usageError("test accepts at most one test name");
      break;
    case "status":
      args.interval = args.interval ?? 2.0;
      break;
    case "log":
      args.tail = args.tail ?? 80;
      break;
    case "web":
      args.host = args.host ?? "127.0.0.1";
      args.port = args.port ?? 8765;
      break;
    case "watch":
      args.verify = args.verify ?? [];
      break;
    default:
      if (positionals.length) usageError(`${command} does not take positional arguments`);
      break;
  }

  return args;
}

function rootOf(value?: string | null): string {
  try {
    return repoRoot(value || process.cwd()).raw;
  } catch (error) {
    if (error instanceof GitError) {
      throw new SystemExit("nightwatch: command requires a Git repository");
    }
    throw error;
  }
}

function storeOf(value?: string | null): NightwatchStore {
  return new NightwatchStore(rootOf(value));
}

function exitCodeForState(state: string): number {
  switch (state) {
    case State.DONE:
    case State.AWAITING_ACCEPTANCE:
      return 0;
    case State.BLOCKED:
      return 10;
    case State.STOPPED:
      return 11;
    case State.FAILED:
      return 12;
    default:
      return 20;
  }
}

function installSignalHandlers(supervisor: Supervisor): void {
  const handle = () => {
    try {
      supervisor.requestStop();
    } catch {
      /* best-effort */
    }
  };
  process.on("SIGINT", handle);
  process.on("SIGTERM", handle);
}

function nodeRelaunchPrefix(): string[] {
  const entry = fileURLToPath(import.meta.url);
  const argv = process.argv;
  const entryIdx = argv.findIndex((item) => {
    try {
      return path.resolve(item) === path.resolve(entry) || item.endsWith("/cli.ts") || item.endsWith("/cli.js");
    } catch {
      return false;
    }
  });
  if (entryIdx > 0) return argv.slice(0, entryIdx + 1);
  if (entry.endsWith(".ts")) return [process.execPath, "--import", "tsx", entry];
  return [process.execPath, entry];
}

function maybeInhibit(args: CliArgs, rootPath: string): void {
  if (args.noInhibit || process.env.NIGHTWATCH_INHIBITED === "1") return;
  const binary = which("systemd-inhibit");
  if (!binary) return;
  try {
    const probe = spawnSync(binary, ["--what=sleep", "--mode=block", "--why=Nightwatch probe", "/bin/true"], {
      timeout: 5_000,
      stdio: "ignore",
    });
    if (probe.error || probe.status !== 0) {
      console.error("nightwatch: systemd-inhibit is not usable here; continuing without a sleep lock");
      return;
    }
  } catch {
    console.error("nightwatch: systemd-inhibit is not usable here; continuing without a sleep lock");
    return;
  }

  const child: string[] = [...nodeRelaunchPrefix(), args.command];
  if (args.command === "run") {
    child.push(String(args.goal ?? ""));
    if (args.provider) child.push("--provider", args.provider);
    if (args.agyPrintTimeout) child.push("--agy-print-timeout", args.agyPrintTimeout);
    if (args.thread) child.push("--thread", args.thread);
    if (args.model) child.push("--model", args.model);
    if (args.reasoningEffort) child.push("--reasoning-effort", args.reasoningEffort);
    if ((args.accountMode ?? "current-only") !== "current-only") {
      child.push("--account-mode", String(args.accountMode));
    }
    for (const selector of args.accounts ?? []) child.push("--account", selector);
    for (const command of args.verify ?? []) child.push("--verify", command);
  }
  child.push("--repo", rootPath, "--no-inhibit");

  const env = { ...process.env, NIGHTWATCH_INHIBITED: "1" };
  const result = spawnSync(
    binary,
    ["--what=sleep", "--mode=block", "--why=Nightwatch supervisor", ...child],
    { env, stdio: "inherit" },
  );
  process.exit(result.status ?? (result.signal ? 1 : 0));
}

async function cmdRun(args: CliArgs): Promise<number> {
  const root = rootOf(args.repo);
  const store = new NightwatchStore(root);
  if (store.exists()) {
    try {
      store.loadState();
    } catch (error) {
      if (error instanceof StateIntegrityError) {
        throw new SystemExit(`nightwatch: refusing to overwrite invalid durable state: ${error.message}`);
      }
      throw error;
    }
  }
  if (store.legacyDirectory.exists()) {
    console.error("nightwatch: legacy repo/.nightwatch state is ignored for schema 2; it is preserved for forensic review");
  }
  if (args.thread && (args.accountMode || "").replaceAll("-", "_").toUpperCase() === "AUTO_POOL") {
    throw new SystemExit(
      "nightwatch: auto-pool adoption of existing interactive threads via --thread is not supported; use current-only mode.",
    );
  }
  const provider = args.provider || process.env.NIGHTWATCH_PROVIDER || "codex";
  let authorized: string[];
  try {
    const runSpec = new RunSpec({
      repo: root,
      goal: String(args.goal ?? ""),
      model: args.model ?? null,
      reasoningEffort: args.reasoningEffort ?? null,
      verifyCommands: args.verify ?? [],
      threadId: args.thread ?? null,
      service: Boolean(args.service),
      accountMode: args.accountMode ?? "current-only",
      accountSelectors: args.accounts ?? [],
      provider,
      agyPrintTimeout: args.agyPrintTimeout ?? null,
    });
    authorized = resolveAuthorizedAccounts(runSpec, root);
  } catch (error) {
    if (error instanceof AccountBrokerError || error instanceof ValueError) {
      throw new SystemExit(`nightwatch: configuration rejected: ${error.message}`);
    }
    throw error;
  }
  if (args.service) validateInstallTargets(root);
  else maybeInhibit(args, root);

  const state = store.initialize(
    makeRunId(root),
    String(args.goal ?? ""),
    root,
    null,
    [...(args.verify ?? [])],
    args.thread ?? null,
    args.model ?? null,
    args.reasoningEffort ?? null,
    (args.accountMode ?? "current-only").replaceAll("-", "_").toUpperCase(),
    authorized,
    provider,
    args.agyPrintTimeout ?? null,
  );
  if (args.service) {
    installUserFiles(root);
    try {
      startUserService(serviceName(root));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error(
        "nightwatch: goal was saved as NEW but the user service was not started; " +
          `run \`nightwatch resume --repo ${root}\` after fixing systemd: ${message}`,
      );
      return 1;
    }
    console.log(`Nightwatch service started: run_id=${String(state.run_id)} repo=${root}`);
    return 0;
  }
  const supervisor = new Supervisor(store);
  installSignalHandlers(supervisor);
  const final = await supervisor.execute(true);
  console.log(
    `Nightwatch ${String(final.state)}: run_id=${String(final.run_id)} thread_id=${String(final.thread_id || "(not captured)")}`,
  );
  return exitCodeForState(String(final.state));
}

async function cmdResume(args: CliArgs): Promise<number> {
  const root = rootOf(args.repo);
  maybeInhibit(args, root);
  const store = new NightwatchStore(root);
  const supervisor = new Supervisor(store);
  installSignalHandlers(supervisor);
  const final = await supervisor.execute(false);
  console.log(
    `Nightwatch ${String(final.state)}: run_id=${String(final.run_id)} thread_id=${String(final.thread_id || "(not captured)")}`,
  );
  return exitCodeForState(String(final.state));
}

function cmdRecover(args: CliArgs): number {
  const store = storeOf(args.repo);
  const state = store.loadState();
  if (!state.resume_claim) {
    console.log("nightwatch: no ambiguous resume claim is present");
    return 0;
  }
  if (!args.ackAmbiguous) {
    console.error("nightwatch: refusing to clear an ambiguous claim without --ack-ambiguous");
    return 10;
  }
  store.mutate(
    "human_override",
    "user acknowledged ambiguous exact-thread recovery after manual review",
    (item) => ({ ...item, resume_claim: null, last_error: "human acknowledged ambiguous lease" }),
  );
  if (store.loadState().state === State.BLOCKED) {
    store.transition(State.RECOVERING, "ambiguous_claim_acknowledged", "manual acknowledgement permits an exact-thread resume");
  }
  console.log("nightwatch: ambiguous claim acknowledged; run `nightwatch resume` to issue one exact-thread continuation");
  return 0;
}

function lastProviderEvent(store: NightwatchStore, generation: number): JsonObject | null {
  const target = store.runsPath.join(`generation-${generation}.events.jsonl`);
  let fd: number;
  try {
    fd = fs.openSync(target.raw, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  } catch {
    return null;
  }
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile()) return null;
    const offset = Math.max(0, info.size - 65_536);
    const buffer = Buffer.alloc(Math.min(65_536, info.size - offset));
    fs.readSync(fd, buffer, 0, buffer.length, offset);
    const chunk = buffer.toString("utf8");
    let lines = chunk.split(/\r?\n/);
    if (offset && lines.length) lines = lines.slice(1);
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const line = lines[i];
      if (!line.trim()) continue;
      try {
        const value = JSON.parse(line) as unknown;
        if (value && typeof value === "object" && !Array.isArray(value)) return value as JsonObject;
      } catch {
        continue;
      }
    }
    return null;
  } finally {
    fs.closeSync(fd);
  }
}

function agentRuntime(state: JsonObject): JsonObject {
  const active = state.active_process;
  const owner = state.supervisor_owner;
  const ownerRecord = owner && typeof owner === "object" ? (owner as JsonObject) : null;
  const ownerAlive = ownerRecord
    ? ownerRecord.starttime && ownerRecord.executable
      ? processMatches(ownerRecord as { pid?: unknown; starttime?: unknown; executable?: unknown })
      : pidAlive(typeof ownerRecord.pid === "number" ? ownerRecord.pid : null)
    : false;
  if (active && typeof active === "object" && processMatches(active as { pid?: unknown; starttime?: unknown; executable?: unknown })) {
    const activeRec = active as JsonObject;
    return { status: "RUNNING", pid: activeRec.pid, action: activeRec.action };
  }
  let status: string;
  if (state.state === State.WAIT_QUOTA) {
    if (state.account_mode === "AUTO_POOL") status = "WAITING_ACCOUNT_POOL";
    else status = !state.thread_id ? "WAITING_QUOTA (first launch deferred)" : "WAITING_QUOTA";
  } else if (TERMINAL_STATES.has(state.state as never)) {
    status = String(state.state);
  } else if (ownerAlive) {
    status = `SUPERVISOR_${String(state.state)}`;
  } else {
    status = "IDLE";
  }
  return {
    status,
    pid: null,
    action: null,
    supervisor_pid: ownerAlive && ownerRecord ? ownerRecord.pid : null,
  };
}

function statusSnapshot(args: CliArgs): JsonObject {
  try {
    const store = storeOf(args.repo);
    const state = store.loadState();
    const plan = store.loadPlan();
    const progress = planProgress(plan);
    const agent = agentRuntime(state);
    return {
      state,
      agent,
      plan,
      progress,
      last_provider_event: lastProviderEvent(store, Number(state.generation ?? 0)),
      terminal: TERMINAL_STATES.has(state.state as never),
    };
  } catch (error) {
    if (error instanceof SystemExit || error instanceof StateIntegrityError) {
      return { state: "NO_RUN", error: error.message, terminal: true };
    }
    throw error;
  }
}

function progressBar(percent: number, width = 24): string {
  const filled = Math.min(width, Math.max(0, Math.round((percent / 100) * width)));
  return `[${"#".repeat(filled)}${"-".repeat(width - filled)}]`;
}

function renderStatus(value: JsonObject): void {
  if (value.state === "NO_RUN") {
    console.log(`Nightwatch: no trusted run (${String(value.error ?? "")})`);
    return;
  }
  const state = value.state as JsonObject;
  const plan = value.plan as JsonObject;
  const progress = value.progress as JsonObject;
  const agent = value.agent as JsonObject;
  console.log("Nightwatch");
  console.log(`STATE          ${String(state.state)}`);
  const agentDetail = agent.pid ? ` pid=${String(agent.pid)} action=${String(agent.action || "(unknown)")}` : "";
  console.log(`AGENT          ${String(agent.status)}${agentDetail}`);
  const threadDisplay =
    state.thread_id ||
    (state.state === State.WAIT_QUOTA ? "(not captured — first launch deferred)" : "(not captured)");
  console.log(`THREAD         ${String(threadDisplay)}`);
  const provider = String(state.provider ?? "codex");
  console.log(`PROVIDER       ${provider}`);
  const defaultLabel = `(${provider.charAt(0).toUpperCase()}${provider.slice(1)} default)`;
  console.log(`MODEL          ${String(state.model || defaultLabel)}`);
  console.log(`REASONING      ${String(state.reasoning_effort || defaultLabel)}`);
  if (provider === "agy") console.log(`PRINT TIMEOUT  ${String(state.agy_print_timeout || "60m")}`);
  console.log(`RUN_ID         ${String(state.run_id)}`);
  console.log(`GENERATION     ${String(state.generation)}`);
  console.log(`QUOTA CYCLES   ${String(state.quota_cycles ?? 0)}`);
  console.log(`RECOVERY FAILS ${String(state.recovery_failures ?? 0)}`);
  const accountMode = String(state.account_mode ?? "CURRENT_ONLY");
  const authorized = Array.isArray(state.authorized_accounts) ? state.authorized_accounts : [];
  const currentAccount = state.current_account_fingerprint || "(none selected)";
  console.log(`ACCOUNT MODE   ${accountMode}`);
  console.log(`ACCOUNT POOL   ${authorized.length} explicitly authorized; current=${String(currentAccount)}`);
  console.log(`ACCOUNT LEASE  ${state.account_lease ? "owned by this run" : "(not held)"}`);
  console.log(`QUOTA SOURCE   ${String(state.quota_source || "(none)")}`);
  const quality =
    state.quota_source === "live_app_server"
      ? "LIVE_APP_SERVER"
      : state.quota_source === "rollout_schedule_only" || state.quota_source === "unavailable"
        ? "GUARDED_PROBE_ONLY"
        : "(unknown)";
  console.log(`RECOVERY MODE  ${quality}`);
  const quota = (state.quota && typeof state.quota === "object" ? state.quota : {}) as JsonObject;
  for (const [key, fallback] of [
    ["primary", "5h"],
    ["secondary", "weekly"],
  ] as const) {
    const window = quota[key];
    if (window && typeof window === "object") {
      const win = window as JsonObject;
      const label = String(win.name || fallback).toUpperCase().padEnd(7);
      console.log(`QUOTA ${label} ${String(win.used_percent ?? "(unknown)")}% used; reset=${String(win.resets_at || "(unknown)")}`);
    }
  }
  console.log(`SUPERVISOR     ${String(agent.supervisor_pid || "(none)")}`);
  console.log(`RESET          ${String(state.next_resume_at || "(none)")}`);
  console.log(
    `PROGRESS       ${progressBar(Number(progress.verified_percent ?? 0))} ${String(progress.verified_percent)}% verified`,
  );
  console.log(`VERIFIED       ${String(progress.verified_count)} / ${String(progress.total_count)} milestones`);
  console.log(
    `IMPLEMENTED    ${String(progress.implemented_count)} / ${String(progress.total_count)} milestones (${String(progress.implemented_percent)}%)`,
  );
  const milestones = Array.isArray(plan.milestones) ? (plan.milestones as JsonObject[]) : [];
  const blocked = milestones.filter((item) => item.status === "blocked").length;
  console.log(`BLOCKED        ${blocked}`);
  if (state.last_error) console.log(`LAST ERROR     ${String(state.last_error)}`);
  const current = milestones.find((item) => ["working", "implemented", "pending"].includes(String(item.status)));
  if (current) console.log(`CURRENT        ${String(current.id)} — ${String(current.title)}`);
  console.log("MILESTONES");
  const markers: Record<string, string> = {
    pending: " ",
    working: ">",
    implemented: "+",
    verified: "x",
    blocked: "!",
  };
  for (const item of milestones) {
    const statusValue = String(item.status ?? "pending");
    console.log(`  [${markers[statusValue] ?? "?"}] ${String(item.id)} ${String(item.title)} (${statusValue})`);
  }
  const event = (value.last_provider_event || {}) as JsonObject;
  if (Object.keys(event).length) {
    console.log(`LAST EVENT     ${String(event.type ?? "(unknown)")} (${String(event.action || event.status || "provider")})`);
  }
  console.log(`UPDATED        ${String(state.updated_at || "(unknown)")}`);
}

async function cmdStatus(args: CliArgs): Promise<number> {
  let first = true;
  try {
    for (;;) {
      const value = statusSnapshot(args);
      if (args.json) {
        if (args.watch) console.log(JSON.stringify(value));
        else console.log(JSON.stringify(value, null, 2));
      } else {
        if (args.watch && !first && process.stdout.isTTY) process.stdout.write("\u001b[2J\u001b[H");
        renderStatus(value);
      }
      if (!args.watch || value.terminal) return 0;
      first = false;
      await new Promise((resolve) => setTimeout(resolve, Math.round((args.interval ?? 2) * 1000)));
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).name === "AbortError") {
      /* ignore */
    } else if (!args.json && error instanceof Error && error.message.includes("SIGINT")) {
      console.log("\nNightwatch status watch stopped.");
    } else {
      throw error;
    }
  }
  return 0;
}

function cmdLog(args: CliArgs): number {
  const store = storeOf(args.repo);
  if (!store.logPath.exists()) {
    console.log("Nightwatch: no supervisor log");
    return 0;
  }
  const lines = store.logPath.readText().split(/\r?\n/);
  const tail = Math.max(1, args.tail ?? 80);
  for (const line of lines.slice(-tail)) {
    if (line !== undefined) console.log(line);
  }
  return 0;
}

function cmdReport(args: CliArgs): number {
  const store = storeOf(args.repo);
  const state = store.loadState();
  const verification = (state.last_verification as JsonObject | null | undefined) ?? null;
  const text = buildReport(store, state, verification);
  const reportPath = store.writeReport(text);
  process.stdout.write(text);
  console.error(`Report written: ${reportPath.raw}`);
  return 0;
}

function cmdStop(args: CliArgs): number {
  const store = storeOf(args.repo);
  const state = store.loadState();
  if (state.state === State.DONE || state.state === State.STOPPED || state.state === State.AWAITING_ACCEPTANCE) {
    console.log(`Nightwatch ${String(state.state)}`);
    return 0;
  }
  const supervisor = new Supervisor(store);
  supervisor.requestStop();
  supervisor.store.transition(State.STOPPED, "manual_stop", "user requested stop; no automatic recovery will continue");
  console.log("Nightwatch STOPPED; durable state preserved");
  return 0;
}

function cmdSteer(args: CliArgs): number {
  const store = storeOf(args.repo);
  const result = queueSteer(store, String(args.instruction ?? ""));
  if (result.ok) {
    console.log(result.message);
    return 0;
  }
  console.error(`nightwatch: ${result.message}`);
  return 1;
}

function cmdDoctor(args: CliArgs): number {
  const report = doctorSnapshot(args.repo ? rootOf(args.repo) : null);
  if (args.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    console.log(`Nightwatch doctor: ${String(report.status)}`);
    console.log(`Codex: ${String(report.codex_version || "(missing)")}`);
    const auth = (report.auth || {}) as JsonObject;
    console.log(`Auth: ${String(auth.status)}`);
    const quota = (report.quota || {}) as JsonObject;
    console.log(`Quota authority: ${String(quota.authority ?? "unavailable")} (${String(quota.source ?? "none")})`);
    if (quota.primary && typeof quota.primary === "object") {
      const primary = quota.primary as JsonObject;
      console.log(`5h: ${String(primary.used_percent)}% used, reset=${String(primary.resets_at)}`);
    }
    if (quota.secondary && typeof quota.secondary === "object") {
      const secondary = quota.secondary as JsonObject;
      console.log(`weekly: ${String(secondary.used_percent)}% used, reset=${String(secondary.resets_at)}`);
    }
    if (report.agy && typeof report.agy === "object") {
      const agy = report.agy as JsonObject;
      console.log(`AGY: ${String(agy.version || "(missing)")}`);
      console.log(`AGY Auth: ${agy.auth_ok ? "ok" : "fail"}`);
      const quotaData = (agy.quota || {}) as JsonObject;
      const gemini = (quotaData.gemini || null) as JsonObject | null;
      if (gemini?.primary && typeof gemini.primary === "object") {
        const primary = gemini.primary as JsonObject;
        console.log(`AGY Gemini 5h: ${String(primary.used_percent)}% used, reset=${String(primary.resets_at)}`);
      }
      if (gemini?.secondary && typeof gemini.secondary === "object") {
        const secondary = gemini.secondary as JsonObject;
        console.log(`AGY Gemini weekly: ${String(secondary.used_percent)}% used, reset=${String(secondary.resets_at)}`);
      }
      const third = (quotaData["3p"] || null) as JsonObject | null;
      if (third?.primary && typeof third.primary === "object") {
        const primary = third.primary as JsonObject;
        console.log(`AGY 3P 5h: ${String(primary.used_percent)}% used, reset=${String(primary.resets_at)}`);
      }
      if (third?.secondary && typeof third.secondary === "object") {
        const secondary = third.secondary as JsonObject;
        console.log(`AGY 3P weekly: ${String(secondary.used_percent)}% used, reset=${String(secondary.resets_at)}`);
      }
      if (quotaData.primary && !gemini) {
        const primary = quotaData.primary as JsonObject;
        console.log(`AGY 5h: ${String(primary.used_percent)}% used, reset=${String(primary.resets_at)}`);
      }
      if (quotaData.secondary && !gemini) {
        const secondary = quotaData.secondary as JsonObject;
        console.log(`AGY weekly: ${String(secondary.used_percent)}% used, reset=${String(secondary.resets_at)}`);
      }
    }
    console.log(`systemd-inhibit: ${report.systemd_inhibit ? "available" : "unavailable"}`);
  }
  return report.status === "ok" ? 0 : 1;
}

function cmdModels(args: CliArgs): number {
  const provider = args.provider || "codex";
  const catalog = listModels(provider);
  if (args.json) {
    console.log(JSON.stringify({ provider, models: catalog }, null, 2));
    return 0;
  }
  console.log(provider === "agy" ? "Installed AGY models (live catalog)" : "Installed Codex models (live local catalog)");
  for (const item of catalog) {
    const levels = Array.isArray(item.supported_reasoning_levels)
      ? (item.supported_reasoning_levels as string[]).join(",")
      : "";
    const levelText = levels || "(not reported)";
    const fallback = String(item.default_reasoning_level || "(not reported)");
    console.log(`${String(item.slug).padEnd(24)} default=${fallback.padEnd(8)} levels=${levelText}`);
  }
  return 0;
}

function cmdInstall(args: CliArgs): number {
  const serviceRoot = args.service ? rootOf(args.repo) : null;
  const [launcher, service] = installUserFiles(serviceRoot);
  console.log(`Installed ${launcher}`);
  if (service != null) {
    console.log(`Installed ${service}`);
    console.log(`Enable it with: systemctl --user daemon-reload && systemctl --user enable --now ${path.basename(service)}`);
  }
  return 0;
}

function cmdUninstall(_args: CliArgs): number {
  const removed: string[] = [];
  const [launcher, legacyService] = installPaths();
  const serviceDirectory = path.dirname(legacyService);
  let dynamicServices: string[] = [];
  try {
    dynamicServices = fs
      .readdirSync(serviceDirectory)
      .filter((name) => name.startsWith("nightwatch-") && name.endsWith(".service"))
      .map((name) => path.join(serviceDirectory, name))
      .sort();
  } catch {
    dynamicServices = [];
  }
  for (const filePath of [launcher, legacyService, ...dynamicServices]) {
    if (!fs.existsSync(filePath) && !isSymlink(filePath)) continue;
    if (isSymlink(filePath)) {
      console.error(`Preserved untrusted symlink: ${filePath}`);
      continue;
    }
    let text = "";
    try {
      text = fs.readFileSync(filePath, "utf8");
    } catch {
      continue;
    }
    if (!text.includes("nightwatch-install")) {
      console.error(`Preserved unrelated file: ${filePath}`);
      continue;
    }
    if (filePath.endsWith(".service")) {
      const binary = which("systemctl");
      if (binary) {
        try {
          spawnSync(binary, ["--user", "disable", "--now", path.basename(filePath)], {
            timeout: 15_000,
            stdio: "ignore",
          });
        } catch {
          /* ignore */
        }
      }
    }
    fs.unlinkSync(filePath);
    removed.push(filePath);
  }
  console.log(`Uninstalled: ${removed.length ? removed.join(", ") : "nothing"}`);
  return 0;
}

function isSymlink(filePath: string): boolean {
  try {
    return fs.lstatSync(filePath).isSymbolicLink();
  } catch {
    return false;
  }
}

async function cmdUi(args: CliArgs): Promise<number> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    console.error("nightwatch: interactive UI requires a terminal");
    return 2;
  }
  return await launchTui(args.repo ?? null);
}

async function cmdWeb(args: CliArgs): Promise<number> {
  const code = await serve({
    host: args.host ?? "127.0.0.1",
    port: args.port ?? 8765,
    openBrowser: !args.noBrowser,
  });
  return code || 0;
}

function cmdTest(args: CliArgs): number {
  if (args.testName === "app-server") {
    try {
      const quota = new AppServerQuotaProvider().read();
      if (quota.source !== "live_app_server") {
        console.log(`REAL_APP_SERVER_RATE_LIMITS = FAIL (${quota.source})`);
        return 20;
      }
      console.log("REAL_APP_SERVER_RATE_LIMITS = PASS");
      console.log(
        `5h=${quota.primary ? quota.primary.usedPercent : null}; weekly=${quota.secondary ? quota.secondary.usedPercent : null}`,
      );
      return 0;
    } catch (error) {
      const name = error instanceof Error ? error.name : "Error";
      console.log(`REAL_APP_SERVER_RATE_LIMITS = FAIL (${name})`);
      return 20;
    }
  }
  if (args.testName !== "quota-soak") {
    console.log("Available non-destructive tests: nightwatch test quota-soak | nightwatch test app-server");
    return 0;
  }
  console.log("REAL_QUOTA_SOAK = PENDING_REAL_QUOTA_SOAK");
  console.log("No quota is intentionally consumed. A natural provider limit cycle can be recorded later.");
  return 0;
}

function cmdAdopt(args: CliArgs): number {
  if ((args.accountMode || "").replaceAll("-", "_").toUpperCase() === "AUTO_POOL") {
    throw new SystemExit(
      "nightwatch: auto-pool adoption of existing interactive threads is not supported; use current-only mode to adopt interactive threads.",
    );
  }
  const root = rootOf(args.repo);
  const store = new NightwatchStore(root);
  if (store.exists()) {
    const state = store.loadState();
    throw new SystemExit(
      `nightwatch: a run already exists in ${root} (state=${String(state.state)}, thread=${String(state.thread_id ?? "")})`,
    );
  }
  const provider = args.provider || process.env.NIGHTWATCH_PROVIDER || "codex";
  let authorized: string[];
  try {
    const spec = new RunSpec({
      repo: root,
      goal: args.goal || "Adopted conversation",
      model: args.model ?? null,
      reasoningEffort: args.reasoningEffort ?? null,
      verifyCommands: args.verify ?? [],
      threadId: args.thread ?? null,
      service: false,
      accountMode: args.accountMode ?? "current-only",
      accountSelectors: args.accounts ?? [],
      provider,
      agyPrintTimeout: args.agyPrintTimeout ?? null,
    });
    authorized = resolveAuthorizedAccounts(spec, root);
  } catch (error) {
    if (error instanceof AccountBrokerError || error instanceof ValueError) {
      throw new SystemExit(`nightwatch: configuration rejected: ${error.message}`);
    }
    throw error;
  }
  const state = store.initialize(
    makeRunId(root),
    args.goal || "Adopted conversation",
    root,
    null,
    [...(args.verify ?? [])],
    args.thread ?? null,
    args.model ?? null,
    args.reasoningEffort ?? null,
    (args.accountMode ?? "current-only").replaceAll("-", "_").toUpperCase(),
    authorized,
    provider,
    args.agyPrintTimeout ?? null,
  );
  console.log(`Nightwatch: adopted thread ${String(args.thread)} for repo ${root} (run_id=${String(state.run_id)})`);
  console.log("Run `nightwatch resume` to start unattended supervision.");
  return 0;
}

function printAmbiguousSessions(sessions: JsonObject[], root: string): void {
  console.log("=".repeat(60));
  console.log("AMBIGUOUS_ACTIVE_SESSIONS");
  console.log(`Multiple active Codex sessions found for ${root}:`);
  for (const session of sessions) {
    const title = String(session.title || "").slice(0, 40);
    const titleStr = title ? ` | Title: ${title}` : "";
    console.log(
      `  - PID ${String(session.pid)} | Thread: ${String(session.thread_id)} | Model: ${String(session.model || "(unknown)")} | Branch: ${String(session.branch || "(unknown)")}${titleStr}`,
    );
  }
  console.log("Please specify --thread <EXACT_THREAD_ID> to select one session.");
  console.log("=".repeat(60));
}

function printWatchSnapshot(snap: JsonObject, root: string): void {
  console.log("=".repeat(60));
  console.log(`REPO         ${root}`);
  console.log(`THREAD ID    ${String(snap.thread_id || "(none)")}`);
  console.log(
    `PROCESS      PID ${String(snap.pid || "(none)")} (${snap.pid_alive ? "ALIVE" : "NOT RUNNING / EXITED"})`,
  );
  if (snap.takeover_pending) {
    console.log("TAKEOVER     TAKEOVER_PENDING (waiting for interactive process to exit)");
  }
  console.log(`MODEL        ${String(snap.model || "(unknown)")} [branch: ${String(snap.branch || "(unknown)")}]`);
  if (snap.title) console.log(`GOAL/TITLE   ${String(snap.title).slice(0, 80)}`);
  const limits = (snap.rate_limits || {}) as JsonObject;
  const primary = (limits.primary || {}) as JsonObject;
  const secondary = (limits.secondary || {}) as JsonObject;
  if (Object.keys(primary).length) {
    const reset = primary.resets_at ? `, reset=${String(primary.resets_at)}` : "";
    console.log(`QUOTA 5H     ${String(primary.used_percent ?? 0)}% used${reset}`);
  }
  if (Object.keys(secondary).length) {
    console.log(`QUOTA WEEKLY ${String(secondary.used_percent ?? 0)}% used`);
  }
  const tokens = (snap.tokens || {}) as JsonObject;
  if (Object.keys(tokens).length) {
    const fmt = (n: unknown) => Number(n ?? 0).toLocaleString("en-US");
    console.log(`TOKENS       total=${fmt(tokens.total_tokens)}, input=${fmt(tokens.input_tokens)}, output=${fmt(tokens.output_tokens)}`);
  }
  const subs = Array.isArray(snap.subagents) ? (snap.subagents as unknown[]) : [];
  if (subs.length) {
    const subStr = subs
      .map((item) => {
        if (Array.isArray(item) && item.length >= 2) return `${String(item[1])} (${String(item[0]).slice(0, 8)}...)`;
        return String(item);
      })
      .join(", ");
    console.log(`SUBAGENTS    ${subStr}`);
  }
  console.log("=".repeat(60));
}

async function cmdWatch(args: CliArgs): Promise<number> {
  const root = rootOf(args.repo);
  const store = new NightwatchStore(root);
  const watcher = new PassiveWatcher(store, null, args.thread ?? null);

  if (args.once) {
    const snapshot = watcher.inspectLiveSnapshot();
    if (args.json) {
      console.log(JSON.stringify(snapshot, null, 2));
      return snapshot.status === "OK" ? 0 : 1;
    }
    if (snapshot.status === "AMBIGUOUS_ACTIVE_SESSIONS") {
      printAmbiguousSessions((snapshot.sessions as JsonObject[]) || [], root);
      return 1;
    }
    if (!snapshot.active) {
      console.log(`Nightwatch watch: no active Codex session found for ${root}`);
      if (snapshot.pid) console.log(`PID ${String(snapshot.pid)} running but no proven rollout file located`);
      return 1;
    }
    printWatchSnapshot(snapshot, root);
    return 0;
  }

  const initSnapshot = watcher.inspectLiveSnapshot();
  if (initSnapshot.status === "AMBIGUOUS_ACTIVE_SESSIONS") {
    if (args.json) console.log(JSON.stringify(initSnapshot));
    else printAmbiguousSessions((initSnapshot.sessions as JsonObject[]) || [], root);
    return 1;
  }

  console.log(`Nightwatch watch: passively monitoring ${root} (Ctrl+C to stop)...`);
  try {
    const onUpdate = (snap: JsonObject) => {
      if (args.json) console.log(JSON.stringify(snap));
      else if (snap.status === "AMBIGUOUS_ACTIVE_SESSIONS") {
        printAmbiguousSessions((snap.sessions as JsonObject[]) || [], root);
      } else {
        printWatchSnapshot(snap, root);
      }
    };
    const result = await watcher.watch(
      onUpdate,
      2.0,
      Boolean(args.autoTakeover),
      args.goal ?? null,
      args.verify ?? null,
      args.model ?? null,
      args.reasoningEffort ?? null,
    );
    if (args.autoTakeover && result && typeof result === "object") {
      const status = result.status;
      if (status === "TAKEOVER_HANDOFF_COMPLETE") {
        console.log(
          `Nightwatch auto-takeover handoff complete: service=${String(result.service)} thread_id=${String(result.thread_id)}`,
        );
        return 0;
      }
      if (status === "TAKEOVER_SERVICE_START_FAILED") {
        console.error(
          "nightwatch: goal was saved as NEW but the user service was not started; " +
            `run \`nightwatch resume --repo ${root}\` after fixing systemd: ${String(result.error)}`,
        );
        return 1;
      }
    }
  } catch (error) {
    if (error instanceof Error && (error.name === "KeyboardInterrupt" || error.message === "KeyboardInterrupt")) {
      console.log("\nNightwatch watch stopped.");
      return 0;
    }
    throw error;
  }
  return 0;
}

export async function main(argv?: string[] | null): Promise<number> {
  const effective = [...(argv == null ? process.argv.slice(2) : argv)];
  if (!effective.length) {
    if (process.stdin.isTTY && process.stdout.isTTY) {
      const code = await serve({ openBrowser: true });
      return code || 0;
    }
    printHelp();
    return 2;
  }

  let args: CliArgs;
  try {
    args = parseCliArgs(effective);
  } catch (error) {
    if (error instanceof SystemExit) {
      if (error.message) console.error(error.message);
      return 1;
    }
    throw error;
  }

  if (args.command === "__version__") {
    console.log(`nightwatch ${VERSION}`);
    return 0;
  }
  if (args.command === "__help__" || args.help) {
    printHelp();
    return 0;
  }

  try {
    switch (args.command) {
      case "run":
        return await cmdRun(args);
      case "resume":
        return await cmdResume(args);
      case "recover":
        return cmdRecover(args);
      case "status":
        return await cmdStatus(args);
      case "log":
        return cmdLog(args);
      case "report":
        return cmdReport(args);
      case "stop":
        return cmdStop(args);
      case "doctor":
        return cmdDoctor(args);
      case "models":
        return cmdModels(args);
      case "ui":
        return await cmdUi(args);
      case "web":
        return await cmdWeb(args);
      case "install":
        return cmdInstall(args);
      case "uninstall":
        return cmdUninstall(args);
      case "test":
        return cmdTest(args);
      case "watch":
        return await cmdWatch(args);
      case "adopt":
        return cmdAdopt(args);
      case "steer":
        return cmdSteer(args);
      default:
        console.error(`nightwatch: unknown command ${args.command}`);
        return 2;
    }
  } catch (error) {
    if (error instanceof SupervisorAlreadyRunning) {
      console.error("nightwatch: already supervised by another process; state was not changed");
      return 0;
    }
    if (error instanceof StateIntegrityError) {
      console.error(`nightwatch: FAIL CLOSED: ${error.message}`);
      return 20;
    }
    if (error instanceof SystemExit) {
      if (error.message) console.error(error.message);
      return 1;
    }
    if (error instanceof RuntimeError) {
      console.error(`nightwatch: RuntimeError: ${error.message}`);
      return 20;
    }
    const name = error instanceof Error ? error.name : "Error";
    const message = error instanceof Error ? error.message : String(error);
    console.error(`nightwatch: ${name}: ${message}`);
    return 20;
  }
}

function isMainModule(): boolean {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    const resolved = fs.realpathSync(path.resolve(entry));
    return import.meta.url === pathToFileURL(resolved).href;
  } catch {
    return false;
  }
}

if (isMainModule()) {
  main().then(
    (code) => {
      process.exitCode = code;
    },
    (error: unknown) => {
      const name = error instanceof Error ? error.name : "Error";
      const message = error instanceof Error ? error.message : String(error);
      console.error(`nightwatch: ${name}: ${message}`);
      process.exitCode = 20;
    },
  );
}
