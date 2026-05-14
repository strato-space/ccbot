# Telegram Delivery Pipeline

This note closes `T26` for the multi-runtime topic-control plan.

The compact ontology companions for this note are:

- [`/home/tools/ccbot/ontology/topic-control.md`](/home/tools/ccbot/ontology/topic-control.md)
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
- warning artifacts stay visible as durable system notices
- the latest human-facing commentary remains visible as a dedicated artifact so
  progress narrative does not disappear under mutable status churn
- reasoning and thinking summaries are routed through the mutable status
  artifact
- tool lifecycle summaries are routed through the mutable status artifact
- poll-only tool lifecycle updates such as empty `write_stdin(..., poll)` may
  update an existing status artifact but must not create a standalone `Tool`
  bubble in compact mode
- Telegram no-op edit errors (`message is not modified`) are idempotent
  success for mutable technical status; they keep the same Telegram message id
  and are audited as `edit_noop` rather than causing a fresh `Tool` bubble
- command-execution summaries, including Claude-style `local_command`, are
  routed through the mutable status artifact
- file-change summaries are routed through the mutable status artifact
- internal injected user payloads such as `<skill>...</skill>` never appear as
  ordinary chat content
- ordinary user echo remains visible in compact mode unless it matches an
  explicit internal payload shape
- placeholder reasoning such as `[reasoning]` is suppressed
- raw tool payloads, giant command stdout dumps, and full file bodies must be summarized before they reach Telegram
- repeated identical warnings on one control surface deduplicate into one latest warning
  bubble, with a visible repeat counter only when `N > 2`
- runtime-discontinuity system summaries are warning artifacts rather than
  assistant-final messages; when both pane screenshot evidence and raw summary
  text are available, screenshot delivery precedes the raw text bubble
- live-runtime detection for Codex must treat a visible active footer/status or
  prompt surface as authoritative even after the initial `OpenAI Codex` banner
  has scrolled out of the pane; banner visibility alone is not a valid exit
  signal
- when tool or file summaries are surfaced, they should prefer Codex-style
  code-aware formatting: shell payloads in fenced `sh` blocks, JSON payloads
  in fenced `json` blocks, with truncation footers outside the fenced block
  body and outcome metadata rendered as a separate footer rather than as raw
  transcript spill
- when a surfaced technical preview already conveys the outcome clearly, the
  visible bubble should not add a redundant footer like
  `completed · output 1 line(s)` just for symmetry
- command-like tool outputs that carry developer transport wrappers such as
  `Chunk ID`, `Wall time`, `Process exited`, `Original token count`, and
  `Output:` are normalized into `command_execution` even if the paired tool-call
  record is not in the current polling slice; genuine non-command tool JSON is
  left as a tool result
- when compactness conflicts with semantic clarity, the delivery surface
  prefers visibility-first mutable updates over ambiguous suppression

`verbose` is a debug policy for operators. It may expose more raw execution
surface, but it is not the default product-facing mode.

## Out-of-band CLI Result Delivery

`ccbot send_bot_message` is an outbound result-delivery helper for adjacent
services that run inside the same bot instance context, for example
`imm_arena_bot` jobs that need to return a generated archive or report to the
main Telegram chat.

This path is deliberately not modeled as Telegram user input:

- it does not write to tmux stdin
- it does not create runtime replay evidence
- it does not open a user turn
- it does not create or change bindings

Target resolution is hybrid. Explicit `--chat-id` / `--thread-id` arguments win.
Without explicit coordinates, the CLI reads `$CCBOT_DIR/state.json` and resolves
the stored Telegram group routing coordinates for the bound control surface. If
the state has multiple plausible targets, the helper fails closed and asks for
an explicit target instead of guessing between surfaces.

## Ordering Rules

The delivery pipeline keeps:

- one mutable progress/status artifact per `(user_id, control surface)`
- one latest-only visible commentary artifact per `(user_id, control surface)`
- one mutable plan-update artifact per `(user_id, control surface)` within the
  current assistant turn; opening a new user turn drops the old tracking pointer
  so a new plan appears at the chat tail rather than editing history up-thread
- one mutable pending-input artifact per `(user_id, control surface)`
- one mutable interactive question artifact per `(user_id, control surface)`
  when the runtime exposes a durable blocking question record
