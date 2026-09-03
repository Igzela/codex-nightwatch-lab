# Google Antigravity (AGY) CLI Integration Contract

**Date:** 2026-09-03
**Status:** AUDITED & VERIFIED

---

## 1. Executable Identity & Environment Probe

- **Executable Binary:** `/home/charlie/.local/bin/agy`
- **Binary Type:** ELF 64-bit LSB pie executable, x86-64, dynamically linked
- **CLI Version:** `1.1.25` (`agy --version` outputs `1.1.25`)
- **State / Config Directory:** `~/.gemini/antigravity-cli`
  - Auth token: `~/.gemini/antigravity-cli/antigravity-oauth-token` (JSON format with `auth_method` and `token`)
  - Conversations DB: `~/.gemini/antigravity-cli/conversations/<conversation-id>.db` (SQLite)
  - Logs: `~/.gemini/antigravity-cli/log/` and `~/.gemini/antigravity-cli/cli.log`

---

## 2. CLI Invocation Flags & Execution Contract

### Non-Interactive Print Mode
- Non-interactive prompt execution:
  `-p "<PROMPT>"` or `--print "<PROMPT>"`
- Automatic permission approval:
  `--dangerously-skip-permissions` (auto-approves all tool permission requests without interactive prompts)
- Execution timeout:
  `--print-timeout <duration>` (default `5m0s`)
- Model selection:
  `--model <model-name>`
- Reasoning effort:
  `--effort <low|medium|high>`

### Output Format (`--output-format stream-json`)
When `--output-format stream-json` is supplied, `agy` emits line-delimited NDJSON events to stdout:
1. `init` event:
   ```json
   {
     "event": "init",
     "conversation_id": "<uuid>",
     "init": {
       "model": "<model-name>",
       "cwd": "<working-directory>",
       "tools": [...]
     }
   }
   ```
2. `step_update` event:
   ```json
   {
     "event": "step_update",
     "step_update": {
       "conversation_id": "<uuid>",
       "step_index": <int>,
       "state": "ACTIVE" | "DONE",
       "step_type": "user_input" | "agent_response" | "system_message",
       "text_delta": "<text>",
       "usage": {
         "input_tokens": <int>,
         "output_tokens": <int>,
         "thinking_tokens": <int>,
         "total_tokens": <int>
       }
     }
   }
   ```
3. `result` event:
   ```json
   {
     "event": "result",
     "result": {
       "conversation_id": "<uuid>",
       "status": "SUCCESS" | "ERROR",
       "response": "<final-text>",
       "duration_seconds": <float>,
       "num_turns": <int>,
       "usage": { ... }
     }
   }
   ```

---

## 3. Exact Conversation Resume vs Heuristic Resume

- **Deterministic Exact Resume:**
  `--conversation <EXACT_CONVERSATION_ID>`
  Resumes the exact conversation matching the UUID.
  Verified with two-turn sequential invocation: the agent retains full memory of prior turns.
- **Fail-Closed Boundary:**
  If `--conversation <ID>` is given a nonexistent ID:
  - `agy` prints `warning: conversation "<ID>" not found` to stderr.
  - It does *not* crash; instead it creates a *new* conversation with a newly generated UUID on the `init` event.
  - **Nightwatch Rule:** Nightwatch MUST verify that the `init` event's `conversation_id` exactly matches the requested `thread_id`. If `conversation_id != requested_id` (or if stderr indicates not found), Nightwatch MUST immediately abort and fail closed.
- **Prohibited Flags:**
  `-c` / `--continue` without explicit ID is heuristic ("most recent conversation") and is strictly forbidden.

---

## 4. Quota Authority & Usage Probing

- Slash command probe:
  `agy --output-format stream-json -p "/usage"`
- Returns an immediate structured `command_result` event with zero token burn (`duration_seconds: 0`, `input_tokens: 0`, `output_tokens: 0`):
  ```json
  {
    "event": "command_result",
    "command": {
      "name": "usage",
      "data": {
        "groups": [
          {
            "name": "Gemini Models",
            "buckets": [
              {
                "id": "gemini-weekly",
                "name": "Weekly Limit Remaining",
                "window": "weekly",
                "remaining_fraction": 0.63,
                "reset_time": "2026-09-06T00:35:03Z"
              },
              {
                "id": "gemini-5h",
                "name": "Five Hour Limit Remaining",
                "window": "5h",
                "remaining_fraction": 0.47,
                "reset_time": "2026-09-03T13:51:53Z"
              }
            ]
          },
          {
            "name": "Claude and GPT models",
            "buckets": [
              {
                "id": "3p-weekly",
                "window": "weekly",
                "remaining_fraction": 0.96,
                "reset_time": "2026-09-06T04:46:25Z"
              },
              {
                "id": "3p-5h",
                "window": "5h",
                "remaining_fraction": 1.0,
                "reset_time": "2026-09-03T17:25:23Z"
              }
            ]
          }
        ]
      }
    }
  }
  ```
- **Windows Tracked:**
  - `5h`: Five-hour sliding demand window.
  - `weekly`: Weekly tier quota.
- **Reset Times:** Full ISO-8601 UTC timestamps (e.g. `2026-09-03T13:51:53Z`).

---

## 5. Model Catalog

Live model listing via `agy models`:
- `gemini-3.8-flash-high`
- `gemini-3.8-flash-medium`
- `gemini-3.8-flash-low`
- `gemini-3.7-flash-high`
- `gemini-3.7-flash-medium`
- `gemini-3.7-flash-low`
- `gemini-3.6-flash-high`
- `gemini-3.6-flash-medium`
- `gemini-3.6-flash-low`
- `gemini-3.1-pro-high`
- `gemini-3.1-pro-low`
- `claude-sonnet-4-6`
- `claude-opus-4-6-thinking`
- `gpt-oss-120b-medium`

---

## 6. Account & Pool Capabilities

- AGY CLI does not support multi-account CLI switching (`--account` flag not present).
- Single account per active user session / environment (`CURRENT_ONLY`).
- Auto-pool mode is rejected / unsupported for AGY provider; Nightwatch gracefully runs AGY in `CURRENT_ONLY` mode.
