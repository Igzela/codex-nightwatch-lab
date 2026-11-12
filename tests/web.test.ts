import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import type { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { after, afterEach, beforeEach, describe, it } from "node:test";

import { repoRoot } from "../src/git.js";
import { planProgress, type JsonObject } from "../src/models.js";
import { NightwatchStore, makeRunId } from "../src/storage.js";
import {
  isLoopbackHost,
  makeServer,
  serve,
  setSpawnSupervisorForTests,
} from "../src/web.js";

const INDEX_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "src",
  "web_static",
  "index.html",
);

function gitRepo(root: string): void {
  const init = spawnSync("git", ["init", "-q"], { cwd: root, encoding: "utf8" });
  assert.equal(init.status, 0, init.stderr);
  assert.equal(
    spawnSync("git", ["config", "user.email", "nightwatch@example.invalid"], { cwd: root }).status,
    0,
  );
  assert.equal(spawnSync("git", ["config", "user.name", "Nightwatch Test"], { cwd: root }).status, 0);
  fs.writeFileSync(path.join(root, "README.md"), "fixture\n", "utf8");
  assert.equal(spawnSync("git", ["add", "README.md"], { cwd: root }).status, 0);
  assert.equal(spawnSync("git", ["commit", "-qm", "fixture"], { cwd: root }).status, 0);
}

type RequestResult = {
  status: number;
  payload: unknown;
  headers: Record<string, string>;
  raw: Buffer;
};

