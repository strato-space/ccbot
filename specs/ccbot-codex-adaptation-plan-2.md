# Plan: Multi-Runtime Telegram Topic Control

**Generated**: 2026-04-03  
**Branch**: `feat/multi-runtime-topic-control`  
**Continuation Of**: [ccbot-codex-adaptation-plan.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan.md)

## Overview

This plan extends the completed Codex-only ontology migration into a true multi-runtime control plane:

- Operate `Claude Code`, `Codex`, `fast-agent`, and later similar runtimes from Telegram forum topics
- Keep `tmux` as the live operator surface
- Keep runtime `stdin/stdout` reserved for the human-observable CLI, not for machine transport
- Standardize on an ACP-equivalent event model while rejecting ACP-first transport over agent stdio
- Restore Claude-era progress and final-result delivery to Telegram on top of the new ontology
- Fix topic binding semantics so explicit `/unbind` disables future implicit bind until explicit `/bind`
- Add `/rename` and `/resume <name|id>` as capability-aware commands instead of pretending every runtime supports the same persisted identity model

The first plan already established the critical separation:

`Telegram topic -> binding -> tmux window -> runtime process -> persisted conversation identity`

This continuation makes explicit the further split between:

- live semantic stream
- persisted replay evidence

This continuation keeps that ontology but generalizes `Codex thread` into a runtime-specific persisted identity:

- Claude Code session
- Codex thread
- fast-agent session identity (`session_id` plus user-facing title metadata)

The plan must not regress shared surfaces for `voice`, `task`, or the existing `ACP-module`.

## Prerequisites

- Previous plan tasks `T1-T16` are complete
- Working tree and implementation branch: `/home/tools/ccbot` on `feat/multi-runtime-topic-control`
- Upstream reference checkout available at `/home/tools/ccbot-upstream`
- Upstream reference available at `upstream/main`
- Local runtime research sources available:
  - `/home/tools/ccbot`
  - `/home/tools/ccbot-upstream`
  - `/home/tools/codex`
  - `/home/tools/codex-tools`
  - `/home/tools/fast-agent`
- Official reference used during planning:
  - fast-agent docs at `https://fast-agent.ai`

## Definitions

This document is production-isolated, so the core nouns are repeated here instead
of assuming the reader has the first plan open.

- **Telegram topic**
  - The user-facing control lane in Telegram.
  - Commands, screenshots, progress notices, and final results are shown here.

- **Topic control policy**
  - The persisted control rule attached to a Telegram topic that governs whether
    plain messages may trigger implicit bind or instead require explicit bind.
  - This is a normative routing guard, not a live runtime object and not a binding state.

- **Binding**
  - The bot's persisted association from a Telegram topic to a live tmux window,
    together with the runtime metadata needed to route input and notifications safely.

- **tmux window**
  - The live terminal container managed by the bot.
  - It hosts one active runtime process at a time.

- **Runtime process**
  - The live interactive CLI process running inside the tmux window.
  - Examples: `claude`, `codex`, `fast-agent`.

- **Runtime conversation identity**
  - The persisted conversation object that can later be resumed.
  - Examples:
    - Claude Code session
    - Codex thread
    - fast-agent session id, with optional title metadata layered on top of it

- **Semantic emitter / supervisor**
  - The runtime-side or wrapper-side layer that emits machine-readable semantic
    events without taking over the live human CLI stdio.
  - Depending on the runtime, this may be:
    - native runtime emission
    - a launcher-side wrapper
    - a sidecar/supervisor

- **Live semantic stream**
  - The semantically meaningful machine-readable event stream observed while the
    runtime is active.
  - Examples:
    - Claude transcript tail / SDK stream as consumed live
    - Codex rollout tail as consumed live
    - fast-agent ACP-equivalent side-channel updates

- **Persisted replay evidence**
  - The append-only or otherwise replayable persisted evidence used for restart
    recovery, history reconstruction, and deterministic testing.
  - Examples:
    - Claude transcript / SDK-backed persisted evidence
    - Codex rollout JSONL and session index
    - fast-agent `acp_log.jsonl`

- **ACP-equivalent event model**
  - A runtime-neutral semantic vocabulary shaped to preserve ACP-grade meaning
    for progress, commentary, tool lifecycle, blocked-input state, and final
    result delivery.
  - This document treats `ACP-protocol` primarily as the semantic reference
    model, not as a requirement to run machine transport over the live agent
    `stdin/stdout`.

- **ACP-protocol**
  - The agent protocol and semantic model used as the reference vocabulary for
    event meanings.

- **ACP-module**
  - The existing `ccbot` product surface named `ACP`.
  - This is a legacy product/module boundary, not the same thing as the
    `ACP-protocol`.

- **Terminal-surface observation**
  - A read of the visible live terminal surface, used only for prompt-state
    classification, screenshots, and fail-closed blocked-input detection.
  - This is not persisted runtime evidence and must not be treated as identity
    or history source.

- **Normalized event**
  - The bot's runtime-neutral event object derived from the live semantic
    stream, the persisted replay evidence, or both.
  - This is what the Telegram delivery layer consumes.

- **Runtime adapter**
  - The code layer that translates runtime-specific launch, identity, input,
    prompt-state, and evidence semantics into the bot's generic behavior.

- **Message channel**
  - A routed text-producing source that submits atomic messages into the
    runtime through the message plane.
  - Telegram is mandatory.
  - Human-routed text submission is an admissible second message channel when
    implemented through the same routing surface.
  - Direct raw `tmux` keystrokes are not message channels.

- **Operator control layer**
  - Raw human terminal actions outside the equal message channels.
  - Examples:
    - direct `tmux` attach
    - `Ctrl+C`
    - shell recovery
    - ad hoc terminal takeover

- **Routing mode**
  - The semantic handling mode for a message submitted by any equal message
    channel.
  - Initial required modes:
    - `queue`
    - `steer`

## Ontology

The first plan established the correct distinction between topic, window,
process, persisted identity, and log. That ontology remains valid here, but the
persisted identity is now runtime-dependent rather than Codex-only.

### Canonical model

The model must distinguish these relations:

- `Telegram topic --governed by topic control policy--> may or may not enter a binding flow`
- `binding -> tmux window -> runtime process`
- `runtime process -> binds to runtime conversation identity`
- `runtime process -> semantic emitter / supervisor`
- `semantic emitter / supervisor -> live semantic stream`
- `semantic emitter / supervisor -> persisted replay evidence`
- `runtime conversation identity scopes/indexes the live semantic stream and persisted replay evidence`
- `live semantic stream and/or persisted replay evidence -> normalized events`
- `normalized events -> Telegram notifications / history views`

