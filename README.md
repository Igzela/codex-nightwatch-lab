# 🌙 Nightwatch

<div align="center">

**A fail-closed, quota-aware, exact-thread Linux supervisor for OpenAI Codex CLI.**  
*Zero external dependencies • Direct App Server JSON-RPC • Frozen verification gates • Native systemd integration*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](#installation)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-orange.svg)](#system-requirements)
[![Tests: 64 Passing](https://img.shields.io/badge/Tests-64%20Passing-success.svg)](#validation)
[![Codex: 0.150.1+](https://img.shields.io/badge/OpenAI%20Codex-0.150.1%2B-purple.svg)](https://github.com/openai/codex)

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
               │       Resumes exact same thread on reset window        │
               │        Runs frozen user-defined verify commands        │
               └────────────────────────────────────────────────────────┘
```

---

## ✨ Key Features

- 🔄 **Authoritative Quota Recovery**: Communicates directly with the official Codex App Server over JSON-RPC 2.0 stdio (`account/rateLimits/read`). No brittle regex scraping or ANSI parsing.
- 🧵 **Exact-Thread Continuation**: Resumes the *exact* session thread (`codex exec --json resume <thread_id> -`) across quota windows and crashes. Never uses `--last` guesswork.
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
Codex: codex-cli 0.150.1
Auth: ok
Quota authority: LIVE_APP_SERVER (live_app_server)
5h: 7.0% used, reset=1787866896
weekly: 1.0% used, reset=1788453696
systemd-inhibit: available
```

---

## 📖 Usage Modes

### Mode A: Unattended Overnight Run (Full Autonomous Supervisor)

Launch a goal with explicit verification gates:

```bash
cd /path/to/my-project
nightwatch run \
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
# Snapshot current telemetry
nightwatch watch --once

# Continuous live telemetry
nightwatch watch

# Automatic takeover when quota exhausts or terminal closes
nightwatch watch --auto-takeover --verify "pytest -q"
```

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
nightwatch adopt --thread 01a04416-c7aa-7271-9ede-7fe2d40cf950 --verify "pytest"
nightwatch resume
```

---

## 🛠️ CLI Reference

| Command | Description |
| :--- | :--- |
| `nightwatch run "<goal>" [--verify <cmd>] [--service]` | Initialize and run a new supervised goal |
| `nightwatch watch [--auto-takeover] [--once] [--json]` | Passively monitor active Codex sessions in current repo |
| `nightwatch adopt --thread <id> [--verify <cmd>]` | Adopt an existing thread into Nightwatch |
| `nightwatch resume` | Resume the current repo's exact-thread goal |
| `nightwatch status [--json]` | Show durable task state, quota status & milestone progress |
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
└── runs/                      # Per-generation sanitized stdout & stderr logs
```

The repository workspace contains only the untrusted agent mailbox (`.nightwatch-agent/`), preventing untrusted LLM outputs from tampering with acceptance authority.

---

## 🧪 Validation

Nightwatch comes with a comprehensive, hardened automated test suite:

```bash
python3 -m unittest discover -s nightwatch/tests -v
```
```text
Ran 64 tests in 3.563s
OK
```

- ✅ Real Codex 0.150.1 App Server rate-limit RPC handshake validation
- ✅ Real process collision and race-condition prevention
- ✅ Real Linux PID identity & state integrity under SIGKILL crash restarts
- ✅ Symlink escape and mailbox injection attack prevention

---

## 📄 License

[MIT License](LICENSE) © 2026 Igzela
