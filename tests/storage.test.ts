import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, it } from "node:test";

import { State } from "../src/models.js";
import { LOCK_EX, LOCK_NB, LOCK_UN, WouldBlock, flock } from "../src/posix.js";
import {
  NightwatchStore,
  SupervisorAlreadyRunning,
  makeRunId,
} from "../src/storage.js";

function gitRepo(root: string): void {
  assert.equal(spawnSync("git", ["init", "-q"], { cwd: root }).status, 0);
  assert.equal(
    spawnSync("git", ["config", "user.email", "nightwatch@example.invalid"], { cwd: root }).status,
    0,
  );
  assert.equal(spawnSync("git", ["config", "user.name", "Nightwatch Test"], { cwd: root }).status, 0);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  assert.equal(spawnSync("git", ["add", "README.md"], { cwd: root }).status, 0);
  assert.equal(spawnSync("git", ["commit", "-qm", "fixture"], { cwd: root }).status, 0);
}

describe("storage", () => {
  const previousStateHome = process.env.NIGHTWATCH_STATE_HOME;
  let stateHome = "";
  let repoRoot = "";

  beforeEach(() => {
    stateHome = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-storage-state-"));
    repoRoot = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-storage-repo-"));
    process.env.NIGHTWATCH_STATE_HOME = stateHome;
    gitRepo(repoRoot);
  });

  afterEach(() => {
    if (previousStateHome === undefined) delete process.env.NIGHTWATCH_STATE_HOME;
    else process.env.NIGHTWATCH_STATE_HOME = previousStateHome;
    fs.rmSync(stateHome, { recursive: true, force: true });
    fs.rmSync(repoRoot, { recursive: true, force: true });
  });

  it("initialize creates durable NEW state loadable from disk", () => {
    const store = new NightwatchStore(repoRoot);
    const state = store.initialize("run-storage", "goal", repoRoot, "2026-01-01T00:00:00Z", [
      "git diff --check",
    ]);
    assert.equal(state.state, State.NEW);
    assert.equal(state.run_id, "run-storage");
    assert.equal(state.initialized, true);
    assert.ok(store.exists());
    const loaded = store.loadState();
    assert.equal(loaded.state, State.NEW);
    assert.equal(loaded.run_id, "run-storage");
    assert.equal(loaded.goal, "goal");
  });

  it("transition updates state and appends an event", () => {
    const store = new NightwatchStore(repoRoot);
    store.initialize("run-storage", "goal", repoRoot, "2026-01-01T00:00:00Z");
    const next = store.transition(State.PREFLIGHT, "preflight_started", "test");
    assert.equal(next.state, State.PREFLIGHT);
    assert.equal(store.loadState().state, State.PREFLIGHT);
    const events = store.loadEvents();
    assert.ok(events.some((item) => item.event === "preflight_started"));
  });

  it("makeRunId returns unique ids for distinct repos and calls", () => {
    const a = makeRunId(repoRoot);
    const other = path.join(os.tmpdir(), `nightwatch-other-${process.pid}-${Date.now()}`);
    const b = makeRunId(other);
    assert.notEqual(a, b);
    assert.ok(a.length > 0);
    const c = makeRunId(repoRoot, "2026-01-01T00:00:00Z");
    const d = makeRunId(repoRoot, "2026-01-01T00:00:01Z");
    assert.notEqual(c, d);
  });

  it("supervisor lease acquire+release; nested acquire WouldBlock / SupervisorAlreadyRunning", () => {
    const store = new NightwatchStore(repoRoot);
    store.initialize("run-storage", "goal", repoRoot);

    store.withSupervisorLease(() => {
      assert.throws(
        () => store.withSupervisorLease(() => undefined),
        (error: unknown) => error instanceof SupervisorAlreadyRunning,
      );
    });

    const handle = store.supervisorLockPath.open("a+");
    try {
      flock(handle.fileno(), LOCK_EX | LOCK_NB);
      const second = store.supervisorLockPath.open("a+");
      try {
        assert.throws(() => flock(second.fileno(), LOCK_EX | LOCK_NB), (error: unknown) => error instanceof WouldBlock);
      } finally {
        second.close();
      }
      flock(handle.fileno(), LOCK_UN);
    } finally {
      handle.close();
    }

    store.withSupervisorLease(() => "ok");
  });

  it("log append writes supervisor.log", () => {
    const store = new NightwatchStore(repoRoot);
    store.initialize("run-storage", "goal", repoRoot);
    store.log("hello from storage test");
    const text = fs.readFileSync(store.logPath.raw, "utf8");
    assert.match(text, /hello from storage test/);
    assert.match(text, /trusted control plane initialized/);
  });
});