This is more precise than a single flat chain because `topic control policy` is
not the same kind of thing as `window`, `process`, or `identity`. It governs
whether binding and routing are permitted; it is not itself a live runtime lane.

For some runtimes the live semantic stream and the persisted replay evidence may
be realized by the same append-only artifact. They remain conceptually distinct
even when operationally co-located.

### Human-first transport rule

The live runtime `stdin/stdout` belongs to the human-observable CLI surface
hosted in `tmux`.

Therefore this plan rejects an `ACP-first transport` design where
`ACP-protocol` transport itself
occupies the same `stdin/stdout` used by the interactive CLI. That design would
reduce or destroy:

- human observability of the true CLI surface
- live human injection into the same agent process
- operator takeover and recovery from the authoritative terminal surface
- equality between Telegram-submitted messages and human-submitted messages

The accepted architecture is:

- `tmux stdio CLI interface first`
- semantic transport second
- ACP-equivalent event model as the target meaning layer

This is not an anti-`ACP-protocol` decision. It is a decision that human observability,
injection, and control outrank protocol purity for the live execution surface.
If a runtime offers literal `ACP-protocol` transport only by taking over
`stdin/stdout`, that
surface is not acceptable as the primary `ccbot` operator model.

### Message-layer equality and routing semantics

Equal message channels, whenever implemented through the message plane:

- Telegram
- human text submission routed through the same atomic message surface

Rules:

- channels are equal at the message layer
- source does not affect priority
- mode affects routing semantics
- raw terminal control is a separate operator layer, not part of the equal
  message channels

Initial routing modes:

- `queue`
  - default mode
  - an atomic message is appended as the next turn when the runtime is busy
- `steer`
  - a message is intended to affect the current turn rather than merely wait
    behind it

This plan therefore separates:

- equal message submission
- raw operator intervention

That separation is required so that direct `tmux` attach remains possible
without pretending that raw keystrokes and bot messages are the same kind of
thing.

### Required topic control axes

Topic policy and binding state must be modeled as distinct axes.

`topic_policy`:

- `implicit_bind_allowed`
- `manual_bind_required`

`binding_state`:

- `none`
- `binding_in_progress`
- `bound`

Once a topic enters `manual_bind_required` via explicit `/unbind` or explicit
cancel of a bind flow, plain messages must not trigger implicit bind again.
Only explicit `/bind` or explicit `/resume` may re-enter a bind-capable flow.

### Write path

Allowed write target:

`Telegram topic, subject to topic policy, routes through current binding -> tmux window -> runtime process`

Not allowed:

- writing "to a runtime conversation identity"
- writing "to live semantic stream artifacts" as though they were command targets
- writing "to persisted replay evidence" as though it were a command target
- treating rollout/transcript/session-index files as command targets

### Read path

Allowed read path:

`runtime conversation identity scopes/indexes live semantic stream and persisted replay evidence -> normalized events -> Telegram notifications/history`

Not allowed:

- treating tmux pane capture as the primary persisted history source
- treating pane text as the sole source of identity resolution
- treating Telegram delivery state as the source of truth for progress lifecycle
- treating terminal-surface observation as if it were persisted runtime evidence

tmux pane capture may still be used for:

- screenshots
- prompt-state hints
- fail-closed blocked-input detection

### Forbidden equalities

These equalities are false and must remain false in code, docs, and tests:

- `topic == binding`
- `topic policy == binding`
- `topic policy == binding state`
- `topic == window`
- `window == runtime conversation identity`
- `runtime process == runtime conversation identity`
- `runtime process == live semantic stream`
- `runtime process == persisted replay evidence`
- `runtime conversation identity == live semantic stream`
- `runtime conversation identity == persisted replay evidence`
- `terminal-surface observation == live semantic stream`
- `terminal-surface observation == persisted replay evidence`
- `notification == persisted replay evidence`
- `equal message channel == operator control layer`
- `ACP-protocol transport over stdio == acceptable primary control plane`

### Operational invariants

- A topic may bind to at most one live tmux window at a time.
- A tmux window may host at most one active runtime process at a time.
- A live runtime process may be associated with at most one primary runtime conversation identity at a time.
- A runtime conversation identity may have multiple persisted replay artifacts over time.
- Resume attaches a new or reused live process to an existing persisted identity; it does not restore the prior live process.
- History is reconstructed from normalized events derived from the live semantic
  stream and/or persisted replay evidence, not from pane capture alone.
- Equal message channels are source-agnostic at the routing layer.
- Raw terminal control may preempt message routing without changing the rule
  that message channels themselves remain equal.

### Resolution policy

Runtime identity resolution must be deterministic and fail closed.

Preferred precedence:

1. Explicit id chosen by the operator
2. Explicit runtime-aware launcher or registration record
3. Exact normalized cwd match with a single valid candidate
4. User-visible disambiguation

Never do this:

- silently choose between multiple same-name or same-cwd candidates
- guess identity from pane text alone
- assume every runtime supports rename or resume in the same way

## Upstream Claude Parity

The upstream reference for Claude Code behavior is `/home/tools/ccbot-upstream`.

The new implementation must preserve, test, and prove parity for the Claude
topic-to-Telegram flow that already exists upstream:

- upstream transcript parsing of `thinking`, `tool_use`, `tool_result`, and final assistant content
- upstream queue behavior that:
  - maintains a live status message
  - converts the status message into the first content message when possible
  - edits `tool_use` messages in place when `tool_result` arrives
  - restores status after content delivery
- upstream topic delivery behavior where assistant progress and final result are posted into the forum topic chat

The fact that the current branch has a runtime-neutral pipeline does not relax
that requirement. The final version in `/home/tools/ccbot` must prove that
Claude Code handling from `/home/tools/ccbot-upstream` has not been lost.

### Planning-time observations to be revalidated by `T18` and `T18.2`

The upstream comparison has already established these facts:

- The core Telegram delivery machinery in `handlers/message_queue.py` is still materially aligned with upstream for:
  - live status message handling
  - status-to-content conversion
  - `tool_use` / `tool_result` edit-in-place behavior
  - post-content status restoration
