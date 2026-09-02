# Release hardening security review

Date: 2026-09-02 (Asia/Tokyo)

## Status

**BLOCKED_PENDING_REAL_TWO_ACCOUNT_ACCEPTANCE**. No quota was burned to force
a natural exhaustion/recovery cycle. The isolated codex-auth contract smoke
passed, but one tested non-active account cannot complete an authoritative App
Server session, so refresh-preservation and cross-account exact-thread gates
remain inconclusive.

## Closed release blockers

| Boundary | Contract after hardening |
|---|---|
| Control plane | Schema-2 state is in `~/.local/state/codex-nightwatch/<stable-repo-id>/`, mode 0700/0600; it is outside Codex workspace-write scope. |
| Repository identity | State key binds canonical path and origin Git identity; same basenames cannot collide and a moved repo does not silently attach old state. |
| Workspace input | `.nightwatch-agent` is an untrusted mailbox with regular-file, no-symlink, 1 MB, JSON-depth, field, count, ID, and string limits. |
| Shell capability | Only user-supplied `--verify` commands are frozen into trusted policy. Model `verification_commands` are rejected and never reach `/bin/sh`. |
| DONE authority | A model only supplies milestone structure/progress. `verified` comes only from frozen trusted checks; diff-only policy yields `AWAITING_ACCEPTANCE`. |
| Quota authority | Current Codex App Server handshake is sequential and live-authoritative. Rollout JSONL is schedule-only and cannot confirm recovery. |
| Supervisor concurrency | A non-blocking lifetime `supervisor.lock` rejects a second resume without state mutation. |
| PID reuse | Active provider identity persists PID, `/proc` starttime, and executable; mismatch is fail-closed. |
| Ambiguous lease | Claims before spawn preparation are proven-not-sent; later ambiguity blocks until `recover --ack-ambiguous` records human override. |
| Service restart | Exit contract is `0` clean/acceptance handoff, `10` BLOCKED, `11` STOPPED, `12` FAILED, `20` internal. systemd restarts failures but prevents 10/11/12 restart loops. |
| Secrets | Logs, events, reports, stderr and verification output are redacted; verification gets an allowlisted environment rather than all environment variables. |

## Evidence

- 61 unit/fake/fault/recovery/security/concurrency tests passed with
  `ResourceWarning` promoted to errors.
- Fake App Server verifies required initialize response, `initialized`
  notification, request ordering, wrong IDs, notifications, malformed JSON,
  timeout, exit, error response, and millisecond resets.
- Historical 0.3 real Codex `0.150.1` App Server handshake and rate-limit read
  passed on this machine. It returned live 5h and weekly windows.
- A real user-systemd disposable fixture reached DONE; a second fixture was
  SIGKILLed after exact thread capture, systemd restarted it, and it resumed
  `TEST-001` rather than opening a second thread.
- A fresh real-Codex schema-2 fixture captured an external exact thread. Its
  first frozen verification failed and was BLOCKED; an exact-thread resume
  repaired the fixture and reached DONE only after the frozen checks passed.
- User-local install smoke passed: `~/.local/bin/nightwatch`, `doctor`, and
  live App Server rate-limit read all completed. `quota-soak` remains pending
  rather than manufacturing a provider limit.
- Final path-boundary adversarial smoke passed: a symlinked
  `repo/.nightwatch-agent` was rejected before Codex launch with zero writes to
  the outside target; state homes under the repo were rejected; an external
  state home initialized successfully.
- Regression coverage also proves that replacing an already initialized
  mailbox root with a symlink is rejected on later reads, and that a state-home
  symlink resolving into the workspace is rejected before creation.

- Current 0.4 regression coverage is 192 tests. The audited upstream
  `codex-auth 0.3.0-alpha.11` binary was built in isolation and passed the
  Nightwatch adapter contract smoke for list, switch, remove, import, export,
  and round-trip behavior. Canonical discovery found three stored accounts;
  the active account App Server probe passed, while both tested non-active
  snapshots returned upstream 401 token-parse failures.

## Residual limits

- A user can intentionally provide a weak non-diff verification command; that
  is explicit local authority, not a model capability.
- `recover --ack-ambiguous` is deliberately a documented human override.
- User-service persistence after logout depends on the host's user-manager
  linger policy; Nightwatch does not alter it.

## 0.4 account-pool security boundary

- `codex-auth` is optional. Nightwatch consumes only its schema-v1 local JSON
  discovery contract and never uses its remote usage API.
- Account credentials remain outside Git in 0700 capsule directories and
  restricted files. Capsule synchronization is required before cleanup; an
  uncertain refresh state is retained for recovery rather than discarded.
- Per-account lock files are held for the complete App Server/provider
  lifetime. Symlinked roots/paths, corrupt metadata, stale PID reuse, and
  changed ownership fail closed without deleting another owner's record.
- Canonical registry synchronization uses a short-lived directory-inode kernel
  lock after the account lease; the metadata pathname is checked for symlink,
  corruption, and replacement, and the lock is not held during provider work.
- AUTO_POOL selection is explicitly scoped to the run's authorized subset and
  requires authoritative 5h plus weekly quota. Cross-account exact-thread
  continuity is not advertised; the tested fallback is an audited controlled
  mission handoff.
- Real two-account Nightwatch routing reached DONE with exactly two authorized
  keys in a disposable repository and preserved the canonical active account,
  but this does not substitute for the failed second-account App Server gate.
