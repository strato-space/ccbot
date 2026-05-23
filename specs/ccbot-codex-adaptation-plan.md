# Plan: CCBot Re-Architecture For Codex tmux Operation

**Generated**: 2026-04-02

## Overview
Fork status:
- Source repo: `https://github.com/six-ddc/ccbot`
- Fork: `https://github.com/strato-space/ccbot`
- Local checkout: `/home/tools/ccbot`

Target outcome:
- Operate Codex from Telegram forum topics while keeping tmux as the live control surface.
- Support the core lane first: create, bind, monitor, send input, inspect history, resume.
- Keep `voice`, `task`, and the existing `ACP-module` out of scope for feature work and explicitly guarded from regression.

## Definitions
- **Telegram topic**: the user-facing control lane in Telegram.
- **Binding**: persisted association from topic to a live tmux window plus its runtime metadata.
- **tmux window**: the live terminal container.
- **Codex process**: the interactive `codex` CLI instance running inside a window.
- **Codex thread**: the persisted conversation identity that can be resumed later.
- **Live semantic stream**: the stream of semantically meaningful Codex events as they are observed by the monitor.
- **Persisted replay evidence**: the append-only JSONL evidence on disk under `~/.codex/sessions/.../rollout-*.jsonl` and related index rows.
- **Rollout log**: the Codex-specific persisted replay evidence artifact for this first plan.
- **Runtime adapter**: the code layer that maps runtime-specific launch, identity, input, and event semantics into generic bot behavior.
- **ACP-module**: the pre-existing `ccbot` product surface named `ACP`. In this document it is not the ACP protocol.

## Ontological Model
The plan uses this model and must not collapse these entities:

`Telegram control surface -> binding -> tmux window -> Codex process -> Codex thread -> rollout log`

Constraints:
- A window is not a thread.
- A live process is not identical to its persisted thread.
- A rollout log is persisted replay evidence emitted by the runtime process and indexed by the persisted thread, not the process or thread themselves.
- In this narrower Codex-only plan, the monitor tails the append-only rollout log directly, so the live semantic stream and persisted replay evidence are realized by the same artifact. They remain conceptually distinct even when operationally co-located.
- The bot sends input to the live process through tmux, but reads history and notifications from normalized rollout-log-derived events.
- Resume binds a new or reused live process to an existing thread; it does not restore the old process.

## Established Claims
- `ccbot` today is Claude-oriented in four hard ways:
  - launch semantics in [`tmux_manager.py`](/home/tools/ccbot/src/ccbot/tmux_manager.py)
  - persisted state and session lookup in [`session.py`](/home/tools/ccbot/src/ccbot/session.py)
  - monitor offsets in [`monitor_state.py`](/home/tools/ccbot/src/ccbot/monitor_state.py)
  - hook-based registration in [`hook.py`](/home/tools/ccbot/src/ccbot/hook.py)
- Codex local storage already distinguishes persisted threads and rollout files:
  - thread persistence docs in [`sdk/typescript/README.md`](/home/tools/codex/sdk/typescript/README.md)
  - thread schema in [`Thread.ts`](/home/tools/codex/codex-rs/app-server-protocol/schema/typescript/v2/Thread.ts)
  - event taxonomy in [`ThreadItem.ts`](/home/tools/codex/codex-rs/app-server-protocol/schema/typescript/v2/ThreadItem.ts)
  - local operator tooling in [`codex-tools/README.md`](/home/tools/codex-tools/README.md)

## Working Hypotheses
- File-backed Codex integration using `session_index.jsonl` plus rollout JSONL files is sufficient for the first release.
- tmux pane inspection is sufficient for safe prompt detection in the core lane, but not yet proven for full remote approval control.
- Explicit launcher-side registration can replace the Claude `SessionStart` hook and should be the primary binding path.

## Prerequisites
- Access to `/home/tools/ccbot`
- Working `codex` binary on target hosts
- tmux installed and stable
- Telegram bot with forum topics enabled
- Access to `$HOME/.codex`

## Dependency Graph

```text
T1 ──┬── T2 ──┬── T3 ──┬── T4 ──┬── T6 ──┬── T9 ──┬── T12 ──┐
     │        │        │        │        │        │         │
     │        │        │        │        │        └── T13 ──┼── T16
     │        │        │        │        │                  │
     │        │        │        │        └── T10 ──┬────────┤
     │        │        │        │                  │        │
     │        │        │        └── T7 ──┬── T8 ───┘        │
     │        │        │                 │                  │
     │        │        └── T5 ───────────┘                  │
     │        │                                             │
     │        └─────────────────────────────── T11 ─────────┤
     │                                                      │
     └──────────────────────────────────────── T14 ── T15 ──┘
```

## Tasks