- `handle_new_message()` remains materially aligned in its queueing semantics for upstream Claude-style progress and final-result delivery.
- The main risk of lost Claude behavior is not the queue worker itself, but the surrounding runtime-specific surfaces:
  - session/rollout discovery
  - runtime selection and command surface
  - blocked-prompt handling
  - proof that Claude evidence still reaches the generic event pipeline unchanged

Therefore the final implementation must test parity at the Claude adapter and
live-stream / replay-evidence ingestion boundary, not only at the queue worker.

### Planning-time fast-agent findings from `/home/tools/fast-agent`

The current upstream review has already established these facts:

- fast-agent has a real persisted session layer under `.fast-agent/sessions/` with:
  - stable session ids
  - rotating JSON history files per agent
  - metadata including a user-facing `title`
  - resume via `--resume` and session commands
- fast-agent history serialization preserves extended conversation structure, including:
  - `tool_calls`
  - `tool_results`
  - `channels`
  - reasoning-related channel data when emitted by the provider
- fast-agent live progress and streamed reasoning/content are not reducible to persisted session history alone:
  - live updates flow through stream listeners and progress events
  - tool progress is emitted through progress/event machinery
  - `ACP-protocol` streaming attaches to that runtime stream surface directly
  - a dedicated mirrored journal is the missing persisted replay surface
- Therefore:
  - `fast-agent session title` is not the same thing as persisted session id
  - `fast-agent session history` is not the same thing as live progress/result transport
- Current upstream fast-agent live semantic source is `ACP-protocol`
  `session/update`,
  not pane text and not session snapshots alone; the accepted integration
  target is ACP-equivalent side-channel semantics that preserve the same
  meaning without taking over live human stdio
- The accepted target remains ACP-equivalent semantics on a side channel, not
  literal `ACP-protocol` transport on the same stdio surface as the live human CLI
  - `/resume` can be designed against a confirmed persisted identity surface
  - `/rename` must be capability-scoped, with session-title rename separated from any hypothetical session-id rename

## Runtime Capability Matrix

The implementation must model capabilities explicitly before wiring commands.

| Runtime | Fresh launch | Resume existing identity | Rename tmux window | Rename persisted identity | Live semantic source | Persisted replay evidence |
|---|---|---|---|---|---|---|
| Claude Code | Yes | Yes | Yes | Likely limited / probe required | transcript / SDK live consumption | transcript / SDK persisted evidence |
| Codex | Yes | Yes (`codex resume <id|thread-name>`) | Yes | Capability exists internally; public-safe integration must be proved | rollout tail as live stream | rollout JSONL + session index |
| fast-agent | Yes | Yes (`--resume` / session resume against persisted session id) | Yes | Session-title rename confirmed; persisted session-id rename not proved, so degraded mode required | ACP-equivalent side-channel stream | `acp_log.jsonl` |

Any cell not backed by a stable runtime surface must be implemented in degraded mode and documented as such.

## Dependency Graph

```text
T17 ──┬── T20 ── T20.1 ──┬── T27 ──┐
      │                  │         ├── T28 ──┐
T18 ──┼── T18.1 ─────────┼── T25 ──┼── T26 ──┼── T29 ── T30 ── T31
      │                  │         │         │
T19 ──┼── T21 ───────────┼── T22 ──┤         │
      │                  │         ├── T23 ──┤
      │                  │         └── T24 ──┘
      └──────────────────┘
```

## Tasks

### T17: Formalize Multi-Runtime Ontology Extension
- **depends_on**: []
- **location**: `/home/tools/ccbot/doc/runtime-ontology.md`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md`
- **description**: Extend the current Codex-focused ontology into a runtime-neutral model that explicitly defines topic policy state, runtime conversation identity, semantic emitter/supervisor, live semantic stream, persisted replay evidence, capability-scoped commands, human-first transport, equal message channels, and the separate operator control layer. Add forbidden equalities for `policy != binding`, `bind flow != message routing`, `status notification != content delivery`, and `literal ACP-protocol-over-stdio != acceptable primary control plane`.
- **validation**: Ontology note updated; command and state terms are normalized across docs; ontology-focused review finds no category mistakes in the central chain.
- **status**: Completed
- **log**:
  - Rewrote the ontology note into a runtime-neutral model with explicit policy, binding, runtime identity, semantic emitter/supervisor, live stream, replay evidence, equal message channels, and operator control layer distinctions.
  - Normalized the runtime/state vocabulary across the English, Russian, and Chinese docs plus the Strato runbook.
  - Added ontology contract tests to freeze the new core nouns and forbidden equalities.
- **files edited/created**:
  - `/home/tools/ccbot/doc/runtime-ontology.md`
  - `/home/tools/ccbot/README.md`
  - `/home/tools/ccbot/README_RU.md`
  - `/home/tools/ccbot/README_CN.md`
  - `/home/tools/ccbot/doc/state-migration.md`
  - `/home/tools/ccbot/doc/strato-ops-codex.md`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T18: Capture Evidence From Current Bot, Upstream Claude, Codex, and fast-agent
- **depends_on**: []
- **location**: `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`, `/home/tools/ccbot/src/ccbot/session_monitor.py`, `/home/tools/ccbot/src/ccbot/transcript_parser.py`, `upstream/main`, `/home/tools/codex`, `/home/tools/codex-tools`, `/home/tools/fast-agent`, `/home/tools/ccbot/doc/`
- **description**: Build an evidence note for implementation that records:
  - current regressions in progress/result delivery
  - current implicit bind behavior after `/unbind` and cancel
  - upstream Claude message/status/tool-update behavior
  - Codex resume/name/event surfaces
  - fast-agent identity/live-stream/replay surfaces
  - unsupported or risky operations that require degraded-mode behavior
- **validation**: Evidence note includes concrete file references and runtime capability findings; no plan task depends on undocumented assumptions after this note exists.
- **status**: Completed
- **log**: Captured source-backed evidence for current progress/result delivery gaps, implicit bind after `/unbind` and cancel, upstream Claude message/status/tool-update behavior, Codex resume/name/event surfaces, fast-agent identity/live-stream/replay surfaces, and degraded-mode risk gates.
- **files edited/created**: `/home/tools/ccbot/doc/implementation-evidence-t18.md`

### T18.2: Define Upstream Claude Parity Contracts
- **depends_on**: [T18]
- **location**: `/home/tools/ccbot-upstream/`, `/home/tools/ccbot/tests/fixtures/`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/tests/`
- **description**: Capture the Claude-only behavioral contract from `/home/tools/ccbot-upstream` that must survive the multi-runtime rewrite. At minimum, pin:
  - transcript parsing categories used for Telegram delivery
  - status-message lifecycle
  - tool-use/tool-result in-place editing behavior
  - final-result delivery into the topic chat
  - interactive-tool handling behavior that gates normal message delivery
  Convert those into parity fixtures and executable contract notes for the final implementation.
