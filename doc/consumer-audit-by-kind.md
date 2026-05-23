# T41 Consumer Audit By Kind

This audit separates runtime schema consumers from locator consumers, replay
readers, fixture/test contracts, operator workflows, and documentation
witnesses. Documentation-only mentions are not counted as direct consumers.

## Method

Scanned `src/`, `tests/`, `doc/`, `scripts/`, and `.claude/rules/` for
`monitor_state.json`, `session_map.json`, `file_path`, `replay_path`, and
`replay evidence`.

## Direct schema readers/writers

### `monitor_state.json`

- `src/ccbot/monitor_state.py:27-71, 88-183` owns the persisted
  `TrackedSession` shape, including `session_id`, `file_path`, and
  `last_byte_offset`, and performs the JSON load/save cycle.
- `src/ccbot/session_monitor.py:44-58, 278-320` instantiates `MonitorState`,
  loads the file on startup, and writes back tracked offsets as replay sources
  advance.

Risk: internal Python only, high. A schema mismatch here affects restart
recovery and duplicate suppression.

### `session_map.json`

- `src/ccbot/hook.py:238-299` writes the hook-generated window-to-session map on
  every `SessionStart`.
- `src/ccbot/session.py:310-338, 500-715` reads the map, migrates the legacy
  shape to the versioned envelope, and rewrites stale or legacy entries.
- `src/ccbot/state_schema.py:100-139` provides the shared envelope helpers, but
  is not itself a runtime consumer.

Risk: internal Python only, high. This is the operator-facing bind path and the
primary compatibility boundary for persisted window identity.

### Replay-evidence path fields

- `src/ccbot/monitor_state.py:27-71` persists `TrackedSession.file_path` and the
  `replay_path` alias.
- `src/ccbot/runtime_types.py:131-180` defines `ThreadLocator.replay_path` and
  `RolloutSource.replay_path`.

Risk: internal Python only, medium. These fields are compatibility aliases that
feed path consumers, not standalone schema roots.

## Locator/path consumers

- `src/ccbot/codex_threads.py:147-155, 246-363` turns Codex rollout files into
  `ThreadLocator` records and carries `rollout_file` paths through resolution.
- `src/ccbot/fast_agent_sessions.py:136-143, 182-249` turns fast-agent session
  metadata into `ThreadLocator` records and carries `replay_file` paths.
- `src/ccbot/session.py:773-1012, 1404-1433` resolves legacy Claude transcript
  paths and then reads them for direct lookup and message extraction.
- `src/ccbot/bot.py:2621-2689` only stats locator paths for reporting, not
  schema migration.

Risk: internal Python only, medium. Breakage here usually degrades discovery or
reporting before it corrupts persisted state.

## Replay-evidence readers

- `src/ccbot/session_monitor.py:86-170, 176-320` tails active replay sources and
  parses new JSONL lines into normalized events.
- `src/ccbot/session.py:780-833, 996-1005` reads legacy Claude transcript JSONL
  during direct lookup.
- `src/ccbot/codex_threads.py:275-363` reads Codex rollout JSONL and session
  metadata.
- `src/ccbot/fast_agent_sessions.py:52-83, 182-249` reads `session.json`,
  `history_*.json`, and `acp_log.jsonl` for fast-agent discovery.

Risk: internal Python only, medium. These readers affect event ingestion and
session discovery, but not the schema envelope itself.

## Fixture/test contracts

- `tests/ccbot/test_monitor_state.py`
- `tests/integration/test_monitor_state_integration.py`
- `tests/ccbot/test_session_monitor.py`
- `tests/ccbot/test_state_migration.py`
- `tests/ccbot/test_hook.py`
- `tests/ccbot/test_runtime_types.py`
- `tests/ccbot/test_claude_runtime_adapter.py`
- `tests/ccbot/test_fast_agent_sessions.py`
- `tests/ccbot/test_bot_contracts.py`
- fixtures under `tests/fixtures/cross_runtime/*` and
  `tests/fixtures/codex/rollouts/*`

Risk: fixture/test contract, high. These are the strongest guards on legacy
field names and migration shapes.

## Operator workflows

- `README.md`
- `README_CN.md`
- `README_RU.md`
- `doc/strato-ops-codex.md`
- `.claude/rules/architecture.md`
- `.claude/rules/topic-architecture.md`
- `scripts/restart.sh`

Risk: operator workflow, medium. These guide recovery and restart behavior but
do not themselves implement the persisted schema.

## Documentation witnesses

- `doc/runtime-ontology.md`
- `doc/runtime-capabilities.md`
- `doc/claude-runtime-adapter.md`
- `doc/fast-agent-runtime-adapter.md`
- `doc/multi-runtime-rollout.md`
- `doc/cross-runtime-fixture-corpus.md`
- `doc/state-migration.md`
- `doc/WEBSOCKET_PROTOCOL_REVERSED.md`

Risk: documentation witness, low. These documents describe the schema and
workflow, but they are not runtime consumers and must not be counted as such.