- one ordered content queue per user
- one current turn generation per `(user_id, control surface)`
- one terminal turn artifact: `assistant_final`
  - generated-image tool success text with a saved local artifact path may be
    promoted to this terminal text artifact; this does not imply automatic
    Telegram media attachment
- one broader pre-final visible surface:
  - commentary
  - orchestration milestones
  - plan updates
  - any future human-facing preview bubble that the product chooses to surface
- one latest-warning artifact with warning-dedup state per `(user_id, control surface)`
  for ordinary warnings; runtime-discontinuity warnings may use a distinct
  warning identity when separate events must remain separately visible

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
9. when a final assistant artifact is observed, delivery must wait for that
   terminal artifact before a queued follow-up user turn advances the generation
10. a user echo opens a new turn as ordinary content and must not be created by
    editing a previous technical status artifact
11. no late status artifact may appear below the final answer for the same turn
12. no late commentary, orchestration milestone, plan update, or surfaced preview bubble
    may appear below the final answer for the same turn
13. warning artifacts are not members of the current-turn pre-final surface and
    are not dropped by terminal closure; warning dedup state is keyed by control surface
    and latest warning text
14. before a new user turn advances the control-surface turn generation, the
    queue flushes any already-queued terminal turn artifact for that surface so
    the previous answer lands before the next turn is opened
15. a new user turn then advances the control-surface turn generation before the
    new turn's artifacts are enqueued
16. stale close tasks from an older generation must fail closed instead of
    reclosing the newer turn's visible or status surface
17. this ordering contract applies to the whole `pre-final visible artifact`
    class, not only to commentary
18. if an already-started multipart content send becomes stale mid-flight, the
    remaining parts and trailing image/document/status sends must abort rather than
    surfacing below a newer turn or below the terminal turn artifact

The pending-input artifact is outside that turn-output barrier. It previews
future queued user input rather than current-turn assistant output, so it may
remain visible while the current turn is still running without being treated as
either status churn or pre-final visible content.

Interactive question artifacts are a separate control lane. They are created
from runtime-owned durable question records, not from pane scraping alone. For
OMX, the source record is `kind=omx.question/v1` under
`.omx/state/questions/` or `.omx/state/sessions/*/questions/`. Telegram
renders the question body and predefined options with inline buttons, edits the
same message while the question remains active, and answers by writing the
durable record to terminal status `answered`. The OMX renderer may be a
temporary tmux split pane, but that pane inherits the parent bound tmux window;
it is never promoted to a Telegram control surface or delivery source. When
the OMX record provides a tmux return bridge, the bot best-effort sends the
normal `[omx question answered] ...` continuation line back to the return pane
and closes the temporary question pane. While the record is active, ordinary
Telegram text/media input to the same bound window fails closed so it cannot
bypass the blocking control question. This artifact is not a technical status
artifact, not a user turn opener, and not a terminal assistant answer.

This preserves the upstream Claude shape:

- status first
- tool lifecycle edits in order
- final answer last

This pipeline keeps the upstream-style rule that `tool_result` may edit the
earlier `tool_use` message in place when the runtime and delivery mode expose
tool lifecycle as ordinary content. In the default `compact` mode, that same
tool lifecycle is typically collapsed into the mutable status artifact instead.
The narrow exception is generated-image success output with a saved artifact
path: compact delivery promotes that text to the terminal assistant bubble so
the originating Telegram thread receives the success/result/path message even
though no generated image is automatically attached.

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

Queued follow-up messages are different again. They describe future input that
has not yet opened its turn, so compact mode may surface them as a separate
latest-only pending-input artifact modeled after the Codex bottom pane:

- queued follow-up messages
- optional edit-last-queued-message hint

This artifact is not a durable history bubble, not a user turn opener by
itself, and not a member of the current turn's pre-final visible artifact
class.

It is closed by queue-owned lifecycle changes (`queue-empty`, stale binding, or
explicit clear), not merely because the current assistant turn has produced its
terminal answer.

The preview should preserve queued follow-up text literally. The parser may
strip Codex UI marker glyphs such as checkbox bullets, but it must not
normalize away user punctuation like `/`, `#`, `$`, `>` or phrases like
`Waiting for ...` merely because they resemble other terminal surfaces.

