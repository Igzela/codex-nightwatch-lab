# Nightwatch hardening release report

Status: **USABLE_PENDING_REAL_QUOTA_SOAK**

## Result

Nightwatch v2 is Linux-first, Codex-only, exact-thread, quota-aware,
crash-resilient, fail-closed, and has a trusted control plane outside the
Codex workspace. The previous release's architecture blockers are closed:
workspace authority, model-to-shell escalation, incomplete App Server protocol,
rollout-as-live-quota semantics, transaction-only locking, PID reuse, ambiguous
claims, and systemd exit-loop semantics.

## Architecture

- Trusted state: `~/.local/state/codex-nightwatch/<safe-name>-<sha256>/`,
  0700 root and 0600 files. Identity binds real repository path and origin.
- Untrusted mailbox: `repo/.nightwatch-agent/`; bounded no-symlink JSON input.
- Frozen acceptance: user `--verify` commands are hash-bound before Codex runs.
  Only those commands may execute; model verification commands are rejected.
- Completion: `DONE` requires trusted policy, verified milestones, final checks,
  Git safety and final report. Weak/diff-only acceptance is
  `AWAITING_ACCEPTANCE`, not DONE.
- Quota: live App Server revalidation is authority. Rollout JSONL is
  schedule-only; a reset without live authority permits one guarded probe per
  exact-thread generation.
- Recovery: lifetime lock, Linux PID identity, durable claim phase and explicit
  `recover --ack-ambiguous` for irreducible uncertainty.

## Validation

- 55 automated tests pass.
- Real Codex 0.150.1 App Server rate-limit handshake/read passes.
- Real user-systemd normal completion and post-thread-capture SIGKILL restart
  fixtures pass, with `TEST-001` exact resume evidence.
- A fresh schema-2 real Codex calculator fixture captured an exact thread
  externally, rejected an initial failed frozen check, then resumed that same
  thread and reached DONE only after final checks passed.

## Known limits

- `REAL_QUOTA_SOAK` remains pending a natural quota cycle; it was not forced.
- Explicit user verification commands are trusted local capabilities; users
  should choose commands that actually express their acceptance criteria.
- Logout persistence depends on host user-manager linger configuration.

## Use

```bash
nightwatch run --service --verify 'pytest -q' --verify 'git diff --check' 'goal'
nightwatch status
nightwatch report
nightwatch test app-server
```

Detailed design and security evidence are in `architecture-decision.md`,
`security-review.md`, and `verification-log.md`.
