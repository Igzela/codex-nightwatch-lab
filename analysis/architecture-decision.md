# Architecture decision — schema 2 trusted control plane

## Decision

Nightwatch is a Python standard-library Linux supervisor for one exact Codex
thread per canonical Git repository. Its trusted control plane lives outside
the workspace in an XDG user-state directory keyed by canonical path plus Git
origin identity. Codex receives workspace-write access only to the repository,
where `.nightwatch-agent/` is an explicitly untrusted mailbox.

Schema-1 `repo/.nightwatch` state is forensic legacy data, never a source of
truth. The v2 store validates state/repo identity, frozen policy binding,
permissions, atomic writes, event sequence monotonicity, and symlink safety.

## Acceptance and verification

The user provides `--verify COMMAND` at `run` time. Commands are copied and
hashed into trusted state; `/bin/sh -lc` is used only for these explicit user
capabilities and receives a restricted environment. Codex can propose
milestone IDs/titles/weights and report implementation progress, but any
verification command, trusted-policy field, invalid mailbox shape, or symlink
is rejected.

Each accepted milestone maps to the frozen default profile. A no-policy or
diff-only policy cannot produce DONE for a natural-language goal:
`AWAITING_ACCEPTANCE` is the honest terminal state.

## Quota protocol

`app_server.py` owns JSON-RPC lifecycle: start stdio server, send `initialize`,
wait for that exact response, send `initialized`, issue parameterless
`account/rateLimits/read`, then wait for its exact response while ignoring
notifications/wrong IDs. Live App Server data is recovery authority.

Rollout JSONL is only schedule evidence. At reset, an unavailable live source
permits one claimed exact-thread availability probe per quota generation; a
second quota event creates a new generation. It never blind-loops resumes.

## Recovery

`supervisor.lock` covers the entire Supervisor lifecycle. Active child identity
uses PID + `/proc/<pid>/stat` starttime + executable, not PID liveness alone.
Claim phase `claimed` is proven-not-sent; `spawn_prepared`/`spawned` after a
crash is ambiguous and blocks until an explicit audited human acknowledgement.

The systemd unit uses `Restart=on-failure` plus `RestartPreventExitStatus=10
11 12`, where those exits mean BLOCKED, STOPPED, and expected provider/auth
failure. Unexpected code 20 is restarted.

## 0.4 account-pool extension

`CURRENT_ONLY` remains the backward-compatible default. `AUTO_POOL` is opt-in
and accepts only stable account keys resolved from an explicitly selected
`codex-auth list --skip-api --json` subset. Account selection is localized in
`account_broker.py`: a fresh official App Server `account/rateLimits/read`
sample must contain both 5h and weekly windows, and the deterministic policy
maximizes the smaller remaining capacity before applying the documented tie
breakers. codex-auth remote usage is never a Nightwatch authority.

Each probe and provider turn obtains a global external filesystem lease for the
account. A per-run external `CODEX_HOME` capsule contains one selected account;
its refreshed snapshot is synchronized through codex-auth before the lease is
released. The lease is held through child lifetime, uses Linux PID birth
identity, and fails closed on corrupt or ambiguous metadata. Stale capsules are
reconciled only through an attributable manifest and exact synchronization.

Cross-account exact-thread portability is intentionally `INCONCLUSIVE` until a
safe version-bound real experiment proves it. The implemented fallback is a
`CONTROLLED_THREAD_HANDOFF`: Nightwatch starts a new provider conversation from
trusted goal, policy, repository/Git HEAD, and milestone facts, retaining the
prior thread only for audit. The fake E2E proves A→B→wait→A rotation and both
5h/weekly governing behavior without claiming exact-thread continuity.

## 0.4.0 Durable Thread Store & Ephemeral Auth Hardening

A critical architecture refinement resolved a P1 release blocker where destroying
ephemeral per-account capsules wiped out `$CODEX_HOME/sessions`, eliminating local
session rollout files and causing official Codex to fail with `no rollout found for
thread id` on multi-turn and cross-account resumes.

Under the hardened architecture:
1. **Persistent Run CODEX_HOME**: `NightwatchStore.codex_home` (`~/.local/state/codex-nightwatch/<repo_id>/codex-runtime/codex-home/`, mode `0700`) persists across all provider turns, supervisor restarts, and account handoffs within the run.
2. **Durable Thread Store**: Local Codex session state (`sessions/` rollouts and SQLite databases) remains intact in the persistent run `codex_home`.
3. **Ephemeral Leased Auth**: `AccountCapsule` manages credential material (`auth.json`, `registry.json`, `accounts/`) strictly within the active account lease window. Single-account staging pruning strips all foreign accounts before import.
4. **Credential Scrubbing**: Upon turn completion, crash, or account rotation, credential material is scrubbed from the run `codex_home` while strictly preserving the durable Thread Store.
5. **Adoption Protection**: `AUTO_POOL` mode explicitly rejects `--thread` and `adopt` to prevent session collision and foreign credential leakage.
6. **Exact-Thread Cross-Account Portability**: Proven empirically with official `codex-cli 0.152.1` in Section 12 (Account A -> Account B -> Account A exact resume). For proven Codex versions (`0.152.1`), `cross_account_thread_mode` resolves to `PROVEN` and uses `codex exec resume <thread_id> -`. For unproven or inconclusive versions, Nightwatch safely falls back to `CONTROLLED_THREAD_HANDOFF`.

