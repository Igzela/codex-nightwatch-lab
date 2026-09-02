# Nightwatch 0.4.0 Account Pool Final Report

## Hardening acceptance addendum — 2026-09-02

This addendum supersedes the historical fake-only checkpoint below. The
historical evidence is retained and explicitly labeled; it is not reused as
proof of the real integration gates.

FINAL_HEAD: 6139102d5bfe7cef4a462c4e77d920a5e202bc37
VERSION: 0.4.0
PR: #1
PR_STATE: DRAFT
BASE_MASTER: 1f7dbd12bce89f26df2379aaed37c39c1004a49c

CODEX_VERSION: codex-cli 0.152.1
CODEX_AUTH_VERSION: codex-auth 0.3.0-alpha.11
CODEX_AUTH_SHA: 0fde29598c2e02e28e0e8bcc33a4bb8d45d7b23a
CODEX_AUTH_TEST_BINARY: /home/igzela/.local/lib/nightwatch-test/codex-auth/0.3.0-alpha.11/codex-auth
CODEX_AUTH_BINARY_SHA256: 3a766717d2b3263a678de170594373885dcebedd71bfb590755af460f8a65b69
CODEX_AUTH_HOST_VERSION: codex-auth 0.2.10 (unchanged)
CODEX_AUTH_JSON_SCHEMA: 1

UNIT_TEST_COUNT: 192
CI_311: PASS
CI_312: PASS
CI_313: PASS
COMPILEALL: PASS
DIFF_CHECK: PASS
PACKAGE_INSTALL: PASS
DOCTOR: PASS

REMOVE_SCHEMA_REAL_CONTRACT: PASS
CANONICAL_REGISTRY_LOCK: PASS
CONCURRENT_A_B_REFRESH_PRESERVATION: PASS
ACCOUNT_LEASE_PARALLELISM: PASS
REGISTRY_LOCK_PROVIDER_SCOPE: PASS
LOCK_ORDER: account lease -> registry lock
LOCK_ORDER_RESULT: PASS
NORMAL_QUOTA_CYCLES_OVER_20: PASS
NORMAL_QUOTA_CYCLES_TESTED: 30
RECOVERY_FAILURE_CIRCUIT_BREAKER: PASS
PROVIDER_CAPSULE_SELECTED_ACCOUNT_ONLY: PASS
ALL_ACCOUNT_EXPORT_REMOVED_BEFORE_PROVIDER: PASS

REAL_CODEX_AUTH_LIST: PASS
REAL_CODEX_AUTH_SWITCH: PASS
REAL_CODEX_AUTH_REMOVE: PASS
REAL_CODEX_AUTH_IMPORT_EXPORT: PASS
REAL_TWO_ACCOUNT_DISCOVERY: PASS
REAL_ACCOUNT_A_APP_SERVER: PASS
REAL_ACCOUNT_B_APP_SERVER: FAIL
REAL_AUTH_REFRESH_PRESERVATION: INCONCLUSIVE
CROSS_ACCOUNT_EXACT_THREAD: INCONCLUSIVE
CONTROLLED_THREAD_HANDOFF: PASS
REAL_TWO_ACCOUNT_NIGHTWATCH_SMOKE: PASS
REAL_NATURAL_ACCOUNT_ROTATION: NOT_OBSERVED

CURRENT_ONLY_REGRESSION: PASS
DEFERRED_START_REGRESSION: PASS
ADOPT_REGRESSION: PASS
MULTI_RUN_REGRESSION: PASS

KNOWN_LIMITATIONS:

- Live canonical discovery found three stored accounts. The active account
  returned authoritative App Server quota; the selected second account
  returned upstream `401 Unauthorized` token-parsing errors. The other
  non-active candidate showed the same condition. This prevents a real
  two-usable-account acceptance, so the PR remains Draft.
- Real two-account auth-refresh preservation cannot be classified PASS while
  the second account cannot complete an authoritative App Server session.
- Cross-account exact-thread portability was not attempted after the failed
  second-account gate and remains INCONCLUSIVE. Production behavior remains
  `CONTROLLED_THREAD_HANDOFF` (mission continuity yes; conversation continuity
  is a new thread).
- Natural quota rotation was not observed and no quota was intentionally
  consumed to manufacture it.

MERGE_DECISION: KEEP_DRAFT
READY_FOR_REVIEW: NO