### T1: Capture Real Codex Evidence And Non-Root Fixtures
- **depends_on**: []
- **location**: `$HOME/.codex/`, `/home/tools/codex-tools/`, `/home/tools/ccbot/tests/fixtures/`, `/home/tools/ccbot/doc/`
- **description**: Capture redacted fixtures for the actual Codex objects in scope: thread metadata, rollout logs, session index rows, tmux pane snapshots, and resume behavior. Use both root-owned and non-root-shaped paths in fixtures so the implementation does not accidentally hardcode `/root/.codex`.
- **validation**: Fixture corpus includes fresh thread, resumed thread, same-cwd multiple threads, stale index entry, missing rollout file, interrupted turn, reasoning, command execution, tool call/output, and at least one prompt snapshot.
- **status**: Completed
- **log**:
  - Captured real Codex evidence from `~/.codex/session_index.jsonl`, `~/.codex/sessions/**/*.jsonl`, and a live pane in `tmux` session `0`.
  - Wrote a redacted fixture corpus for fresh thread, resumed thread, same-cwd ambiguity, stale index row, missing rollout reference, interrupted turn, reasoning, command execution, tool call/output, and a live prompt snapshot.
  - Added a maintainer note describing provenance, redaction policy, and the tmux/path-shape gotchas that later tasks must preserve.
  - Added a sanity test that validates coverage and file existence so later tasks can depend on these fixtures safely.
- **files edited/created**:
  - `/home/tools/ccbot/tests/fixtures/codex/manifest.json`
  - `/home/tools/ccbot/tests/fixtures/codex/session_index_rows.json`
  - `/home/tools/ccbot/tests/fixtures/codex/thread_metadata.json`
  - `/home/tools/ccbot/tests/fixtures/codex/monitor_state_missing_rollout.json`
  - `/home/tools/ccbot/tests/fixtures/codex/rollouts/fresh_home_thread.jsonl`
  - `/home/tools/ccbot/tests/fixtures/codex/rollouts/resumed_home_thread.jsonl`
  - `/home/tools/ccbot/tests/fixtures/codex/rollouts/nonroot_reasoning_turn.jsonl`
  - `/home/tools/ccbot/tests/fixtures/codex/rollouts/root_tool_call_and_output.jsonl`
  - `/home/tools/ccbot/tests/fixtures/codex/rollouts/interrupted_turn_nonroot.jsonl`
  - `/home/tools/ccbot/tests/fixtures/codex/panes/tmux_session_0_resume_prompt.json`
  - `/home/tools/ccbot/doc/codex-fixtures.md`
  - `/home/tools/ccbot/tests/ccbot/test_codex_fixtures.py`

### T2: Write The Runtime Ontology And Invariants Into The Fork
- **depends_on**: [T1]
- **location**: `/home/tools/ccbot/doc/`, `/home/tools/ccbot/README.md`
- **description**: Add a maintainer note that defines `binding`, `window`, `process`, `thread`, and `rollout log`, and states the non-collapsing invariants. This becomes the contract every implementation task follows.
- **validation**: The note explicitly states what the bot writes to, what it reads from, and which equalities are forbidden.
- **status**: Completed
- **log**:
  - Added a dedicated maintainer note that defines the runtime entities for the Codex adaptation and makes the write-path/read-path split explicit.
  - Declared forbidden equalities such as `window == thread` and `process == rollout log` so later implementation tasks cannot silently collapse these concepts back into a generic `session`.
  - Added a short README pointer so later tasks and operators have a single ontology source of truth inside the repo.
- **files edited/created**:
  - `/home/tools/ccbot/doc/runtime-ontology.md`
  - `/home/tools/ccbot/README.md`

