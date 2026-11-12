/** Discover trusted Nightwatch runs for the web control plane (subset of Python tui). */

import fs from "node:fs";
import path from "node:path";

import { TERMINAL_STATES, type JsonObject } from "./models.js";
import { NightPath } from "./path.js";
import { controlPlaneRoot, MAX_EVENT_BYTES, NightwatchStore, StateIntegrityError } from "./storage.js";

export const MAX_DISCOVERED_RUNS = 1_000;

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
  return JSON.parse(fs.readFileSync(target.raw, "utf8")) as unknown;
}

export class RunRecord {
  private _events: JsonObject[] | null = null;

  constructor(
    readonly store: NightwatchStore,
    readonly state: JsonObject,
    readonly plan: JsonObject,
  ) {}

  get events(): JsonObject[] {
    if (this._events === null) this._events = this.store.loadEvents();
    return this._events;
  }

  get repo(): NightPath {
    return this.store.repo;
  }

  get threadId(): string | null {
    const value = this.state.thread_id;
    return typeof value === "string" ? value : null;
  }

  get terminal(): boolean {
    const state = this.state.state;
    return typeof state === "string" && TERMINAL_STATES.has(state as never);
  }

  get active(): boolean {
    return !this.terminal;
  }

  get provider(): string {
    const value = this.state.provider;
    return typeof value === "string" && value ? value : "codex";
  }
}

export class RunCatalog {
  readonly root: NightPath;
  errors: string[] = [];

  constructor(stateHome?: string | NightPath | null) {
    this.root = stateHome
      ? new NightPath(typeof stateHome === "string" ? stateHome : stateHome.raw).expanduser().resolve()
      : controlPlaneRoot().resolve();
  }

  discover(): RunRecord[] {
    this.errors = [];
    let rootInfo: fs.Stats;
    try {
      rootInfo = fs.lstatSync(this.root.raw);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      this.errors.push("trusted state root is unreadable");
      return [];
    }
    if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) {
      this.errors.push("trusted state root is not a real directory");
      return [];
    }
    let children: NightPath[];
    try {
      children = this.root
        .iterdir()
        .sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
        .slice(0, MAX_DISCOVERED_RUNS);
    } catch {
      this.errors.push("trusted state root cannot be listed");
      return [];
    }
    const records: RunRecord[] = [];
    for (const child of children) {
      try {
        const childInfo = fs.lstatSync(child.raw);
        if (childInfo.isSymbolicLink() || !childInfo.isDirectory()) continue;
        const metadataPath = child.join("metadata.json");
        if (!metadataPath.exists()) continue;
        const metadata = readJsonRegular(metadataPath);
        const repoValue =
          metadata !== null && typeof metadata === "object" && !Array.isArray(metadata)
            ? (metadata as JsonObject).repo_path
            : undefined;
        if (typeof repoValue !== "string" || !path.isAbsolute(repoValue)) {
          throw new StateIntegrityError("metadata repo path is invalid");
        }
        const store = new NightwatchStore(repoValue, this.root);
        if (store.directory.raw !== child.resolve().raw) {
          throw new StateIntegrityError("metadata does not match its trusted directory");
        }
        const state = store.loadState();
        const plan = store.loadPlan();
        records.push(new RunRecord(store, state, plan));
      } catch (error) {
        const errno = typeof error === "object" && error !== null && typeof (error as NodeJS.ErrnoException).code === "string";
        if (
          error instanceof StateIntegrityError ||
          error instanceof TypeError ||
          error instanceof SyntaxError ||
          (error instanceof Error && error.name === "ValueError") ||
          errno
        ) {
          const name = error instanceof Error ? error.name : "Error";
          this.errors.push(`${child.name}: ${name}`);
          continue;
        }
        throw error;
      }
    }
    return records.sort((a, b) => {
      const left = `${a.repo.raw}\0${String(a.state.created_at ?? "")}`;
      const right = `${b.repo.raw}\0${String(b.state.created_at ?? "")}`;
      return left < right ? -1 : left > right ? 1 : 0;
    });
  }
}
