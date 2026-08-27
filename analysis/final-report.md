# Nightwatch final report

Status: **USABLE_PENDING_REAL_QUOTA_SOAK**

## 1. Final architecture

Nightwatch is a small Python 3.11+ standard-library Linux CLI. One Git
repository owns one `.nightwatch/` state directory and one exact Codex thread.
The foreground supervisor launches `codex exec --json` with workspace-write
sandboxing and a prompt on stdin, captures `thread.started.thread_id`, and
resumes only with `codex exec --json resume <EXACT_THREAD_ID> -`.

State transitions, atomic JSON snapshots, append-only JSONL evidence, file
locking, bounded retry budgets, Git recovery checks, milestone verification,
quota generation leases, and final DONE guards are all local and durable.
`systemd-inhibit` and an optional user-level systemd unit are integrations, not
correctness prerequisites.

## 2. Reference projects audited

Cloned and source-audited: `continuation-layer`, `unsnooze`, `codex-auto`,
`tmux-codex-auto-continue`, `codex-limit`, and `agent-resume`. The checkouts
are in `refs/`; none was modified and no source code was copied. Detailed
comparisons are in `source-audit.md`, `feature-matrix.md`, and
`design-review.md`.

## 3. Adopted and rejected designs

Adopted: structured JSONL identity/rate limits, App Server RPC shape,
rollout structured fallback, exact-ID argv resume, watchdog polling, atomic
state, single-flight leases, bounded backoff, PID reconciliation, and Git
reality checks.

Rejected: TUI/screen scraping as an authority, PTY/tmux as a correctness
dependency, `resume --last`, latest-session guessing, model-owned progress,
unlocked writes, infinite retry, auto approval, sandbox bypass, and a broad
multi-provider framework. No reference code was reused; license details and
the reason are recorded in `attribution.md`.

## 4. Product layout

```text
nightwatch/
├── nightwatch/{cli,codex,git,milestones,models,quota,storage,supervisor}.py
├── tests/{test_unit,test_fake_e2e,test_fault_matrix,test_recovery}.py
├── bin/nightwatch
├── systemd/nightwatch.service
├── pyproject.toml
└── README.md
```

Runtime state is `.nightwatch/state.json`, `goal.md`, `plan.json`,
`checkpoint.md`, `events.jsonl`, `supervisor.log`, `runs/`, and `reports/`.

## 5. Tests

The final automated suite contains **37 tests**, all passing with
`ResourceWarning` promoted to errors. It includes pure unit tests, fake-Codex
E2E, the full fault matrix, quota leases, exact-thread recovery, Codex child
crash recovery, and Nightwatch `SIGKILL` restart recovery.

## 6. Real validation

- Real Codex JSONL thread capture: **PASS**.
- Real Codex calculator smoke and exact-thread manual resume: **PASS**.
- Real Codex child `SIGKILL` recovery: **PASS**.
- Nightwatch supervisor crash recovery: **PASS** in isolated automated fixture.
- DONE guard against failed verification: **PASS**.
- Secret redaction and no dangerous auto-approval/bypass flags: **PASS**.
- Doctor: **PASS** for Linux/Codex/auth; quota source was structured
  `rollout_jsonl` fallback because the App Server was unreachable here.

## 7. Quota source

The source order is:

1. Codex App Server `account/rateLimits/read`.
2. Recent local Codex rollout JSONL `rate_limits`.
3. Structured Codex error payload/relative reset information for scheduling.

The fallback is freshness-bounded. If quota recovery cannot be confirmed, no
resume is sent and the run remains `WAIT_QUOTA` or becomes `BLOCKED`.

Reports include each validated 5h and weekly window's used percentage, duration,
and reset epoch when a snapshot was available during that run.

## 8. Real quota soak

`REAL_QUOTA_SOAK = PENDING_REAL_QUOTA_SOAK`. It was intentionally not forced:
the test requires a natural 5h/weekly provider limit cycle. This is the only
release-gate item not immediately completed.

## 9. Known limitations

- The Codex App Server protocol is experimental and may change.
- Rollout fallback is local evidence, not a substitute for a live provider
  revalidation; stale records are rejected.
- A provider resume that fails after a quota lease is deliberately `BLOCKED`
  instead of retried in the same generation, preserving the exactly-once
  recovery invariant.
- Sleep inhibition depends on a functioning user/systemd bus; the supervisor
  remains usable without it.
- A required milestone must provide explicit verification commands. A model's
  “done” text never satisfies the DONE guard.

## 10. Install, use, uninstall

From the product directory after tests:

```bash
python3 bin/nightwatch install
cd /path/to/git-repo
nightwatch run "继续当前仓库规划，完成所有未完成任务并逐阶段验证。"
nightwatch status
nightwatch report
```

Optional repo-bound user service:

```bash
nightwatch install --service --repo /path/to/git-repo
systemctl --user enable --now nightwatch.service
```

Rollback is:

```bash
nightwatch uninstall
```

Uninstall removes only files containing Nightwatch's install marker and
preserves unrelated same-name files.

## 11. Final Git commit

Parent project commit after the final regression and install smoke:

`01ec614b304b8c2308a6c1d912eecf43c9aae709`
