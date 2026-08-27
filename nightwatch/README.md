# nightwatch

`nightwatch` is a Linux-first unattended supervisor for one exact OpenAI Codex
CLI thread in one Git repository. It is intentionally a small trusted control
plane, not an agent framework.

## Trust boundary

Authoritative state is outside the Codex workspace:

```text
~/.local/state/codex-nightwatch/<repo-name>-<path-and-git-hash>/
├── state.json, plan.json, acceptance.json, verification-policy.json
├── events.jsonl, supervisor.log, supervisor.lock
├── runs/, reports/, checkpoint.md, metadata.json
```

The repository contains only the untrusted agent mailbox:

```text
.nightwatch-agent/{context.json,proposed-plan.json,progress.json,blocker.json}
```

Codex cannot change thread identity, quota leases, progress authority, verified
status, reports, or verification policy. Legacy `repo/.nightwatch/` data is
preserved but ignored; schema-1 state is never silently reinterpreted.

## Use

Pass real acceptance commands yourself. They are frozen before Codex starts;
model-proposed shell commands are rejected and never executed. A lone `git diff
--check` is deliberately insufficient for an arbitrary natural-language goal.

```bash
cd /path/to/git-repo
nightwatch run --service \
  --verify 'pytest -q' \
  --verify 'git diff --check' \
  '完成当前仓库规划中的所有剩余任务，并逐阶段验证，直到所有验收条件满足'
```

The service is user-level systemd. It survives terminal closure, retries only
unexpected internal exits, and does not loop for `BLOCKED`, `STOPPED`, or
provider/auth `FAILED` outcomes.

```bash
nightwatch status
nightwatch log
nightwatch report
nightwatch stop
nightwatch recover --ack-ambiguous  # only after manual ambiguous-lease review
```

Quota authority is reported explicitly. Live App Server reads can authorize
recovery; rollout JSONL is schedule-only. If live quota status is unavailable
after a provider-declared reset, Nightwatch permits at most one guarded exact
thread availability probe for that quota generation.

```bash
nightwatch doctor
nightwatch test app-server
nightwatch test quota-soak
```

Install/update is user-local and reversible:

```bash
nightwatch install
nightwatch uninstall
```

Nightwatch backs up prior Nightwatch-marked launcher/unit files under its local
state directory before replacing them and never overwrites unrelated files.
