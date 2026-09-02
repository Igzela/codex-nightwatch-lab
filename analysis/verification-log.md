# Verification log — hardening release

Date: 2026-08-28 (Asia/Shanghai)

## Automated

- `PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v`: **PASS**, 61 tests.
- `python3 -m compileall -q nightwatch`: **PASS**.
- Trusted-control tests cover external state, repo legacy-state ignorance,
  mailbox command injection, frozen policy, symlink rejection, diff-only DONE
  guard, event sequence corruption, secret environment exclusion, PID identity,
  and real two-process resume contention.
- App Server fake protocol covers initialize ordering, required initialized
  notification, wrong IDs, notifications, malformed JSON, error, timeout,
  server exit, seconds/milliseconds, primary/secondary windows and 99.9/100+.
- Existing fake-Codex, fault matrix, exact-thread recovery, quota lease,
  malformed JSON, Git conflict, Codex child crash and supervisor crash tests
  were migrated to schema 2 and pass.
- New path-boundary regressions: **PASS**. Mailbox root symlink rejection,
  `NIGHTWATCH_STATE_HOME` inside repo rejection, `XDG_STATE_HOME` inside repo
  rejection, and valid external state home are covered.

## Real Codex / App Server

- Codex CLI: **PASS**, `codex-cli 0.150.1`.
- `nightwatch test app-server`: **REAL_APP_SERVER_RATE_LIMITS = PASS**. The
  client sent initialize, waited for id 1, sent initialized, then parameterless
  `account/rateLimits/read` id 2; live 5h and weekly windows parsed.
- Disposable schema-2 calculator fixture: **PASS**. A real `codex exec --json`
  run captured thread `01a043f8-3633-7dd2-a4d0-623d69886f43` in external trusted
  state. The first frozen discovery check failed, correctly producing BLOCKED;
  `nightwatch resume` then issued `codex exec --json resume <that exact id>`.
  The same thread repaired the layout, all frozen checks passed, and only then
  reached DONE. The fixture and its external test state were removed after
  inspection.

## Real systemd

- Disposable Fake-Codex service fixture: **PASS**. `run --service` reached
  DONE through frozen checks and service became inactive (no terminal loop).
- Disposable crash fixture: **PASS**. A test-only SIGKILL immediately after
  durable exact thread capture caused systemd restart; the replacement service
  issued `resume TEST-001`, then reached DONE. Test units, manager environment,
  launcher and fixtures were removed afterward.

## Pending

`REAL_QUOTA_SOAK = PENDING_REAL_QUOTA_SOAK`. No real quota exhaustion was
manufactured. Future natural quota evidence must show reset authority/probe,
one lease, and same-thread post-reset success before changing this to PASS.

## Final path-boundary smoke

- Scenario A, `repo/.nightwatch-agent -> outside`: **PASS**; initialization
  failed closed, outside remained untouched, and Codex was not launched.
- Scenario B, state home inside repo: **PASS**; initialization failed closed
  and no trusted state was created in the repo.
- Scenario C, external state home: **PASS**; schema-2 initialization completed
  with trusted state outside the workspace.

## 0.4 account-pool extension

- `python3 -m unittest discover -s nightwatch/tests -v`: **PASS**, 195 tests.
- `python3.11 -m unittest discover -s nightwatch/tests -q`: **PASS**, 195 tests;
  compileall also passed.
- One preceding Python 3.11 full-suite attempt reported a single state-identity
  failure in `test_state_is_outside_workspace_and_legacy_state_is_ignored`;
  the isolated test and an immediate full-suite rerun both passed. This is
  recorded as a non-reproduced test-harness observation, not hidden as a pass.
- Fake account-pool E2E: **PASS** for A→B quota rotation, all-account wait,
  reset re-probe, return to A, controlled handoff, weekly governing quota,
  global leases, crash release, stale PID protection, and opaque auth refresh
  preservation.
- Fault coverage includes a real `AFTER_PROVIDER_EXIT` SIGKILL followed by
  account-lease reacquisition and successful supervisor reconciliation, plus
  hidden lock-root replacement while a descendant retains the inherited lease
  FD.
- Historical host-only check: installed `codex-auth 0.2.10` does not expose
  the required schema-v1 JSON commands (rc=2, empty stdout), so AUTO_POOL
  without the isolated test override is safely unavailable; CURRENT_ONLY
  remains operational. Upstream audited contract is schema v1.
- Live `nightwatch doctor` on Codex `0.152.1`: **PASS**; App Server authority
  returned validated 5h and weekly windows. No real two-account rotation was
  attempted because the installed account tool could not provide the required
  local machine-readable pool contract.
- Isolated upstream `codex-auth 0.3.0-alpha.11` at commit
  `0fde29598c2e02e28e0e8bcc33a4bb8d45d7b23a`: **PASS** for the real adapter
  contract smoke covering list, switch, remove, import, export, and
  import/export round-trip. The host `0.2.10` binary was not overwritten.
- Canonical discovery through that isolated binary: **PASS**, three stored
  accounts found without switching the user's active account. The active
  account App Server probe passed; both tested non-active snapshots returned
  upstream 401 token-parse failures, so real two-account acceptance remains
  blocked.
