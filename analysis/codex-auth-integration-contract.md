# codex-auth integration contract

Audit date: 2026-09-02

## Audited upstream

- Repository: `https://github.com/Loongphy/codex-auth`
- Commit: `0fde29598c2e02e28e0e8bcc33a4bb8d45d7b23a`
- Version: `v0.3.0-alpha.11`
- Audit inputs: upstream `docs/json-api.md`, `docs/permissions.md`, `src/workflows/import.zig`, `src/registry/export.zig`, `src/registry/account_ops.zig`, and the JSON output implementation.

The installed host release remains `codex-auth 0.2.10`; it is not used as
proof of this contract. A compatible binary must be installed in an isolated
test location and selected with `NIGHTWATCH_CODEX_AUTH_BIN`.

## Machine-readable behavior Nightwatch relies on

- JSON commands emit exactly one JSON document plus a newline on stdout.
- Every JSON document has `schema_version: 1`; stderr is diagnostics only.
- Unknown object fields are forward-compatible and ignored by Nightwatch.
- Discovery uses `list --skip-api --json`; active reconciliation uses
  `list --active --skip-api --json` semantics.
- `account_key` is the stable identity. The display `number` is ephemeral and
  is never used for selection, switch, or removal.
- `switch <query> --json` returns an object in `switched_to`; Nightwatch
  requires its `account_key` to equal the requested stable key.
- `remove ... --json` returns `removed` as an array of account objects, not an
  array of strings. Every returned entry used by Nightwatch must be an object
  with a valid `account_key`, and the requested key must be present.
- Standard `export <directory>` writes managed `*.auth.json` snapshots for
  the registry accounts. Standard `import <path>` reads the source snapshots,
  mutates the whole registry, and saves it when entries are applied. Export
  and import use human reports rather than a JSON machine result; Nightwatch
  checks exit status and does not parse report text.

## Synchronization and authority implications

The upstream import workflow loads the complete registry, applies imported
records, and saves the complete registry. Upstream atomic replacement protects
file integrity but is not an inter-process lost-update protocol. Nightwatch
therefore serializes every canonical `list`, `switch`, `remove`, `export`, and
`import` operation with its short-lived kernel-backed canonical registry lock.

The account capsule adapter disables that canonical lock because its
`CODEX_HOME` is private to the capsule. The account lease is acquired before
canonical export/import, and the registry lock is released before provider or
App Server execution. Codex App Server remains the quota authority; codex-auth
usage fields and any remote usage API are not used for quota selection.

Upstream `syncActiveAccountFromAuth` and activation paths can preserve a
refreshed active auth snapshot while reconciling the registry. Nightwatch
therefore synchronizes only after the provider exits and imports only the
selected account snapshot back into the canonical registry.

## Two-account real acceptance verification (2026-09-03)

- Two distinct authentic accounts, Account A (`acct-4cb1604810cd`) and Account B (`acct-7ce14e017d7b`), verified in canonical registry without altering the active account identity (`acct-4cb1604810cd`).
- Official Codex runtime creates ephemeral helper symlinks in `$CODEX_HOME/tmp/arg0/...`; `AccountCapsule` safely removes runtime `tmp` before tree hardening and deletion, failing closed if `tmp` itself is symlinked.
- Explicit account selector resolution in `operations.py` updated to accept account fingerprints (`acct-...`) alongside stable account keys and aliases.
- Live App Server probes verified authoritative 5h and weekly quota windows for both accounts.
- Sequential capsule synchronization verified auth-refresh preservation without snapshot regression.
- PR #1 is ready for review with all real blockers resolved.
