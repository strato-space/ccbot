# Plan: Ontology Tail Cleanup After Codex Alignment

**Generated**: 2026-04-03  
**Continuation Of**:
- [ccbot-codex-adaptation-plan.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan.md)
- [ccbot-codex-adaptation-plan-2.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md)

## Purpose

This document extracts the non-blocking tails that remain after:

- the original Codex-only implementation plan was completed
- the plan ontology was normalized post hoc
- the implementation was brought into partial alignment with that ontology

This is not a replacement for the earlier plans. It is a cleanup tranche for the
remaining compatibility residue.

## Current Status

Established claims:

- The current Codex-oriented implementation is `GO` for continued development.
- The latest implementation pass aligned the monitor/state layer with the new
  ontology at the code/API level.
- Tests and lint for the touched layers are green.
- The remaining tails are cleanup and migration work, not current correctness blockers.
- A dedicated `ontology/` folder now exists in the repo as the compact
  source-of-truth entrypoint for core nouns and delivery boundaries.
- The hidden-opener vs lifecycle-reopen edge case (`server-wmf`) is closed:
  `turn_started` now reopens a turn only as a lane-closed fallback, preserving
  the non-turn contract for hidden internal user payloads.

Concrete implemented alignment already landed in:

- [monitor_state.py](/home/tools/ccbot/src/ccbot/monitor_state.py)
- [runtime_types.py](/home/tools/ccbot/src/ccbot/runtime_types.py)
- [session_monitor.py](/home/tools/ccbot/src/ccbot/session_monitor.py)
- [session.py](/home/tools/ccbot/src/ccbot/session.py)

### Post-Plan Delta (2026-04-04)

- Implemented lifecycle fallback in `handle_new_message`:
  - reopen generation on lifecycle `turn_started` only when the pre-final /
    technical-status lanes are still closed
  - do not reopen when lanes are already open
- Added regression coverage:
  - `test_handle_new_message_reopens_turn_on_lifecycle_turn_started_when_lane_closed`
  - `test_handle_new_message_does_not_reopen_turn_on_lifecycle_turn_started_when_lane_open`
- Validation and rollout completed:
  - `uv run --extra dev pytest tests/ccbot -q`
  - `uv run --extra dev ruff check src tests`
  - deployed on `str` with `ccbot.service` active

## Definitions

- **Implementation-era compatibility alias**
  - A code-level legacy name retained so older call sites and persisted state
    keep working during migration.
  - Examples:
    - `session_id` as alias for persisted thread identity
    - `file_path` as alias for persisted replay evidence path
    - `get_session()` as alias for `get_tracked_source()`

- **Persisted monitor schema**
  - The on-disk shape in `~/.ccbot/monitor_state.json`.
  - Today it still serializes legacy keys:
    - `session_id`
    - `file_path`

- **Schema consumer**
  - Code or tooling that reads or writes the persisted monitor schema fields
    themselves.
  - Example:
    - code that expects `session_id` or `file_path` keys in
      `monitor_state.json`

- **Locator consumer**
  - Code or tooling that does not care about the full schema, but does consume
    replay-evidence locations or persisted identity locations derived from it.

- **Evidence reader**
  - Code or tooling that reads the replay evidence artifact itself.
  - Example:
    - code that tails rollout JSONL or transcript JSONL

- **Documentation witness**
  - A document or note that describes a schema or workflow, but is not itself a
    runtime consumer of that schema.

- **Runtime-native adapter term**
  - A name that is native to a specific runtime and may remain valid inside a
    runtime adapter even if the shared-core vocabulary is more general.
  - Example:
    - `session_id` inside a fast-agent-specific adapter may be a true runtime
      concept, not automatically naming debt

- **Vocabulary cleanup**
  - Removal or narrowing of names that falsely imply:
    - `session == thread`
    - `session file == replay evidence`
    - `Claude path/config == generic runtime source`
  - Vocabulary cleanup must not erase legitimate runtime-native adapter terms.

## Tail Inventory

### Tail 1: Persisted monitor schema is still legacy-shaped

The implementation now exposes runtime-neutral aliases in code, but the
persisted monitor state still writes:

- `session_id`
- `file_path`

This is acceptable for compatibility, but it means the ontology is only
partially realized:

- API layer: normalized
- persisted schema layer: still legacy

Relevant code:

