import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, it } from "node:test";

import { State } from "../src/models.js";
import { RunSpec, doctorSnapshot } from "../src/operations.js";
import { RunCatalog } from "../src/run-catalog.js";
import { makeRunId } from "../src/storage.js";

describe("smoke imports", () => {
  const previousStateHome = process.env.NIGHTWATCH_STATE_HOME;
  let stateHome: string | null = null;

  afterEach(() => {
    if (previousStateHome === undefined) delete process.env.NIGHTWATCH_STATE_HOME;
    else process.env.NIGHTWATCH_STATE_HOME = previousStateHome;
    if (stateHome) {
      fs.rmSync(stateHome, { recursive: true, force: true });
      stateHome = null;
    }
  });

  it("exposes State values", () => {
    assert.equal(State.NEW, "NEW");
    assert.equal(State.DONE, "DONE");
    assert.equal(State.STOPPED, "STOPPED");
    assert.equal(State.FAILED, "FAILED");
    assert.ok(Object.keys(State).length >= 10);
  });

  it("makeRunId returns a non-empty string", () => {
    const id = makeRunId("/tmp/nightwatch-smoke-repo");
    assert.equal(typeof id, "string");
    assert.ok(id.length > 0);
    assert.notEqual(id, makeRunId("/tmp/nightwatch-smoke-other"));
  });

  it("RunSpec validates goal and accepts a normal goal", () => {
    assert.throws(() => new RunSpec({ repo: "/tmp/x", goal: "   " }), /goal/i);
    const spec = new RunSpec({
      repo: os.tmpdir(),
      goal: "Ship a smoke check",
      verifyCommands: ["git diff --check"],
      service: false,
      accountMode: "current-only",
    });
    assert.equal(spec.goal, "Ship a smoke check");
    assert.deepEqual([...spec.verifyCommands], ["git diff --check"]);
    assert.equal(spec.accountMode, "CURRENT_ONLY");
    assert.equal(spec.provider, "codex");
  });

  it("doctorSnapshot has expected top-level keys", () => {
    const report = doctorSnapshot(null);
    for (const key of ["platform", "codex_binary", "auth", "quota", "systemd_inhibit", "agy"]) {
      assert.ok(key in report, `missing ${key}`);
    }
    assert.ok(report.auth && typeof report.auth === "object");
    assert.ok(report.quota && typeof report.quota === "object");
  });

  it("RunCatalog discovers nothing on an empty state home", () => {
    stateHome = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-smoke-state-"));
    process.env.NIGHTWATCH_STATE_HOME = stateHome;
    const catalog = new RunCatalog();
    assert.deepEqual(catalog.discover(), []);
    assert.deepEqual(catalog.errors, []);
  });
});
