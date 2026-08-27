# Architecture decision

## Decision

Nightwatch v1 is a Python 3.11+ standard-library Linux CLI. It runs one supervisor process per Git repository goal, launches `codex exec --json` with an explicit prompt, persists the first valid `thread.started.thread_id`, and resumes only with `codex exec resume <exact-id> --json`.

The repository is the unit of ownership. A goal owns one `.nightwatch/` directory, one exact thread ID, one durable state machine, one milestone plan, and one supervisor lock. No tmux, PTY, web dashboard, other providers, account rotation, or system service is required for correctness.

## Why Python stdlib

The problem is I/O, process supervision, JSONL, filesystem durability, SQLite read-only inspection, and subprocess management. Python's standard library supplies all of these with a small auditable dependency surface. A Rust/Go rewrite would add build/install complexity without improving the correctness properties required here.

## State and event design

- `state.json` is the validated current snapshot.
- `events.jsonl` is append-only evidence. Each automatic action records run ID, generation, thread ID, repo head, and decision reason.
- `runs/` stores redacted provider JSONL/stderr and command metadata; no token/auth file is read into logs.
- `plan.json` is Nightwatch-owned. Codex may propose milestones through a constrained JSON output, but Nightwatch validates and owns statuses/evidence.
- Every write uses a per-goal `fcntl.flock` lock, temp file, `fsync`, and `os.replace`; events flush and fsync before a state transition returns.

## Provider hierarchy

1. Fresh App Server `account/rateLimits/read` for quota and reset epochs.
2. Current rollout JSONL `event_msg`/`token_count.rate_limits` and `session_meta` for local structured evidence and a bounded fallback quota read.
3. `codex exec --json` events and error payloads.
4. Human provider text only to classify an already exited process and extract a reset duration; never to invent a thread.
5. TUI/screen scraping is not implemented as an automatic path.

## Recovery and concurrency

The supervisor lock prevents two Nightwatch processes for one goal. A quota recovery uses `(generation, thread_id)` as its idempotency key. Under the lock it revalidates the persisted state, records `resume_claimed`, increments a lease token, and only then starts one child. A second timer/poller/restart sees the claim or the in-flight generation and does nothing. Child exit always records a durable result before the next decision.

Automatic quota recovery sequence:

```text
provider limit → classify 5h/weekly → WAIT_QUOTA + reset provenance
→ sleep/poll wall clock → fresh quota read
→ if all governing windows recovered: claim generation
→ exact-thread resume → parse result → repeat/verify
```

No quota revalidation means no resume. A missing reset, missing thread, corrupt state, changed repo identity, unresolved Git conflict, or ambiguous provider result stops fail closed.

## Acceptance authority

Milestones are `pending`, `working`, `implemented`, `verified`, or `blocked`. Implemented is informational; verified requires an explicit command or evidence record. Formal progress is computed mechanically from milestone weights. `DONE` requires all required milestones verified, all verification commands passing, clean/allowed Git state, no blockers, and a final verification pass.

## Quota limitations

The App Server is experimental and may change. Nightwatch validates the RPC response shape and records the source/version. If it is unavailable, a recent local rollout `rate_limits` record can be used as a bounded structured fallback; stale or missing rollout data is rejected. Provider error reset time can schedule a conservative wait, but if no sufficiently fresh revalidation source exists, the run remains `WAIT_QUOTA`/`BLOCKED`; it never assumes the timer alone proves recovery.

## Security boundaries

Nightwatch never reads or writes `auth.json`, never prints environment variables, never enables `--dangerously-bypass-approvals-and-sandbox`, and never escalates privileges. It passes only a fixed allowlist of Codex arguments plus the user's goal. User-level systemd is optional and uses the foreground CLI.