- [monitor_state.py](/home/tools/ccbot/src/ccbot/monitor_state.py#L36)
- [monitor_state.py](/home/tools/ccbot/src/ccbot/monitor_state.py#L59)

### Tail 2: Wider codebase still speaks Claude/session language

The monitor/state layer was normalized, but a large part of the surrounding
codebase still uses Claude-shaped and session-shaped names:

- `claude_projects_path`
- `ClaudeSession`
- `list_sessions_for_directory()`
- `clear_window_session()`
- `resume_session_id`
- `session_map.json`

Some of these are legitimate compatibility boundaries.
Some are runtime-native adapter terms.
Some are just naming debt that will keep reintroducing category mistakes.

Relevant code:

- [config.py](/home/tools/ccbot/src/ccbot/config.py#L76)
- [session.py](/home/tools/ccbot/src/ccbot/session.py#L51)
- [session.py](/home/tools/ccbot/src/ccbot/session.py#L595)
- [bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1117)

### Tail 3: Persisted and locator consumers have not yet been audited

Before any persisted schema migration, the project must identify every direct
consumer of:

- `monitor_state.json`
- `session_map.json`
- replay-evidence path fields

The risk is not internal Python breakage alone. The risk is silent breakage in:

- tests that assert old field names
- fixtures that encode the old schema shape
- local operator tooling
- downstream scripts

Documents may mention these artifacts, but they are not the same kind of thing
as direct consumers and must not be counted as such in the audit.

Concrete evidence that direct field-name coupling exists today:

- [tests/integration/test_monitor_state_integration.py](/home/tools/ccbot/tests/integration/test_monitor_state_integration.py)
- [tests/fixtures/codex/monitor_state_missing_rollout.json](/home/tools/ccbot/tests/fixtures/codex/monitor_state_missing_rollout.json)

### Tail 4: Read-path naming is now cleaner than write/control naming

The latest pass normalized replay/read language more than write/control
language.

Read-side is now comparatively clean:

- tracked replay source
- replay evidence
- thread-scoped locator

But write/control side still has older names:

- `clear_window_session`
- `wait_for_session_map_entry`
- `session_map`
- `session_id` in logs and bot messaging internals

This asymmetry is not a bug, but it creates conceptual drag for the
multi-runtime work in plan 2.

### Tail 5: Contract tests do not yet guard the new ontology explicitly

Current tests prove compatibility and local alias behavior, but they do not yet
assert stronger cross-layer invariants such as:

- persisted identity is not treated as emitter
- replay evidence is read-only for bot-side control logic
- replay evidence is written only by the runtime process or semantic emitter/supervisor
- monitor state schema migration preserves backward readability
- docs and code agree on the vocabulary of thread vs replay evidence

## Non-Goals

This cleanup tranche must not:

- redesign the multi-runtime architecture from scratch
- break current Codex functionality
- rename every legacy symbol in one sweep
- migrate persisted artifacts without a versioned cutover plan
- remove Claude compatibility

## Required Outcome

By the end of this tranche:

- the persisted schema tail is either migrated or explicitly frozen as a
  deliberate compatibility format
- direct schema consumers, locator consumers, and evidence readers are enumerated
- the remaining Claude/session vocabulary is classified into:
  - true compatibility boundary
  - runtime-native adapter term
  - removable naming debt
- tests explicitly guard the post-ontology implementation model

## Workstreams

### T66: Add Explicit Ontology Source-Of-Truth Folder

- **description**: Create a compact `ontology/` layer in the repo so runtime,
  delivery-surface, and boundary nouns stop depending on scattered maintainer
  notes alone.
- **status**: Completed
- **log**:
  - Added `ontology/README.md` as the ontology index.
  - Added `ontology/runtime.md` for live-control, persisted-identity, and
    replay-evidence nouns.
  - Added `ontology/delivery-surface.md` for terminal turn artifact,
    pre-final visible artifact, and technical status artifact.
  - Added `ontology/boundaries.md` for ACP distinctions, replay-evidence write
    ownership, and forbidden equalities.
  - Updated repo docs so `README.md`, `doc/runtime-ontology.md`,
    `doc/runtime-event-contract.md`, and `doc/telegram-delivery-pipeline.md`
    now point to the ontology folder explicitly.
- **acceptance**:
  - The repo exposes one obvious compact ontology entrypoint.
  - Runtime, delivery, and boundary nouns are separated into distinct notes.
  - Doc-contract tests assert the new ontology folder exists and carries the
    expected core nouns.
- **validation**:
  - `uv run --extra dev pytest tests/ccbot/test_docs_contracts.py`
  - `uv run --extra dev ruff check README.md ontology tests/ccbot/test_docs_contracts.py`

### T41: Consumer Audit By Kind

- **description**: Enumerate direct readers/writers of `monitor_state.json`,
  `session_map.json`, and replay-evidence path fields, but separate them by kind.
- **status**: Completed
- **log**:
  - Source scan completed across `src/`, `tests/`, `doc/`, `scripts/`, and
    `.claude/rules/`.
  - Direct schema consumers, locator/path consumers, replay-evidence readers,
    fixture/test contracts, operator workflows, and documentation witnesses are
    now separated in a source-backed audit.
  - Documentation witnesses were explicitly excluded from the direct consumer
    count.
- **files edited**:
  - `[doc/consumer-audit-by-kind.md](/home/tools/ccbot/doc/consumer-audit-by-kind.md)`
  - `[tests/ccbot/test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)`
- **required inventory kinds**:
  - direct schema readers/writers
  - locator/path consumers
  - replay-evidence readers
  - fixture/test contracts
  - operator workflows
  - documentation witnesses
- **deliverables**:
  - explicit inventory of direct consumers
  - risk classification:
    - internal Python only
    - external script
    - fixture/test contract
    - operator workflow
    - documentation witness
- **validation**:
  - no persisted schema migration starts before this inventory exists
  - no documentation witness is miscounted as a direct schema consumer

### T42: Decide Persisted Monitor Schema Strategy

- **description**: Make an explicit decision for `monitor_state.json`:
  - keep legacy keys permanently as compatibility envelope
  - or migrate to runtime-neutral keys with dual-read/write cutover
- **status**: completed
- **log**:
  - explicit strategy note recorded in [monitor-state-schema-strategy.md](/home/tools/ccbot/doc/monitor-state-schema-strategy.md)
  - `monitor_state.json` is now explicitly frozen on the compatibility envelope for nested tracked-session fields
  - top-level versioned envelope remains in place; nested `schema v2` cutover was rejected for this tranche
- **files edited**:
  - [monitor-state-schema-strategy.md](/home/tools/ccbot/doc/monitor-state-schema-strategy.md)
  - [state-migration.md](/home/tools/ccbot/doc/state-migration.md)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **options**:
  - `compatibility envelope`
    - keep `session_id` and `file_path`
    - document them as compatibility transport fields
  - `schema v2`
    - write `thread_id` and `replay_path`
    - continue reading legacy shape
    - optionally dual-write during cutover
- **validation**:
  - chosen approach is documented and tested

### T43: Versioned Monitor-State Migration If Needed

- **depends_on**: `T41`, `T42`
- **description**: If `schema v2` is chosen, introduce a versioned migration for
  `monitor_state.json`.
- **status**: completed_not_selected
- **log**:
  - `T42` selected the compatibility envelope, so nested tracked-session migration was deliberately not started
  - migration remains unnecessary unless a future tranche explicitly chooses `schema v2`
- **files edited**:
  - [monitor-state-schema-strategy.md](/home/tools/ccbot/doc/monitor-state-schema-strategy.md)
  - [ccbot-codex-adaptation-plan-4.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md)
- **requirements**:
  - preserve read compatibility for old files
  - preserve operator recoverability
  - avoid duplicate notifications after restart
  - preserve a clean distinction between persisted identity fields and
    replay-evidence fields
- **validation**:
  - migration tests cover old -> new -> old-reader-safe cases where applicable

### T44: Runtime-Neutral Naming Audit

- **description**: Audit remaining Claude/session-shaped names and classify each as:
  - required compatibility surface
  - runtime-native adapter term
  - transitional alias
  - removable naming debt
- **status**: completed
- **initial candidates**:
  - `claude_projects_path`
  - `ClaudeSession`
  - `list_sessions_for_directory`
  - `clear_window_session`
  - `session_map`
  - `resume_session_id`
- **log**:
  - source-backed audit recorded in [runtime-naming-audit.md](/home/tools/ccbot/doc/runtime-naming-audit.md)
  - each surviving legacy name now has a written reason and source references
  - runtime-native adapter terms were kept separate from shared-core naming debt
- **files edited**:
  - [/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md)
  - [runtime-naming-audit.md](/home/tools/ccbot/doc/runtime-naming-audit.md)
- **validation**:
  - each surviving legacy name has a written reason
  - runtime-native adapter terms are not misclassified as shared-core debt

### T45: Control-Path Vocabulary Cleanup

- **depends_on**: `T44`
- **description**: Clean up the highest-value write/control names that now lag
  behind the normalized read path.
- **status**: completed
- **log**:
  - shared-core call sites now use `clear_window_binding()` for persisted-identity cleanup after `/clear`
  - `clear_window_session()` remains as a backward-compatible alias pending any later alias-cut decision
  - control-path log messages now speak about persisted bindings instead of generic sessions
- **files edited**:
  - [session.py](/home/tools/ccbot/src/ccbot/session.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [test_session.py](/home/tools/ccbot/tests/ccbot/test_session.py)
  - [test_forward_command.py](/home/tools/ccbot/tests/ccbot/test_forward_command.py)
  - [runtime-naming-audit.md](/home/tools/ccbot/doc/runtime-naming-audit.md)
- **target examples**:
  - `clear_window_session` -> clearer persisted-identity wording
  - `resume_session_id` -> wording that does not collapse thread/session across runtimes
  - log messages that still imply `session == runtime`
- **constraints**:
  - do not erase runtime-native adapter language where it names a true runtime concept
- **validation**:
  - no behavior change
  - legacy aliases kept where externally required

### T46: Ontology Contract Tests

- **description**: Add tests and doc-contract checks that explicitly enforce the
  implementation ontology.
- **status**: completed
- **log**:
  - contract tests now freeze the compatibility-envelope choice for `monitor_state.json`
  - code tests now assert that tracked-session persistence does not silently switch to `thread_id` / `replay_path`
  - docs now explicitly pin replay-evidence writing to the runtime process or semantic emitter/supervisor
- **files edited**:
  - [test_monitor_state.py](/home/tools/ccbot/tests/ccbot/test_monitor_state.py)
  - [test_state_migration.py](/home/tools/ccbot/tests/ccbot/test_state_migration.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
  - [monitor-state-schema-strategy.md](/home/tools/ccbot/doc/monitor-state-schema-strategy.md)
  - [state-migration.md](/home/tools/ccbot/doc/state-migration.md)
- **must cover**:
  - `TrackedSession.thread_id` / `replay_path` compatibility behavior
  - persisted identity not treated as event emitter
  - replay evidence is not a bot-side control-path write target
  - replay evidence is written only by the runtime process or semantic emitter/supervisor
  - docs/code vocabulary alignment for replay evidence vs session file
  - schema-strategy docs match the actual implementation choice

### T47: Cut Legacy Aliases Only After Audit

- **depends_on**: `T41`, `T42`, `T44`, `T46`
- **description**: Remove compatibility aliases only if the audit proves they
  are not externally required.
- **status**: completed
- **log**:
  - no new alias cut was approved beyond internal call-site cleanup
  - `clear_window_session()` remains as a compatibility alias because the audit did not prove absence of external consumers
  - future alias removal now requires a narrower explicit consumer proof, not just shared-core cleanup desire
- **files edited**:
  - [session.py](/home/tools/ccbot/src/ccbot/session.py)
  - [runtime-naming-audit.md](/home/tools/ccbot/doc/runtime-naming-audit.md)
  - [ccbot-codex-adaptation-plan-4.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md)
- **not before**:
  - consumer inventory complete
  - schema decision made
  - contract tests in place

### T48: Existing-Window Bind Must Register Live Read Path

- **description**: Fix the existing-window `/bind` picker flow so binding a live
  tmux window restores the full runtime read path, not just the write path.
- **status**: completed
- **log**:
  - existing-window bind now resolves `cwd` and runtime metadata from live tmux
    window data before the topic is bound
  - the callback path now routes through the same registration helper used by
    launch/resume flows, so the topic receives both send-path and replay-event
    delivery semantics
  - picker bind now fails closed when a selected live window has no detectable
    workspace path, instead of creating a send-only half-bind
- **files edited**:
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [ccbot-codex-adaptation-plan-4.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md)
- **acceptance criteria**:
  - binding an existing live window registers `cwd` and runtime metadata before
    topic binding completes
  - an existing-window bind no longer leaves `state.json` with an empty
    `cwd/session_id` descriptor for the selected window
  - if `cwd` cannot be detected, the picker rejects the bind and keeps the
    topic out of send-only mode
- **validation**:
  - callback-path regression tests cover successful registration and fail-closed
    `cwd`-missing behavior
  - live deployment verification confirms the service is updated and the
    helper resolves real tmux window metadata on `str`

### T49: Runtime-Aware Replay-Source Discovery For Codex Bindings

- **description**: Remove the hidden dependency on legacy Claude project-root
  scanning in the active monitor loop. Active Codex and fast-agent bindings
  must provide replay sources through runtime-aware binding resolution rather
  than `claude_projects_path` scanning.
- **status**: completed
- **log**:
  - `SessionMonitor` now caches active rollout sources from
    `resolve_thread_for_window()` during binding-map resolution
  - active Codex replay files no longer depend on `config.claude_projects_path`
    being repointed to Codex storage
  - legacy project scanning remains only as a fallback for old test surfaces
    and Claude-shaped flows
- **files edited**:
  - [session_monitor.py](/home/tools/ccbot/src/ccbot/session_monitor.py)
  - [test_session_monitor.py](/home/tools/ccbot/tests/ccbot/test_session_monitor.py)
  - [test_state_migration.py](/home/tools/ccbot/tests/ccbot/test_state_migration.py)
  - [ccbot-codex-adaptation-plan-4.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md)
- **acceptance criteria**:
  - active replay sources are derived from runtime-aware binding resolution
  - Codex delivery no longer depends on `CCBOT_CLAUDE_PROJECTS_PATH`
  - tests prove `check_for_updates()` can read Codex rollout files from active
    runtime-resolved sources without invoking legacy project scanning
- **validation**:
  - state-migration / session-monitor tests cover active source population and
    runtime-resolved update reading

### T50: Suppress Duplicate Codex Message Delivery From `event_msg`

- **description**: Codex rollout writes both lightweight `event_msg` message
  records and canonical `response_item.message` records for the same user/final
  assistant turn. Telegram must not deliver both.
- **status**: completed
- **log**:
  - confirmed on live rollout evidence from `str` that `ping 4` produced both:
    - `response_item.message(role=user)` and `event_msg.user_message`
    - `event_msg.agent_message(phase=final_answer)` and
      `response_item.message(role=assistant, phase=final_answer)`
  - Codex normalization now keeps `event_msg` commentary/progress in the
    semantic taxonomy, but suppresses Telegram/history delivery for:
    - `event_msg.user_message`
    - `event_msg.agent_message` with `phase=final_answer`
  - canonical Telegram/history delivery now comes from `response_item.message`
    for user echo and final assistant answer
- **files edited**:
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [ccbot-codex-adaptation-plan-4.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md)
- **acceptance criteria**:
  - one user echo per Codex turn in Telegram
  - one final assistant answer per Codex turn in Telegram
  - commentary/progress `event_msg` delivery remains intact
- **validation**:
  - normalization tests assert duplicate `event_msg` user/final-answer records
    are suppressed from Telegram dispatch while preserved in taxonomy

### T51: Compact Telegram Delivery For Popular-Bot Default

- **description**: The default Telegram surface must be human-facing, not a raw
  execution transcript dump. `$parallel` and similar orchestration turns must
  no longer leak giant skill payloads, full command stdout, or full file
  bodies as ordinary content bubbles.
- **status**: completed
- **log**:
  - added explicit `compact` vs `verbose` Telegram delivery policy
  - default mode is now `compact`
  - internal injected user payloads such as `<skill>...</skill>` are
    suppressed from Telegram
  - placeholder reasoning (`[reasoning]`) is suppressed
  - complete `commentary`, `reasoning`, `command_execution`, and `file_change`
    events are projected into the mutable status artifact in compact mode
  - Codex rollout normalization now summarizes giant `tool_use`,
    `command_execution`, and `file_change` payloads instead of shipping raw
    blobs downstream
- **files edited**:
  - [config.py](/home/tools/ccbot/src/ccbot/config.py)
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - ordinary chat turns no longer show `<skill>` payloads as user echoes
  - placeholder reasoning does not appear in Telegram
  - commentary and execution noise collapse into the mutable status artifact
    instead of spamming separate permanent messages
  - giant `apply_patch` / command stdout / file-change payloads are reduced to
    compact summaries before Telegram rendering
- **validation**:
  - bot contract tests cover compact-mode routing and suppression rules
  - Codex rollout tests prove giant payloads are summarized rather than emitted
    raw
  - doc contracts pin `compact` as the default Telegram delivery mode

### T52: Status-Only Tool Lifecycle In Compact Telegram Mode

- **description**: The first compact-delivery pass still allowed `tool_use` and
  `tool_result` to survive as ordinary Telegram content bubbles, which caused
  `$parallel` turns to spam raw execution surface despite compact summaries.
  In the production-facing `compact` mode, tool lifecycle summaries must flow
  through the mutable status artifact instead of creating permanent chat
  messages.
- **status**: completed
- **log**:
  - expanded compact-mode status projection to include `tool_use` and
    `tool_result`
  - compact-mode projected status artifacts are no longer included in `/history`
  - suppressed additional internal injected user payloads such as
    `# AGENTS.md instructions ...` and `<turn_aborted> ...`
  - clarified docs so `tool_result -> tool_use edit-in-place` remains true for
    verbose/upstream-style content delivery, but not mandatory for default
    compact mode
- **files edited**:
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - `$parallel`-style turns no longer emit raw `🛠 Tool`, `↳ Tool Output`, or
    `⌘ Command` bubbles as ordinary content in default compact mode
  - internal prompt injections such as `AGENTS.md` and turn-abort wrappers do
    not appear as ordinary user echoes
  - default compact mode still preserves final assistant content delivery
- **validation**:
  - bot contract tests cover compact-mode routing for `tool_use`, `tool_result`,
    internal AGENTS payloads, and turn-aborted payloads
  - docs contract tests pin the compact/verbose distinction for tool lifecycle

### T53: Codex-Style Command Summary In Compact Telegram Mode

- **description**: Compact Telegram status should render command execution
  closer to Codex human output: show the actual shell payload rather than the
  wrapper argv (`/bin/bash`, `-lc`) and present it as a short code block plus
  compact status line.
- **status**: completed
- **log**:
  - command summary now extracts shell payload from `bash/zsh/sh -lc` wrappers
  - compact command status renders a fenced `sh` block with the first few
    invocation lines instead of `/bin/bash (+N more lines)`
  - command previews now keep truncation footer outside the fenced code block
  - outcome metadata is no longer forced into a redundant
    `completed · output N line(s)` line when the visible preview already
    conveys the result
- **files edited**:
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
- **acceptance criteria**:
  - compact command summaries no longer surface wrapper argv when the command
    is really a shell payload under `-lc`
  - Telegram-visible command summaries show a short code block with several
    invocation lines when present
  - command summary still avoids raw stdout dumps
  - truncation metadata does not appear inside the code block body
- **validation**:
  - rollout tests assert `bash -lc` payload extraction
  - rollout tests assert code-aware preview plus non-redundant outcome footer

### T54: Preserve Commentary As Visible Progress Narrative

- **description**: Compact Telegram delivery became too aggressive after tool
  lifecycle collapse. Human-readable `commentary` was projected into the same
  mutable status artifact as tool lifecycle, so important progress notes could
  be overwritten or disappear quickly. Codex rollout also emits commentary
  twice: once as lightweight `event_msg.agent_message` and once as canonical
  `response_item.message`. Telegram should keep commentary visible as ordinary
  content while suppressing the duplicate `event_msg` copy.
- **status**: completed
- **log**:
  - compact-mode delivery now keeps complete `commentary` as ordinary content
    instead of status-only churn
  - duplicate Codex `event_msg.agent_message(phase=commentary)` delivery is now
    suppressed so only canonical `response_item.message` commentary reaches
    Telegram/history
  - docs now distinguish human-readable commentary from ephemeral technical
    execution status
- **files edited**:
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - compact-mode commentary remains visible in Telegram as ordinary content
  - commentary is not overwritten simply because tool lifecycle status keeps
    mutating
  - duplicate Codex commentary from `event_msg` does not create extra Telegram
    bubbles
- **validation**:
  - bot contract tests assert compact commentary is content, not status
  - rollout tests assert duplicate `event_msg` commentary is suppressed
  - docs contract tests pin commentary as visible compact-mode content

### T55: Audit Bubble Surface And Humanize Tool Delivery

- **description**: After compact-delivery cleanup, tool and command bubbles
  still needed one more pass. The delivery policy was keyed too narrowly to
  `content_type`, which left `thinking` and Claude-style `local_command`
  exposed to bubble leakage. Tool summaries also remained more machine-shaped
  than Codex human output. The bubble surface must be audited by semantic kind,
  and tool/tool-output summaries must be made human-oriented.
- **status**: completed
- **log**:
  - compact Telegram delivery now routes by semantic kind rather than only by
    raw `content_type`
  - `thinking` now follows the same compact status-only behavior as
    `reasoning`
  - Claude-style `local_command` now follows the same compact status-only
    behavior as other command execution summaries
  - Codex `tool_use` summaries for `exec_command` and `write_stdin` are now
    rendered in a more human-readable form, including short fenced code blocks
    for command payloads or submitted stdin text
  - Codex `tool_result` summaries for `exec_command` now use compact output
    counts instead of leaking raw multi-line blobs into bubble surfaces
  - the compact bubble matrix is now explicit in the Telegram delivery note
- **files edited**:
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - compact mode does not leak `thinking` or `local_command` as permanent
    content bubbles
  - the compact bubble matrix is narrow and explicit
  - when tool bubbles are materialized in verbose/fallback lanes, their text is
    closer to Codex human output than raw JSON argument dumps
- **validation**:
  - bot contract tests assert `thinking` and `local_command` collapse into
    compact status-only delivery
  - rollout tests assert human-readable `exec_command` and `write_stdin`
    summaries
  - docs contract tests pin the compact bubble matrix

### T56: Sync Public Docs With Compact Bubble Contract

- **description**: The product docs still carried stale language from the older
  Telegram surface and implied that thinking, tool lifecycle, and local command
  output were first-class visible bubbles in normal operation. After compact
  delivery became the product default, the public docs and maintainer docs had
  to state the same ontology: semantic eligibility is broader than durable
  Telegram bubble permanence, and commentary is the visible execution
  narrative.
- **status**: completed
- **log**:
  - repo-level maintainer instructions now pin the compact Telegram surface and
    the need to keep delivery docs/tests aligned
  - the public README now describes the narrow compact bubble surface and stops
    promising raw thinking/tool/command bubbles in default operation
  - runtime-event docs now distinguish semantic eligibility from product-facing
    compact projection
  - Telegram feature research notes now record compact delivery as implemented
    behavior and limit reasoning-rich rendering ideas to verbose/debug lanes
- **files edited**:
  - [AGENTS.md](/home/tools/ccbot/AGENTS.md)
  - [CHANGELOG.md](/home/tools/ccbot/CHANGELOG.md)
  - [README.md](/home/tools/ccbot/README.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - public docs no longer promise raw reasoning/tool/command bubbles in the
    default compact Telegram surface
  - the docs distinguish semantic contract from product bubble permanence
  - commentary is described as the visible execution narrative in compact mode
- **validation**:
  - docs contract tests pin the README, runtime-event note, and Telegram
    feature note against the compact delivery contract

### T57: Latest-Only Commentary And Code-Aware Tool/File Surfaces

- **description**: Compact delivery was still too chatty in two ways. First,
  commentary was preserved as ordinary content bubbles, which let a long stack
  of near-duplicate progress notes accumulate. Second, surfaced `Tool`,
  `Tool Output`, and `Δ Files` bubbles still leaked machine-shaped text in
  some lanes instead of following Codex-style human formatting. Compact mode
  should keep only the latest visible commentary artifact, while tool/file
  surfaces should prefer short fenced `sh` / `json` blocks and compact output
  counts.
- **status**: completed
- **log**:
  - compact mode now routes complete commentary into a latest-only visible
    commentary artifact instead of leaving every commentary update as a durable
    content bubble
  - the commentary artifact is distinct from the mutable technical status
    artifact, so commentary stays visible without being overwritten by status
    churn
  - tool-use and tool-result payloads now prefer code-aware formatting when
    surfaced, including fenced `json` blocks for JSON payloads
  - `Δ Files` output now prefers fenced `sh` blocks when it carries multiple
    changed paths
  - the docs now distinguish durable bubbles from the latest-only commentary
    artifact
- **files edited**:
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [cleanup.py](/home/tools/ccbot/src/ccbot/handlers/cleanup.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [response_builder.py](/home/tools/ccbot/src/ccbot/handlers/response_builder.py)
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [README.md](/home/tools/ccbot/README.md)
  - [AGENTS.md](/home/tools/ccbot/AGENTS.md)
  - [CHANGELOG.md](/home/tools/ccbot/CHANGELOG.md)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_response_builder.py](/home/tools/ccbot/tests/ccbot/handlers/test_response_builder.py)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - compact Telegram delivery keeps only the latest visible commentary note per
    topic instead of accumulating commentary bubbles
  - commentary remains distinct from technical status churn
  - tool and tool-output surfaces prefer code-aware formatting when surfaced
  - `Δ Files` output may render changed paths as fenced `sh` blocks
- **validation**:
  - bot contract tests assert compact commentary no longer goes through the
    ordinary content queue
  - message queue tests assert commentary replacement deletes the prior
    commentary bubble
  - response builder tests assert JSON/sh formatting for tool/file surfaces
  - rollout tests assert codex tool summaries prefer JSON blocks when the
    payload is structured JSON

### T58: Close Commentary Lane After Final Assistant Bubble

- **description**: Even after commentary was moved into a latest-only visible
  artifact, it could still appear below the final assistant answer if a late
  commentary update arrived after the final bubble. This breaks the public
  turn boundary and makes the chat look temporally inverted. Once the final
  assistant answer is delivered, compact mode must close the commentary lane
  until the next user turn reopens it.
- **status**: completed
- **log**:
  - compact final-answer delivery now clears the visible commentary artifact
    before enqueuing the final content bubble
  - commentary updates are dropped while the commentary lane is closed
  - the commentary lane reopens on the next user echo
  - topic teardown now clears commentary-lane state as well as the tracked
    message id
- **files edited**:
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [cleanup.py](/home/tools/ccbot/src/ccbot/handlers/cleanup.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - compact mode never shows commentary below the final assistant answer for
    the same turn
  - late commentary is ignored after final delivery until a new user turn
  - teardown clears commentary-lane state for the topic
- **validation**:
  - queue tests assert commentary updates are dropped after the lane is closed
  - bot contract tests assert final delivery clears commentary before final
    content is enqueued
  - docs contract tests pin the no-commentary-after-final invariant

### T59: Render Codex Multi-Agent Orchestration As Human-Facing Milestones

- **description**: Codex renders subagent coordination as human-facing history
  rows like `Spawned Mill [explorer] (gpt-5.4 medium)` and `Waiting for Mill
  [explorer]`, but Telegram was leaking raw `spawn_agent` / `wait_agent` /
  `<subagent_notification>` payloads instead. Compact delivery must synthesize
  Codex-style orchestration milestone bubbles from rollout evidence and keep
  the raw technical payloads out of Telegram/history.
- **status**: completed
- **log**:
  - Codex rollout normalization now synthesizes durable `orchestration`
    events from `spawn_agent`, `wait_agent`, and subagent completion status
  - `spawn_agent` now renders a Codex-style title with agent nickname/role and
    model/reasoning metadata plus a prompt preview detail block
  - `wait_agent` now renders human-facing `Waiting for ...` milestones instead
    of exposing raw tool JSON
  - subagent completion/failure/shutdown status now renders as human-facing
    milestone text and deduplicates against raw `<subagent_notification>`
    payloads
  - the raw `spawn_agent`, `wait_agent`, and `<subagent_notification>`
    transport surfaces are suppressed from Telegram/history
- **files edited**:
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [runtime_types.py](/home/tools/ccbot/src/ccbot/runtime_types.py)
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [README.md](/home/tools/ccbot/README.md)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_response_builder.py](/home/tools/ccbot/tests/ccbot/handlers/test_response_builder.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
- **acceptance criteria**:
  - compact mode shows Codex-style spawned/waiting/completed subagent
    milestones as human-facing content bubbles
  - raw `<subagent_notification>` never appears in Telegram
  - raw `spawn_agent` / `wait_agent` tool payloads do not leak into Telegram
    or `/history`
  - orchestration milestones stay distinct from latest-only commentary and
    mutable technical status
- **validation**:
  - rollout tests assert spawn/wait/completion synthesis and deduplication
  - response-builder tests assert orchestration text is not wrapped in tool or
    commentary prefixes
  - bot contract tests assert compact mode keeps orchestration as ordinary
    content, not status-only or commentary-only

### T60: Generalize Final-Answer Ordering To The Whole Pre-Final Visible Surface

- **description**: The earlier commentary-lane fix was too narrow. The real
  public contract is broader: once `assistant_final` lands for a turn, no
  visible pre-final artifact of that same turn may appear below it. This
  includes commentary, orchestration milestones, and any surfaced preview
  bubble the product chooses to expose.
- **status**: completed
- **log**:
  - the queue now models a broader `pre-final visible surface` instead of only
    a commentary lane
  - final-answer delivery closes that surface via an explicit serialized
    barrier primitive rather than an ad hoc immediate flag
  - hidden user echoes still reopen the next-turn lane even when they are not
    dispatched to Telegram, so turn boundaries remain intact
  - already-queued pre-final visible artifacts may still land before the final
    bubble, but no later artifact can surface below it for the same turn
- **files edited**:
  - [runtime_types.py](/home/tools/ccbot/src/ccbot/runtime_types.py)
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [README.md](/home/tools/ccbot/README.md)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
- **acceptance criteria**:
  - no late commentary, orchestration milestone, or surfaced preview bubble
    appears below `assistant_final` for the same turn
  - hidden user echoes reopen the next-turn pre-final surface even when they
    stay out of Telegram delivery
  - already queued pre-final artifacts may still land before the final bubble
- **validation**:
  - queue tests assert late pre-final content is dropped after the surface is
    closed
  - bot contract tests assert hidden user echoes reopen the next-turn surface
  - docs contract tests pin `pre-final visible artifact` as the governing
    ordering ontology

### T61: Stateful Codex Cross-Poll Canonicalization And Safe Preview Truncation

- **description**: Codex rollout normalization was still batch-local in places
  where the live monitor is poll-sliced. Cross-poll state had to be made
  explicit so that spawn/wait orchestration, canonical `response_item.message`
  preference, and lightweight `event_msg` fallback all survive real monitor
  timing. At the same time, compact preview truncation had to stop breaking
  fenced code blocks.
- **status**: completed
- **log**:
  - introduced explicit per-thread `CodexRolloutState` carried by the monitor
    across poll slices
  - spawn/wait orchestration synthesis now survives split polls rather than
    assuming `function_call` and `function_call_output` arrive in one batch
  - wait-status dedupe is now scoped to a wait generation instead of the whole
    thread lifetime
  - lightweight `event_msg` copies may now buffer briefly so later canonical
    `response_item.message` records can win across poll boundaries; if no
    canonical copy arrives, the buffered message may flush later on an idle
    poll instead of disappearing or being released early on an unrelated
    non-idle poll
  - Codex rollout state now resets cleanly on replay-file truncation
  - compact truncation now closes fenced code blocks before adding the
    `… (+N more lines)` footer
- **files edited**:
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [session_monitor.py](/home/tools/ccbot/src/ccbot/session_monitor.py)
  - [transcript_parser.py](/home/tools/ccbot/src/ccbot/transcript_parser.py)
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [response_builder.py](/home/tools/ccbot/src/ccbot/handlers/response_builder.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_session_monitor.py](/home/tools/ccbot/tests/ccbot/test_session_monitor.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_response_builder.py](/home/tools/ccbot/tests/ccbot/handlers/test_response_builder.py)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
- **acceptance criteria**:
  - cross-poll spawn/wait synthesis produces the same human-facing milestones
    as single-batch normalization
  - canonical `response_item.message` wins over duplicate lightweight
    `event_msg` copies even when they arrive on later polls
  - buffered lightweight messages may flush later on an idle poll if no
    canonical copy arrives
  - truncation footers never live inside fenced code-block bodies
- **validation**:
  - rollout tests cover cross-poll spawn, wait, dedupe, and event flush cases
  - session-monitor tests cover truncation reset, hidden user turn boundaries,
    cross-poll spawn state, and idle flush of buffered messages
  - bot/response-builder tests pin balanced code fences and footer placement

### T62: Close The Whole Terminal Surface After Final Assistant Delivery

- **description**: The earlier barrier still treated `pre-final visible
  artifact` and `technical status artifact` unevenly. In practice, a final
  assistant answer must close both the visible pre-final surface and the
  mutable technical status surface for that turn. Otherwise a late status or
  direct tmux-derived status refresh can still appear below the final bubble
  and invert the turn shape.
- **status**: completed
- **log**:
  - final-delivery queue close now closes both the visible pre-final surface
    and the mutable technical status surface
  - queued or direct status refreshes are dropped after terminal closure until
    the next user turn reopens the lane
  - Codex cross-poll canonicalization no longer flushes buffered duplicate
    `event_msg` copies on unrelated non-idle polls; unmatched copies flush on
    later idle polls instead
  - compact fenced status previews now hard-cap the first visible code lines so
    a single oversized line cannot blow past the status budget
- **files edited**:
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [README.md](/home/tools/ccbot/README.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - no late status artifact appears below `assistant_final` for the same turn
  - no buffered duplicate `event_msg` copy is released on an unrelated non-idle
    poll before a later canonical `response_item.message`
  - compact fenced status previews stay within the status budget even when the
    first line is oversized
- **validation**:
  - queue tests assert late status updates and direct status refreshes are
    dropped after terminal closure
  - rollout tests assert duplicate message buffers survive unrelated non-idle
    polls and only flush on idle polls when unmatched
  - bot contract tests assert fenced status previews stay balanced and inside
    the compact status budget

### T63: User-Turn Opener Semantics And Turn-Generation Barrier

- **description**: The previous compact-terminal repair still treated
  `user-visible user echo` and `user turn opener` as if they were the same
  thing. They are not. A new turn may begin through a hidden internal prompt
  scaffold, and stale close tasks from the previous turn must not re-close the
  new turn after reopen. The queue therefore needs an explicit per-control-surface
  `turn generation`, and Codex rollout needs to treat incremental
  `event_msg.user_message` as the opener of a new turn without reopening that
  same turn again when a later canonical duplicate arrives.
- **status**: completed
- **log**:
  - per-control-surface turn generations now advance on each real user turn opener
  - pre-final and technical-status close tasks are generation-scoped, so a
    stale close from an older turn fails closed instead of reclosing the newer
    turn
  - stale-turn commentary/status/pre-final visible tasks are dropped when
    their generation no longer matches the current topic turn
  - stale `assistant_final` delivery also fails closed once a newer turn has
    already opened, so an older turn cannot land below the newer terminal
    surface
  - stateless Codex one-shot normalization no longer buffers unmatched
    signature-bearing `event_msg` records behind the duplicate window
  - incremental `event_msg.user_message` now opens the new turn immediately,
    while a later canonical duplicate user message inside the duplicate window
    is dropped instead of reopening the turn a second time
  - duplicate suppression state is FIFO per signature rather than single-slot,
    so repeated same-text turns do not overwrite each other before idle flush
  - hidden internal prompt scaffolds may still reopen the terminal surface
    when they start a real user turn; `<subagent_notification>` and
    `<turn_aborted>` remain non-turn notifications
- **files edited**:
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [README.md](/home/tools/ccbot/README.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_session_monitor.py](/home/tools/ccbot/tests/ccbot/test_session_monitor.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - a stateless one-shot Codex parse returns unmatched signature-bearing
    `event_msg` content immediately instead of losing it behind the duplicate
    window
  - a real new user turn reopens the terminal surface even when the opening
    payload is hidden from Telegram
  - non-turn hidden notifications such as `<subagent_notification>` do not
    reopen the terminal surface
  - stale close tasks from an older turn cannot re-close the newer turn after
    reopen
  - a later canonical duplicate user message does not reopen the same turn a
    second time
- **validation**:
  - rollout tests cover stateless unmatched `event_msg`, immediate
    `event_msg.user_message` turn opening, and duplicate canonical user-copy
    suppression
  - queue tests cover turn-generation advance and stale-turn commentary/status
    drop behavior
  - bot contract tests cover hidden turn opener vs non-turn notification
    reopening semantics
  - session-monitor tests cover idle flush of unmatched buffered commentary in
    incremental monitor mode

### T64: Turn-Scoped Codex Canonicalization And Visible-Preview Contract

- **description**: The compact Telegram surface needed one more ontology-first
  repair. The queue-level final barrier already closed the broader
  `pre-final visible artifact` class, but rollout normalization and preview
  rendering still had a few turn-shape leaks: Codex `wait_agent` did not always
  emit a distinct `Finished waiting ...` milestone when the wait tool returned,
  duplicate suppression still needed explicit turn identity as its governing
  key, and surfaced command/tool previews still mixed preview body with
  truncation/outcome metadata in ways that were not aligned with Codex human
  history rows.
- **status**: completed
- **log**:
  - Codex rollout duplicate suppression is now scoped by explicit turn
    identity, using canonical `turn_id` when present and surrogate turn keys
    before that id arrives
  - already-started multipart sends now abort mid-flight when a newer turn
    opens or when the terminal turn artifact closes the surface, so the
    remaining parts cannot leak below the wrong boundary
  - `wait_agent` now models three distinct orchestration facts:
    `Waiting for ...`, `Finished waiting ...`, and then per-agent
    completion/failure plus timeout outcomes when present
  - human-facing command/tool/file previews now keep fenced preview body
    separate from truncation metadata (`preview N/M lines`)
  - surfaced previews no longer add a redundant
    `completed · output 1 line(s)` footer when the visible preview already
    conveys the outcome
- **files edited**:
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [response_builder.py](/home/tools/ccbot/src/ccbot/handlers/response_builder.py)
  - [telegram_delivery_policy.py](/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [README.md](/home/tools/ccbot/README.md)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_response_builder.py](/home/tools/ccbot/tests/ccbot/handlers/test_response_builder.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
- **acceptance criteria**:
  - no visible pre-final artifact of a turn can finish sending below that
    turn's `assistant_final`, even if the multipart send had already started
  - `wait_agent` emits a human-facing `Finished waiting ...` milestone when the
    wait tool returns, distinct from timeout or per-agent completion facts
  - surfaced command/tool/file previews keep preview body, truncation metadata,
    and outcome metadata as distinct layers
  - compact visible previews do not show a redundant
    `completed · output 1 line(s)` footer when the preview itself already
    communicates the result
- **validation**:
  - rollout tests cover wait completion, timeout, and subagent-notification
    deduplication across poll slices
  - queue tests cover mid-flight stale-turn abort of multipart visible content
  - bot/response-builder tests pin footer placement outside fenced blocks and
    the absence of redundant completion footers

### T65: Repair Hydrated Turn Dedupe, Wait Finish Milestones, And Post-Success Terminal Closure
- **description**: Independent xhigh review found three remaining soundness
  defects in the generalized turn-ordering tranche. First, Codex rollout state
  hydration cleared the active-turn user duplicate buffer, so a later canonical
  user copy after restart could reopen the same turn a second time. Second,
  multi-agent wait synthesis dropped `Finished waiting ...` as soon as one
  agent emitted an early `<subagent_notification>`. Third, the terminal barrier
  still closed the pre-final visible/status surface before the final bubble had
  actually been delivered, so a Telegram send failure could leave the turn with
  no visible terminal artifact at all.
- **status**: completed
- **log**:
  - hydrated Codex rollout state now preserves duplicate-suppression entries
    only for the active turn, instead of clearing the whole buffer
  - early `<subagent_notification>` no longer tears down an active multi-agent
    wait before the real `wait_agent` output returns
  - terminal surface closure now happens only after successful final delivery
    inside the content worker, not as a bot-side pre-close task
- **files edited**:
  - [session_monitor.py](/home/tools/ccbot/src/ccbot/session_monitor.py)
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [test_session_monitor.py](/home/tools/ccbot/tests/ccbot/test_session_monitor.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
- **acceptance criteria**:
  - after restart/state hydration, a later canonical duplicate user opener does
    not reopen the same turn a second time
  - multi-agent waits still emit `Finished waiting ...` even if one agent emits
    a completion notification before the final `wait_agent` output
  - pre-final visible/status surfaces are closed only after successful final
    assistant delivery, not before
- **validation**:
  - targeted monitor/rollout/queue/bot tests cover all three defects
  - docs/specs state terminal closure in terms of successful final delivery

### T66: Move The Living Spec Corpus Into The Repo

- **description**: The execution-plan corpus was still split between the repo
  and ad hoc files in `/home`, which left the ontology layer and the plan
  layer with different homes. The project needed a repo-owned `specs/`
  subdirectory so maintainers could treat plans as first-class artifacts
  alongside `ontology/` and `doc/`.
- **status**: completed
- **log**:
  - created `/home/tools/ccbot/specs/` as the repo-owned spec corpus
  - moved the Codex adaptation plans and fast-agent companion spec into that
    directory
  - updated README/runtime-ontology references so the repo now points to the
    in-repo spec corpus rather than external `/home/ccbot-*.md` paths
- **files edited**:
  - [README.md](/home/tools/ccbot/README.md)
  - [runtime-ontology.md](/home/tools/ccbot/doc/runtime-ontology.md)
  - [specs/README.md](/home/tools/ccbot/specs/README.md)
  - [ccbot-codex-adaptation-plan.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan.md)
  - [ccbot-codex-adaptation-plan-2.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md)
  - [ccbot-codex-adaptation-plan-4.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md)
  - [ccbot-fast-agent-jsonl-spec.md](/home/tools/ccbot/specs/ccbot-fast-agent-jsonl-spec.md)
- **acceptance criteria**:
  - the repo contains a dedicated `specs/` entrypoint
  - maintainer docs point at the in-repo spec corpus
  - no external `/home/ccbot-*.md` path remains canonical
- **validation**:
  - doc/spec link checks reference `/home/tools/ccbot/specs/...`

### T67: Model Queued Follow-Up Messages As A Pending-Input Artifact

- **description**: `Queued follow-up messages` are neither current-turn output
  nor mutable technical status. They are previews of future input already
  queued behind the running turn. The ontology and delivery docs needed an
  explicit `pending input artifact`, and the status poller/message queue needed
  a matching mutable Telegram artifact rather than collapsing queued input into
  commentary or ignoring it entirely.
- **status**: completed
- **log**:
  - added a `pending input artifact` to the delivery ontology and runtime
    event/delivery docs
  - status polling now extracts queued follow-up preview from pane text and
    delivers it via a dedicated mutable artifact
  - queue cleanup now clears pending-input preview alongside status/commentary
  - commentary reuse switched to edit-in-place where possible instead of
    delete/recreate churn
  - parser now preserves queued follow-up text literally and strips only
    explicit Codex checkbox marker glyphs
- **files edited**:
  - [delivery-surface.md](/home/tools/ccbot/ontology/delivery-surface.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [README.md](/home/tools/ccbot/README.md)
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [status_polling.py](/home/tools/ccbot/src/ccbot/handlers/status_polling.py)
  - [cleanup.py](/home/tools/ccbot/src/ccbot/handlers/cleanup.py)
  - [terminal_parser.py](/home/tools/ccbot/src/ccbot/terminal_parser.py)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [test_pending_input_status_polling.py](/home/tools/ccbot/tests/ccbot/test_pending_input_status_polling.py)
  - [test_terminal_parser.py](/home/tools/ccbot/tests/ccbot/test_terminal_parser.py)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - queued follow-up messages are modeled as a distinct pending-input artifact
  - pending-input preview is not treated as current-turn pre-final output
  - commentary updates reuse the visible Telegram artifact when possible
  - pending-input preview preserves literal queued text apart from explicit UI markers
- **validation**:
  - parser/polling/queue tests cover queued follow-up extraction and edit-in-place reuse
  - docs/spec contracts name the pending-input artifact explicitly

### T68: Warning Artifact Dedup With Mutable Repeat Counter

- **description**: `warning` is a durable system artifact and must not flood a
  topic when the same warning repeats. Repeated warnings equal to the latest
  warning on the same control surface must reuse one bubble and expose an explicit
  repeat counter once the repetition cardinality is strictly greater than 2.
- **status**: completed
- **depends_on**: []
- **log**:
  - ontology normalized: warning is not a turn opener and not a technical
    status artifact; it is a durable system notice with latest-warning dedup
    semantics
  - message queue now routes `semantic_kind=warning` through dedicated
    warning processing with same-text dedup per topic
  - warning bubble now reuses the existing message for identical text and
    renders `×N` only when repetition cardinality is strictly greater than 2
  - warning state is cleared on worker shutdown to avoid stale carry-over
- **files edited**:
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [test_message_queue.py](/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py)
  - [delivery-surface.md](/home/tools/ccbot/ontology/delivery-surface.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
- **acceptance criteria**:
  - identical warning repeated on the same control surface does not emit a new bubble
  - when repeat count becomes `N > 2`, the warning bubble shows a visible
    bottom counter
  - a different warning text creates a new warning bubble and resets counter
- **validation**:
  - queue tests cover same-warning dedup and counter threshold behavior
  - `uv run --extra dev pytest -q tests/ccbot/handlers/test_message_queue.py`

### T69: External Codex Bind Without tmux

- **description**: Topic bind must support an explicit `external-thread`
  attachment for Codex persisted threads (`thread id` or exact `thread name`)
  even when there is no tmux window for that thread (for example, Codex VS Code
  plugin sessions). This bind is event-stream first and must feed Telegram
  delivery through the same runtime event contract.
- **status**: completed
- **depends_on**: [T68]
- **log**:
  - ontology normalized: bind target is not always a live terminal container;
    external thread bind is a first-class binding kind
  - topic binding model now supports `binding_scope=external` with persisted
    metadata (`runtime_kind`, `source_thread_id`, `file_path`, `read_only`)
  - `/bind <thread-name|id>` in Codex lane can now bind a topic directly to an
    external persisted thread without creating/reusing tmux
  - session monitor now includes external bindings in active replay-source
    resolution even when there is no live tmux window
- **files edited**:
  - [runtime.md](/home/tools/ccbot/ontology/runtime.md)
  - [session.py](/home/tools/ccbot/src/ccbot/session.py)
  - [session_monitor.py](/home/tools/ccbot/src/ccbot/session_monitor.py)
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [test_session.py](/home/tools/ccbot/tests/ccbot/test_session.py)
  - [test_state_migration.py](/home/tools/ccbot/tests/ccbot/test_state_migration.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
- **acceptance criteria**:
  - a topic can bind to a Codex persisted thread without creating/reusing tmux
    window
  - monitor/session routing resolves that binding to replay evidence and
    delivers events to Telegram
  - binding participates in stale-binding cleanup and persisted state reload
- **validation**:
  - session + monitor + bot contract tests cover external bind attach/delivery
  - `uv run --extra dev pytest -q tests/ccbot/test_session.py tests/ccbot/test_state_migration.py tests/ccbot/test_bot_contracts.py`

### T70: Read-Only Injection Guard For External Bind

- **description**: External bind does not imply command-injection capability.
  Telegram input to an external-bound topic must fail closed with explicit
  read-only warning when no live input plane is available.
- **status**: completed
- **depends_on**: [T69]
- **log**:
  - ontology normalized: event delivery capability and input injection
    capability are different modalities and must not be conflated
  - external-bound topics now fail closed on input injection attempts
    (`send_to_window`, `send_special_key_to_window`, `send_input_to_window`)
  - Telegram text sent into external bind mode returns explicit read-only
    warning with reattach hint instead of probing tmux
- **files edited**:
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [session.py](/home/tools/ccbot/src/ccbot/session.py)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [runtime-ontology.md](/home/tools/ccbot/doc/runtime-ontology.md)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
- **acceptance criteria**:
  - Telegram text in external bind mode does not attempt tmux send
  - user receives explicit read-only warning with next action hint
- **validation**:
  - bot contract tests cover read-only warning path
  - `uv run --extra dev pytest -q tests/ccbot/test_bot_contracts.py tests/ccbot/test_session.py`

### T71: Restore User-Echo Surface Contract

- **description**: `user_echo` must remain visible as `👤 ...` in compact mode
  for ordinary user turns. Internal scaffolds stay suppressible, but ordinary
  user echo must not regress silently.
- **status**: completed
- **depends_on**: [T69]
- **log**:
  - ontology normalized: user echo is a turn-opener semantic fact and a
    user-facing artifact unless explicitly classified as internal scaffold
  - compact-mode regression coverage now pins visible plain user echo delivery
    and prevents silent suppression regressions
  - hidden internal scaffold suppression remains intact while ordinary `👤 ...`
    user echoes stay visible
- **files edited**:
  - [bot.py](/home/tools/ccbot/src/ccbot/bot.py)
  - [test_bot_contracts.py](/home/tools/ccbot/tests/ccbot/test_bot_contracts.py)
- **acceptance criteria**:
  - visible user text keeps `👤` echo bubble in compact mode
  - hidden internal payload suppression still works
- **validation**:
  - compact-mode bot tests pin visible-vs-hidden user echo
  - `uv run --extra dev pytest -q tests/ccbot/test_bot_contracts.py`

### T72: Docs/Ontology/Spec Cohesion For New Binding And Warning Semantics

- **description**: Consolidate ontology and operator docs after T68-T71 so
  runtime nouns, delivery semantics, and bot UX match implementation.
- **status**: completed
- **depends_on**: [T68, T69, T70, T71]
- **log**:
  - planner tranche declared before implementation to keep ontology and code
    synchronized
  - ontology/docs now explicitly separate `tmux` bind from `external` bind
    and document read-only guard when injection plane is unavailable
  - delivery docs now formalize warning artifact dedup with mutable repeat
    counter semantics (`N > 2`)
  - doc contract tests now pin the new binding and warning terminology
- **files edited**:
  - [README.md](/home/tools/ccbot/README.md)
  - [runtime.md](/home/tools/ccbot/ontology/runtime.md)
  - [delivery-surface.md](/home/tools/ccbot/ontology/delivery-surface.md)
  - [README.md](/home/tools/ccbot/ontology/README.md)
  - [runtime-ontology.md](/home/tools/ccbot/doc/runtime-ontology.md)
  - [runtime-event-contract.md](/home/tools/ccbot/doc/runtime-event-contract.md)
  - [telegram-delivery-pipeline.md](/home/tools/ccbot/doc/telegram-delivery-pipeline.md)
  - [telegram-bot-features.md](/home/tools/ccbot/doc/telegram-bot-features.md)
  - [test_docs_contracts.py](/home/tools/ccbot/tests/ccbot/test_docs_contracts.py)
- **acceptance criteria**:
  - docs explicitly separate tmux-live bind vs external-thread bind
  - docs explicitly state read-only guard when injection plane is unavailable
  - docs capture warning dedup + mutable counter semantics
- **validation**:
  - docs contract tests pin the new terminology and guarantees
  - `uv run --extra dev pytest -q tests/ccbot/test_docs_contracts.py`

## Success Criteria

- Current Codex behavior remains green.
- The project has an explicit answer to whether legacy persisted field names are:
  - permanent compatibility shape
  - temporary migration debt
- The remaining vocabulary debt is reduced to known, justified compatibility
  boundaries and runtime-native adapter terms.

## Validation

Minimum validation for this tranche:

- relevant unit tests green
- migration/contract tests green
- docs/spec references consistent with the chosen schema strategy
- no regression in monitor restart behavior

## Recommendation

Recommended sequence:

1. `T41`
2. `T42`
3. `T44`
4. `T46`
5. `T43` if migration is chosen
6. `T45`
7. `T47`

Reason:

- first determine what is actually coupled
- then decide whether the persisted schema should move at all
- only then cut names and aliases

### T64: Human-Readable Telegram Runtime Surface And Delivery Audit

- **description**: Telegram must render Codex/OMX runtime facts as human
  artifacts rather than leaking raw transport. Tool calls, tool outputs,
  `omx_state.state_write`, `update_plan`, file changes, warnings, and
  `<hook_prompt>` all need stable semantic projections and a local delivery
  audit so future self-improvement can compare Telegram output with the Codex
  CLI surface.
- **status**: completed
- **log**:
  - mapped `<hook_prompt>` user-shaped rollout records to operator warning
    artifacts instead of `👤` user echo
  - summarized `omx_state.state_write` as mode/phase/active/iteration/task and
    snapshot path rather than raw JSON
  - widened command/tool previews toward a useful Codex-style twenty-line
    preview before truncation footer
  - added local JSONL Telegram delivery audit rows for send/edit attempts on
    content, warning, status, commentary, and plan artifacts
  - documented that `update_plan` owns a dedicated mutable plan artifact with
    the plan body, not a terse label
- **files edited**:
  - [codex_rollout.py](/home/tools/ccbot/src/ccbot/codex_rollout.py)
  - [response_builder.py](/home/tools/ccbot/src/ccbot/handlers/response_builder.py)
  - [message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py)
  - [delivery_audit.py](/home/tools/ccbot/src/ccbot/delivery_audit.py)
  - [config.py](/home/tools/ccbot/src/ccbot/config.py)
  - [test_codex_rollout.py](/home/tools/ccbot/tests/ccbot/test_codex_rollout.py)
  - [test_delivery_audit.py](/home/tools/ccbot/tests/ccbot/test_delivery_audit.py)
- **acceptance criteria**:
  - hook prompts are warning/operator artifacts, not user echoes
  - state writes are human-readable state summaries
  - useful command/tool previews are shown in fenced blocks with footer outside
  - Telegram delivery attempts are auditable locally without raw secret payloads
- **validation**:
  - focused rollout and audit tests cover hook prompt conversion, state-write
    summary, and JSONL audit row shape