- **validation**: The plan contains a concrete, testable Claude parity baseline derived from `/home/tools/ccbot-upstream`, not from memory or informal summary.
- **status**: Completed
- **log**:
  - Added a dedicated Claude parity contract note and fixture corpus that pins the upstream Telegram delivery categories, status lifecycle, tool-use/tool-result in-place editing, final-result delivery, and blocked-prompt gate behavior.
  - Added an executable contract test that parses a minimal Claude transcript sample and classifies blocked prompt samples against the captured upstream baseline.
- **files edited/created**:
  - `/home/tools/ccbot/doc/claude-parity-contract.md`
  - `/home/tools/ccbot/tests/fixtures/claude/parity_contract.json`
  - `/home/tools/ccbot/tests/fixtures/claude/parity_transcript.jsonl`
  - `/home/tools/ccbot/tests/ccbot/test_claude_parity_contract.py`

### T19: Define Execution Review Gates
- **depends_on**: []
- **location**: `/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md`, `/home/tools/ccbot/tests/`, `/home/tools/ccbot/doc/`
- **description**: Add the mandatory execution policy for the implementation phase: every task ends with self-review, independent code review, and ontology review for any task that changes core nouns, state machines, or command semantics.
- **validation**: Plan explicitly requires post-implementation code review and ontology re-check for relevant tasks.
- **status**: Completed
- **log**:
  - Added an explicit execution-policy section to the plan and a doc note that names the required closeout gates.
  - Added a doc contract test to freeze the policy text.
- **files edited/created**:
  - `/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md`
  - `/home/tools/ccbot/doc/execution-review-policy.md`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T18.1: Build Cross-Runtime Fixture Corpus
- **depends_on**: [T18, T18.2]
- **location**: `/home/tools/ccbot/tests/fixtures/`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/tests/`
- **description**: Capture redacted, reproducible fixture corpora for Claude Code and fast-agent, matching the role Codex fixtures already play from the first plan. Include:
  - `live_semantic_stream` fixtures where the runtime exposes a distinct live stream
  - `persisted_replay_evidence` fixtures:
    - fresh launch metadata
    - resume cases
    - progress/status streams
    - tool-progress / tool-result transitions
    - degraded or failure cases needed for deterministic tests
  - `terminal_surface_observation` fixtures:
    - blocked-input / prompt-visible states
    - interactive prompt visibility states that must not be treated as persisted history
- **validation**: Test fixtures exist for all three runtimes and are sufficient to exercise normalization, delivery, and resume logic without depending on live sessions.
- **status**: Completed
- **log**:
  - Added a cross-runtime fixture corpus rooted at `tests/fixtures/cross_runtime` with manifest-driven coverage for Claude Code, Codex, and fast-agent.
  - Reused the existing Codex rollout fixtures, but added cross-runtime Codex launch metadata and prompt-observation records so `launch_metadata`, `blocked_input`, and `interactive_prompt_visible` remain independently testable.
  - Added a corpus note and executable fixture coverage test so normalization, delivery, and resume logic can be exercised without live sessions.
- **files edited/created**:
  - `/home/tools/ccbot/doc/cross-runtime-fixture-corpus.md`
  - `/home/tools/ccbot/tests/ccbot/test_cross_runtime_fixture_corpus.py`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/manifest.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/manifest.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/live_semantic_stream.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/persisted_replay_evidence/launch_metadata.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/persisted_replay_evidence/resume_case.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/persisted_replay_evidence/progress_status_stream.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/persisted_replay_evidence/tool_transitions.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/persisted_replay_evidence/degraded_failure_case.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/terminal_surface_observation/blocked_input.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/claude/terminal_surface_observation/interactive_prompt_visible.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/codex/manifest.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/codex/persisted_replay_evidence/launch_metadata.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/codex/terminal_surface_observation/blocked_input.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/codex/terminal_surface_observation/interactive_prompt_visible.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/manifest.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/live_semantic_stream.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/persisted_replay_evidence/launch_metadata.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/persisted_replay_evidence/resume_case.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/persisted_replay_evidence/session.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/persisted_replay_evidence/history_agent.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/persisted_replay_evidence/progress_status_stream.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/persisted_replay_evidence/tool_transitions.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/persisted_replay_evidence/degraded_failure_case.jsonl`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/terminal_surface_observation/blocked_input.json`
  - `/home/tools/ccbot/tests/fixtures/cross_runtime/fast-agent/terminal_surface_observation/interactive_prompt_visible.json`

### T20: Design Topic Control Policy State Machine
- **depends_on**: [T17, T18, T19]
- **location**: `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/state_schema.py`, `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/tests/`
- **description**: Specify the two-axis topic control model:
  - `topic_policy` distinguishes whether implicit bind is allowed or manual bind is required
  - `binding_state` distinguishes whether the topic currently has no binding, is in a bind flow, or is bound
  Define exact transitions for message handling, `/bind`, `/unbind`, picker cancel, stale binding cleanup, and topic reopen behavior without collapsing policy into binding state.
- **validation**: State chart and storage shape are documented; no transition re-enables implicit bind after explicit unbind/cancel without explicit `/bind`; policy and binding state remain distinct.
- **status**: Completed
- **log**:
  - Added persisted `topic_policies` and `topic_binding_states` axes to the state schema and session manager so policy and binding state remain distinct.
  - Added explicit `/bind` handling plus manual-bind guards for `/unbind`, picker cancel, and plain-message handling after explicit unbind/cancel.
  - Documented the state chart and storage shape in a dedicated topic-control state-machine note and covered the transitions with tests.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/state_schema.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/doc/topic-control-state-machine.md`
  - `/home/tools/ccbot/tests/ccbot/test_session.py`
  - `/home/tools/ccbot/tests/ccbot/test_state_migration.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`