### T3: Introduce Runtime-Neutral Core Types
- **depends_on**: [T2]
- **location**: `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/session_monitor.py`, `/home/tools/ccbot/src/ccbot/transcript_parser.py`, `/home/tools/ccbot/src/ccbot/` (new runtime modules)
- **description**: Replace Claude-shaped implicit nouns with explicit runtime-neutral types: binding, live process descriptor, thread locator, rollout source, normalized event, and input action. The core must stop speaking of a single generic `session` where different entities are involved.
- **validation**: Core modules compile and tests pass with type names and APIs that distinguish window/process/thread/log concerns.
- **status**: Completed
- **log**:
  - Added `runtime_types.py` with explicit core dataclasses for `TopicBinding`, `LiveProcessDescriptor`, `ThreadLocator`, `RolloutSource`, `NormalizedEvent`, and `InputAction`.
  - Refactored `session.py` to use runtime-neutral types internally and added compatibility wrappers such as `get_window_state`, `list_sessions_for_directory`, and `resolve_session_for_window` so the wider codebase can migrate incrementally.
  - Refactored `session_monitor.py` to monitor rollout sources and emit normalized events while preserving `SessionInfo` and `NewMessage` as aliases.
  - Rebased `transcript_parser.py` on normalized rollout events by aliasing parsed entries to the new event type and re-exporting `InputAction` for the upcoming runtime input driver work.
  - Added unit coverage for the new dataclasses and the new structured topic-binding surface on `SessionManager`.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/runtime_types.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/session_monitor.py`
  - `/home/tools/ccbot/src/ccbot/transcript_parser.py`
  - `/home/tools/ccbot/tests/ccbot/test_runtime_types.py`
  - `/home/tools/ccbot/tests/ccbot/test_session.py`
- **errors / gotchas**:
  - System `pytest` in this host does not automatically expose `src/` or install project dependencies, so direct validation failed on import resolution and missing `aiofiles`.
  - Validation succeeded via `uv run --extra dev`, which created the repo-local `.venv` and exercised the touched modules against the declared project dependencies without changing tracked files.
  - Review gate found that `runtime_kind` was introduced in the new core types but not yet preserved through state/session-map round-trips; this was fixed before accepting T3 so later mixed-runtime tasks do not start from false `claude` defaults.

### T4: Version Persisted State And Define Migration/Cutover
- **depends_on**: [T3]
- **location**: `/home/tools/ccbot/src/ccbot/config.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/monitor_state.py`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/tests/`
- **description**: Add schema versioning and `runtime_kind` namespacing to bot state. Define the cutover path for existing Claude-era `state.json`, `session_map.json`, and monitor offsets: dual-read plus explicit rollback, or one-time migration plus reversible backup. The migration must preserve existing topic bindings until Codex binding has been validated.
- **validation**: Tests load pre-migration Claude state, migrated mixed-runtime state, and rollback artifacts without losing thread/topic bindings or replaying old messages.
- **status**: Completed
- **log**:
  - Added a versioned persisted-state envelope with `schema_version` and `runtime_kind` for bot state, session maps, and monitor offsets.
  - Chose the one-time migration + reversible backup cutover path and kept legacy topic bindings intact until tmux validation can re-resolve them.
  - Updated the readers for legacy and versioned `session_map.json` so existing Claude-era bindings can be loaded without replaying old messages.
  - Verified the migration flow with focused pytest coverage and ruff on the touched files.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/state_schema.py`
  - `/home/tools/ccbot/src/ccbot/config.py`
  - `/home/tools/ccbot/src/ccbot/runtime_types.py`
  - `/home/tools/ccbot/src/ccbot/monitor_state.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/session_monitor.py`
  - `/home/tools/ccbot/src/ccbot/hook.py`
  - `/home/tools/ccbot/doc/state-migration.md`
  - `/home/tools/ccbot/tests/ccbot/test_state_migration.py`
- **errors / gotchas**:
  - The first migration test run exposed two gaps: legacy session maps were being wrapped without entry-level `runtime_kind`, and `LiveProcessDescriptor.to_dict()` still omitted the default runtime kind. Both were fixed before completion.
  - Review gate found that `MonitorState.load()` could still crash on a non-numeric `schema_version` because `ValueError` was not handled; this was fixed locally before accepting T4.

### T5: Freeze And Test Out-Of-Scope Compatibility Surface
- **depends_on**: [T3]
- **location**: `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/src/ccbot/transcribe.py`, `/home/tools/ccbot/src/ccbot/handlers/`, `/home/tools/ccbot/tests/`
- **description**: Identify the existing flows that must not change while Codex support is added: voice handling, photo forwarding, topic close/rename cleanup, raw passthrough commands, and any task/ACP-module-adjacent branching that already exists. Convert them into non-regression tests before feature work lands in shared modules.
- **validation**: Compatibility tests fail if a Codex refactor changes behavior in preserved out-of-scope paths.
- **status**: Completed
- **log**:
  - Added contract tests that freeze the preserved out-of-scope surfaces: bot handler registration, topic close cleanup, topic rename cleanup, photo forwarding, voice handling, and raw passthrough slash commands including `task`/`ACP-module`-adjacent cases.
  - Added a transcribe test fixture guard that strips host proxy environment variables so local `httpx` client creation stays deterministic in CI and on developer machines.
  - Restored `LiveProcessDescriptor.to_dict()` to preserve the legacy `claude` on-disk shape by omitting `runtime_kind` unless it is explicitly non-default, so the existing runtime type contract stays stable while the Codex migration progresses.
  - Full `tests/ccbot` suite passes after the contract tests were added.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/runtime_types.py`
  - `/home/tools/ccbot/tests/ccbot/test_forward_command.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_transcribe.py`
  - `/home/tools/ccbot/tests/ccbot/test_runtime_types.py`

### T6: Build The Codex Thread Catalog Adapter
- **depends_on**: [T1, T3, T4]
- **location**: `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/session_monitor.py`, `/home/tools/ccbot/src/ccbot/` (new `codex_threads.py` or equivalent), `/home/tools/ccbot/tests/`
- **description**: Implement the adapter that enumerates persisted Codex threads and their rollout files from `session_index.jsonl` and `~/.codex/sessions`. Resolve exact thread identity without collapsing it into a live process or window.
- **validation**: The adapter exposes deterministic thread candidates with precedence rules: explicit thread id > explicit launcher registration > exact normalized cwd > user-visible disambiguation. It never silently auto-selects among ambiguous same-cwd threads.
- **status**: Completed
- **log**:
  - Added `CodexThreadCatalog` with deterministic rollout-backed candidates, exact cwd normalization, explicit thread-id / launcher-registration precedence, and fail-closed ambiguity handling.
  - Wired `SessionManager.list_threads_for_directory()` and `resolve_thread_for_window()` through the new adapter so Codex threads are resolved without collapsing identity into a live tmux window.
  - Normalized tmux cwd comparisons in `SessionMonitor` through the same helper so the adapter and monitor agree on exact cwd matching.
  - Added fixture-backed tests for candidate enumeration, ambiguity handling, explicit-id precedence, missing-rollout rejection, and SessionManager integration.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/codex_threads.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/session_monitor.py`
  - `/home/tools/ccbot/tests/ccbot/test_codex_threads.py`
- **errors / gotchas**:
  - Review gate found a mixed-runtime regression: once any Codex candidates existed for a `cwd`, directory listing could silently hide legacy Claude threads for the same path. This was fixed locally by merging Codex candidates with legacy Claude transcripts using thread-id dedupe instead of early-returning.

### T7: Build The Codex Rollout Normalizer
- **depends_on**: [T1, T3]
- **location**: `/home/tools/ccbot/src/ccbot/transcript_parser.py`, `/home/tools/ccbot/src/ccbot/session_monitor.py`, `/home/tools/ccbot/src/ccbot/` (new `codex_rollout.py` or equivalent), `/home/tools/ccbot/tests/`
- **description**: Normalize Codex rollout JSONL records into generic bot events. Preserve the distinction between user message, assistant message, commentary, reasoning, command execution, tool call, tool output, file change, and lifecycle marker.
- **validation**: Fixture tests cover partial writes, corrupted lines, truncation/reset, offset repair, repeated reads without duplicate emission, and mixed event turns.
- **status**: Completed
- **log**:
  - Added `codex_rollout.py` to normalize Codex `session_meta`, `turn_context`, `response_item`, and `event_msg` records into generic `NormalizedEvent` values while preserving user/assistant/commentary/reasoning/tool/command/file/lifecycle distinctions.
  - Updated `session_monitor.py` to detect Codex rollout streams, repair offsets for partial/corrupt JSONL writes, and preserve lifecycle markers in the normalized stream while keeping them out of the Telegram callback path.
  - Added focused fixture tests for Codex taxonomy coverage plus monitor tests for partial writes, corrupted complete lines, truncation/reset, offset repair, and duplicate suppression on repeated reads.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/codex_rollout.py`
  - `/home/tools/ccbot/src/ccbot/session_monitor.py`
  - `/home/tools/ccbot/src/ccbot/transcript_parser.py`
  - `/home/tools/ccbot/tests/ccbot/test_codex_rollout.py`
  - `/home/tools/ccbot/tests/ccbot/test_session_monitor.py`
