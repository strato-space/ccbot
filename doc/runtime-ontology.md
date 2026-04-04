# Runtime Ontology For Multi-Runtime Topic Control

This note defines the entities that the multi-runtime adaptation work must keep
distinct. It is the contract for implementation tasks in
`/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md`.

The compact ontology index for maintainers now lives in:

- [`/home/tools/ccbot/ontology/README.md`](/home/tools/ccbot/ontology/README.md)
- [`/home/tools/ccbot/ontology/runtime.md`](/home/tools/ccbot/ontology/runtime.md)
- [`/home/tools/ccbot/ontology/delivery-surface.md`](/home/tools/ccbot/ontology/delivery-surface.md)
- [`/home/tools/ccbot/ontology/boundaries.md`](/home/tools/ccbot/ontology/boundaries.md)

The execution-plan corpus now lives in:

- [`/home/tools/ccbot/specs/README.md`](/home/tools/ccbot/specs/README.md)
- [`/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md`](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md)

This file remains the expanded maintainer note for the runtime layer.

## Why this exists

The current implementation history is still full of the word `session`, but the
system now needs a stricter model. A live runtime process, a persisted
conversation identity, and replay evidence on disk are not the same thing.

The bot must also keep the human terminal surface separate from any semantic
transport used by machine consumers.

## Definitions

- **Telegram topic**
  - The user-facing control lane in Telegram.
  - Commands, screenshots, progress notices, and final results are shown here.

- **Topic control policy**
  - The persisted rule attached to a topic that governs whether plain messages
    may trigger implicit bind or instead require explicit bind.
  - This is normative routing state, not a live runtime object.

- **Binding**
  - The bot's persisted association from a Telegram topic to a live tmux
    window, together with the runtime metadata needed to route input and
    notifications safely.

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
    - fast-agent `session_id`, with optional title metadata layered on top

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

- **Normalized event**
  - The bot's runtime-neutral event object derived from the live semantic
    stream, the persisted replay evidence, or both.
  - This is what the Telegram delivery layer consumes.

- **Capability-scoped command**
  - A command that is only valid for the runtimes that explicitly advertise the
    required capability.
  - Examples:
    - launch
    - resume
    - rename tmux window
    - rename persisted identity
    - queue / steer routing

- **Message channel**
  - A routed text-producing source that submits atomic messages into the runtime
    through the message plane.
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

- **Runtime adapter**
  - The code layer that translates runtime-specific launch, identity, input,
    prompt-state, and evidence semantics into the bot's generic behavior.

- **ACP-protocol**
  - The agent protocol and semantic model used as the reference vocabulary for
    event meanings.

- **ACP-module**
  - The existing `ccbot` product surface named `ACP`.
  - This is a legacy product/module boundary, not the same thing as the
    `ACP-protocol`.

## Canonical model

The model must distinguish these relations:

`Telegram topic --governed by topic control policy--> may or may not enter a binding flow`

`binding -> tmux window -> runtime process`

`runtime process -> binds to runtime conversation identity`

`runtime process -> semantic emitter / supervisor`

`semantic emitter / supervisor -> live semantic stream`

`semantic emitter / supervisor -> persisted replay evidence`

`runtime conversation identity scopes/indexes the live semantic stream and persisted replay evidence`

`live semantic stream and/or persisted replay evidence -> normalized events`

`normalized events -> Telegram notifications / history views`

This is more precise than a single flat chain because `topic control policy` is
not the same kind of thing as `window`, `process`, or `identity`. It governs
whether binding and routing are permitted; it is not itself a live runtime lane.

For some runtimes the live semantic stream and the persisted replay evidence may
be realized by the same append-only artifact. They remain conceptually distinct
even when operationally co-located.

## Human-first transport rule

The live runtime `stdin/stdout` belongs to the human-observable CLI surface
hosted in `tmux`.

Therefore this plan rejects an `ACP-first transport` design where
`ACP-protocol` transport itself occupies the same `stdin/stdout` used by the
interactive CLI. That design would reduce or destroy:

- human observability of the true CLI surface
- live human injection into the same agent process
- operator takeover and recovery from the authoritative terminal surface
- equality between Telegram-submitted messages and human-submitted messages

The accepted architecture is:

- `tmux stdio CLI interface first`
- semantic transport second
- ACP-equivalent event model as the target meaning layer

This is not an anti-`ACP-protocol` decision. It is a decision that human
observability, injection, and control outrank protocol purity for the live
execution surface. If a runtime offers literal `ACP-protocol` transport only by
taking over `stdin/stdout`, that surface is not acceptable as the primary
`ccbot` operator model.

## Message-layer equality and routing semantics

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

## Forbidden equalities

These equalities are false and must stay false in code, docs, and tests:

- `policy == binding`
- `bind flow == message routing`
- `status notification == content delivery`
- `window == thread`
- `process == thread`
- `process == rollout log`
- `thread == rollout log`
- `topic == thread`
- `topic == window`
- `literal ACP-protocol-over-stdio == acceptable primary control plane`

More precise statements:

- A tmux window hosts a live process, but it is not the persisted thread.
- A process may attach to an existing thread via resume, but it is not that
  thread.
- A rollout log is emitted evidence from process/thread activity, not the
  process itself.
- A Telegram topic is governed by topic control policy and routed bindings, not
  directly to a persisted log file.
- A status message is a transport artifact for progress; it is not the same
  thing as the final content payload.

## Operational invariants

- A topic may bind to at most one live tmux window at a time.
- A tmux window may host at most one active runtime process at a time.
- A live process may be associated with at most one primary runtime conversation
  identity at a time.
- A runtime conversation identity may have multiple historical replay artifacts
  over time.
- Resume attaches a new or reused live process to an existing identity; it does
  not restore the previous live process.
- History is reconstructed from normalized replay evidence, not from the live
  process buffer.

## Resolution policy

Identity resolution must be deterministic and fail closed.

Preferred precedence:

1. Explicit identity chosen by the operator
2. Explicit launcher-side registration record
3. Exact normalized cwd match with a single valid candidate
4. User-visible disambiguation

Never do this:

- silently choose between multiple same-cwd candidates
- guess identity from pane text alone
- assume `/root/.codex` is the only valid home for a runtime
- route a plain topic message as if it were a direct write to a persisted log

## Scope guard

The multi-runtime adaptation work does not introduce new `voice`, `task`, or
`ACP-module` behavior.

Those flows are shared-surface compatibility constraints and must be protected
by non-regression tests while the runtime model changes underneath them.