### T20.1: Specify Topic Policy Migration And Backward Compatibility
- **depends_on**: [T20, T19]
- **location**: `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/state_schema.py`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/tests/`
- **description**: Define how persisted topic policy and binding state migrate from the current schema to the new model. Cover backward-compatible reads, forward writes, stale callback invalidation, and restart-safe bind-flow versioning/nonces so old picker buttons cannot reopen a binding after explicit `/unbind` or cancel.
- **validation**: Migration rules exist before bind semantics are implemented; callback invalidation behavior is deterministic across restart and stale UI interactions.
- **status**: Completed
- **log**:
  - Added persisted bind-flow version/nonce credentials to the topic state schema and session manager for restart-safe bind-flow invalidation.
  - Embedded bind-flow credentials into picker callback payloads and validated them fail-closed so stale buttons cannot reopen binding after restart, `/unbind`, or cancel.
  - Preserved safe `Cancel` recovery from callback-message topic context when unrelated traffic clears `_pending_thread_id`, so visible cancel buttons still exit bind flow without silently reopening binding.
  - Added a dedicated migration note plus tests for backward-compatible reads, forward writes, and stale callback rejection.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/state_schema.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/handlers/callback_data.py`
  - `/home/tools/ccbot/src/ccbot/handlers/directory_browser.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/doc/topic-policy-migration.md`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_directory_browser.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_session.py`
  - `/home/tools/ccbot/tests/ccbot/test_state_migration.py`

### T21: Introduce Runtime Capability Registry
- **depends_on**: [T17, T18, T19]
- **location**: `/home/tools/ccbot/src/ccbot/runtime_types.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/tmux_manager.py`, `/home/tools/ccbot/src/ccbot/input_driver.py`, `/home/tools/ccbot/src/ccbot/`
- **description**: Design a runtime registry that exposes capabilities instead of hard-coded Claude/Codex assumptions. At minimum it must define launch, resume, rename-tmux, rename-identity, live-stream-discovery, replay-evidence-discovery, progress-source, final-result-source, prompt-detection, blocked-input policy, message-routing-mode support (`queue` / `steer`), optional interactive-control support, and safe degraded-mode behavior. The registry must preserve `tmux stdio CLI-first` and must not require any runtime to yield its live stdio to machine transport.
- **validation**: Registry shape supports Claude Code, Codex, and fast-agent without forcing identical command semantics or input-surface behavior.
- **status**: Completed
- **log**: Added runtime capability registry with runtime-neutral launch/resume/rename/discovery/routing metadata; threaded capability-aware launch/input/blocking behavior through tmux, session, and input driver layers; preserved tmux stdio CLI-first and safe degraded modes.
- **files edited/created**: `/home/tools/ccbot/src/ccbot/runtime_types.py`, `/home/tools/ccbot/src/ccbot/launcher_registration.py`, `/home/tools/ccbot/src/ccbot/tmux_manager.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/input_driver.py`, `/home/tools/ccbot/doc/runtime-capabilities.md`, `/home/tools/ccbot/tests/ccbot/test_runtime_registry.py`, `/home/tools/ccbot/tests/ccbot/test_tmux_manager.py`, `/home/tools/ccbot/tests/ccbot/test_session.py`, `/home/tools/ccbot/tests/ccbot/test_input_driver.py`, `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T22: Restore Claude Code On The New Runtime Base
- **depends_on**: [T18.1, T18.2, T19, T21, T25]
- **location**: `/home/tools/ccbot/src/ccbot/launcher_registration.py`, `/home/tools/ccbot/src/ccbot/session_monitor.py`, `/home/tools/ccbot/src/ccbot/transcript_parser.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/handlers/`
- **description**: Reintroduce Claude Code as a first-class runtime adapter on the new ontology. That includes fresh launch, resume, input routing, persisted identity resolution, live semantic consumption, persisted replay evidence handling, and event normalization without collapsing back to the old `window=session` model.
- **validation**: Claude runtime flows fit the runtime registry; no Claude-specific path requires ontological shortcuts removed in the first plan; upstream Claude parity contracts remain satisfiable.
- **status**: Completed
- **log**:
  - Reframed the Claude adapter as a first-class tmux-first runtime profile with transcript-tail live semantics and transcript JSONL replay evidence.
  - Added executable Claude adapter coverage for capability registry, launch-command synthesis, and session-monitor replay consumption on the new ontology.
  - Updated Claude-facing documentation so the adapter explicitly names tmux as the live human control surface and keeps `SessionStart` hook registration as the persisted identity bridge.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/launcher_registration.py`
  - `/home/tools/ccbot/src/ccbot/session_monitor.py`
  - `/home/tools/ccbot/src/ccbot/transcript_parser.py`
  - `/home/tools/ccbot/doc/claude-runtime-adapter.md`
  - `/home/tools/ccbot/tests/ccbot/test_claude_runtime_adapter.py`
  - `/home/tools/ccbot/tests/ccbot/test_runtime_registry.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T23: Specify Codex Resume And Rename Semantics
- **depends_on**: [T18, T18.1, T19, T21]
- **location**: `/home/tools/ccbot/src/ccbot/codex_threads.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/tmux_manager.py`, `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/doc/`
- **description**: Define Codex-specific command behavior:
  - `/resume <codex-thread-name|id>` must create or reuse a tmux window and call `codex resume <name|id>`
  - `/rename` must rename the tmux window and only rename the persisted Codex thread through a stable public or proven-safe surface
  - duplicate names, name/id collisions, cross-directory matches, and runtime mismatches must fail closed with a picker or explicit error, never by silent guessing
  - if persisted thread rename cannot be made safe, use a documented degraded mode rather than direct file hacking
- **validation**: Codex command semantics are deterministic; ambiguity resolution and degraded mode are explicit where necessary.
- **status**: Completed
- **log**:
  - Added deterministic Codex thread resolution helpers for explicit `/resume` and `/rename` tokens, with exact id/name matching and fail-closed ambiguity handling for duplicate names and id/name collisions.
  - Added tmux-side `create_or_reuse_window()` semantics so exact window reuse remains safe, runtime mismatches fail closed, and reusable shell windows can still launch `codex resume <thread-id>`.
  - Documented Codex command semantics in a dedicated note and covered the exact-match, collision, degraded-rename, and reuse rules with focused tests.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/codex_threads.py`
  - `/home/tools/ccbot/src/ccbot/tmux_manager.py`
  - `/home/tools/ccbot/doc/codex-command-semantics.md`
  - `/home/tools/ccbot/tests/ccbot/test_codex_threads.py`
  - `/home/tools/ccbot/tests/ccbot/test_tmux_manager.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T24: Add fast-agent Runtime Adapter