Evidence details: the isolated real contract smoke exercised import, list,
switch, remove, export, and import/export round-trip through Nightwatch's
adapter. The real Nightwatch routing smoke used exactly two authorized keys in
a disposable Git repository, completed `DONE`, and preserved the canonical
active account; the unusable second candidate was skipped. No credentials or
token differences were logged.

The hardening checkpoint `6139102` additionally makes active-account
read/import/restore one canonical transaction, passes the directory lock FD to
the codex-auth child, rejects replaced metadata paths, and exercises seven
capsule crash seams plus the three canonical registry crash-hook boundaries.
The report commit is documentation-only; `FINAL_HEAD` identifies the exact
implementation checkpoint covered by these results.

## Historical fake-only checkpoint

HISTORICAL_FINAL_HEAD: 325b2aa1fe523b9b5b202094c9e51b710eb0c75c
BRANCH: account-pool-0-4
VERSION: 0.4.0
DATE: 2026-09-02

`FINAL_HEAD` is the exact implementation checkpoint covered by the local
verification below. The report commit is documentation-only and is called out
separately by the delivery message.

## Executive result

The opt-in account-pool implementation is complete at the code and test seam.
`CURRENT_ONLY` remains the default and does not require `codex-auth`. The
explicit `AUTO_POOL` path is fail-closed when the optional machine-readable
adapter is unavailable, and the fake end-to-end suite proves deterministic
selection, both quota windows, global account leases, external account
capsules, rotation, wait/re-probe, refresh preservation, and controlled
cross-account handoff.

The host's installed `codex-auth` is 0.2.10 and does not provide the required
JSON contract: both local `list --skip-api --json` probes exited non-zero with
empty stdout, and `export --help` is unsupported. Therefore no real account
switch, real two-account rotation, or natural quota consumption was attempted.
That is an environment capability limit, not a claimed production pass.

## Requirement-by-requirement acceptance

| Requirement | Result | Evidence or boundary |
|---|---|---|
| Start from current master and preserve unrelated work | PASS | `origin/master` was refreshed at `1f7dbd12bce89f26df2379aaed37c39c1004a49c`; implementation was made in linked worktree `/home/igzela/Projects/.worktrees/codex-nightwatch-lab/account-pool-0-4`. |
| Default current-account behavior | PASS | Legacy state loads as `CURRENT_ONLY`; current-only runs do not invoke account-pool discovery. Covered by `test_pre_pool_state_loads_as_current_only` and the existing regression suite. |
| Explicit account subset only | PASS | `AUTO_POOL` requires one or more explicit `--account` selectors; exact stable keys and unique alias/name matches are accepted; all stored accounts are never implicitly enrolled. |
| Stable account identity | PASS | `account_key` is the only adapter identity; persisted and displayed operationally as a one-way fingerprint. Row numbers, email-like fields, and unknown fields are not identity inputs. |
| Optional `codex-auth` machine contract | PASS | Adapter uses argv-only local `list [--active] --skip-api --json` and `switch <account_key> --json`, requires schema v1, ignores unknown fields, rejects future/malformed schema, and never parses stderr as logic. |
| No `codex-auth` remote usage API | PASS | Adapter has no remote usage call; quota selection uses only fresh App Server `account/rateLimits/read` or the explicitly marked fake-file authority. |
| Both 5h and weekly windows govern | PASS | Selection rejects missing/unknown windows and maximizes `min(5h remaining, weekly remaining)`; weekly-only exhaustion is separately tested. |
| Deterministic selection | PASS | Tie order is minimum remaining capacity, 5h remaining, weekly remaining, earlier reset, then fingerprint. |
| Global same-account exclusion | PASS | Kernel-backed file lifetime lease; same account is exclusive, different accounts are independent, and lease metadata is audited. |
| PID identity and stale-state safety | PASS | PID starttime plus executable identity; corrupt, symlinked, mismatched, and live-owner records fail closed. |
| Provider/App Server lease lifetime | PASS | The account lease and capsule surround the quota/provider boundary and are released only after provider exit and capsule synchronization. Lease fd is passed to child boundaries. |
| External `CODEX_HOME` capsules | PASS | Per-run external 0700 capsule; managed files are hardened 0600; canonical auth remains outside the repository; capsule cleanup is bounded and recoverable. |
| Auth refresh preservation | PASS | Opaque snapshot refresh test proves A → B → A preserves refreshed A state without Nightwatch parsing or logging tokens. |
| Account switch after provider exit | PASS | Pool rotation is triggered only after the provider result is classified and the prior account lease has exited its provider phase. |
| Cross-account exact-thread claim | INCONCLUSIVE | Exact-thread portability is not assumed or reported. The implementation uses `CONTROLLED_THREAD_HANDOFF` with a new conversation and an auditable trusted packet. |
| Controlled handoff packet | PASS | Packet binds goal, frozen verification commands, repo, last Git HEAD, prior thread for audit, generation, milestones, and blocker. |
| Pool exhaustion and busy wait | PASS | All selected accounts exhausted or leased enter `WAIT_QUOTA`; the earliest relevant authoritative reset is persisted and the complete authorized pool is re-probed. |
| Best account after reset | PASS | Fake weekly scenario proves a previously exhausted account is selected again when it becomes the greatest usable capacity. |
| All-authorized auth failure | PASS | All account authentication failures transition to `BLOCKED`; no retry loop is created. |
| Restart reconciliation | PASS | Active account is re-read after supervisor restart; an active account outside the explicit pool fails closed; ambiguous claims remain fail closed. |
| TUI and CLI surfaces | PASS | CLI flags, status fields, wait status, TUI account picker, current-only fallback, account fingerprints, and lease/pool state are covered by tests. |
| Deferred first launch | PASS | Existing deferred-start tests remain green; no provider spawn occurs while authoritative quota is exhausted. |
| Adopt/resume and multi-run safety | PASS | Existing adopt/resume and same-repo/same-account collision suites remain green; account lease is independent per account and supervisor state remains single-writer. |

