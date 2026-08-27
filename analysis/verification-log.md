# Verification log

Date: 2026-08-27 (Asia/Shanghai)

## Automated

- `PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v`: **PASS**, 37 tests.
- `python3 -m compileall -q nightwatch`: **PASS**.
- Fake Codex covers normal completion, 5h and weekly limits, quota revalidation,
  quota hit again, temporary 429, capacity, network, auth, blocker, crash,
  malformed JSONL, missing thread ID, duplicate events, slow output, and the
  done-but-verification-fails guard.
- Fault/recovery tests cover wrong repo, Git conflict, non-descendant history,
  corrupt state, supervisor restart, ambiguous resume claims, duplicate
  single-flight claims, and explicit manual resume.
- The suite was rerun after the command-line compatibility and quota fallback
  changes; no `ResourceWarning` remained.

## Real Codex

- Read-only JSONL smoke: **PASS**. Codex `0.150.1` emitted a real
  `thread.started` event with thread ID `01a0438e-6757-7372-88be-2483f88eb418`.
- Real Nightwatch calculator fixture: **PASS**. Initial thread was
  `01a0438e-e19d-71b0-ade5-b1749315a464`; the first mechanical verification
  correctly failed because the fixture environment lacked pytest. An explicit
  `nightwatch resume` used the same exact thread, then the final fixture
  verification passed with 13 tests and state became `DONE`.
- Real Codex crash injection: **PASS**. In the isolated parity fixture, the
  Codex child was terminated with `SIGKILL` after thread capture. Nightwatch
  recorded `codex_crash`, bounded backoff, issued one exact-ID resume, and
  reached `DONE` with `crash_attempt=1` and the same thread ID
  `01a043b3-de1f-7e31-bad4-5f4ee4e72652`.
- Nightwatch process crash recovery: **PASS** in the automated fixture. The
  supervisor and its fake child were killed, `nightwatch resume` loaded the
  same run/thread/plan, and the fake provider recorded `starts=1`, `resumes=1`.

## Quota and environment

- Fake App Server structured read: **PASS**, including 5h/weekly usage,
  duration, and reset epoch parsing.
- Real `nightwatch doctor --json`: **PASS** for Linux, Codex binary/version,
  and auth. The live App Server request was unavailable because this execution
  environment cannot reach the provider; the doctor then successfully used the
  recent local `rollout_jsonl` structured fallback and reported 5h/weekly
  windows. No auth token was read or logged.
- `nightwatch test quota-soak`: **PENDING_REAL_QUOTA_SOAK**. No quota was
  intentionally consumed; a natural provider limit cycle is required for this
  final soak evidence.

## Installation

- Temporary-HOME `nightwatch install --service --repo ...` and
  `nightwatch uninstall`: **PASS**. The generated unit was repo-bound and the
  uninstall removed only Nightwatch-marked files.
- Final user-local install: **PASS**. `command -v nightwatch` resolved to
  `/home/charlie/.local/bin/nightwatch`; installed `--version`, `status`,
  `report`, `doctor --json`, and `test quota-soak` all behaved as expected.
- `systemd-inhibit` binary exists, but the current container cannot connect to
  the user/system bus. The CLI probe reports it unusable and continues without
  a sleep lock; a normal host with a working user bus gets the inhibitor.
