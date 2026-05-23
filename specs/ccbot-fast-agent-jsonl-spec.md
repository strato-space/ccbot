# CCBot Fast-Agent Semantic Side-Channel Spec

Companion document to [ccbot-codex-adaptation-plan-2.md](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md).

## Purpose

Define the required `fast-agent` work so that `ccbot` can integrate it on a
semantically rigorous basis consistent with the now-established ontology:

- `tmux` remains the live human control surface
- the live CLI keeps ownership of runtime `stdin/stdout`
- ACP-equivalent semantics remain the meaning reference layer
- `acp_log.jsonl` is the persisted replay journal
- existing session history and existing `fastagent.jsonl` logger remain intact

This document replaces the earlier JSONL-primary framing. For `fast-agent`, the
correct model is:

`live semantic stream + persisted replay evidence, using an ACP-equivalent event model`

## Target Outcome

Operate `fast-agent` from Telegram control surfaces through `tmux`, while
reading live semantic events from a side-channel that preserves ACP-equivalent
meaning, and replaying them after restart from colocated append-only persisted
replay evidence in `acp_log.jsonl`.

## Non-Negotiable Rule

For `fast-agent`, the bot must follow this split:

- write path:
  - `Telegram control surface -> binding -> tmux window -> fast-agent process`
- live semantic read path:
  - `fast-agent process -> semantic emitter / supervisor -> live semantic stream -> normalized events -> Telegram`
- replay / recovery path:
  - `fast-agent session identity scopes/indexes persisted replay evidence -> normalized events -> Telegram/history recovery`

Not allowed:

- treating the visible `tmux` pane as the primary source of semantic events
- rebuilding progress/result delivery from terminal text alone
- treating session snapshot history JSON as the primary live progress transport
- treating literal `ACP-protocol` transport over agent stdio as the primary control surface
- treating semantic transport as an in-memory-only stream with no replayable persisted mirror

`tmux` pane capture may still be used for:

- screenshots
- blocked-input / prompt-state hints
- operator debugging

This model assumes `fast-agent` exposes a semantic emission surface equivalent
to `ACP-protocol` meaning. That surface may be implemented by:

- native runtime emission
- an ACP wrapper
- another side-channel that preserves the same meaning while leaving human
  `stdin/stdout` untouched

## Ontology

- **Telegram control surface**
  - User-facing control lane in Telegram.
  - Named forum topics are one species; no-topics main-chat mode is another.

- **Binding**
  - Persisted association from Telegram topic to one live `tmux` window.

- **tmux window**
  - Live terminal container that hosts one `fast-agent` process.

- **fast-agent process**
  - Interactive CLI process running in the window.

- **fast-agent session identity**
  - Persisted conversation identity used for resume.
  - Current upstream evidence shows this is the `session_id`.
  - Session `title` is mutable metadata layered on top of the `session_id`.

- **semantic emitter / supervisor**
  - The runtime-side or wrapper-side layer that emits machine-readable semantic
    events without taking over the live human CLI stdio.
  - Depending on deployment, this may be:
    - a native fast-agent ACP-facing component
    - a wrapper around that component
    - an external supervisor that preserves the same event meaning

- **live semantic stream**
  - The semantically meaningful side-channel emission surface whose events
    preserve `ACP-protocol`-grade meaning.
  - This is the live source for commentary, reasoning, tool lifecycle, status,
    and final-result delivery.
  - It does not imply that literal `ACP-protocol` transport owns the live CLI
    stdio.

- **persisted replay evidence**
  - The append-only or otherwise replayable persisted evidence used after
    consumer restart or bot reconnect.
  - In this spec, the canonical replay artifact is `acp_log.jsonl`.

- **`acp_log.jsonl`**
  - Append-only persisted replay evidence for one session, using ACP-equivalent
    event meaning.

- **session snapshot history**
  - Existing `fast-agent` persisted session history JSON.
  - Useful for resume and completed-turn inspection, but not the canonical live
    progress bus.

- **terminal-surface observation**
  - Visible pane text from `tmux`.
  - Useful for screenshots and blocked-input hints, but not equivalent to the
    live semantic stream or the persisted replay journal.