- **errors / gotchas**:
  - Existing Codex fixtures covered commentary, tool, reasoning, and lifecycle paths, but not a clean `assistant_message`; a synthetic mixed-turn record was added in tests to close that coverage gap without changing the fixture corpus.
  - Ontology review passed: the rollout normalizer preserves `rollout log -> normalized event` semantics and does not infer or overwrite the live `topic/window/process` binding from log evidence.

### T8: Build The Runtime Input Driver
- **depends_on**: [T3]
- **location**: `/home/tools/ccbot/src/ccbot/tmux_manager.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/src/ccbot/terminal_parser.py`, `/home/tools/ccbot/tests/`
- **description**: Replace Claude-specific send semantics with a runtime input driver for Codex. This driver owns text submit timing, multiline paste, raw slash commands, `Esc`, arrows, and shell-mode transitions.
- **validation**: Tests prove the bot can drive a Codex process reliably, not merely launch one. Unsupported controls must degrade to no-op or hidden UI, never to a false-success action.
- **status**: Completed
- **log**:
  - Added `input_driver.py` as the Codex-oriented input layer that owns submit timing, shell-transition splitting for `!`, multiline paste, raw slash-command dispatch, and explicit special-key handling.
  - Refactored `session.py` and `bot.py` to route text and control input through the driver instead of direct Claude-shaped `send_keys` calls, with unsupported controls failing closed.
  - Added conservative terminal-surface classification in `terminal_parser.py` for future prompt-safe input gating and added focused tests for driver semantics, session routing, bot contracts, and parser classification.
  - 2026-04-26 `server-np4` repair: multiline text submission now uses tmux
    paste-buffer payload delivery followed by a separate submit primitive, so
    paste-only success is not reported as a completed queued message. A later
    same-day live recurrence showed Codex may ignore post-paste `C-m`; the
    implementation now uses bare `Enter` for multiline post-paste turn opening
    and keeps `C-m` for single-line typed submits.
  - 2026-04-30 `str` repair: a 255-character multiline Telegram payload was
    pasted successfully but remained in the Codex composer until a later manual
    Enter. The first repair added a conservative delay; the follow-up replaced
    that heuristic with a 0.1s readiness gap plus bounded bare-Enter retries,
    and reports success only after the bound Codex rollout JSONL appends a
    turn-acceptance record.
  - 2026-04-26 `server-z2p` repair: Codex `update_plan` function calls now
    normalize to `plan_update` and render as a dedicated mutable Telegram plan
    artifact, updated only by newer plan events.
  - Validated with focused `pytest` and `ruff check` on the touched files.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/input_driver.py`
  - `/home/tools/ccbot/src/ccbot/tmux_manager.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/src/ccbot/terminal_parser.py`
  - `/home/tools/ccbot/tests/ccbot/test_input_driver.py`
  - `/home/tools/ccbot/tests/ccbot/test_session.py`
  - `/home/tools/ccbot/tests/ccbot/test_terminal_parser.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
- **errors / gotchas**:
  - Initial draft still referenced an old config default in `input_driver.py`; that was corrected before validation.
  - The repository already had parallel edits in `session_monitor.py` and `transcript_parser.py`; they were left untouched to avoid colliding with other workers.
  - Review gate confirmed the active Telegram control path now routes through the runtime input driver instead of direct bot-level `tmux.send_keys(...)` calls; prompt-state gating remains intentionally deferred to `T12`.

