# Reference source audit

审计日期：2026-08-27。六个仓库均来自用户指定的 GitHub 地址，按浅克隆检查了源码、测试、CI、依赖和 license；没有修改任何 reference repo。

## continuation-layer (`Hsi431/continuation-layer`, 7c0c0b1)

- Entrypoint: `bin/continuity.mjs`; provider process: `src/supervisor/process-runner.mjs`; Codex adapter: `src/providers/codex.mjs`.
- Uses `child_process.spawn` for non-interactive work and optional `node-pty` for interactive TUI work.
- Stores `current_session_id` in `.agent/state.json`, but its normal non-interactive command is not forced to `--json`; extraction falls back to regexes for `session_id`/`conversation_id`.
- Has useful cooldown parsing (`resets_at`, absolute ISO, relative durations), usage-window and conservative fallback anchors, and a real watchdog loop in `watchManagedSession`.
- Durable files are plain writes and append writes without a cross-process lock or atomic replace. Git status/diff recovery checks are present. Tests cover cooldown watchdog, stale handoff, missing IDs, max resumes, and abort.
- Regex/TUI scraping is a fallback/interactive path, not a structured Codex protocol. It does not use App Server rate-limit RPC.
- License: Apache-2.0. We reuse no source code.

## unsnooze (`saaranshM/unsnooze`, 8fdf34f)

- Entrypoints: `bin/unsnooze.js`, `src/launcher.js`, `src/monitor.js`, `src/resumer.js`.
- Broad agent/multiplexer product: tmux, zellij, herdr, headless; launcher and monitor use child processes and pane capture/injection rather than an exact non-interactive Codex turn.
- Strong process/pane ownership: leases, PID birth times, pane stamps, singleton lock, compare-and-set stop episodes, stale-lock handling, bounded retry backoff, and persisted state.
- Codex session discovery prefers `~/.codex/state_5.sqlite` and rollout metadata, with `session_index.jsonl` fallback. The adapter still has a `--last` fallback for cases where identity is absent, which Nightwatch rejects on automatic paths.
- Git workspace fingerprints and `workspaceHold` help prevent unsafe injection. Quota parsing is primarily transcript/TUI-specific; the product does not use Codex App Server rate-limit RPC as its core source.
- Extensive Node tests and CI. Dependency surface is large for Nightwatch v1. License: MIT. We reuse no source code.

## codex-auto (`daguanren21/codex-auto`, 2e50fae)

- Core paths: `packages/core/src/codex/rollout.ts`, `codex/sessions.ts`, `resume/scheduler.ts`, `resume/runner.ts`, `resume/state.ts`.
- Stream-parses Codex rollout JSONL and recognizes `session_meta.session_id`, `cwd`, token usage, and `rate_limits.primary/secondary` with `used_percent`, `window_minutes`, and `resets_at`.
- Uses exact session IDs for resume candidates and atomic JSON replacement; a watcher lock prevents two schedulers, while pending/triggered/expired jobs suppress duplicate scheduled resumes.
- Git probes are bounded and persisted state survives restart, but the public repository is a multi-package Node 22 project with no root license file and its resume trigger remains an external runtime dispatch.
- No TUI screen scraping or PTY requirement in the core path. Tests cover rollout parsing, scheduler, and state behavior.
- We used CodeGraph in an independent `/tmp` clone to trace the relevant symbols; no code was copied.

## tmux-codex-auto-continue (`yeahdongcn/tmux-codex-auto-continue`, 4fe4c06)

- Entrypoint: `bin/tmux-codex-auto-continue`; integration harness: `tests/worked_integration.py`.
- Linux `/proc` and tmux foreground-process verification, exact Codex glyph/layout matching, visible-pane revalidation, composer safety checks, bracketed paste plus real Enter, and a strict fail-closed policy are its strongest ideas.
- It recovers an existing interactive pane by input injection; it does not own a durable exact thread ID, structured event stream, milestone authority, or Git-based task recovery.
- Handles transient 429/overload, disconnection, safety notices, process identity, pane leases, and daemon restart with substantial integration coverage. Depends on tmux and English UI layout.
- License: MIT. We reuse no source code.

## codex-limit (`kajiwara321/codex-limit`, 3ced828)

- Entrypoint: `index.js`/`cli.js`.
- Minimal, zero-background-CPU App Server client: spawn `codex app-server`, send JSON-RPC `initialize`, then `account/rateLimits/read`, return `rateLimits`.
- Structured output exposes primary/secondary windows, percentage, duration, reset epoch, and plan type. This is the decisive quota design for Nightwatch.
- No task state, exact-thread launch, recovery loop, locking, tests, or Git checks. It assumes the experimental App Server protocol remains compatible.
- License: MIT. We reimplemented the protocol client with bounded timeouts and stricter validation rather than copying it.

## agent-resume (`megamen32/agent-resume`, d40274c)

- Entrypoint: `agent_resume.py`; Codex-specific path reads MCP `_meta.threadId`, or read-only `state_5.sqlite`/rollout metadata for discovery.
- `build_resume_command` requires an explicit Codex session ID and rejects `use_last`; command construction is argv-based and preserves a frozen model where available.
- Supports durable background job records and safe process launch, but is an MCP helper for several agents, not a Git/task supervisor. It has no milestone verification or quota source of authority.
- Tests cover client installation, waiting, and command construction; paid Codex smoke is opt-in.
- License: MIT. We reuse no source code.

## Current Codex observations

- `codex-cli 0.150.1` supports `codex exec --json`, `codex exec resume <SESSION_ID> --json`, and the experimental `codex app-server` stdio transport.
- Local `~/.codex/state_5.sqlite` has a `threads` table with `id`, `rollout_path`, `cwd`, `updated_at_ms`, `model`, `git_sha`, and `git_branch`.
- Current `codex doctor --json` is redacted and safe to call; it reports authentication/config/runtime checks without exposing tokens. Network reachability is currently restricted in this environment, so Nightwatch's real doctor used the bounded `rollout_jsonl` structured fallback after the App Server read failed.