describe("web control plane", () => {
  let previousStateHome: string | undefined;
  let stateHome = "";
  let server: http.Server | null = null;
  let base = "";
  let port = 0;
  const repoCleanup: string[] = [];

  beforeEach(async () => {
    previousStateHome = process.env.NIGHTWATCH_STATE_HOME;
    stateHome = fs.mkdtempSync(path.join(os.tmpdir(), "nightwatch-web-state-"));
    process.env.NIGHTWATCH_STATE_HOME = stateHome;
    setSpawnSupervisorForTests(null);
    server = await makeServer("127.0.0.1", 0);
    const address = server.address() as AddressInfo;
    port = address.port;
    base = `http://127.0.0.1:${port}`;
  });

  afterEach(async () => {
    setSpawnSupervisorForTests(null);
    if (server) {
      const active = server;
      server = null;
      await new Promise<void>((resolve, reject) => {
        active.close((error) => (error ? reject(error) : resolve()));
      });
    }
    if (previousStateHome === undefined) delete process.env.NIGHTWATCH_STATE_HOME;
    else process.env.NIGHTWATCH_STATE_HOME = previousStateHome;
    fs.rmSync(stateHome, { recursive: true, force: true });
    while (repoCleanup.length) {
      const root = repoCleanup.pop();
      if (root) fs.rmSync(root, { recursive: true, force: true });
    }
  });

  after(() => {
    setSpawnSupervisorForTests(null);
  });

  function makeRepo(name: string): string {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), `nightwatch-web-${name}-`));
    repoCleanup.push(root);
    gitRepo(root);
    return root;
  }

  function request(
    method: string,
    reqPath: string,
    payload?: Record<string, unknown> | null,
    raw?: Buffer | null,
  ): Promise<RequestResult> {
    return new Promise((resolve, reject) => {
      let body: Buffer | undefined;
      const headers: Record<string, string> = {};
      if (payload != null) {
        body = Buffer.from(JSON.stringify(payload), "utf8");
        headers["Content-Type"] = "application/json; charset=utf-8";
        headers["Content-Length"] = String(body.length);
      } else if (raw != null) {
        body = raw;
        headers["Content-Type"] = "application/json";
        headers["Content-Length"] = String(body.length);
      }
      const req = http.request(
        {
          hostname: "127.0.0.1",
          port,
          path: reqPath,
          method,
          headers,
        },
        (res) => {
          const chunks: Buffer[] = [];
          res.on("data", (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
          res.on("end", () => {
            const rawBody = Buffer.concat(chunks);
            const headerMap: Record<string, string> = {};
            for (const [key, value] of Object.entries(res.headers)) {
              if (typeof value === "string") headerMap[key] = value;
              else if (Array.isArray(value)) headerMap[key] = value.join(", ");
            }
            const contentType = headerMap["content-type"] ?? "";
            let parsed: unknown;
            if (contentType.includes("application/json")) {
              parsed = JSON.parse(rawBody.toString("utf8"));
            } else {
              parsed = rawBody.toString("utf8");
            }
            resolve({
              status: res.statusCode ?? 0,
              payload: parsed,
              headers: headerMap,
              raw: rawBody,
            });
          });
        },
      );
      req.on("error", reject);
      if (body) req.write(body);
      req.end();
    });
  }

  it("empty home lists no runs", async () => {
    const { status, payload, headers } = await request("GET", "/api/runs");
    assert.equal(status, 200);
    assert.deepEqual(payload, { runs: [], errors: [] });
    assert.equal(headers["content-type"], "application/json; charset=utf-8");
  });

  it("doctor reports the five checks", async () => {
    const { status, payload, headers, raw } = await request("GET", "/api/doctor");
    assert.equal(status, 200);
    assert.equal(headers["content-type"], "application/json; charset=utf-8");
    assert.equal(typeof payload, "object");
    assert.ok(payload && typeof payload === "object");
    const body = payload as JsonObject;
    const checks = body.checks as Array<Record<string, string>>;
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
    assert.equal(typeof body.summary, "string");
    assert.equal(String(body.summary).split(".").length - 1, 1);
    assert.ok("codex_binary" in body);
    assert.ok("auth" in body);
    assert.ok("quota" in body);
    assert.ok("systemd_inhibit" in body);
    assert.ok(!raw.toString("utf8").includes("\\u"));
    const systemd = checks.find((item) => item.id === "systemd")!;
    if (systemd.level !== "ok") {
      assert.equal(systemd.level, "warn");
      assert.ok(systemd.detail!.includes("Background runs still work"));
    }
  });

  it("empty goal is rejected and does not spawn", async () => {
    let called = 0;
    setSpawnSupervisorForTests(() => {
      called += 1;
      return 1;
    });
    const { status, payload, headers } = await request("POST", "/api/runs", {
      repo: "/tmp/nightwatch-web-empty-goal",
      goal: "   ",
    });
    assert.equal(status, 400);
    assert.equal(headers["content-type"], "application/json; charset=utf-8");
    assert.ok(payload && typeof payload === "object");
    const body = payload as JsonObject;
    assert.equal(body.ok, false);
    assert.ok(String(body.message).toLowerCase().includes("goal"));
    assert.equal(called, 0);
    assert.deepEqual(fs.readdirSync(stateHome), []);
  });

  it("invalid json and oversized body are 400", async () => {
    let called = 0;
    setSpawnSupervisorForTests(() => {
      called += 1;
      return 1;
    });
    {
      const { status, payload } = await request("POST", "/api/runs", null, Buffer.from("{"));
      assert.equal(status, 400);
      assert.ok(payload && typeof payload === "object");
      assert.equal((payload as JsonObject).ok, false);
    }
    {
      const oversized = Buffer.concat([
        Buffer.from('{"repo":"/tmp/x","goal":"'),
        Buffer.alloc(70 * 1024, 0x61),
        Buffer.from('"}'),
      ]);
      const { status, payload } = await request("POST", "/api/runs", null, oversized);
      assert.equal(status, 400);
      assert.ok(payload && typeof payload === "object");
      const body = payload as JsonObject;
      assert.equal(body.ok, false);
      assert.ok(String(body.message).includes("64"));
    }
    assert.equal(called, 0);
  });

  it("POST run initializes and returns patched pid", async () => {
    const repo = makeRepo("post");
    const goal = "Ship the feature 修复配额";
    let spawnArgs: unknown[] = [];
    setSpawnSupervisorForTests((arg) => {
      spawnArgs = [arg];
      return 4242;
    });
    const { status, payload, headers, raw } = await request("POST", "/api/runs", {
      repo,
      goal,
      verify: ["git diff --check"],
    });
    assert.equal(status, 201);
    assert.equal(headers["content-type"], "application/json; charset=utf-8");
    assert.ok(payload && typeof payload === "object");
    const body = payload as JsonObject;
    assert.deepEqual(new Set(Object.keys(body)), new Set(["ok", "message", "run_id", "repo", "supervisor_pid"]));
    assert.equal(body.ok, true);
    assert.equal(body.supervisor_pid, 4242);
    assert.equal(body.repo, repoRoot(repo).raw);
    assert.ok(!raw.toString("utf8").includes("\\u"));
    assert.equal(spawnArgs.length, 1);
    const spawned = spawnArgs[0] as { raw?: string } | string;
    const spawnedPath = typeof spawned === "string" ? spawned : String(spawned.raw ?? spawned);
    assert.equal(path.resolve(spawnedPath), repoRoot(repo).raw);

    const store = new NightwatchStore(repo);
    const state = store.loadState();
    assert.equal(body.run_id, state.run_id);
    assert.equal(state.state, "NEW");
    assert.equal(state.account_mode, "CURRENT_ONLY");
    assert.deepEqual(store.loadPolicy().final_commands, ["git diff --check"]);

    const listed = await request("GET", "/api/runs");
    assert.equal(listed.status, 200);
    assert.ok(listed.payload && typeof listed.payload === "object");
    const listBody = listed.payload as { runs: Array<Record<string, unknown>> };
    assert.deepEqual(
      listBody.runs.map((item) => item.run_id),
      [state.run_id],
    );
    assert.equal(listBody.runs[0]!.id, store.directory.name);
    assert.equal(listBody.runs[0]!.goal, goal);
    assert.ok(listed.raw.toString("utf8").includes(goal));
    assert.ok(!listed.raw.toString("utf8").includes("\\u"));
    assert.equal(listBody.runs[0]!.terminal, false);
  });

  it("list and detail after initialize", async () => {
    const older = makeRepo("older");
    const newer = makeRepo("newer");
    const olderStore = new NightwatchStore(older);
    const newerStore = new NightwatchStore(newer);
    olderStore.initialize(makeRunId(older), "Older goal", older, "2020-01-01T00:00:00Z", [
      "git diff --check",
    ]);
    const newerState = newerStore.initialize(
      makeRunId(newer),
      "修复配额恢复",
      newer,
      "2024-06-01T00:00:00Z",
      ["git diff --check"],
    );
    const listed = await request("GET", "/api/runs");
    assert.equal(listed.status, 200);
    assert.ok(listed.payload && typeof listed.payload === "object");
    const listBody = listed.payload as { runs: Array<Record<string, unknown>> };
    assert.deepEqual(
      listBody.runs.map((item) => item.id),
      [newerStore.directory.name, olderStore.directory.name],
    );
    assert.deepEqual(
      listBody.runs.map((item) => item.updated_at),
      ["2024-06-01T00:00:00Z", "2020-01-01T00:00:00Z"],
    );
    const summaryKeys = new Set([
      "id",
      "run_id",
      "repo",
      "goal",
      "state",
      "thread_id",
      "provider",
      "model",
      "verified_percent",
      "verified_count",
      "total_count",
      "updated_at",
      "terminal",
    ]);
    assert.deepEqual(new Set(Object.keys(listBody.runs[0]!)), summaryKeys);
    assert.ok(listed.raw.toString("utf8").includes("修复配额恢复"));
    assert.ok(!listed.raw.toString("utf8").includes("\\u"));

    const detailRes = await request("GET", `/api/runs/${newerStore.directory.name}`);
    assert.equal(detailRes.status, 200);
    assert.equal(detailRes.headers["content-type"], "application/json; charset=utf-8");
    assert.ok(detailRes.payload && typeof detailRes.payload === "object");
    const detail = detailRes.payload as JsonObject;
    for (const key of summaryKeys) assert.ok(key in detail);
    for (const key of ["milestones", "quota", "last_error", "progress", "log_tail"]) {
      assert.ok(key in detail);
    }
    const plan = newerStore.loadPlan();
    assert.deepEqual(
      detail.milestones,
      (plan.milestones as JsonObject[]).map((item) => ({
        id: item.id,
        title: item.title,
        status: item.status,
      })),
    );
    assert.deepEqual(detail.progress, planProgress(plan));
    assert.equal(detail.run_id, newerState.run_id);
    assert.equal(detail.goal, "修复配额恢复");
    assert.equal(detail.state, "NEW");
    assert.equal(detail.terminal, false);
    assert.equal(detail.quota, null);
    assert.equal(detail.last_error, null);
    const expectedTail = fs
      .readFileSync(newerStore.logPath.raw, "utf8")
      .split(/\r\n|\n|\r/);
    if (expectedTail.length && expectedTail[expectedTail.length - 1] === "") expectedTail.pop();
    assert.deepEqual(detail.log_tail, expectedTail.slice(-80));
    assert.ok((detail.log_tail as string[]).some((line) => line.includes("NEW:")));

    fs.unlinkSync(newerStore.logPath.raw);
    const missingLog = await request("GET", `/api/runs/${newerStore.directory.name}`);
    assert.equal(missingLog.status, 200);
    assert.ok(missingLog.payload && typeof missingLog.payload === "object");
    assert.deepEqual((missingLog.payload as JsonObject).log_tail, []);
  });

  it("stop new run returns ok true", async () => {
    const repo = makeRepo("stop");
    const store = new NightwatchStore(repo);
    store.initialize(makeRunId(repo), "Stop me", repo, null, ["git diff --check"]);
    const { status, payload } = await request("POST", `/api/runs/${store.directory.name}/stop`);
    assert.equal(status, 200);
    assert.deepEqual(payload, { ok: true, message: "Nightwatch STOPPED; durable state preserved" });
    assert.equal(store.loadState().state, "STOPPED");
  });

  it("resume finished run does not spawn", async () => {
    const repo = makeRepo("resume");
    const store = new NightwatchStore(repo);
    store.initialize(makeRunId(repo), "Already done", repo);
    store.mutate("marked_done", "test closed the run", (item) => ({
      ...item,
      state: "DONE",
      thread_id: "TEST-001",
    }));
    let called = 0;
    setSpawnSupervisorForTests(() => {
      called += 1;
      return 1;
    });
    const { status, payload } = await request("POST", `/api/runs/${store.directory.name}/resume`);
    assert.equal(status, 200);
    assert.deepEqual(payload, {
      ok: false,
      message: "This run is already finished. Start a new goal.",
    });
    assert.equal(called, 0);
  });

  it("resume stopped run spawns supervisor", async () => {
    const repo = makeRepo("resume-stopped");
    const store = new NightwatchStore(repo);
    store.initialize(makeRunId(repo), "Paused", repo, null, null, "TEST-001");
    await request("POST", `/api/runs/${store.directory.name}/stop`);
    let spawnArgs: unknown[] = [];
    setSpawnSupervisorForTests((arg) => {
      spawnArgs = [arg];
      return 6161;
    });
    const { status, payload } = await request("POST", `/api/runs/${store.directory.name}/resume`);
    assert.equal(status, 200);
    assert.deepEqual(payload, {
      ok: true,
      message: "Detached supervisor started.",
      supervisor_pid: 6161,
    });
    assert.equal(spawnArgs.length, 1);
    const spawned = spawnArgs[0] as { raw?: string } | string;
    const spawnedPath = typeof spawned === "string" ? spawned : String(spawned.raw ?? spawned);
    assert.equal(path.resolve(spawnedPath), store.repo.raw);
  });

  it("resume new run returns patched pid", async () => {
    const repo = makeRepo("resume-new");
    const store = new NightwatchStore(repo);
    store.initialize(makeRunId(repo), "Keep going", repo);
    let spawnArgs: unknown[] = [];
    setSpawnSupervisorForTests((arg) => {
      spawnArgs = [arg];
      return 5150;
    });
    const { status, payload } = await request("POST", `/api/runs/${store.directory.name}/resume`);
    assert.equal(status, 200);
    assert.deepEqual(payload, {
      ok: true,
      message: "Detached supervisor started.",
      supervisor_pid: 5150,
    });
    assert.equal(spawnArgs.length, 1);
    const spawned = spawnArgs[0] as { raw?: string } | string;
    const spawnedPath = typeof spawned === "string" ? spawned : String(spawned.raw ?? spawned);
    assert.equal(path.resolve(spawnedPath), store.repo.raw);
  });

  it("id traversal is 404", async () => {
    for (const reqPath of ["/api/runs/../x", "/api/runs/..%2Fx", "/api/runs/%2e%2e%2fx", "/api/runs/%2e%2e"]) {
      const { status, payload, headers } = await request("GET", reqPath);
      assert.equal(status, 404, reqPath);
      assert.equal(headers["content-type"], "application/json; charset=utf-8");
      assert.ok(payload && typeof payload === "object");
      const body = payload as JsonObject;
      assert.equal(body.ok, false);
      assert.ok("message" in body);
    }
    const missing = await request("GET", "/api/runs/not-a-real-run");
    assert.equal(missing.status, 404);
    assert.deepEqual(missing.payload, { ok: false, message: "No Nightwatch run matches that id." });
  });

  it("unknown path is 404", async () => {
    const { status, payload } = await request("GET", "/api/missing");
    assert.equal(status, 404);
    assert.deepEqual(payload, { ok: false, message: "Not found." });
  });

  it("index serves html bytes matching src/web_static/index.html", async () => {
    const index = fs.readFileSync(INDEX_PATH);
    for (const reqPath of ["/", "/index.html"]) {
      const { status, headers, raw } = await request("GET", reqPath);
      assert.equal(status, 200, reqPath);
      assert.equal(headers["content-type"], "text/html; charset=utf-8");
      assert.ok(raw.equals(index));
    }
  });

  it("steer without a thread returns 200", async () => {
    const repo = makeRepo("steer");
    const store = new NightwatchStore(repo);
    store.initialize(makeRunId(repo), "Steer me", repo);
    const { status, payload } = await request("POST", `/api/runs/${store.directory.name}/steer`, {
      instruction: "Keep the public API stable.",
    });
    assert.equal(status, 200);
    assert.ok(payload && typeof payload === "object");
    const body = payload as JsonObject;
    assert.deepEqual(new Set(Object.keys(body)), new Set(["ok", "message"]));
    assert.equal(body.ok, false);
    assert.ok(String(body.message).toLowerCase().includes("thread"));
    const missing = await request("POST", "/api/runs/does-not-exist/steer", {
      instruction: "nope",
    });
    assert.equal(missing.status, 404);
    assert.deepEqual(missing.payload, { ok: false, message: "No Nightwatch run matches that id." });
  });

  it("makeServer binds ipv6 loopback", async () => {
    const ipv6 = await makeServer("::1", 0);
    try {
      const address = ipv6.address() as AddressInfo;
      assert.equal(address.address, "::1");
      assert.ok(address.port > 0);
    } finally {
      await new Promise<void>((resolve, reject) => {
        ipv6.close((error) => (error ? reject(error) : resolve()));
      });
    }
  });

  it("serve rejects wildcard without binding", async () => {
    assert.equal(isLoopbackHost("127.0.0.1"), true);
    assert.equal(isLoopbackHost("::1"), true);
    assert.equal(isLoopbackHost("0.0.0.0"), false);
    assert.equal(isLoopbackHost("localhost"), false);
    assert.equal(isLoopbackHost("::"), false);
    const code = await serve("0.0.0.0", 8765);
    assert.equal(code, 2);
    await assert.rejects(async () => makeServer("0.0.0.0", 0), /refusing to bind/);
  });

});
