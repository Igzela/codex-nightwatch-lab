# 🌙 Nightwatch

<div align="center">

**A fail-closed, quota-aware, exact-thread Linux supervisor for OpenAI Codex CLI.**  
*Zero external dependencies • Direct App Server JSON-RPC • Frozen verification gates • Native systemd integration*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](#installation)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-orange.svg)](#system-requirements)
[![Tests: 168 Passing](https://img.shields.io/badge/Tests-168%20Passing-success.svg)](#validation)
[![Codex: 0.152.1+](https://img.shields.io/badge/OpenAI%20Codex-0.152.1%2B-purple.svg)](https://github.com/openai/codex)

[**English**](README.md) | [**中文说明**](README_CN.md)

</div>

---

## ⚡ The "3 AM Problem"

You give your AI coding agent a complex milestone before going to sleep. You wake up expecting a finished feature and green tests. Instead:
- ❌ **Quota Exhaustion**: 25 minutes in, Codex hit the 5-hour usage limit and halted.
- ❌ **Terminal / SSH Disconnect**: Your laptop went to sleep or the SSH session dropped, killing the agent.
- ❌ **Hallucinated "Done"**: The model claimed *"Everything is complete!"* in chat without ever passing tests.
- ❌ **Context Amnesia**: Restarting starts a *new* session, discarding millions of tokens and corrupting task state.

**Nightwatch solves this entirely.**

```text
               ┌────────────────────────────────────────────────────────┐
               │                  Nightwatch Supervisor                 │
               │  (Trusted Control Plane outside workspace: ~/.local/)  │
               └───────────┬────────────────────────────────┬───────────┘
                           │                                │
        [1] Spawns & Supervises              [2] Live JSON-RPC stdio
        `codex exec --json`                  `account/rateLimits/read`
                           │                                │
                           ▼                                ▼
               ┌───────────────────────┐        ┌───────────────────────┐
               │    OpenAI Codex CLI   │        │   Official App Server │
               │  (Workspace Sandbox)  │        │   (Quota Authority)   │
               └───────────┬───────────┘        └───────────┬───────────┘
                           │                                │
                           │ [3] Hits 5h/Weekly Limit       │ [4] Window Resets
                           ▼                                ▼
               ┌────────────────────────────────────────────────────────┐
               │               Automatic Quota Revalidation             │
               │        Re-probes account pool at reset boundary         │
               │        Runs frozen user-defined verify commands        │
               └────────────────────────────────────────────────────────┘
```

---

## ✨ Key Features

- 🔄 **Authoritative Quota Recovery**: Communicates directly with the official Codex App Server over JSON-RPC 2.0 stdio (`account/rateLimits/read`). No brittle regex scraping or ANSI parsing.
- 🧵 **Exact-Thread Continuation**: Resumes the *exact* session thread (`codex exec --json resume <thread_id> -`) across quota windows and crashes when that route is proven. Never uses `--last` guesswork.
- 👥 **Opt-in Account Pool**: An explicitly selected pool can rotate only after a provider exits. Each account is leased globally, checked by a fresh App Server quota session, and evaluated against both 5h and weekly limits.
- 🔐 **Serialized Canonical Auth Sync**: Canonical `codex-auth` registry operations use a short-lived kernel lock acquired after the account lease and released before provider execution. Provider capsules retain only the selected account; all-account staging is removed before launch.
- 🔒 **Tamper-Proof Trust Boundary**: State lives outside the Git workspace in `~/.local/state/codex-nightwatch/` (0700 permissions). Model-proposed verification scripts are strictly rejected.
- 🧪 **Frozen Verification Gate**: Goal completion (`DONE`) strictly requires your frozen `--verify` commands (e.g. `pytest`, `cargo test`, `git diff --check`) to exit `0`.
- 🛡️ **Zero-Interruption Live Watching (`nightwatch watch`)**: Passively monitors existing interactive terminal sessions without collision or interruption. With `--auto-takeover`, seamlessly takes over overnight when quota runs out.
- 🔋 **Linux-Native Resilience**: Zero external Python packages (standard library only). Built-in `systemd-inhibit` sleep lock and `systemctl --user` daemonization.

---

## 🚀 Quick Start

### 1-Line Install

```bash
curl -fsSL https://raw.githubusercontent.com/Igzela/codex-nightwatch-lab/master/install.sh | bash
```

*Or install manually:*

```bash
git clone https://github.com/Igzela/codex-nightwatch-lab.git ~/.local/share/codex-nightwatch
~/.local/share/codex-nightwatch/nightwatch/bin/nightwatch install
```

### Verify Environment

```bash
nightwatch doctor
```
```text
Nightwatch doctor: ok
Codex: codex-cli 0.152.1
Auth: ok
Quota authority: LIVE_APP_SERVER (live_app_server)
5h: 7.0% used, reset=1787866896
weekly: 1.0% used, reset=1788453696
systemd-inhibit: available
```

---

## 📖 Usage Modes

### Interactive TUI (Recommended)

Run Nightwatch without a subcommand inside a terminal:

```bash
cd /path/to/my-project
nightwatch
```

Natural language starts a guided run preview when no active run is selected. With an active run selected it becomes a confirmed steer request to that exact thread. Nothing mutating is sent before the preview is confirmed.

```text
Nightwatch 0.4.0 · MULTI-THREAD CONTROL
Runs 2 · ↑/↓ select · / commands · Esc quit

▶ RUNNING             payments-retry         01a050ac-1149…
    ███████████░░░░░░░ 61%  gpt-5.6-luna · high  quota 5h 52% · week 8%
  WAIT_QUOTA          inventory-import       01a050bd-82ae…
    ███████░░░░░░░░░░░ 38%  gpt-5.6-luna · medium

Thread     01a050ac-1149… · generation 2
Agent      RUNNING · PID 18234 · resume
Next       continue current milestone
Source: trusted state + sequence-validated events

Input › natural language starts a goal (or steers an active run); / opens command palette
```

Typing `/` opens the described command palette. Important views include `/status`, `/plan`, `/timeline`, `/explain`, `/thread`, `/quota`, `/logs`, `/recap`, and `/report`. Mutating commands such as `/run`, `/adopt`, `/steer`, `/resume`, and `/stop` show a confirmation preview first. `/adopt` lists active sessions whose PID, rollout, repository and exact thread can be proven; manual thread entry remains the explicit fallback.

`/multi` watches every trusted run under the control-plane state root. Concurrent writing agents may use different repositories or isolated Git worktrees. If the selected repository already has a run, the `/run` wizard creates the confirmed worktree under `.worktrees/<repo>/<label>` and a repo-specific systemd user unit. Nightwatch never permits two supervised writers in one working directory.

At `DONE`, `BLOCKED`, `FAILED`, `STOPPED`, or `AWAITING_ACCEPTANCE`, the TUI rings the terminal bell and shows the exact terminal state. `/recap` gives a short evidence-grounded summary; `/report` writes the durable report with model, thread, generations, milestones, checks, quota and trusted timeline. Model narrative is explicitly excluded from trusted facts.

### Choose a Codex Model and Reasoning Level

Nightwatch reads the catalog from the installed Codex CLI, so it follows the models and levels available on this machine instead of freezing a stale list:

```bash
nightwatch models
nightwatch models --json
```

Select both values when creating or adopting a run. They are stored in trusted durable state and reused for every exact-thread continuation:

```bash
nightwatch run \
  --model gpt-5.6-luna \
  --reasoning-effort high \
  --verify "pytest -q" \
  "Implement the feature and pass the test suite"
```

If either option is omitted, Codex's configured default remains authoritative. The installed Codex CLI performs the final model/level compatibility check.

### Optional Account Pool

Runs default to `CURRENT_ONLY`; they never discover or enroll every stored account. With the separately installed `codex-auth` capability, explicitly select a subset:

```bash
nightwatch run \
  --account-mode auto-pool \
  --account personal \
  --account backup \
  --verify "pytest -q" \
  "Implement the feature and pass the tests"
```

Nightwatch uses `codex-auth list --skip-api --json` only for stable account discovery and `switch <account_key> --json` inside an external 0700 capsule. It never uses codex-auth's remote usage API. Actual selection comes from a fresh official Codex App Server `account/rateLimits/read` response. An account is usable only when both 5h and weekly windows are known and not exhausted; the deterministic policy maximizes the smaller remaining capacity, then the 5h/weekly remainder, reset time, and fingerprint.

Before each App Server probe or provider turn, a global external lease prevents another Nightwatch run from using the same account. The lease is held through the child process and released only after that process exits and refreshed auth state is synchronized. If all selected accounts are unavailable, the run enters `WAIT_QUOTA`, sleeps to the earliest relevant reset, and re-probes the whole pool.

Cross-account exact-thread portability is not assumed. Until a safe experiment proves it for the installed Codex version, AUTO_POOL uses `CONTROLLED_THREAD_HANDOFF`: a new provider conversation receives a trusted packet containing the goal, frozen verification policy, repository/Git HEAD, milestone state, and prior thread for audit. The mission continues, but the new conversation is not reported as the old exact thread. Missing or incompatible codex-auth disables AUTO_POOL while preserving CURRENT_ONLY.

Normal AUTO_POOL quota exhaustion is an informational `quota_cycles` count and is not limited by the defensive recovery budget. `recovery_failures` records bounded abnormal recovery failures. The real upstream `codex-auth` contract was audited and exercised with isolated `v0.3.0-alpha.11` at commit `0fde29598c2e02e28e0e8bcc33a4bb8d45d7b23a`; the installed host binary is left unchanged. The current live acceptance discovered three stored accounts, but only one of the two tested non-active account snapshots returned live App Server quota, so two-account production acceptance remains pending. Exact-thread portability across accounts is therefore `INCONCLUSIVE`, and controlled handoff remains the safe behavior.

### Mode A: Unattended Overnight Run (Full Autonomous Supervisor)

Launch a goal with explicit verification gates:

```bash
cd /path/to/my-project
nightwatch run \
  --model gpt-5.6-luna \
  --reasoning-effort high \
  --verify "pytest -q" \
  --verify "git diff --check" \
  "Implement payment webhook retry handler and pass all tests"
```

To run as a persistent user-level **systemd service** (survives terminal closure & logout):

```bash
nightwatch run --service \
  --verify "cargo test" \
  --verify "git diff --check" \
  "Refactor engine storage interface"
```

### Mode B: Passive Watching with Auto-Takeover (Interactive to Overnight)

Already running an interactive Codex session in your terminal? Passively monitor it without interfering:

```bash
# Snapshot current telemetry (fails closed if multiple sessions exist unless --thread is given)
nightwatch watch --once

# Continuous live telemetry (specify --thread if multiple sessions exist)
nightwatch watch [--thread <ID>]

# Automatic takeover: waits for the interactive process to exit, then takes over exact thread
nightwatch watch --auto-takeover --verify "pytest -q" [--thread <ID>]
```

> **Auto-Takeover Semantics**: When quota exhausts (`used_percent >= 100%`), Nightwatch marks status as `TAKEOVER_PENDING` and continues passive observation. It **strictly waits for the original interactive process to exit** before starting the trusted supervisor, ensuring no concurrent process collision or corrupted workspace states.

```text
============================================================
REPO         /home/user/projects/my-app
THREAD ID    01a04416-c7aa-7271-9ede-7fe2d40cf950
PROCESS      PID 466574 (ALIVE)
MODEL        gpt-5.6-luna [branch: main]
QUOTA 5H     7.0% used, reset=1787866896
QUOTA WEEKLY 1.0% used
TOKENS       total=15,947,329, input=15,882,347, output=64,982
SUBAGENTS    Copernicus (01a0442b...), Kepler (01a0442b...)
============================================================
```

### Mode C: Adopt an Existing Thread

Bind an existing conversation directly into Nightwatch's trusted control plane:

```bash
nightwatch adopt --thread 01a04416-c7aa-7271-9ede-7fe2d40cf950 \
  --model gpt-5.6-luna --reasoning-effort high --verify "pytest"
nightwatch resume
```

### Interaction and Live Progress

`nightwatch run` is deliberately unattended: it starts `codex exec --json`, sends the goal over stdin, and supervises that one exact thread. It is not a chat UI. Interact with the control plane from another terminal:

```bash
nightwatch status                 # one durable snapshot
nightwatch status --watch         # live agent state, progress, and milestones
nightwatch status --json          # machine-readable snapshot
nightwatch log --tail 100         # supervisor audit trail
nightwatch report                 # acceptance report
nightwatch stop                   # stop safely; preserve state and thread
nightwatch resume                 # continue that exact thread
```

The live status distinguishes the supervisor from the Codex child (`AGENT RUNNING`, PID and start/resume action), shows the selected model and reasoning level, displays trusted implemented/verified milestone progress, and exits automatically at a terminal state. Workspace mailbox progress is untrusted input until Nightwatch validates and incorporates it into the durable plan.

For a normal interactive Codex chat, keep using Codex directly and run `nightwatch watch` in another terminal. `watch --auto-takeover` can hand that exact thread to unattended supervision after the interactive process exits.

The TUI is an adapter over the same durable interfaces. Every operation remains available through explicit CLI commands for scripts and recovery; the UI does not maintain a second hidden state.

---

## 🛠️ CLI Reference

| Command | Description |
| :--- | :--- |
| `nightwatch` / `nightwatch ui` | Open the interactive multi-thread dashboard and `/` command palette |
| `nightwatch models [--json]` | Show the installed Codex model catalog and supported reasoning levels |
| `nightwatch run "<goal>" [--model <slug>] [--reasoning-effort <level>] [--verify <cmd>] [--service]` | Initialize and run a new supervised goal (defaults to `CURRENT_ONLY`) |
| `nightwatch run "<goal>" --account-mode auto-pool --account <key-or-alias> [--account <key-or-alias> ...]` | Run with an explicitly authorized account subset |
| `nightwatch watch [--thread <id>] [--auto-takeover] [--once] [--json]` | Passively monitor active interactive Codex sessions; model options apply to takeover |
| `nightwatch adopt --thread <id> [--model <slug>] [--reasoning-effort <level>] [--verify <cmd>]` | Adopt an existing thread into Nightwatch |
| `nightwatch resume` | Resume the current repo's exact-thread goal |
| `nightwatch status [--watch] [--interval <seconds>] [--json]` | Show or continuously watch agent state, account-pool state, quota and trusted milestone progress |
| `nightwatch log [--tail N]` | Show human-readable supervisor audit log |
| `nightwatch report` | Output/generate the structured acceptance report |
| `nightwatch stop` | Gracefully halt automatic supervision (state preserved) |
| `nightwatch doctor` | Check Linux, Codex CLI, auth, quota authority & systemd |
| `nightwatch test app-server` | Test live JSON-RPC connection to Codex App Server |

---

## ⚖️ Architecture Comparison

| Feature | Raw Codex CLI | Tmux / PTY Scripts | **Nightwatch** |
| :--- | :---: | :---: | :---: |
| **Quota Recovery** | ❌ Manual retry | ⚠️ Regex screen scrape | **✅ Native JSON-RPC App Server stdio** |
| **Session Memory** | ❌ None on retry | ⚠️ `--last` heuristic | **✅ Exact durable `thread_id`** |
| **Verification Gate** | ❌ Model self-reports | ❌ None | **✅ User-frozen commands only** |
| **Security Sandbox** | ⚠️ Model can edit tests | ❌ Control inside repo | **✅ External 0700 trusted control plane** |
| **Process Model** | ❌ Foreground only | ⚠️ PTY injection | **✅ systemd user service + sleep inhibit** |
| **External Dependencies** | — | Node / pnpm / Tmux | **✅ Zero (Python Standard Library)** |

---

## 🔒 Trust Boundary & Security

Authoritative state is strictly isolated outside the Git workspace:

```text
~/.local/state/codex-nightwatch/<repo-name>-<repo-hash>/
├── state.json                 # Durable state machine (generation, thread_id, status)
├── verification-policy.json   # Hash-bound user verification commands
├── acceptance.json            # Acceptance criteria & goal binding
├── events.jsonl               # Append-only sequence-validated audit trail
├── supervisor.lock            # Process lifetime lease (PID reuse safe)
├── account-leases/            # Global per-account lifetime locks (0700/0600)
├── account-capsules/          # Ephemeral external CODEX_HOME capsules
└── runs/                      # Per-generation sanitized stdout & stderr logs
```

The repository workspace contains only the untrusted agent mailbox (`.nightwatch-agent/`), preventing untrusted LLM outputs from tampering with acceptance authority. Account credentials and refreshable auth files never enter the repository; capsule cleanup is deferred if synchronization cannot be proven safe.

---

## 🧪 Validation

Nightwatch comes with a comprehensive, hardened automated test suite:

```bash
python3 -m unittest discover -s nightwatch/tests -v
```
```text
Ran 168 tests
OK
```

- ✅ Codex App Server rate-limit RPC handshake validation
- ✅ Real process collision and race-condition prevention
- ✅ Real Linux PID identity & state integrity under SIGKILL crash restarts
- ✅ Symlink escape and mailbox injection attack prevention

---

## 📄 License

[MIT License](LICENSE) © 2026 Igzela
