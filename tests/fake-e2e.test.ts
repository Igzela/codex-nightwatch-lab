import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, it } from "node:test";

import { QuotaSnapshot, QuotaWindow, State } from "../src/models.js";
import { NightwatchStore } from "../src/storage.js";
import { Supervisor } from "../src/supervisor.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FAKE = path.join(ROOT, "test-artifacts", "fake-codex", "fake_codex.py");
const FAKE_AVAILABLE = fs.existsSync(FAKE);

class ScriptedQuota {
  calls = 0;

  read(): QuotaSnapshot {
    this.calls += 1;
    const resets = Math.floor(Date.now() / 1000) + 100;
    return new QuotaSnapshot(
      "live_app_server",
      "now",
      new QuotaWindow("5h", 0, 300, resets),
      new QuotaWindow("weekly", 0, 10080, resets),
    );
  }
}

function gitRun(cwd: string, ...args: string[]): void {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  assert.equal(result.status, 0, result.stderr);
}

function fixture(): { temporary: string; root: string; planFile: string; progressFile: string } {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-e2e-"));
  const root = temporary;
  gitRun(root, "init", "-q");
  gitRun(root, "config", "user.email", "nightwatch@example.invalid");
  gitRun(root, "config", "user.name", "Nightwatch Test");
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  gitRun(root, "add", "README.md");
  gitRun(root, "commit", "-qm", "fixture");
  const planFile = path.join(root, "plan-source.json");
  const progressFile = path.join(root, "progress-source.json");
  fs.writeFileSync(
    planFile,
    JSON.stringify({ milestones: [{ id: "M1", title: "implement fixture", weight: 1 }] }),
    "utf8",
  );
  fs.writeFileSync(
    progressFile,
    JSON.stringify({ milestones: [{ id: "M1", status: "implemented" }] }),
    "utf8",
  );
  return { temporary, root, planFile, progressFile };
}

describe("fake-e2e", { timeout: 120_000 }, () => {
  const envKeys = [
    "NIGHTWATCH_STATE_HOME",
    "NIGHTWATCH_CODEX_BIN",
    "NIGHTWATCH_SKIP_AUTH_CHECK",
    "NIGHTWATCH_QUOTA_BUFFER_SECONDS",
    "NIGHTWATCH_WAIT_POLL_SECONDS",
    "FAKE_CODEX_SCENARIO",
    "FAKE_CODEX_PLAN_FILE",
    "FAKE_CODEX_PROGRESS_FILE",
  ] as const;
  const previousEnv = Object.fromEntries(envKeys.map((key) => [key, process.env[key]]));
  const cleanup: string[] = [];

  afterEach(() => {
    for (const key of envKeys) {
      const previous = previousEnv[key];
      if (previous === undefined) delete process.env[key];
      else process.env[key] = previous;
    }
    while (cleanup.length) {
      const target = cleanup.pop();
      if (target) fs.rmSync(target, { recursive: true, force: true });
    }
  });

  it(
    "normal completion reaches DONE with fake Codex and creates fake-implemented.txt",
    { skip: !FAKE_AVAILABLE, timeout: 120_000 },
    async () => {
      const { temporary, root, planFile, progressFile } = fixture();
      cleanup.push(temporary);
      const stateHome = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-e2e-state-"));
      cleanup.push(stateHome);

      process.env.NIGHTWATCH_STATE_HOME = stateHome;
      process.env.NIGHTWATCH_CODEX_BIN = FAKE;
      process.env.NIGHTWATCH_SKIP_AUTH_CHECK = "1";
      process.env.NIGHTWATCH_QUOTA_BUFFER_SECONDS = "0";
      process.env.NIGHTWATCH_WAIT_POLL_SECONDS = "0.05";
      process.env.FAKE_CODEX_SCENARIO = "normal";
      process.env.FAKE_CODEX_PLAN_FILE = planFile;
      process.env.FAKE_CODEX_PROGRESS_FILE = progressFile;

      const store = new NightwatchStore(root);
      store.initialize("run-normal", "implement fixture", root, null, [
        "test -f fake-implemented.txt",
        "git diff --check",
      ]);

      const final = await new Supervisor(store, new ScriptedQuota()).execute(true);
      assert.equal(final.state, State.DONE);
      assert.equal(final.thread_id, "TEST-001");
      assert.equal(final.final_verification_passed, true);
      assert.equal(store.loadPlan().milestones?.[0]?.status, "verified");
      assert.ok(fs.existsSync(path.join(root, "fake-implemented.txt")));
      assert.ok(fs.existsSync(store.reportsPath.join("latest.md").raw));
    },
  );
});