This keeps the chat human-readable while preserving the live CLI and replay
evidence as the authoritative technical surfaces.

The reopen side of the contract is semantic, not merely visual:

- any real `user turn opener` reopens the terminal surface for the next turn
- a hidden internal prompt scaffold may still be a real user turn opener
- if that hidden opener was suppressed for Telegram visibility and the lanes
  remain closed, lifecycle `turn_started` is allowed to reopen turn generation
  as an idempotent fallback
- hidden notifications such as `<subagent_notification>` or
  `<turn_aborted>` are not user turn openers and must not reopen the surface
- hidden internal technical payloads such as `<bash-stdout>`,
  `<bash-stderr>`, `<local-command-caveat>`, and `<system-reminder>` are also
  not user turn openers and must not reopen the surface
- a message is not hidden merely because it resembles instructions text; hidden
  payload classification must rely on explicit tagged/internal shapes so a
  user can still paste repository guidance or XML-like snippets visibly
- once a newer turn opens, stale pre-final and technical-status artifacts from
  the older turn must fail closed instead of surfacing below the newer turn
- terminal final artifacts are different: if already queued when the next user
  turn arrives, they must be flushed before generation advances; only an
  actually late/unbound terminal artifact may fail closed

This fallback is intentionally narrow:

- it is keyed to lifecycle event `turn_started`
- it must not create a visible duplicate user-opener bubble
- it must not advance generation again if the lanes are already open

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
- warning artifacts (latest-warning dedup with `×N` counter for `N > 2`;
  distinct runtime-discontinuity warnings may intentionally bypass collapse)
- final assistant text

In addition to those durable bubbles, `compact` keeps one latest-only visible
commentary artifact. Each new commentary update replaces the previous one so
the chat shows the current human-readable execution narrative without
accumulating a long stack of near-duplicate commentary bubbles. That commentary
artifact is explicitly cleared when the final assistant answer is delivered and
must not reappear below the final answer unless a new user turn has begun.
Commentary is not clipped by the internal status-helper ceiling; if one
Telegram message is insufficient, it may span multiple Telegram messages while
remaining one logical commentary artifact.
If a complete commentary or orchestration event arrives after that closure
point, the bot must drop it instead of reopening the closed pre-final lane.

`compact` may also keep one latest-only pending-input artifact that previews
queued follow-up messages held behind the current running turn. Unlike
commentary, that artifact belongs to future input, not current-turn output, so
it is not closed by the terminal turn artifact unless the queue itself is
cleared or rebound.

The terminal final answer is always a fresh Telegram message sequence. It must
not be materialized by editing/reusing the visible commentary artifact.

Formatting ownership is intentionally split:

- preview builders/rollout formatters emit fenced blocks and preview footers
- compact-policy clipping may shorten those previews for budget reasons
- compact policy must not add a second preview footer or wrap an already-fenced
  preview again

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

Each `wait_agent` invocation owns its own waiting/finished milestone pair. If
two overlapping waits target the same agent set, both lifecycles must remain
visible instead of collapsing into one shared dedupe key.

Warning artifacts are intentionally separate from both technical status churn
and pre-final turn artifacts:

- warnings are durable system notices
- they are deduplicated against the latest warning text on the same control
  surface
- a different warning text creates a new warning bubble and resets the counter
- usage-limit / quota-exhaustion banners are warning artifacts too; they are
  not technical status and not assistant-final
- runtime-discontinuity warnings may use a distinct warning identity so
  separate exit/loss events with identical raw text remain separately visible

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
- shell execution is one artifact: `exec_command` starts as a command preview
  and the matching completion edits that command with the real output body
  instead of creating a separate generic `Tool Output` bubble
- command-execution artifacts with command identity are not merged with adjacent
  content tasks; the `tool_use_id` pairing is stronger than bubble batching
- command completions strip transport-only wrapper lines (`Chunk ID`, wall time,
  exit/token metadata, `Output:` marker) before selecting `sh` or `json` for the
  actual output preview
- parsed Codex read/list/search command metadata may render as `• Explored`
  with `Read`, `List`, and `Search` rows, matching the Codex CLI history cell
  semantics instead of exposing low-value shell plumbing