Forbidden equalities:

- `tmux window == fast-agent session identity`
- `fast-agent session title == fast-agent session id`
- `live semantic stream == tmux pane text`
- `live semantic stream == session snapshot history`
- `persisted replay evidence == session snapshot history`
- `terminal-surface observation == live semantic stream`
- `terminal-surface observation == acp_log.jsonl`
- `literal ACP-over-stdio == acceptable primary operator model`
- `fast-agent process == semantic emitter / supervisor`

## Why Literal ACP-First Is Rejected

`ccbot` is built around a stronger operational property:

- the agent lives in `tmux`
- the agent does not need to know about Telegram
- the operator may observe, inject, interrupt, and recover from the live CLI

If literal `ACP-protocol` transport takes over the same `stdin/stdout` as the interactive
CLI, then `tmux` ceases to be the authoritative operator intervention surface.
That would sacrifice:

- human observability of the true CLI
- live human injection into the same session
- manual interruption and recovery
- parity between bot-submitted and human-submitted messages at the message layer

Therefore the accepted priority order is:

1. `tmux stdio CLI interface first`
2. human observability, injection, and control
3. ACP-equivalent semantic model over a side-channel
4. persisted replay in `acp_log.jsonl`

This is not a rejection of `ACP-protocol` semantics. It is a rejection of
`ACP-protocol transport first` on the same stdio surface as the human CLI.

## Message-Layer Equality

The control model for `ccbot` is:

- channels are equal at the message layer
- source does not affect priority
- mode affects routing semantics
- raw terminal control is a separate operator layer, not part of the equal
  message channels

Equal message channels:

- Telegram
- human atomic text submission through the same routed message path, when that
  routed path is implemented

Separate operator layer:

- direct `tmux` attach
- `Ctrl+C`
- raw shell recovery
- ad hoc terminal takeover

This separation keeps the message model simple without pretending that raw
terminal control and queued/steered messages are the same kind of action.

## Current State Findings

These findings are based on the current upstream `fast-agent` checkout in
`/home/tools/fast-agent`, plus reference ACP adapters in
`/home/tools/codex-acp-upstream` and `/home/tools/claude-agent-acp-upstream`.

### Confirmed fast-agent session surface

- Persisted session ids exist in `.fast-agent/sessions/`.
- Session title metadata exists and is mutated separately from session id.
- Resume exists through `--resume` and session commands.
- Session history JSON preserves rich structure such as:
  - `tool_calls`
  - `tool_results`
  - `channels`

Concrete evidence:

- session title metadata write: [session_manager.py](/home/tools/fast-agent/src/fast_agent/session/session_manager.py#L474)
- session creation/id handling: [session_manager.py](/home/tools/fast-agent/src/fast_agent/session/session_manager.py#L520)
- resume by session id/history load: [session_manager.py](/home/tools/fast-agent/src/fast_agent/session/session_manager.py#L697)
- CLI `--resume`: [agent_setup.py](/home/tools/fast-agent/src/fast_agent/cli/runtime/agent_setup.py#L454)
- `/session title`: [sessions.py](/home/tools/fast-agent/src/fast_agent/commands/handlers/sessions.py#L404)
- extended JSON history preservation: [prompt_serialization.py](/home/tools/fast-agent/src/fast_agent/mcp/prompt_serialization.py#L102)

### Confirmed fast-agent ACP-protocol live stream surface

`fast-agent` already emits semantically meaningful ACP `session_update`
notifications for:

- session info
- streamed assistant text
- streamed reasoning
- tool call start / delta / completion
- mode updates
- available command updates
- slash-command responses
- history replay into ACP sessions

Concrete evidence:

- central ACP context wrapper: [acp_context.py](/home/tools/fast-agent/src/fast_agent/acp/acp_context.py#L418)
- streamed text / reasoning updates: [agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L2318)
- status-line update: [agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L1307)
- session history replay into ACP updates: [agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L1692)
- slash-command response into ACP update: [agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L2266)
- ACP tool progress manager: [tool_progress.py](/home/tools/fast-agent/src/fast_agent/acp/tool_progress.py#L92)

These findings confirm current upstream `ACP-protocol` emission semantics, but not the
architectural acceptability of running machine transport on the same stdio
surface as the live human CLI.

### Confirmed gap

The current upstream does not yet persist the live semantic stream as
session-colocated persisted replay evidence.

Today the semantic stream is live, but replay is split across:

- session history snapshots
- generic `fastagent.jsonl` logger
- optional diagnostics traces
- test-only or provider-specific stream captures

Concrete evidence:

- generic file logger setting: [config.py](/home/tools/fast-agent/src/fast_agent/config.py#L1080)
- generic JSONL file transport: [transport.py](/home/tools/fast-agent/src/fast_agent/core/logging/transport.py#L101)
- opt-in interactive trace helper: [interactive_diagnostics.py](/home/tools/fast-agent/src/fast_agent/ui/interactive_diagnostics.py#L15)
- replay-oriented JSONL utilities/tests: [test_event_replay.py](/home/tools/fast-agent/tests/unit/scripts/test_event_replay.py#L111)
- committed streaming fixture workflow: [README.md](/home/tools/fast-agent/tests/fixtures/llm_traces/README.md#L1)

The missing piece is dedicated persisted replay evidence tied to the session
identity that `ccbot` can consume deterministically.

## ACP-Protocol Reference Adapters And Precise Insertion Points

The external `ACP-protocol` adapters show that the correct insertion layer is
the semantic notification boundary, not terminal rendering.

### codex-acp

Precise insertion points exist at two levels:

- event-to-ACP translation:
  - [thread.rs](/home/tools/codex-acp-upstream/src/thread.rs#L947)
- central notification sender:
  - [thread.rs](/home/tools/codex-acp-upstream/src/thread.rs#L2454)

Why this matters:

- semantic events are translated from runtime-native events into ACP updates in
  `handle_event()`
- all updates ultimately pass through `SessionClient.send_notification()`

Architectural conclusion:

- mirroring `acp_log.jsonl` at the semantic notification sender is clean and exact
- no terminal scraping is required

### claude-agent-acp

Precise insertion points also exist here:

- content-to-ACP conversion:
  - [acp-agent.ts](/home/tools/claude-agent-acp-upstream/src/acp-agent.ts#L1835)
- stream-event-to-ACP conversion:
  - [acp-agent.ts](/home/tools/claude-agent-acp-upstream/src/acp-agent.ts#L2093)
- transport send sites:
  - [acp-agent.ts](/home/tools/claude-agent-acp-upstream/src/acp-agent.ts#L523)

Why this matters:

- Claude content is first normalized into ACP notifications
- those notifications are then sent to the client

Architectural conclusion:

- the clean journal point is either:
  - a tiny wrapper around `client.sessionUpdate(...)`, or
  - a helper that all ACP notification emission flows pass through

### Implication for fast-agent

`fast-agent` should follow the same principle:

- do not log from console rendering
- do not log from pane capture
- do not synthesize replay from status text
- mirror semantic updates at the emission boundary

## Decision

For `fast-agent`, the canonical live meaning layer is the live semantic stream
emitted by a semantic emitter / supervisor and shaped to preserve
`ACP-protocol` meaning.

`acp_log.jsonl` is the canonical persisted replay evidence for `ccbot`.

Existing artifacts keep their current roles:

- session history JSON:
  - resume and completed-turn recovery
- generic `fastagent.jsonl`:
  - internal runtime logging and diagnostics
- provider trace fixtures:
  - test and replay harnesses for provider internals

None of those replaces `acp_log.jsonl`.

## Required `acp_log.jsonl`

### Placement

Preferred location:

- `.fast-agent/sessions/<session_id>/acp_log.jsonl`

This keeps the replay journal colocated with the persisted session identity
without modifying or replacing existing session files.

### Enablement

The mirror must be opt-in and runtime-controlled.

Acceptable controls:

- env var such as `FAST_AGENT_ACP_LOG_JSONL=1`
- config setting under ACP or logger settings
- explicit CLI switch for ACP mode

If disabled:

- `ACP-protocol` transport may still function
- `ccbot` must treat fast-agent replay/progress restoration as unavailable or
  degraded
- no silent fallback to pane scraping

### Writer properties

- append-only
- line-delimited JSON
- stable schema versioning
- monotonic per-session sequence numbers
- safe under partial last-line write
- independent of terminal rendering
- does not modify legacy session history files
- does not require rewriting existing `fastagent.jsonl`

### Record envelope

Each line must be a single mirrored semantic emission with envelope fields such
as:

- `schema_version`
- `record_type: "semantic_update"`
- `session_id`
- `seq`
- `timestamp`
- `source`
- `protocol_ref: "acp.session/update"`
- `update_type`
- `update`

Recommended optional fields:

- `agent_name`
- `session_title`
- `cwd`
- `turn_hint`
- `transport_meta`
- `protocol_version`

### What is mirrored

The journal mirrors semantic updates shaped by the ACP-equivalent event model,
not pane text and not generic runtime
logs.

Minimum required mirrored semantic update kinds:

- `session_info_update`
- `agent_message_chunk`
- `agent_thought_chunk`
- `tool_call`
- `tool_call_update`
- `available_commands_update`
- `current_mode_update`

Optional but useful:

- explicit lifecycle/status markers when represented through the semantic model
- status-line metadata if already emitted through semantic meta

### Logging semantics

The journal records semantic emission intent, not transport acknowledgement.

In practice:

- construct semantic update
- append mirrored record to `acp_log.jsonl`
- attempt `session_update` transport send

Rationale:

- replay must survive client disconnects and consumer restarts
- the persisted journal must reflect what the agent emitted semantically, not
  only what one consumer happened to receive

## Minimum Semantic Event Set For CCBot

The `ccbot` fast-agent adapter must be able to derive this normalized set from
the live semantic stream and from persisted replay evidence:

- `user_echo`
- `assistant_commentary_delta`
- `assistant_reasoning_delta`
- `assistant_final`
- `tool_start`
- `tool_progress`
- `tool_result`
- `command_start`
- `command_progress`
- `command_result`
- `input_blocked`
- `input_unblocked`
- `lifecycle_started`
- `lifecycle_ready`
- `lifecycle_finished`
- `lifecycle_error`

History rules:

- `/history` is built from history-worthy content updates
- lifecycle/status-only updates do not appear as normal conversation lines

## Source-Of-Truth Rules

### For identity

Source of truth:

- `session_id`

Secondary display metadata:

- `title`

Rules:

- `/resume` targets `session_id`
- `/rename` may update `tmux` window name
- `/rename` may also update `title`
- `/rename` must not rewrite `session_id` unless `fast-agent` later grows a
  safe public rename-id surface

### For live semantics

Source of truth:

- the live semantic stream emitted by the semantic emitter / supervisor

### For replay

Source of truth:

- mirrored `acp_log.jsonl`

Not source of truth:

- pane capture
- session history snapshots alone
- generic `fastagent.jsonl`
- in-memory listeners alone

## Preferred Implementation Shape In fast-agent

### Preferred insertion strategy

The cleanest implementation is a logging proxy or wrapper around the semantic
emission boundary. In current upstream shapes, the easiest concrete boundary is
the ACP connection's `session_update(...)` method.

Why this is preferred:

- it captures all emission paths, including direct ones
- it avoids scattering file writes across dozens of call-sites
- it naturally covers:
  - `ACPContext.send_session_update()`
  - streaming updates in `AgentACPServer`
  - tool progress updates in `ACPToolProgressManager`
  - slash-command responses
  - history replay updates

Concrete current boundary candidates:

- connection install point: [agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L2610)
- ACP context helper: [acp_context.py](/home/tools/fast-agent/src/fast_agent/acp/acp_context.py#L418)
- tool progress direct sender: [tool_progress.py](/home/tools/fast-agent/src/fast_agent/acp/tool_progress.py#L247)

### Acceptable fallback strategy

If proxying the current ACP connection is impractical, refactor to a single
helper such as `emit_semantic_update(session_id, update, **meta)` and route all
semantic notifications through it.

This is weaker than a proxy because it requires call-site discipline, but it is
still acceptable if enforced by tests.

## Required Fast-Agent Changes

### FA1: Add semantic-replay journal config

Add an opt-in semantic-replay journal mode that enables `acp_log.jsonl`
without changing existing session history or generic logging.

### FA2: Add semantic update mirror writer

Implement an append-only writer for `acp_log.jsonl` with:

- versioned envelope
- per-session monotonic sequence
- safe append behavior
- colocated session file path resolution

### FA3: Add mirrored emission boundary

Mirror semantic updates at a single boundary:

- preferred: ACP connection proxy
- fallback: unified emission helper

### FA4: Cover direct update sources

Ensure the mirrored path covers:

- streamed text/reasoning updates
- tool progress lifecycle
- status-line updates
- slash-command responses
- session-info/title updates
- session-history replay into the live semantic stream

### FA5: Preserve existing artifacts

Do not break or repurpose:

- session history JSON
- generic `fastagent.jsonl`
- diagnostics traces
- provider replay fixtures

### FA6: Add fast-agent tests

Tests must prove:

- `acp_log.jsonl` is append-only
- direct and streamed semantic updates are all mirrored
- title/session-info updates are mirrored
- replay order matches live emission order
- partial trailing line does not break replay
- disabling the feature leaves legacy behavior unchanged

## Required CCBot Changes

### CB1: Add fast-agent replay-evidence discovery

Given `session_id`, resolve:

- session directory
- `acp_log.jsonl`
- optional session snapshot history files

### CB2: Add fast-agent replay-evidence normalizer

Normalize persisted replay evidence into the shared runtime-neutral event
contract.

### CB3: Add fast-agent fixture corpus

Capture reproducible fixtures for:

- fresh launch
- resume
- reasoning stream
- tool start/progress/result
- slash-command response
- title update
- blocked input / degraded mode
- final result

### CB4: Add degraded-mode semantics

If the live semantic stream is unavailable but `acp_log.jsonl` exists:

- replay and recovery may continue
- live progress may be degraded

If neither the live semantic stream nor `acp_log.jsonl` is available:

- bind/control may remain available
- semantic progress/result delivery must be explicitly degraded
- no silent pane-scrape substitution

### CB5: Add parity tests

Tests must prove:

- `ccbot` fast-agent adapter never depends on `tmux` pane text for semantic
  events
- replay from `acp_log.jsonl` reproduces Telegram progress/result ordering
- restart does not lose semantic state beyond the last safely written mirrored
  semantic record

## Why `tmux + acp_log.jsonl` Is Architecturally Clean

This architecture is clean if each layer keeps its own kind of responsibility:

- `tmux`
  - live human/operator control surface
- ACP-equivalent semantic stream
  - canonical live meaning layer, regardless of whether the current runtime
    obtains it through native ACP or a side-channel mirror
- `acp_log.jsonl`
  - replay and recovery journal
- session snapshot history
  - runtime-owned conversation state and resume backing store

This is cleaner than pane scraping because it:

- preserves semantic structure
- avoids UI-format drift
- supports replay after restart
- supports the same agent being used from `tmux`, Telegram, and IDE clients

It is not clean only if these layers are collapsed back together, for example:

- `tmux` treated as semantic source
- generic logs treated as semantic replay without ACP meaning
- `acp_log.jsonl` treated as a replacement for session identity or history
- machine transport allowed to take over the same stdio surface as the human CLI

## Explicit Non-Goals

- using pane scraping as the main fast-agent adapter
- replacing fast-agent session history with persisted replay evidence
- replacing generic `fastagent.jsonl` with persisted replay evidence
- making `ACP-protocol` transport acknowledgement the definition of persisted truth
- rewriting historical `session_id`s

## Recommended Next Step

Implement the fast-agent-side semantic replay evidence first:

1. add an opt-in semantic journal flag
2. wrap ACP-equivalent semantic emission with a mirrored writer
3. capture fixtures
4. teach `ccbot` to normalize `acp_log.jsonl`

Without that, a `ccbot` fast-agent adapter will either depend on ephemeral live
streams only or drift back toward pane scraping, both of which violate the
target ontology.