### T9: Replace Hook-Coupled Binding With Explicit Process Registration
- **depends_on**: [T4, T6, T8]
- **location**: `/home/tools/ccbot/src/ccbot/hook.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/src/ccbot/` (new launcher/registration module), `/home/tools/ccbot/tests/`
- **description**: Remove Claude `SessionStart` as the source of truth for Codex binding. Introduce explicit launcher-side registration for the live process and attach it to a window first, then to a thread. Heuristics are fallback only and must fail closed on ambiguity.
- **validation**: Tests cover same-cwd parallel starts, delayed session index writes, stale historical threads, resume-vs-new races, and restart recovery without wrong-topic binding.
- **status**: Completed
- **log**:
  - Added launcher-side runtime detection and live-process registration so Codex windows are bound at window creation time instead of waiting for the Claude hook path.
  - Reworked window-to-thread resolution and monitor binding load so the active mapping now resolves through `topic binding -> live process descriptor -> persisted thread`, with ambiguity remaining fail-closed.
  - Added Codex registration tests for same-cwd parallel starts, delayed index writes, explicit resume thread selection, stale state reset on reused window IDs, and monitor recovery from persisted registration.
  - Post-implementation review caught three real defects and they were fixed before sign-off: legacy Claude direct lookup was still scanning the Codex catalog, wrapped launcher commands like `env FOO=1 codex` were misclassified as Claude, and reused tmux window IDs were retaining stale `thread_id/registered_at` state across registrations.
  - Validation: `uv run --extra dev python -m pytest -q tests/ccbot/test_codex_threads.py tests/ccbot/test_state_migration.py tests/ccbot/test_bot_contracts.py tests/ccbot/test_session.py` (`59 passed`), `uv run --extra dev ruff check src/ccbot/launcher_registration.py src/ccbot/runtime_types.py src/ccbot/codex_threads.py src/ccbot/session.py src/ccbot/session_monitor.py src/ccbot/bot.py src/ccbot/hook.py tests/ccbot/test_codex_threads.py tests/ccbot/test_state_migration.py tests/ccbot/test_session.py tests/ccbot/test_bot_contracts.py`.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/launcher_registration.py`
  - `/home/tools/ccbot/src/ccbot/runtime_types.py`
  - `/home/tools/ccbot/src/ccbot/codex_threads.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/session_monitor.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/src/ccbot/hook.py`
  - `/home/tools/ccbot/tests/ccbot/test_codex_threads.py`
  - `/home/tools/ccbot/tests/ccbot/test_state_migration.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`

### T10: Rework Topic Browser And Resume UX Around Threads
- **depends_on**: [T6, T9]
- **location**: `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/src/ccbot/handlers/directory_browser.py`, `/home/tools/ccbot/src/ccbot/handlers/callback_data.py`, `/home/tools/ccbot/tests/`
- **description**: Make the Telegram browser and picker speak of Codex threads explicitly, not generic sessions. New window creation creates a live process; resume selects a persisted thread and binds a new or reused process to it.
- **validation**: The UI distinguishes fresh thread creation from thread resume, surfaces ambiguity instead of guessing, and binds the selected topic to the correct live window/process/thread chain.
- **status**: Completed
- **log**:
  - Renamed the picker surface from generic “session” language to explicit “thread” language while keeping callback payloads backward-compatible through aliases.
  - Switched directory-confirm flow to call `list_threads_for_directory()` directly, cache persisted thread candidates under thread-specific state keys, and route resume actions by `thread.thread_id` instead of legacy `session_id` naming.
  - Updated picker copy and success messages so the UI now distinguishes “Fresh Thread” from “Resume Existing Thread” and makes it clear when a topic is bound to a live window versus resumed onto a persisted thread.
  - Added focused contract tests for directory-confirm -> thread-picker transition, resume selection, fresh-thread selection, and thread-oriented picker labels.
  - Post-implementation review found no remaining behavioral regressions after validation; ontology remained intact because the picker still selects persisted threads while the actual topic binding continues to target the live tmux window/process chain.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/src/ccbot/handlers/directory_browser.py`
  - `/home/tools/ccbot/src/ccbot/handlers/callback_data.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_directory_browser.py`
- **errors / gotchas**:
  - The initial callback contract test accidentally asserted against `_selected_path`, but the real confirm flow sources the directory from `BROWSE_PATH_KEY`; the test was corrected to mirror the actual browser state machine instead of the post-picker state.
  - Backward-compatible aliases for `CB_SESSION_*`, `STATE_SELECTING_SESSION`, `SESSIONS_KEY`, and `build_session_picker()` were kept intentionally so downstream surfaces can migrate incrementally without breaking older callback payloads or imports.

### T11: Rebuild History And Notification Read Paths From Rollout Evidence
- **depends_on**: [T6, T7]
- **location**: `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/handlers/history.py`, `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`, `/home/tools/ccbot/src/ccbot/handlers/message_sender.py`, `/home/tools/ccbot/tests/`
- **description**: Rebuild `/history` and outbound Telegram notifications so they read normalized rollout evidence, not a Claude-shaped transcript abstraction. History is about the thread’s persisted event trail; notification routing is about the currently bound live process/window.
- **validation**: Tests prove stable pagination, no duplicate notifications after restart, correct routing by current binding, and sensible summaries for commentary, reasoning, commands, and tool output.
- **status**: Completed
- **log**:
  - Updated `SessionManager.get_recent_messages()` to recognize Codex rollout records and normalize them through `parse_codex_rollout_entries()` instead of forcing them back through the legacy Claude transcript parser.
  - Reworked history and live-response formatting so commentary, reasoning, command execution, tool calls, tool output, and file changes each render with a stable Telegram-facing prefix while preserving full text in `/history`.
  - Tightened queue merge semantics so content tasks only merge when both the topic and content type match, preventing commentary/reasoning/command/tool/file-change events from being silently merged under the wrong label.
  - Added focused tests for Codex history parsing, richer response-builder prefixes, and message-queue merge invariants, then revalidated the combined T9/T10/T11 regression set.
  - Post-implementation review found and fixed three real defects before sign-off: Codex thread resolution was still returning a catalog candidate instead of a locator on one path, lifecycle markers were leaking into `/history`, and user messages started paginating after the new formatter landed.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/handlers/history.py`
  - `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
  - `/home/tools/ccbot/tests/ccbot/test_session.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_response_builder.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py`
