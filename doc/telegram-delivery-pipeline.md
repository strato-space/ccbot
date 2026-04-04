# Telegram Delivery Pipeline

This note closes `T26` for the multi-runtime topic-control plan.

The compact ontology companions for this note are:

- [`/home/tools/ccbot/ontology/delivery-surface.md`](/home/tools/ccbot/ontology/delivery-surface.md)
- [`/home/tools/ccbot/ontology/boundaries.md`](/home/tools/ccbot/ontology/boundaries.md)

Those files define the delivery nouns and boundary claims. This note expands
them into the concrete Telegram pipeline contract.

## Goal

Telegram delivery must preserve the upstream Claude user-visible progress/result
behavior while remaining runtime-neutral.

The pipeline consumes `NormalizedEvent` objects and applies delivery rules
based on semantic meaning, not on the source runtime.

## Default Delivery Mode

The default Telegram surface is `compact`, not `verbose`.

`compact` is the production-facing policy:

- human-facing final answers stay as ordinary content
- human-facing orchestration milestones stay as ordinary content
- the latest human-facing commentary remains visible as a dedicated artifact so
  progress narrative does not disappear under mutable status churn
- reasoning and thinking summaries are routed through the mutable status
  artifact
- tool lifecycle summaries are routed through the mutable status artifact
- command-execution summaries, including Claude-style `local_command`, are
  routed through the mutable status artifact
- file-change summaries are routed through the mutable status artifact
- internal injected user payloads such as `<skill>...</skill>` never appear as
  ordinary chat content
- placeholder reasoning such as `[reasoning]` is suppressed
- raw tool payloads, giant command stdout dumps, and full file bodies must be summarized before they reach Telegram
- when tool or file summaries are surfaced, they should prefer Codex-style
  code-aware formatting: shell payloads in fenced `sh` blocks, JSON payloads
  in fenced `json` blocks, with truncation footers outside the fenced block
  body and outcome metadata rendered as a separate footer rather than as raw
  transcript spill
- when a surfaced technical preview already conveys the outcome clearly, the
  visible bubble should not add a redundant footer like
  `completed · output 1 line(s)` just for symmetry

`verbose` is a debug policy for operators. It may expose more raw execution
surface, but it is not the default product-facing mode.

## Ordering Rules

The delivery pipeline keeps:

- one mutable progress/status artifact per `(user_id, topic)`
- one latest-only visible commentary artifact per `(user_id, topic)`
- one ordered content queue per user
- one current turn generation per `(user_id, topic)`
- one terminal turn artifact: `assistant_final`
- one broader pre-final visible surface:
  - commentary
  - orchestration milestones
  - any future human-facing preview bubble that the product chooses to surface

Ordering guarantees:

1. progress/status updates may appear while a turn is still running
2. the first real content part may convert the status artifact into content
3. when tool lifecycle is materialized as content, `tool_result` may edit the earlier `tool_use` message in place
4. pre-final visible artifacts already queued may land before the terminal
   final answer
5. final assistant content lands in the topic after the progress/tool lifecycle
6. only after final assistant content has been delivered successfully, the
   whole pre-final visible
   surface is closed until the next user turn
7. only after final assistant content has been delivered successfully, the
   mutable technical status
   artifact is also closed until the next user turn
8. "delivered successfully" means the final assistant content finished
   successfully in full; a partial multipart send does not close the surface
9. no late status artifact may appear below the final answer for the same turn
10. no late commentary, orchestration milestone, or surfaced preview bubble may
   appear below the final answer for the same turn
11. a new user turn advances the topic turn generation before the new turn's
    artifacts are enqueued
12. stale close tasks from an older generation must fail closed instead of
    reclosing the newer turn's visible or status surface
13. this ordering contract applies to the whole `pre-final visible artifact`
    class, not only to commentary
14. if an already-started multipart content send becomes stale mid-flight, the
    remaining parts and trailing image/status sends must abort rather than
    surfacing below a newer turn or below the terminal turn artifact

This preserves the upstream Claude shape:

- status first
- tool lifecycle edits in order
- final answer last

This pipeline keeps the upstream-style rule that `tool_result` may edit the
earlier `tool_use` message in place when the runtime and delivery mode expose
tool lifecycle as ordinary content. In the default `compact` mode, that same
tool lifecycle is typically collapsed into the mutable status artifact instead.

## Progress Routing

Progress/status delivery is driven by runtime-neutral event metadata.

- `status_message_eligible=true`
  - marks events that may drive the live Telegram progress artifact
- complete content stays ordinary content
  - this preserves Claude-style `thinking` / `tool_use` / `tool_result`
    bubbles when the runtime emits them as complete content events
- incomplete progress events become status updates
- explicit `tool_progress` events also become status updates even when marked
  complete, because they are semantically ephemeral

In other words:

- complete content remains content
- incomplete progress becomes mutable status
- lifecycle-only events are never delivered as normal content

For the default `compact` Telegram surface, some complete events are
intentionally projected into the mutable status artifact instead of becoming
permanent content bubbles:

