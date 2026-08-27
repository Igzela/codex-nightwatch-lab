# Architecture decision — schema 2 trusted control plane

## Decision

Nightwatch is a Python standard-library Linux supervisor for one exact Codex
thread per canonical Git repository. Its trusted control plane lives outside
the workspace in an XDG user-state directory keyed by canonical path plus Git
origin identity. Codex receives workspace-write access only to the repository,
where `.nightwatch-agent/` is an explicitly untrusted mailbox.

Schema-1 `repo/.nightwatch` state is forensic legacy data, never a source of
truth. The v2 store validates state/repo identity, frozen policy binding,
permissions, atomic writes, event sequence monotonicity, and symlink safety.

## Acceptance and verification

The user provides `--verify COMMAND` at `run` time. Commands are copied and
hashed into trusted state; `/bin/sh -lc` is used only for these explicit user
capabilities and receives a restricted environment. Codex can propose
milestone IDs/titles/weights and report implementation progress, but any
verification command, trusted-policy field, invalid mailbox shape, or symlink
is rejected.

Each accepted milestone maps to the frozen default profile. A no-policy or
diff-only policy cannot produce DONE for a natural-language goal:
`AWAITING_ACCEPTANCE` is the honest terminal state.

## Quota protocol

`app_server.py` owns JSON-RPC lifecycle: start stdio server, send `initialize`,
wait for that exact response, send `initialized`, issue parameterless
`account/rateLimits/read`, then wait for its exact response while ignoring
notifications/wrong IDs. Live App Server data is recovery authority.

Rollout JSONL is only schedule evidence. At reset, an unavailable live source
permits one claimed exact-thread availability probe per quota generation; a
second quota event creates a new generation. It never blind-loops resumes.

## Recovery

`supervisor.lock` covers the entire Supervisor lifecycle. Active child identity
uses PID + `/proc/<pid>/stat` starttime + executable, not PID liveness alone.
Claim phase `claimed` is proven-not-sent; `spawn_prepared`/`spawned` after a
crash is ambiguous and blocks until an explicit audited human acknowledgement.

The systemd unit uses `Restart=on-failure` plus `RestartPreventExitStatus=10
11 12`, where those exits mean BLOCKED, STOPPED, and expected provider/auth
failure. Unexpected code 20 is restarted.