- **errors / gotchas**:
  - The queue merge path was more coupled to Claude-era assumptions than it looked: merging across content types caused prefixed Codex events to inherit the wrong label from the first item. Matching on content type and topic was required to keep the new ontology visible at the Telegram surface.
  - History parsing needed an explicit lifecycle filter even after Codex normalization, otherwise `session_meta` and other lifecycle-only markers surfaced as empty history rows. The read path now drops lifecycle markers while retaining real commentary/reasoning/tool/file events.

### T12: Add Safe Prompt Detection For The Core Lane
- **depends_on**: [T1, T8, T11]
- **location**: `/home/tools/ccbot/src/ccbot/terminal_parser.py`, `/home/tools/ccbot/src/ccbot/handlers/interactive_ui.py`, `/home/tools/ccbot/src/ccbot/handlers/status_polling.py`, `/home/tools/ccbot/tests/`
- **description**: Detect Codex TUI states that are necessary for safe remote operation in the first release: input-ready, busy, blocked on visible prompt, and unknown. Unknown states must degrade to read-only visibility with no misleading actions.
- **validation**: Fixture tests cover positive and negative prompt detection. Unsupported states never expose active buttons.
- **status**: Completed
- **log**:
  - Reworked terminal surface classification around the core-lane states `input_ready`, `busy`, `blocked_prompt`, and `unknown`, including a fail-closed `VisiblePromptError` path for prompt-visible Codex error banners.
  - Switched interactive prompt handling from active inline controls to read-only prompt snapshots for the core lane so unsupported prompt states never advertise remote actions before `T14`.
  - Updated poller and send paths to respect blocked prompts: status polling now tracks blocked prompt visibility explicitly, and `send_to_window()` plus bound-topic text forwarding fail closed when a visible prompt is already waiting in the terminal.
  - Added focused parser/UI/session/bot contract coverage for the new surface model, read-only prompt rendering, blocked-prompt send rejection, and read-only prompt surfacing for slash-command forwarding.
  - Post-implementation code review found one real gap and fixed it before sign-off: read-only prompt visibility initially existed only for `text_handler()`, while other send paths degraded to a generic error. Prompt surfacing is now centralized across slash commands, `/usage`, photo/voice sends, and pending-text forwarding. No remaining findings after the fix.
  - Ontology review passed because prompt-state detection remains a property of the visible terminal surface rather than a substitute for binding/process/thread identity.
  - Validation: `uv run --extra dev python -m pytest -q tests/ccbot/test_terminal_parser.py tests/ccbot/handlers/test_interactive_ui.py tests/ccbot/handlers/test_status_polling.py tests/ccbot/test_session.py tests/ccbot/test_bot_contracts.py tests/ccbot/test_forward_command.py` (`105 passed`), `uv run --extra dev ruff check src/ccbot/terminal_parser.py src/ccbot/handlers/interactive_ui.py src/ccbot/handlers/status_polling.py src/ccbot/session.py src/ccbot/bot.py tests/ccbot/test_terminal_parser.py tests/ccbot/handlers/test_interactive_ui.py tests/ccbot/handlers/test_status_polling.py tests/ccbot/test_session.py tests/ccbot/test_bot_contracts.py tests/ccbot/test_forward_command.py`.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/terminal_parser.py`
  - `/home/tools/ccbot/src/ccbot/handlers/interactive_ui.py`
  - `/home/tools/ccbot/src/ccbot/handlers/status_polling.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/tests/ccbot/test_terminal_parser.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_interactive_ui.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_status_polling.py`
  - `/home/tools/ccbot/tests/ccbot/test_session.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_forward_command.py`

### T13: Rationalize Telegram Command Surface Around The Core Lane
- **depends_on**: [T5, T8, T10, T11, T12]
- **location**: `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/README.md`, `/home/tools/ccbot/doc/telegram-bot-features.md`, `/home/tools/ccbot/tests/`
- **description**: Rebuild the bot command/menu surface so it truthfully advertises only the Codex-supported core lane. Remove or runtime-gate Claude-only assumptions and preserve raw passthrough only where it remains semantically valid for Codex.
- **validation**: Registered commands, help text, and forwarding behavior align with the actual supported Codex workflow and do not imply unsupported remote controls.
- **status**: Completed
- **log**:
  - Replaced the old Claude-oriented Telegram menu surface with a smaller Codex core-lane command set and updated bot copy to describe live tmux-window control instead of generic Claude sessions.
  - Runtime-gated `/usage` as a legacy Claude-only helper instead of silently rewriting it inside Codex windows, so the Telegram surface no longer claims a non-existent Codex command while still preserving Claude compatibility where that runtime survives.
  - Updated README and Telegram feature notes so the documented menu surface matches the actual supported Codex workflow and clearly separates documented support from best-effort raw slash passthrough.
  - Added contract coverage for the registered bot command list plus runtime-gated `/usage` behavior for Codex-vs-Claude windows.
  - Post-implementation code review caught one real defect before sign-off: the plan/docs/tests still described `/usage -> /status` after the runtime gate had landed. They were corrected locally so the release contract now matches the actual command behavior.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/README.md`
  - `/home/tools/ccbot/doc/telegram-bot-features.md`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`

### T14: Optional Lane For Remote Prompt/Approval Control
- **depends_on**: [T12]
- **location**: `/home/tools/ccbot/src/ccbot/handlers/interactive_ui.py`, `/home/tools/ccbot/src/ccbot/terminal_parser.py`, `/home/tools/ccbot/tests/`
- **description**: If needed after the core lane is stable, add remote prompt/approval actions for positively identified Codex states. This lane is explicitly optional and must not block shipping create/bind/monitor/send/history/resume.
- **validation**: Only positively identified supported prompts expose controls; all other states remain read-only.
- **status**: Completed
- **log**:
  - Added positive-identification support for real Codex overlays: exec approval, patch approval, approvals popup, model picker, and reasoning picker.
  - Re-enabled interactive controls only for those positively identified Codex prompt types while keeping `VisiblePromptError`, legacy read-only snapshots, and other unsupported prompt states in the read-only core lane.
  - Tightened prompt keyboard layouts so Codex overlays expose only the navigation keys they actually need instead of the broader Claude-era button surface.
  - Added focused parser/UI/poller coverage for actionable Codex prompts and retained read-only coverage for unsupported states.
  - Post-implementation code review caught one real UX defect before sign-off: actionable Codex prompts initially still displayed the read-only note even when a keyboard was shown. That mismatch was fixed locally before completion.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/terminal_parser.py`
  - `/home/tools/ccbot/src/ccbot/handlers/interactive_ui.py`
  - `/home/tools/ccbot/tests/ccbot/test_terminal_parser.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_interactive_ui.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_status_polling.py`

