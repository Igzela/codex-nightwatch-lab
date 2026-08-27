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
