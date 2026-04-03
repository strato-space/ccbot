# State Migration And Cutover

This note describes the persisted-state cutover used by the multi-runtime
adaptation work. The goal is to make the old Claude-era files readable while
the bot starts writing versioned envelopes.

## Files

- `state.json`
- `session_map.json`
- `monitor_state.json`

## Strategy

The cutover is one-time migration with reversible backups.

When a legacy file is loaded:
- the loader accepts the old shape
- a sidecar backup is created at `*.v1.bak`
- the file is rewritten in the new versioned shape

Rollback uses the backup sidecar as the source of truth if migration must be
reversed.

## Versioned shape

- `state.json` gets a top-level `schema_version` and `runtime_kind`
- `session_map.json` is stored as a versioned envelope with `schema_version`,
  `runtime_kind`, and `entries`
- `monitor_state.json` gets a top-level `schema_version` and `runtime_kind`

## Guarantees

- Existing topic bindings are preserved during migration.
- Existing replay offsets are preserved during migration.
- Legacy files remain recoverable through the backup sidecar.
- Readers continue to accept legacy and versioned shapes during cutover.
