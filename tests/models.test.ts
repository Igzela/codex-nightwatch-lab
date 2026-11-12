import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import {
  State,
  TERMINAL_STATES,
  crossAccountThreadModeForVersion,
  emptyState,
  planProgress,
  validateState,
  type JsonObject,
} from "../src/models.js";

describe("models", () => {
  const previousForce = process.env.NIGHTWATCH_UNSAFE_FORCE_CROSS_ACCOUNT_PROVEN;
  const previousMode = process.env.NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE;

  afterEach(() => {
    if (previousForce === undefined) delete process.env.NIGHTWATCH_UNSAFE_FORCE_CROSS_ACCOUNT_PROVEN;
    else process.env.NIGHTWATCH_UNSAFE_FORCE_CROSS_ACCOUNT_PROVEN = previousForce;
    if (previousMode === undefined) delete process.env.NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE;
    else process.env.NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE = previousMode;
  });

  it("emptyState has the durable NEW shape", () => {
    const state = emptyState("run-1", "ship it", "/repo", "repo-id", "2026-01-01T00:00:00Z");
    assert.equal(state.schema_version, 2);
    assert.equal(state.run_id, "run-1");
    assert.equal(state.goal, "ship it");
    assert.equal(state.repo, "/repo");
    assert.equal(state.repo_id, "repo-id");
    assert.equal(state.provider, "codex");
    assert.equal(state.agy_print_timeout, null);
    assert.equal(state.thread_id, null);
    assert.equal(state.state, State.NEW);
    assert.equal(state.generation, 1);
    assert.equal(state.created_at, "2026-01-01T00:00:00Z");
    assert.equal(state.updated_at, "2026-01-01T00:00:00Z");
    assert.equal(state.last_event, "run_created");
    assert.equal(state.final_verification_passed, false);
    assert.equal(state.plan_ready, false);
    assert.equal(state.account_mode, "CURRENT_ONLY");
    assert.deepEqual(state.authorized_accounts, []);
    assert.equal(state.cross_account_thread_mode, "INCONCLUSIVE");
    assert.equal(state.initialized, false);
    assert.equal(state.retry_attempt, 0);
    assert.equal(state.crash_attempt, 0);
    assert.equal(state.recoveries, 0);
    assert.equal(state.quota_cycles, 0);
  });

  it("validateState accepts emptyState", () => {
    const state = emptyState("run-1", "ship it", "/repo", "repo-id", "2026-01-01T00:00:00Z");
    assert.doesNotThrow(() => validateState(state));
  });

  it("planProgress reports zero progress for an all-pending plan", () => {
    const plan: JsonObject = {
      schema_version: 2,
      authority: "nightwatch",
      policy_hash: "test",
      milestones: [
        {
          id: "M1",
          title: "one",
          weight: 2,
          required: true,
          status: "pending",
          verification_profile: "default",
          evidence: [],
        },
        {
          id: "M2",
          title: "two",
          weight: 3,
          required: true,
          status: "pending",
          verification_profile: "default",
          evidence: [],
        },
      ],
    };
    const progress = planProgress(plan);
    assert.equal(progress.implemented_count, 0);
    assert.equal(progress.verified_count, 0);
    assert.equal(progress.total_count, 2);
    assert.equal(progress.implemented_weight, 0);
    assert.equal(progress.verified_weight, 0);
    assert.equal(progress.total_weight, 5);
    assert.equal(progress.implemented_percent, 0);
    assert.equal(progress.verified_percent, 0);
  });

  it("planProgress reports partial progress mechanically", () => {
    const plan: JsonObject = {
      schema_version: 2,
      authority: "nightwatch",
      policy_hash: "test",
      milestones: [
        {
          id: "M1",
          title: "one",
          weight: 2,
          required: true,
          status: "verified",
          verification_profile: "default",
          evidence: [],
        },
        {
          id: "M2",
          title: "two",
          weight: 3,
          required: true,
          status: "implemented",
          verification_profile: "default",
          evidence: [],
        },
      ],
    };
    const progress = planProgress(plan);
    assert.equal(progress.implemented_count, 2);
    assert.equal(progress.verified_count, 1);
    assert.equal(progress.total_count, 2);
    assert.equal(progress.implemented_weight, 5);
    assert.equal(progress.verified_weight, 2);
    assert.equal(progress.total_weight, 5);
    assert.equal(progress.implemented_percent, 100);
    assert.equal(progress.verified_percent, 40);
  });

  it("TERMINAL_STATES contains DONE FAILED BLOCKED STOPPED AWAITING_ACCEPTANCE", () => {
    for (const value of [
      State.DONE,
      State.FAILED,
      State.BLOCKED,
      State.STOPPED,
      State.AWAITING_ACCEPTANCE,
    ]) {
      assert.ok(TERMINAL_STATES.has(value), `missing ${value}`);
    }
    assert.equal(TERMINAL_STATES.size, 5);
    assert.ok(!TERMINAL_STATES.has(State.NEW));
    assert.ok(!TERMINAL_STATES.has(State.RUNNING));
  });

  it("crossAccountThreadModeForVersion returns PROVEN for the allowlisted version", () => {
    delete process.env.NIGHTWATCH_UNSAFE_FORCE_CROSS_ACCOUNT_PROVEN;
    delete process.env.NIGHTWATCH_CROSS_ACCOUNT_THREAD_MODE;
    assert.equal(crossAccountThreadModeForVersion("0.152.1"), "PROVEN");
    assert.equal(crossAccountThreadModeForVersion("codex-cli 0.152.1"), "PROVEN");
    assert.equal(crossAccountThreadModeForVersion("fake-0.1"), "INCONCLUSIVE");
    assert.equal(crossAccountThreadModeForVersion(null), "INCONCLUSIVE");
  });
});
