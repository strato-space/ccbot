# Monitor State Schema Strategy

This note records the explicit `T42` decision for `monitor_state.json`.

## Decision

`monitor_state.json` keeps the `compatibility envelope`.

- `monitor_state.json` keeps legacy tracked-session keys
- `session_id` and `file_path` remain the persisted transport fields
- `thread_id` and `replay_path` remain code/API aliases only

The file still uses a versioned top-level envelope:

- `schema_version`
- `runtime_kind`
- `tracked_sessions`

The compatibility decision applies only to the nested tracked-session payloads.

## Why This Decision Won

`T41` showed that the nested tracked-session keys are coupled to:

- monitor-state persistence and reload logic
- session-monitor restart recovery
- migration tests and integration tests
- fixture corpora used by Codex and cross-runtime contract tests

Changing the nested keys now would buy cleaner on-disk naming, but it would
also enlarge the cutover surface without unlocking new runtime capability.

## Non-Decision

`schema v2` is not selected in this tranche.

That means:

- no dual-write cutover for `thread_id` / `replay_path`
- no nested tracked-session schema migration
- no operator-facing recovery change for `monitor_state.json`

T43 is closed as not selected because its precondition, choosing `schema v2`,
was not met.

## Resulting Contract

- `monitor_state.json` keeps a versioned top-level envelope
- `tracked_sessions` entries remain on the compatibility envelope
- `session_id` and `file_path` stay on disk
- `thread_id` / `replay_path` are API aliases, not persisted schema keys

This keeps restart recovery stable while allowing the shared-core code and docs
to speak in runtime-neutral terms.
