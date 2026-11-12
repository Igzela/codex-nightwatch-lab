import { spawnSync } from "node:child_process";

import { NightPath } from "./path.js";

export class GitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GitError";
  }
}

export class GitSnapshot {
  constructor(
    readonly root: string,
    readonly head: string | null,
    readonly branch: string | null,
    readonly status: string,
    readonly conflicts: boolean,
  ) {}

  get clean(): boolean {
    return !this.status.trim();
  }

  toDict(): Record<string, unknown> {
    return { root: this.root, head: this.head, branch: this.branch, status: this.status, conflicts: this.conflicts, clean: this.clean };
  }
}

const CONFLICT_CODES = new Set(["DD", "AU", "UD", "UA", "DU", "AA", "UU"]);

function run(args: string[], cwd: string, timeout = 5000): string {
  const result = spawnSync("git", args, { cwd, encoding: "utf8", timeout, stdio: ["ignore", "pipe", "pipe"] });
  if (result.error || result.status !== 0) throw new GitError(`git ${args.join(" ")} failed`);
  return (result.stdout ?? "").trim();
}

export function repoRoot(cwd: string): NightPath {
  return new NightPath(run(["rev-parse", "--show-toplevel"], cwd)).resolve();
}

export function snapshot(root: string): GitSnapshot {
  const resolved = new NightPath(root).resolve().raw;
  const status = run(["status", "--short"], resolved);
  let head: string | null = null;
  let branch = "(detached)";
  try {
    head = run(["rev-parse", "HEAD"], resolved);
  } catch {
    head = null;
  }
  try {
    branch = run(["symbolic-ref", "--short", "HEAD"], resolved);
  } catch {
    branch = "(detached)";
  }
  const conflicts = status.split("\n").filter(Boolean).some((line) => CONFLICT_CODES.has(line.slice(0, 2)));
  return new GitSnapshot(resolved, head, branch, status, conflicts);
}

export function isAncestor(ancestor: string, descendant: string, root: string): boolean {
  try {
    run(["merge-base", "--is-ancestor", ancestor, descendant], root);
    return true;
  } catch {
    return false;
  }
}

export function diffCheck(root: string): [boolean, string] {
  const result = spawnSync("git", ["diff", "--check"], { cwd: root, encoding: "utf8", timeout: 10000, stdio: ["ignore", "pipe", "pipe"] });
  if (result.error) return [false, `git diff --check failed: ${result.error.name}`];
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`.trim();
  return [result.status === 0, output];
}