## Verification evidence

### Local tests and static checks

- Python 3.14/current: `168` tests passed.
- Python 3.11: `168` tests passed.
- `python3 -m compileall -q nightwatch`: passed.
- Python 3.11 compileall: passed.
- `git diff --check`: passed before the implementation commit.
- Focused account-pool tests: adapter/schema, selection, lease, capsule,
  probe, fake rotation, weekly wait/recovery, and TUI coverage all passed.
- Isolated package install in a temporary virtual environment: passed;
  `nightwatch --version` reported `0.4.0`.
- Live packaged `nightwatch doctor`: passed with Codex `codex-cli 0.152.1`,
  live App Server quota authority, and `systemd-inhibit` available.

Expected test output includes argparse diagnostics for a deliberately invalid
model and a no-user-bus service warning; both are asserted test scenarios.

### Fake end-to-end evidence

- Two-account 5h rotation: A exhausts, B is selected, B exhausts, the pool
  waits and re-probes, then A is selected again. The final state is `DONE`,
  with three account generations, three provider sessions, eight fresh quota
  probes, and controlled handoff packets marked `INCONCLUSIVE` for exact
  cross-account thread portability.
- Weekly-governing scenario: an account with healthy 5h but exhausted weekly
  quota is rejected; the pool waits through the relevant reset boundaries and
  reselects the best recovered account.
- Lease crash/restart seams: kernel-release, stale PID, corrupt metadata,
  symlink, same-account collision, and different-account parallelism are
  covered.

## Live capability audit and deferred experiments

- Installed `codex-auth`: `0.2.10`.
- Audited upstream JSON contract: schema v1, stdout-only JSON document,
  stderr diagnostics, stable `account_key`, local `--skip-api` discovery,
  and local switch/export/import command family.
- Host `codex-auth list --skip-api --json`: unsupported/non-zero with no JSON
  stdout.
- Host `codex-auth list --active --skip-api --json`: unsupported/non-zero with
  no JSON stdout.
- Host `codex-auth export --help`: unsupported.
- No real switch was executed, no provider was run under a second real
  account, and no natural quota was consumed to force rotation.
- No user Nightwatch/Codex workload in this repository was disturbed.

## Known limitations

1. Real two-account and natural-quota rotation remain deferred until an
   installed, verified `codex-auth` build exposes the required local JSON and
   snapshot contract and the user authorizes a safe experiment.
2. Cross-account exact-thread portability is deliberately not certified for
   Codex `0.152.1`; controlled handoff is the fail-closed behavior.
3. Local verification ran Python 3.11 and the current interpreter. Python
   3.12/3.13 interpreters were not installed on the host; the repository CI
   matrix remains the authoritative place to run those versions.
4. The crash tests cover the lease/capsule/restart safety seams and existing
   supervisor crash matrix; they are not a claim that every individual
   filesystem instruction has been fault-injected.

## Release boundary

The implementation is versioned `0.4.0`, but merge/release remains gated on
the exact-head CI and review policy. This report does not certify real-account
production behavior or cross-account exact-thread continuity beyond the
evidence explicitly listed above.