- `reasoning` summaries
- `tool_use` summaries
- `tool_result` summaries
- `command_execution` summaries
- `file_change` summaries

Compact mode keeps commentary visible as a latest-only artifact, because it is
the human-readable execution narrative. The mutable status artifact is reserved
for ephemeral technical execution surface that would otherwise churn too
quickly.

This keeps the chat human-readable while preserving the live CLI and replay
evidence as the authoritative technical surfaces.

The reopen side of the contract is semantic, not merely visual:

- any real `user turn opener` reopens the terminal surface for the next turn
- a hidden internal prompt scaffold may still be a real user turn opener
- hidden notifications such as `<subagent_notification>` or
  `<turn_aborted>` are not user turn openers and must not reopen the surface
- hidden internal technical payloads such as `<bash-stdout>`,
  `<bash-stderr>`, `<local-command-caveat>`, and `<system-reminder>` are also
  not user turn openers and must not reopen the surface
- once a newer turn opens, stale pre-final, technical-status, and stale final
  artifacts from the older turn must fail closed instead of surfacing below
  the newer turn

## Canonical Codex Message Preference

Codex rollout may emit the same human message twice:

- lightweight `event_msg` for live UI/status use
- canonical `response_item.message` for persisted turn history

Telegram/history prefers the canonical copy.

- if both copies appear in the same normalization batch, the lightweight
  `event_msg` copy is suppressed immediately
- if the lightweight `event_msg` arrives first and the canonical copy follows in
  a later poll slice, the lightweight copy may be buffered briefly so the
  canonical `response_item.message` can win
- if no canonical copy arrives, the buffered `event_msg` may flush on a later
  idle poll rather than on an unrelated non-idle poll, so canonical preference
  survives cross-poll monitor churn while human progress still remains visible
- `event_msg.user_message` is treated specially:
  - it may open the user turn immediately in incremental monitor mode
  - a later canonical duplicate is dropped instead of reopening the turn twice
  - duplicate suppression state is FIFO per signature, so repeated identical
    text across distinct turns does not collapse into one logical event

This keeps `canonical response_item wins` true without losing cross-poll live
progress entirely.

## Compact Bubble Matrix

In the production-facing `compact` mode, durable Telegram content bubbles are
deliberately narrow:

- user-visible user echo
- orchestration milestones such as spawned/waiting/completed subagent summaries
- final assistant text

In addition to those durable bubbles, `compact` keeps one latest-only visible
commentary artifact. Each new commentary update replaces the previous one so
the chat shows the current human-readable execution narrative without
accumulating a long stack of near-duplicate commentary bubbles. That commentary
artifact is explicitly cleared when the final assistant answer is delivered and
must not reappear below the final answer unless a new user turn has begun.

The following semantic classes are not meant to survive as permanent content
bubbles in `compact` mode:

- reasoning / thinking
- tool lifecycle
- command execution / local command
- file-change summaries

Subagent and orchestration milestones are different. They are not raw tool
surface, and they are not volatile commentary churn. In compact mode they
should be rendered as human-facing milestone bubbles modeled after Codex
multi-agent history rows:

- spawned agent
- waiting for agent(s)
- finished waiting for agent(s)
- completed / failed / shutdown agent summaries

Those classes must either:

- be suppressed entirely
- or be projected into the mutable status artifact

## Code-Aware Preview Contract

When compact/verbose surfaces materialize technical previews, they must follow
one formatting contract:

- the fenced code block contains only preview body lines
- truncation markers such as `preview 5/91 lines` live outside the fenced block
- outcome metadata such as `completed`, `failed`, or `output 1 line` is a
  separate footer line, not part of the code block body
- if the visible preview already conveys the outcome clearly, the footer should
  not add a redundant `completed · output 1 line(s)` line merely for symmetry

## Teardown And Stale-Delivery Rules

Late delivery must fail closed.

- if a queued task no longer matches the current `(topic -> window)` binding,
  it is dropped
- if the bound tmux window is gone, queued delivery for that binding is dropped
- explicit `/unbind`, topic close, or stale-window cleanup clears the tracked
  status artifact before normal cleanup continues
- deleted or uneditable Telegram status messages fall back to sending a new
  message or clearing the stale tracking entry

This prevents:

- late events posting into explicitly unbound topics
- progress artifacts surviving after teardown
- stale tool-result edits targeting an old topic binding

## Queue And Steer

Message-layer sources are equal:

- Telegram-submitted text
- routed human text submitted through the same message-routing surface

Source does not affect priority.

Routing mode affects semantics:

- `queue`
  - normal turn submission
- `steer`
  - directed intervention into the current turn when the runtime supports it

Raw terminal control is not part of this equal message layer.
Direct human `tmux` input remains a separate operator intervention surface and
is not modeled as an ordinary queued semantic message.

## Why This Is Not ACP-First

`ccbot` keeps `tmux stdio CLI-first`.

Human observability, injection, and operator control outrank protocol purity,
so semantic delivery is rebuilt on runtime-neutral events without surrendering
the live CLI stdio to literal `ACP-protocol` transport ownership.
