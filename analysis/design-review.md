# Design review

## What is worth adopting

1. `continuation-layer`: a live watchdog loop is required; a recorded reset time followed by process exit is not automation. Its reset provenance ladder and Git-aware recovery check are useful.
2. `unsnooze`: atomic state replacement, a real single-writer lock, PID/process ownership, bounded retries, and compare-and-set episode logic are good patterns for a long-lived local supervisor.
3. `codex-auto`: rollout JSONL is a structured local source. `session_meta.session_id`, `cwd`, token counts, and `rate_limits` are more reliable than terminal text. Its job reconciliation is a useful model for idempotence.
4. `tmux-codex-auto-continue`: strict process identity and visible-state revalidation demonstrate why arbitrary pane text must never be the normal automation path.
5. `codex-limit`: the smallest useful App Server flow is `initialize` followed by `account/rateLimits/read`; it avoids scraping and provides exact reset epochs.
6. `agent-resume`: a normal resume must carry an explicit ID as argv and reject `--last`; Codex's current thread identity can come from request metadata or the local rollout/SQLite records.

## What is fragile or rejected

- TUI layout/English regexes are rejected as Nightwatch's normal path. They remain a diagnostic fallback only and can never authorize a resume.
- `--last` is rejected for all automatic recovery. It can point to an unrelated user session after a crash or concurrent Codex use.
- A large multiplexer abstraction is rejected: Nightwatch owns one non-interactive `codex exec` process in one Git repo, so tmux is unnecessary for correctness.
- Model-reported “done” is rejected as acceptance evidence. Only Nightwatch's milestone plan, command verification, Git policy, and final verification can produce `DONE`.
- Unlocked plain state writes are rejected. Every state mutation is locked, validated, atomically replaced, and paired with an append-only event.
- Infinite retry is rejected. Auth/state-integrity/unknown errors stop; transient retries have a limit and backoff; quota waits are bounded by revalidation and circuit breakers.
- Copying code from the references is rejected. Each implementation has a narrow, compatible design but different assumptions and licenses; Nightwatch is a clean Python stdlib implementation.

## Answers to the ten design questions

1. Best reusable designs: structured rollout parsing, App Server quota RPC, exact argv resume, durable watchdog, atomic state/lease, Git reality checks, strict fail-closed input handling.
2. Weak designs: UI regex as authority, latest-session guessing, unlocked files, model text as state, endless retry, and multi-agent/multiplexer breadth for this v1.
3. TUI/regex dependence is concentrated in `continuation-layer` interactive modules, `unsnooze` monitor/patterns, and `tmux-codex-auto-continue`.
4. Structured Codex state is available in `codex-auto` rollout JSONL and local `state_5.sqlite`; quota is structured through the experimental App Server RPC.
5. Most reliable thread ID source: the current launch's JSONL `thread.started` event, cross-checked against the new rollout `session_meta` and optional SQLite `threads` row. A pre-existing “latest” row is never used to start a new goal.
6. Most reliable quota/reset source: a fresh App Server `account/rateLimits/read`; rollout `event_msg/token_count.rate_limits` is second; provider error payloads/relative durations are later fallbacks with provenance.
7. Community race handling: singleton locks, atomic `wx`/rename, PID birth and pane leases, CAS stop episodes, pending-job reconciliation, and dedupe keys.
8. Worth porting conceptually: reset provenance, strict identity, atomic persistence, bounded backoff, and reconciliation. No source file is ported verbatim.
9. Design-only references: all six repos' broad wrappers and screen-scraping paths; they do not match Nightwatch's exact-thread task authority.
10. Licenses: continuation-layer Apache-2.0; unsnooze, tmux-codex-auto-continue, codex-limit, and agent-resume MIT; codex-auto has no root license file in the audited checkout. No reference code is copied, so there is no third-party code attribution requirement.