- **depends_on**: [T18, T18.1, T19, T21, T25]
- **location**: `/home/tools/ccbot/src/ccbot/`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/tests/`
- **description**: Specify fast-agent integration for interactive tmux usage using the now-confirmed session surface:
  - persisted `session_id`
  - optional session title metadata
  - rotating JSON history files
  - resume via `--resume` and equivalent session commands where safe
  Define identity discovery, semantic-emitter placement, ACP-equivalent live semantic consumption, mirrored `acp_log.jsonl` replay, and title/session naming behavior. Explicitly distinguish session-title updates from any unsupported session-id rename, and define how `tmux` remains the live control surface even when fast-agent also has internal slash commands and `ACP-protocol` concepts.
- **validation**: fast-agent adapter design uses the confirmed session/live-stream/replay model; live read path is ACP-equivalent side-channel semantics, replay path is mirrored `acp_log.jsonl`, and rename semantics clearly separate tmux rename, title rename, and any unsupported id rename.
- **status**: Completed
- **log**:
  - Added a fast-agent session catalog adapter that discovers persisted sessions from `.fast-agent/sessions`, prefers `acp_log.jsonl` as replay evidence when present, and falls back to rotating history files otherwise.
  - Wired fast-agent discovery into `SessionManager` through an injectable catalog so multi-runtime directory listing and registration-time resolution remain deterministic and testable.
  - Added fast-agent adapter documentation and focused tests for session discovery, resume-by-title/id resolution, replay-file selection, and title-only rename semantics with explicit unsupported session-id rename.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/fast_agent_sessions.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/doc/fast-agent-runtime-adapter.md`
  - `/home/tools/ccbot/tests/ccbot/test_fast_agent_sessions.py`
  - `/home/tools/ccbot/tests/ccbot/test_runtime_registry.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T25: Define Runtime-Neutral Event Contract For Progress And Results
- **depends_on**: [T18, T18.1, T19, T21]
- **location**: `/home/tools/ccbot/src/ccbot/runtime_types.py`, `/home/tools/ccbot/src/ccbot/session_monitor.py`, `/home/tools/ccbot/src/ccbot/transcript_parser.py`, `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`, `/home/tools/ccbot/doc/`
- **description**: Specify the normalized event contract that all runtimes must emit for Telegram delivery:
  - user echo
  - commentary / thinking / reasoning
  - tool start / tool progress / tool result
  - command execution
  - final assistant content
  - lifecycle markers that must not pollute `/history`
  The contract must support Claude-style progress restoration, Codex rollout events, and fast-agent ACP-equivalent semantics with mirrored replay. The contract must also define enough state to support source-agnostic message routing in `queue` and `steer` modes without treating raw terminal control as just another semantic message channel.
- **validation**: Event taxonomy separates history-worthy content from ephemeral progress; Telegram renderer requirements are runtime-neutral; the contract does not require literal `ACP-protocol` transport ownership of runtime stdio.
- **status**: Completed
- **log**:
  - Added runtime-neutral semantic and delivery fields to `NormalizedEvent`, including `semantic_kind`, `delivery_class`, `include_in_history`, `dispatch_to_telegram`, and `status_message_eligible`.
  - Normalized Claude transcript parsing and monitor/session handling around the new event contract so lifecycle-only and tool-progress events do not pollute `/history` while progress remains Telegram-deliverable.
  - Added a dedicated runtime-event contract note and response-builder coverage for commentary, reasoning, tool progress, command execution, and assistant final delivery semantics.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/runtime_types.py`
  - `/home/tools/ccbot/src/ccbot/session_monitor.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/transcript_parser.py`
  - `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`
  - `/home/tools/ccbot/doc/runtime-event-contract.md`
  - `/home/tools/ccbot/tests/ccbot/test_runtime_types.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_response_builder.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T26: Rebuild Telegram Progress/Result Delivery Pipeline
- **depends_on**: [T18, T19, T20.1, T21, T25]
- **location**: `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`, `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`, `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/src/ccbot/session_monitor.py`, `/home/tools/ccbot/tests/ccbot/handlers/`
- **description**: Specify the repaired Telegram delivery behavior based on upstream Claude UX but reimplemented on the new ontology:
  - persistent live status/progress message
  - conversion of status message into first real content chunk when appropriate
  - final result delivery into the topic chat
  - tool progress/result editing rules
  - no leakage of lifecycle-only events into normal content or `/history`
  - strict separation between notification transport and bind flow state
  - source-agnostic message handling for equal message channels
  - correct interaction between `queue`, `steer`, blocked-input state, and raw operator takeover
  - teardown/error behavior for unbind during streaming, window death, topic close, Telegram edit failure, deleted status messages, and late events targeting stale bindings
- **validation**: Spec includes exact before/after behavior, error teardown rules, message ordering guarantees, and queue/steer interaction rules; upstream Claude behavior is preserved where still valid.
- **status**: Completed
- **log**:
  - Rebuilt the Telegram delivery pipeline around runtime-neutral progress/status handling so incomplete progress and explicit `tool_progress` events drive a mutable status artifact without breaking complete Claude-style content delivery.
  - Added stale-binding guards in the queue worker and teardown cleanup for status artifacts, so late events are dropped after unbind, topic close, or window death instead of leaking into stale topics.
  - Added a dedicated delivery-pipeline note plus focused contract tests for progress routing, stale delivery, and teardown semantics.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/handlers/cleanup.py`
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
  - `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`
  - `/home/tools/ccbot/doc/telegram-delivery-pipeline.md`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T27: Add Explicit `/bind` And Fix `/unbind` Semantics
- **depends_on**: [T19, T20, T20.1, T21]
- **location**: `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/handlers/directory_browser.py`, `/home/tools/ccbot/src/ccbot/handlers/callback_data.py`, `/home/tools/ccbot/tests/ccbot/handlers/`
- **description**: Specify a command surface where:
  - the first message in a fresh topic may trigger implicit bind
  - explicit `/unbind` moves `topic_policy` into `manual_bind_required` and clears live binding state
  - cancel inside bind flow may also move `topic_policy` into `manual_bind_required`
  - stale bind callbacks are invalidated after unbind, cancel, restart, or superseding bind flow
  - once `topic_policy=manual_bind_required`, future plain messages do nothing except explain that `/bind` is required
  - `/bind` explicitly enters the binding flow again
  - explicit `/resume` remains allowed from `topic_policy=manual_bind_required` because it is an intentional bind+launch operation, not implicit message routing