## Teardown And Stale-Delivery Rules

Late delivery must fail closed.

- if a queued task no longer matches the current control-surface binding, it is
  dropped
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

For writable live tmux bindings, queued Telegram text enters that operator
surface as an injection operation with distinct phases:

- payload delivery
- runtime submit-key delivery
- runtime acknowledgement from same-runtime-identity replay evidence

Payload/key success is not a turn opener. For Codex conversational input,
single-line and multiline alike, ccbot reports success only after the currently
attached Codex runtime identity appends replay evidence proving that a new turn
opened. A matching user-message record is the strongest ACK; a bare
`turn_context` may count only inside the per-window ACK guard after the submit
key has been sent.

Multiline text is still injected through tmux's paste-buffer path so
alternate-screen TUIs such as Codex can treat it as one paste event. For Codex
multiline payloads, the post-paste submit primitive uses bare `Enter` rather
than `C-m`, because live `server-np4` evidence showed `C-m` can leave Telegram
text in the composer. Live `str` evidence on 2026-04-30 also showed that
submitting too soon after paste can leave the same draft visible until a later
manual Enter, so Codex multiline submit includes a short post-paste readiness
delay before sending `Enter`. If payload delivery, submit-key delivery, or
replay ACK fails, the bot must return an explicit delivery failure instead of
reporting the message as sent.

A Codex-bound tmux window is writable only while it still exposes a live Codex
input plane. If the window has fallen back to a shell prompt, Telegram input
must fail closed instead of pasting text into `bash`, even when the previous
rollout file remains readable.

Codex "Conversation interrupted" panes are still input-ready surfaces: they ask
for the next user instruction and must not be treated as read-only approval
prompts just because the visible line starts with `■`.

External-thread bind follows the same split:

- replay/event delivery may remain active without tmux
- input injection requires a live tmux binding
- if no live injection plane exists, Telegram input must fail closed with an
  explicit read-only warning and reattach hint

## Why This Is Not ACP-First

`ccbot` keeps `tmux stdio CLI-first`.

Human observability, injection, and operator control outrank protocol purity,
so semantic delivery is rebuilt on runtime-neutral events without surrendering
the live CLI stdio to literal `ACP-protocol` transport ownership.

## Human Rendering And Delivery Audit

The Telegram pipeline has a normalization boundary between runtime transport
and human artifacts. Raw tool JSON is never the product artifact by itself.
Compact rendering uses these projections:

- `exec_command` and command execution: show the shell payload in a fenced `sh`
  preview and keep truncation metadata outside the fence.
- `tool_result`: show JSON only when the result is genuine JSON; otherwise show
  a compact text/code preview. Empty or metadata-only output may be summarized.
- `write_stdin`: empty polls are lifecycle checks, not useful Telegram
  content. They are suppressed from the status lane rather than replacing a
  richer visible status; only real injected characters deserve a compact code
  preview.
- status-polled command output: if the terminal surface exposes a raw
  `Tool Output` wrapper, strip wrapper metadata and render the real stdout/stderr
  as command output instead of showing `Chunk ID` / `Wall time` / token counts.
- `omx_state.state_write`: show the state transition (`mode`, `phase`,
  `active`, `iteration`, task summary, snapshot) rather than the raw JSON.
- `<hook_prompt>`: deliver as an operator warning artifact, never as user echo.
- file-change events: show file paths plus available preview lines in `sh`.
- `update_plan`: update the dedicated plan artifact with the actual plan body,
  not merely a `Plan updated` label.

For self-improvement, every Telegram delivery lifecycle decision is appended to
`telegram_delivery_audit.jsonl` under the ccbot config directory. The audit is
schema-versioned and records both positive and negative lifecycle events.
`send`, `edit`, and `delete` rows describe Telegram API attempts; `suppress`
rows explain intentional non-delivery such as stale turn output or a poll-only
`write_stdin` update arriving when no mutable status artifact exists. Rows
include action, control surface, task/content/semantic class, message id when
available, success flag, reason/error, turn generation and tool-use correlation
where available, text length/hash, and a compact preview. It deliberately omits
full raw payloads and secrets.