### T15: Package Strato Ops Docs, Cutover Notes, And Rollback Procedure
- **depends_on**: [T4, T5, T13]
- **location**: `/home/tools/ccbot/README.md`, `/home/tools/ccbot/README_RU.md`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/scripts/`
- **description**: Document the Strato operating path: config vars, `$HOME/.codex` expectations, tmux policy, launcher behavior, migration steps, rollback path, and the corrected operator tooling path `/home/tools/codex-tools/codex-session-scout`. State explicitly that `voice`, `task`, and the existing `ACP-module` are not part of this release scope.
- **validation**: A new operator can install, migrate, launch, and roll back using docs only.
- **status**: Completed
- **log**:
  - Added a Strato-specific Codex runbook that documents the live runtime chain, preflight checks, `~/.codex` expectations, tmux policy, one-time migration, rollback, and the non-scope boundary for `voice` / `task` / `ACP-module`.
  - Updated the English README to point operators at the new runbook, corrected installation URLs to the Strato fork, documented the legacy `CLAUDE_COMMAND` env var name honestly, and removed stale `ccmux`/Claude contribution references from the primary docs surface.
  - Replaced the outdated Russian README with a shorter Codex-first version that matches the fork's actual operating model and points to the runbook for cutover/rollback.
  - Added a docs contract test so future edits cannot silently remove the required operator details around `CLAUDE_COMMAND`, `~/.codex`, `*.v1.bak`, `codex-session-scout`, and the release-scope boundary.
  - Post-implementation code review caught two real doc defects before sign-off: README still claimed a default `CLAUDE_COMMAND=codex` even though the code default remains legacy `claude`, and the tail of the README still pointed at the old `six-ddc/ccmux` repo. Both were fixed locally before completion.
- **files edited/created**:
  - `/home/tools/ccbot/README.md`
  - `/home/tools/ccbot/README_RU.md`
  - `/home/tools/ccbot/doc/strato-ops-codex.md`
  - `/home/tools/ccbot/pyproject.toml`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`
  - Validation: `uv run --extra dev python -m pytest -q tests/ccbot/test_docs_contracts.py tests/ccbot/test_bot_contracts.py tests/ccbot/test_forward_command.py` (`32 passed`), `uv run --extra dev ruff check src/ccbot/bot.py tests/ccbot/test_bot_contracts.py tests/ccbot/test_forward_command.py tests/ccbot/test_docs_contracts.py`.