- **validation**: Message routing and bind routing are separate; common group chats are not polluted by accidental rebind attempts after explicit unbind.
- **status**: Completed
- **log**:
  - Preserved first-message implicit bind for fresh topics, but made explicit `/unbind` and bind-flow cancel push the topic into `manual_bind_required` so later plain messages do not restart binding implicitly.
  - Kept `/bind` as the explicit re-entry point into bind flow, and separated plain-message routing from bind-flow state so active picker flows and manual-bind topics fail closed with explanatory replies instead of accidental rebind attempts.
  - Added surface tests for bind-flow-active plain messages, explicit `/unbind` on bound and unbound topics, stale callback rejection, and safe cancel recovery from callback-message topic context.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/handlers/directory_browser.py`
  - `/home/tools/ccbot/src/ccbot/handlers/callback_data.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_session.py`

### T28: Specify `/rename` Window Naming And Synchronization Rules
- **depends_on**: [T22, T23, T24, T27]
- **location**: `/home/tools/ccbot/src/ccbot/tmux_manager.py`, `/home/tools/ccbot/src/ccbot/session.py`, `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/doc/`
- **description**: Define the naming policy for tmux windows and `/rename`:
  - initial window name derived from directory basename, with `-2`, `-3`, ... collision suffixes
  - explicit `/rename <new-name>` renames the tmux window
  - precedence between Telegram topic rename, explicit `/rename`, tmux display name, and persisted runtime identity is documented
  - runtime adapters may additionally rename persisted conversation identity when supported
  - if runtime rename is unsupported, bot must say exactly what changed and what did not
- **validation**: Rename outcomes are capability-aware and user-visible; collisions, topic-title interactions, and stale-name state are handled deterministically.
- **status**: Completed
- **log**:
  - Added deterministic tmux window suffix resolution and `/rename`-specific rename flow.
  - Synchronized Telegram topic renames with tmux display names and supported runtime title metadata.
  - Documented rename precedence and updated delivery docs for the new contract.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/tmux_manager.py`
  - `/home/tools/ccbot/src/ccbot/session.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/src/ccbot/fast_agent_sessions.py`
  - `/home/tools/ccbot/doc/codex-command-semantics.md`
  - `/home/tools/ccbot/doc/telegram-bot-features.md`
  - `/home/tools/ccbot/doc/telegram-delivery-pipeline.md`
  - `/home/tools/ccbot/tests/ccbot/test_tmux_manager.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_fast_agent_sessions.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T29: Rework Topic UX, Help, And Operator Feedback
- **depends_on**: [T19, T20.1, T26, T27, T28]
- **location**: `/home/tools/ccbot/src/ccbot/bot.py`, `/home/tools/ccbot/README.md`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/tests/`
- **description**: Update user-facing command/help flows for multi-runtime control:
  - `/bind`, `/unbind`, `/resume`, `/rename`
  - documented `queue` vs `steer` routing semantics where user-visible
  - status/error text for `topic_policy=manual_bind_required`
  - runtime-specific degraded-mode notices
  - concise confirmations for what was bound, resumed, renamed, or refused
- **validation**: Help and UI text no longer imply that every message auto-binds or every runtime supports identical rename/resume semantics; user-facing copy explains message-layer equality without implying that raw terminal control is queued like a normal message.
- **status**: Completed
- **log**:
  - Added a capability-aware `/resume` command surface that binds an unbound topic directly to a persisted Codex thread, while failing closed with explicit degraded-mode notices for Claude Code and fast-agent unbound-topic resume.
  - Reworked help and UI text around `/bind`, `/unbind`, `/resume`, `/rename`, `manual_bind_required`, and `queue` vs `steer` so raw tmux control is no longer implied to be an equal queued message channel.
  - Repaired the shared launch helper so only Claude windows wait for `SessionStart` hook registration; fast-agent and Codex launches stay tmux-first without accidental Claude-hook assumptions.
  - Independent review: completed during the final T30/T31 review pass; no correctness findings beyond the missing Claude degraded-path contract that is now covered in `tests/ccbot/test_bot_contracts.py`.
