import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, it } from "node:test";

import { State } from "../src/models.js";
import {
  RunSpec,
  doctorSnapshot,
  queueSteer,
  startRun,
  stopRun,
} from "../src/operations.js";
import { NightwatchStore } from "../src/storage.js";
import { doctorPayload } from "../src/web.js";

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

describe("operations", () => {
  const previousStateHome = process.env.NIGHTWATCH_STATE_HOME;
  let stateHome = "";
  let repo = "";

  beforeEach(() => {
    stateHome = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-ops-state-"));
    repo = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-ops-repo-"));
    process.env.NIGHTWATCH_STATE_HOME = stateHome;
    gitRepo(repo);
  });

  afterEach(() => {
    if (previousStateHome === undefined) delete process.env.NIGHTWATCH_STATE_HOME;
    else process.env.NIGHTWATCH_STATE_HOME = previousStateHome;
    fs.rmSync(stateHome, { recursive: true, force: true });
    fs.rmSync(repo, { recursive: true, force: true });
  });

  it("startRun(service=false) creates NEW state", () => {
    const spec = new RunSpec({
      repo,
      goal: "operations start",
      verifyCommands: ["git diff --check"],
      service: false,
      accountMode: "current-only",
    });
    const result = startRun(spec, false);
    assert.equal(result.ok, true);
    assert.match(result.message, /initialized/i);
    const store = new NightwatchStore(repo);
    assert.ok(store.exists());
    assert.equal(store.loadState().state, State.NEW);
  });

  it("stopRun transitions NEW to STOPPED", () => {
    const spec = new RunSpec({
      repo,
      goal: "operations stop",
      verifyCommands: ["git diff --check"],
      service: false,
    });
    assert.equal(startRun(spec, false).ok, true);
    const stopped = stopRun(repo);
    assert.equal(stopped.ok, true);
    assert.match(stopped.message, /STOPPED/);
    assert.equal(new NightwatchStore(repo).loadState().state, State.STOPPED);
  });

  it("queueSteer rejects terminal DONE", () => {
    const store = new NightwatchStore(repo);
    store.initialize("run-ops", "goal", repo, null, ["git diff --check"]);
    store.transition(State.PREFLIGHT, "preflight_started", "test");
    store.transition(State.RUNNING, "provider_started", "test");
    store.transition(State.VERIFYING, "verification_started", "test");
    store.transition(State.DONE, "mission_complete", "test");
    const result = queueSteer(store, "please continue");
    assert.equal(result.ok, false);
    assert.match(result.message, /terminal/i);
  });

  it("doctorSnapshot has expected fields and doctorPayload exposes checks", () => {
    const report = doctorSnapshot(null);
    for (const key of [
      "platform",
      "codex_binary",
      "auth",
      "quota",
      "systemd_inhibit",
      "agy",
      "status",
      "linux_first",
      "account_pool",
    ]) {
      assert.ok(key in report, `missing ${key}`);
    }
    assert.ok(report.auth && typeof report.auth === "object");
    assert.ok(report.quota && typeof report.quota === "object");
    assert.ok(report.agy && typeof report.agy === "object");

    const payload = doctorPayload();
    assert.ok(Array.isArray(payload.checks));
    const checks = payload.checks as Array<Record<string, string>>;
    assert.deepEqual(
      checks.map((item) => item.id),
      ["codex", "auth", "quota", "systemd", "agy"],
    );
    for (const item of checks) {
      assert.deepEqual(new Set(Object.keys(item)), new Set(["id", "level", "title", "detail"]));
      assert.ok(["ok", "warn", "fail"].includes(item.level!));
      assert.ok(item.title!.trim());
      assert.ok(item.detail!.trim());
    }
  });
});