### T16: Live End-To-End Smoke And Release Gate
- **depends_on**: [T9, T10, T11, T13, T15]
- **location**: `/home/tools/ccbot/tests/integration/`, `/home/tools/ccbot/doc/`, manual smoke checklist
- **description**: Run the release gate for the core lane: create topic, pick directory, start Codex process in tmux, receive output, send follow-up input, inspect history, resume an existing thread, restart the bot, verify bindings/offsets survive, and verify preserved out-of-scope flows still behave unchanged.
- **validation**: Core lane smoke passes on a live host. Optional prompt-control smoke runs only if T14 is completed.
- **status**: Completed
- **log**:
  - Probed the live host and used the real installed runtime contract as the release gate: `codex-cli 0.119.0-alpha.3`, `tmux 3.4`, populated `~/.codex`, and a large real rollout corpus under `~/.codex/sessions`.
  - Fixed a launch-path regression that broke fresh Codex windows on this host: tmux `start_directory` alone was not reliable because shell init returned panes to `/home/strato-space`, so the launcher now sends an explicit `cd <selected_path> && <runtime>` command before starting the agent.
  - Added positive identification for the real Codex trust prompt observed in tmux and wired it into the blocked-prompt / interactive keyboard flow so fresh windows fail closed until the prompt is explicitly handled.
  - Reworked registration-time Codex thread resolution so live binding does not require a full catalog refresh on every new process registration; the session manager now tries explicit thread-id lookup, then a recent-registration fast path, before falling back to the expensive full refresh.
  - Fixed the final live blocker for resume: Codex on this host resumes via `codex resume <thread_id>`, not `codex --resume <thread_id>`. The launcher is now runtime-aware and preserves the legacy `--resume` form only for Claude-era runtimes.
  - Live smoke passed on the preserved temp state/session: fresh window `@12` in tmux session `ccbot_t16_1775163347_final` produced rollout-backed assistant output `T16_SMOKE_OK`; resumed window `@16` launched as `cd /tmp/ccbot-t16-final-6a6k4jvh/workspace && codex resume 019d4ffb-2e22-7130-94da-eea17191b557`, resolved back to `/root/.codex/sessions/2026/04/02/rollout-2026-04-02T23-55-55-019d4ffb-2e22-7130-94da-eea17191b557.jsonl`, and returned rollout-backed assistant output `T16_RESUME_OK`.
  - Preserved out-of-scope compatibility stayed green under regression tests covering docs, `/usage` runtime gating, forward command behavior, tmux/runtime wiring, rollout parsing, interactive prompt handling, and session/history logic.
  - Post-implementation code review was run after the live fix; no remaining defects were accepted into the plan log. Validation: `uv run --extra dev python -m pytest -q tests/ccbot/test_tmux_manager.py tests/ccbot/test_codex_threads.py tests/ccbot/test_terminal_parser.py tests/ccbot/handlers/test_interactive_ui.py tests/ccbot/test_session.py` (`102 passed`), `uv run --extra dev python -m pytest -q tests/ccbot/test_docs_contracts.py tests/ccbot/test_bot_contracts.py tests/ccbot/test_forward_command.py tests/ccbot/test_tmux_manager.py tests/ccbot/test_codex_threads.py tests/ccbot/test_terminal_parser.py tests/ccbot/handlers/test_interactive_ui.py tests/ccbot/test_session.py` (`134 passed`), and `uv run --extra dev ruff check src/ccbot/bot.py src/ccbot/codex_threads.py src/ccbot/session.py src/ccbot/tmux_manager.py src/ccbot/terminal_parser.py src/ccbot/handlers/interactive_ui.py tests/ccbot/test_docs_contracts.py tests/ccbot/test_bot_contracts.py tests/ccbot/test_forward_command.py tests/ccbot/test_tmux_manager.py tests/ccbot/test_codex_threads.py tests/ccbot/test_terminal_parser.py tests/ccbot/handlers/test_interactive_ui.py tests/ccbot/test_session.py`.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/codex_threads.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/tmux_manager.py`
  - `/home/tools/ccbot/src/ccbot/terminal_parser.py`
  - `/home/tools/ccbot/src/ccbot/handlers/interactive_ui.py`
  - `/home/tools/ccbot/tests/ccbot/test_codex_threads.py`
  - `/home/tools/ccbot/tests/ccbot/test_tmux_manager.py`
  - `/home/tools/ccbot/tests/ccbot/test_terminal_parser.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_interactive_ui.py`

## Parallel Execution Groups

| Wave | Tasks | Can Start When |
|------|-------|----------------|
| 1 | T1 | Immediately |
| 2 | T2, T3 | T1 complete for T2; T2 complete for T3 |
| 3 | T4, T5, T7, T8 | Their direct dependencies complete |
| 4 | T6 | T1, T3, T4 complete |
| 5 | T9, T11 | Their direct dependencies complete |
| 6 | T10, T12 | Their direct dependencies complete |
| 7 | T13, T14 | Their direct dependencies complete |
| 8 | T15 | T4, T5, T13 complete |
| 9 | T16 | T9, T10, T11, T13, T15 complete |

## Testing Strategy
- Build the release on fixture evidence first, not on live manual testing.
- Keep three test layers separate:
- ontology/state tests for binding/process/thread/log separation
- rollout normalization and history tests
- Telegram/tmux integration tests
- Treat out-of-scope compatibility as a hard gate, not as a final sanity check.
- Run live smoke only after migration, binding, and read-path semantics are green in automation.

## Risks & Mitigations
- **Risk**: The plan regresses into conflating thread, process, and rollout log.
- **Mitigation**: Keep the ontology note in-repo and require every adapter API to name which entity it operates on.

- **Risk**: Session index and rollout files disagree or lag.
- **Mitigation**: Use explicit launcher registration as primary truth for live binding and fail closed on ambiguous thread resolution.

- **Risk**: tmux pane heuristics are mistaken for authoritative runtime state.
- **Mitigation**: Use pane parsing only for UI hints and safe prompt classification, never as the sole identity source.

- **Risk**: Cutover breaks existing Claude-era bindings and unread offsets.
- **Mitigation**: Version state, add rollback, and keep migration explicit and test-covered.

- **Risk**: Shared-module refactors accidentally alter `voice`, `task`, or `ACP-module`-adjacent behavior.
- **Mitigation**: Freeze those paths under non-regression tests before Codex feature work lands.