- **files edited/created**:
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/README.md`
  - `/home/tools/ccbot/doc/telegram-bot-features.md`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T30: Build Multi-Runtime Regression And Review Matrix
- **depends_on**: [T18.1, T18.2, T19, T22, T23, T24, T26, T27, T28, T29]
- **location**: `/home/tools/ccbot/tests/`, `/home/tools/ccbot/doc/`, `/home/tools/ccbot/README.md`
- **description**: Define the implementation verification matrix:
  - per-runtime launch/resume tests
  - bind/unbind/topic-policy tests
  - progress/result delivery tests
  - history pollution guards
  - rename behavior tests
  - topic-rename vs `/rename` precedence tests
  - stale callback invalidation tests
  - late-event / stale-binding delivery guards
  - explicit Claude parity tests against `/home/tools/ccbot-upstream` for progress and final-result delivery
  - queue/steer behavior tests with equal message channels
  - guards that raw operator control is never modeled as a normal queued message
  - non-regression tests for `voice`, `task`, `ACP-module`
  - required post-task code review gates
- **validation**: Test matrix covers the newly added state machine and all runtime capability branches, including degraded modes.
- **status**: Completed
- **log**:
  - Froze the multi-runtime verification surface in `doc/multi-runtime-regression-matrix.md` and linked it from the maintainer-facing README references.
  - Added contract coverage that the matrix contains the required per-runtime launch/resume, bind/topic-policy, progress/result, history, rename, stale-binding, queue/steer, raw-operator, and review-gate branches.
  - Extended command-surface and doc contracts so `/resume`, degraded runtime behavior, and the preserved `voice` / `task` / `ACP-module` boundary remain part of the frozen matrix.
  - Validation: `uv run --extra dev pytest /home/tools/ccbot/tests/ccbot/test_bot_contracts.py /home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py /home/tools/ccbot/tests/ccbot/test_claude_parity_contract.py /home/tools/ccbot/tests/ccbot/test_runtime_registry.py /home/tools/ccbot/tests/ccbot/test_claude_runtime_adapter.py /home/tools/ccbot/tests/ccbot/test_codex_threads.py /home/tools/ccbot/tests/ccbot/test_fast_agent_sessions.py /home/tools/ccbot/tests/ccbot/test_runtime_types.py /home/tools/ccbot/tests/ccbot/test_session_monitor.py /home/tools/ccbot/tests/ccbot/test_state_migration.py /home/tools/ccbot/tests/ccbot/test_input_driver.py /home/tools/ccbot/tests/ccbot/test_forward_command.py /home/tools/ccbot/tests/ccbot/test_docs_contracts.py` -> `123 passed`; `uv run --extra dev ruff check /home/tools/ccbot/src/ccbot /home/tools/ccbot/tests/ccbot /home/tools/ccbot/README.md /home/tools/ccbot/doc` -> clean.
- **files edited/created**:
  - `/home/tools/ccbot/README.md`
  - `/home/tools/ccbot/doc/multi-runtime-regression-matrix.md`
  - `/home/tools/ccbot/doc/telegram-bot-features.md`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

### T31: Plan Rollout, Migration, And Cutover
- **depends_on**: [T30]
- **location**: `/home/tools/ccbot/doc/`, `/home/tools/ccbot/README.md`, `/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md`
- **description**: Define rollout strategy for merging the new multi-runtime behavior into the current bot:
  - staged enablement of Claude restore and fast-agent support
  - operator instructions for runtime capability differences
  - fallback/cutover plan if one runtime path is not production-ready
- **validation**: Rollout plan allows partial enablement without silently changing semantics in production topics.
- **status**: Completed
- **log**:
  - Added a dedicated rollout note that keeps the current Codex runbook narrow, while defining staged Claude Code restore and fast-agent canaries around the actual single-lane `CLAUDE_COMMAND` deployment model in this codebase.
  - Documented runtime-specific capability differences, explicit partial-enable rules, and forbidden rollout moves so degraded `/resume` and rename semantics are not hidden behind a generic multi-runtime story.
  - Added a concrete rollout inventory for the current Ring 0 production bot plus reserved Claude/fast-agent canary lanes, explicit Ring 0 promotion gate, and pinned cutover/rollback checklists.
  - Added runtime-specific `/start` coverage for Claude Code and fast-agent degraded `/resume` semantics so canary operator messaging is testable rather than implied only by docs.
  - Added contract coverage that the staged rollout note exists, names the three rollout rings, requires explicit deploy-time lane switches, pins the current rollout inventory, and defines cutover, rollback, and `GO` / `NO GO` criteria for lane promotion.
  - Validation: `uv run --extra dev pytest /home/tools/ccbot/tests/ccbot/test_bot_contracts.py /home/tools/ccbot/tests/ccbot/test_docs_contracts.py` -> `58 passed`; wide regression slice -> `123 passed`; `ruff` clean.
  - Independent review: external review surfaced four T31 gaps (Ring 0 gate, rollout inventory, degraded `/start` coverage, cutover/rollback contract pinning); all four are now closed in the doc/code/test surface.
- **files edited/created**:
  - `/home/tools/ccbot/README.md`
  - `/home/tools/ccbot/doc/multi-runtime-rollout.md`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_docs_contracts.py`

## Parallel Execution Groups

| Wave | Tasks | Can Start When |
|------|-------|----------------|
| 1 | T17, T18, T19 | Immediately |
| 2 | T18.2, T20, T21 | T18 complete; T20/T21 also require T17 and T19 |
| 3 | T18.1, T20.1, T25 | T18.1 requires T18.2; T20.1 requires T20; T25 requires T18.1, T19, T21 |
| 4 | T22, T23, T24, T27 | T22 requires T18.2 and T25; T24 requires T25; T27 requires T20.1 and T21 |
| 5 | T26 | T20.1, T21, T25 complete |
| 6 | T28 | T22, T23, T24, T27 complete |
| 7 | T29 | T20.1, T26, T27, T28 complete |
| 8 | T30 | T18.2, T22, T23, T24, T26, T27, T28, T29 complete |
| 9 | T31 | T30 complete |

## Testing Strategy

- Preserve the runtime ontology as a testable contract, not only a prose note
- Add explicit state-machine tests for `topic_policy=implicit_bind_allowed` vs `topic_policy=manual_bind_required`
- Use per-runtime fixtures for Claude, Codex, and fast-agent live/replay semantics
- Use `/home/tools/ccbot-upstream` as the behavioral oracle for Claude progress/result delivery tests
- Verify progress/status/result delivery order at the queue layer
- Verify `/history` excludes lifecycle-only markers and ephemeral progress
- Verify `/resume` and `/rename` behavior separately for each runtime capability set
- Verify stale callback invalidation and restart-safe bind-flow nonces
- Verify late events cannot post into explicitly unbound or stale topics
- Keep `voice`, `task`, and `ACP-module` under non-regression coverage throughout
- Require post-implementation code review after every task, with ontology review for tasks touching core nouns and command semantics

## Execution Policy

Implementation tasks are not complete until all of the following are true:

- the task implementation is self-reviewed against the acceptance criteria
- an independent code review has been performed on the changed files
- ontology review has been re-run for any task that changes core nouns, state machines, or command semantics
- the plan task is updated in place with files edited, validation performed, and review notes

Canonical policy note:

- [`doc/execution-review-policy.md`](/home/tools/ccbot/doc/execution-review-policy.md)

If review finds a category mistake, hidden assumption, or semantic regression, the task must be repaired before it can be marked complete.

## Risks & Mitigations

- **Risk**: Rename semantics differ sharply by runtime and may tempt unsafe file mutation.
  - **Mitigation**: model rename as a capability; use degraded mode when public-safe rename is unavailable.

- **Risk**: Progress restoration reintroduces old `session == window` assumptions.
  - **Mitigation**: keep progress read path strictly tied to the live semantic stream, persisted replay evidence, and normalized events.

- **Risk**: Bind UX fixes accidentally disable legitimate first-message onboarding.
  - **Mitigation**: persist topic policy state explicitly and test first-message, cancel, unbind, and explicit `/bind` transitions separately.

- **Risk**: fast-agent adds `ACP-protocol` / session concepts that blur the tmux control boundary.
  - **Mitigation**: treat fast-agent internal session management as runtime identity, route live semantics through a separate semantic emitter/supervisor path, and keep tmux as the live write surface.

- **Risk**: Claude restore work regresses Codex behavior already stabilized in T1-T16.
  - **Mitigation**: keep Codex paths under regression tests and avoid renaming back to Claude-shaped types.
