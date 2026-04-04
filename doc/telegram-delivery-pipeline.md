# Telegram Delivery Pipeline

This note closes `T26` for the multi-runtime topic-control plan.

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
  in fenced `json` blocks, and compact output counts instead of raw dumps

`verbose` is a debug policy for operators. It may expose more raw execution
surface, but it is not the default product-facing mode.

## Ordering Rules

The delivery pipeline keeps:

- one mutable progress/status artifact per `(user_id, topic)`
- one latest-only visible commentary artifact per `(user_id, topic)`
- one ordered content queue per user

Ordering guarantees:

1. progress/status updates may appear while a turn is still running
2. the first real content part may convert the status artifact into content
3. when tool lifecycle is materialized as content, `tool_result` may edit the earlier `tool_use` message in place
4. final assistant content lands in the topic after the progress/tool lifecycle
5. once final assistant content is delivered, the commentary lane is closed until
   the next user turn and no late commentary may appear below the final answer

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
- completed / failed / shutdown agent summaries

Those classes must either:

- be suppressed entirely
- or be projected into the mutable status artifact

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
