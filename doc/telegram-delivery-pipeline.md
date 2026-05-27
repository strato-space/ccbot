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
- mutable technical status artifact identity is delivery-surface state, not
  runtime-event history; ccbot persists the active Telegram `message_id` by
  canonical surface key and matching tmux `window_id` so a service restart can
  edit or intentionally clear/replace the same bubble instead of stranding a
  duplicate; poll-only or empty statuses are never persisted
- bare `⌘ Command` previews are shell command previews and must be rendered in
  a `sh` fence; when a leading `set -euo pipefail` line is followed by real
  command content, the preview/history skips that boilerplate; `⌘ Command
  output` is a distinct output category and may keep `text` or `json` fences
  based on payload
- eligible command/tool technical statuses use a two-part mutable compact
  technical status artifact: a bounded delivered technical-status history above
  a fenced current detail panel. File/path-like command-output summaries in
  that history render as the locator itself (for example `↳ .omx/...`) rather
  than quoted generic output; structured JSON command-output summaries render
  from the whole JSON block as a compact one-line prefix instead of only the
  first `{` line. Non-path, non-JSON output uses inline monospace after `↳`
  instead of the generic `output` label; command/tool history payloads likewise
  use inline monospace after Hermes-aligned labels such as `💻 terminal:`,
  `📚 skill_view:`, `🐍 execute_code:`, `📨 send_message:`, `✍️ write_file:`,
  and `📖 read_file:` rather than quoted strings. Path-like
  means a locator prefix such as `.omx/`, `./`, `../`, `/data/`, `/home/`,
  `/tmp/`, `~/`, or `file://`, not arbitrary slash-prefixed prose. This
  history is delivery evidence for the
  visible Telegram artifact, not durable runtime history, and it excludes final
  answers, user echo, commentary, warnings, pending-input previews, generated
  media/result bubbles, terminal-control panels, and OMX workflow panels. The
  marker registry follows Hermes Agent for common tools: `💻` terminal, `📚`
  skill_view/skills, `🐍` execute_code, `📨` send_message, `✍️` write_file,
  `📖` read_file, `🔧` patch, and `🔎` search; `🛠` remains a fallback for
  unknown non-shell tools. It reserves `↳` for output/subline summaries, `🖼`
  for media previews, and `🧭 OMX` for optional workflow telemetry.
- command-execution summaries, including Claude-style `local_command`, are
  routed through the mutable status artifact
- file-change summaries are routed through the mutable status artifact
- internal injected user payloads such as `<skill>...</skill>` never appear as
  ordinary chat content
- ordinary user echo remains visible in compact mode unless it matches an
  explicit internal payload shape; for live Codex windows, first-track replay
  attachment may backfill only the bounded current-turn opener so a tmux-typed
  prompt is not lost when ccbot attaches seconds after the user record
- Telegram ingress receipts for eligible simple Codex text are current-update
  acknowledgements before replay ACK; after matching Codex replay proves the
  same Telegram-originated text, that receipt is the durable user-input bubble
  and the duplicate replay `user_echo` is suppressed. Replay-only or tmux-typed
  prompts still render ordinary user echo.
- `send_chat_action("typing")` is a transient Telegram transport signal, not an
  artifact in compact ordering and not runtime proof
- optional `sendMessageDraft` previews are also transient Telegram transport
  signals: they may show latest-only, debounced, draft-eligible high-frequency
  partial frames, but they are not durable content, not technical status
  artifacts, not pre-final visible artifacts, not `/history`, not replay proof,
  and never final-answer proof; final content and semantic milestones still use
  durable send/edit paths
- `sendMessageDraft` group/topic use is capability-gated by explicit
  operator-approved allowlist and live smoke; support is not inferred from a
  client library exposing `message_thread_id`; draft text must pass compact
  safe-to-show filters and draft lanes stop/drop on final answer, stale
  generation, queue-empty, binding-stale, cancellation, or degraded capability
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

For outbound `--file-type video`, the helper treats geometry as delivery
evidence rather than a cosmetic hint. Local `--file-path` uploads are
best-effort probed with a bounded `ffprobe` call, explicit
`--video-width` / `--video-height` / `--video-duration` / `--thumbnail-path`
flags may override or supplement the probe, and the resulting `send_video`
request metadata (including request method and provided thumbnail path) plus
Telegram-returned `Message.video` / thumbnail geometry are included in JSON/audit
evidence. A status/message-id/url-only result is weak
evidence for final review previews because Telegram clients can render an
otherwise valid vertical MP4 with the wrong preview geometry.


## Operator Replay/Backfill for Missed Terminal Artifacts

`ccbot replay-backfill` is an operator-only repair path for Codex terminal
artifacts that were already consumed by the monitor before a deployment bug was
fixed. It is not a second monitor and it does not rewind `monitor_state.json`.
Generated-image media repair remains the default: operators select an explicit
replay file plus call id and/or byte range, run a dry-run, and then pass
`--deliver` to send only the selected generated-image media.

Missed assistant-final text repair is opt-in with `--text-final`. It must be
scoped by `--byte-range`, `--turn-id`, or `--text-sha256`; broad selections that
match multiple finals fail closed. Delivery requires an explicit Telegram target
or persisted target selector and records `replay_backfill_text`, not ordinary
live-delivery, audit rows.

The command reuses the Codex rollout normalizer, skips already delivered
candidates by default, and records bounded duplicate-prevention evidence:
`replay_backfill` rows contain replay path, byte offsets, Codex thread id, call
id, and media hash; `replay_backfill_text` rows contain replay path, byte
offsets, Codex thread id, turn id, and text hash. This keeps repairs bounded to
the missed terminal artifact and prevents unrelated historical replay from
flooding a Telegram topic.

## Ordering Rules

The delivery pipeline keeps:

- one mutable progress/status artifact per `(user_id, control surface)`
- one latest-only visible commentary artifact per `(user_id, control surface)`
- one mutable plan-update artifact per `(user_id, control surface)` within the
  current assistant turn; opening a new user turn drops the old tracking pointer
  so a new plan appears at the chat tail rather than editing history up-thread
- one mutable pending-input artifact per `(user_id, control surface)`
- one mutable Telegram ingress receipt per pending fast-path proof, distinct
  from the pending-input artifact unless the input is actually queued behind an
  active turn
- one mutable interactive question artifact per `(user_id, control surface)`
  when the runtime exposes a durable blocking question record
- one latest-only mutable runtime image-preview media artifact per
  `(user_id, control surface, turn generation)`; the first same-turn preview
  sends a Telegram photo bubble and later previews edit that media in place;
  if a preview payload contains multiple images, the first image is used for
  the mutable bubble and the truncation is audited
- one ordered content queue per user
- one current turn generation per `(user_id, control surface)`
- one terminal turn artifact: `assistant_final`
  - textual generated-image tool success with a safely validated local artifact
    path may be promoted to a terminal media result artifact when it substitutes
    for absent final assistant text; Codex `image_generation_end` replay events
    with embedded bytes are pre-final runtime image preview artifacts instead
  - the terminal media result is one Telegram photo bubble with caption; if
    validation, read, or media send fails, the saved-path text remains the
    terminal fallback
- one broader pre-final visible surface:
  - commentary
  - orchestration milestones
  - plan updates
  - runtime image preview artifacts, including Codex `image_generation_end` and
    `view_image` / `Viewed Image` latest-only mutable preview/photo bubbles
    sourced from paired replay-embedded image bytes
  - any future human-facing preview bubble that the product chooses to surface
- one latest-warning artifact with warning-dedup state per `(user_id, control surface)`
  for ordinary warnings; runtime-discontinuity warnings may use a distinct
  warning identity when separate events must remain separately visible

Ordering guarantees:

1. progress/status updates may appear while a turn is still running
2. the first real content part may convert the status artifact into content
3. when tool lifecycle is materialized as content, `tool_result` may edit the earlier `tool_use` message in place
4. pre-final visible artifacts already in flight may land before the terminal
   final answer, but queued mutable progress for the same surface/window/turn is
   dropped with audit when the final barrier is observed
5. final assistant content lands in the topic after any already-in-flight
   progress/tool lifecycle
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
    editing a previous technical status artifact; if it is recovered by bounded
    live Codex replay backfill, it is still delivered as a fresh user echo
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
18. a Telegram ingress receipt may pass stale technical status churn but must
    not pass an already queued terminal assistant-final artifact for the same
    control surface
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
`.omx/state/questions/`, `.omx/state/sessions/*/questions/`, or the explicit
`--state-path` carried by a same-window OMX question renderer pane. Telegram
renders the question body and predefined options with inline buttons, edits the
same message while the question remains active, and answers by writing the
durable record to terminal status `answered`. The OMX renderer may be a
temporary tmux split pane, but that pane inherits the parent bound tmux window;
it is never promoted to a Telegram control surface or delivery source. When
status polling discovers a new question while the current turn's pre-final lane
is still open, the initial question prompt is deferred until the explanatory
terminal/informational artifact closes that lane, even if that artifact has not
yet been discovered or queued. If earlier informational/commentary content is
already queued for the same user, the prompt also waits for that queue to drain
so the human sees the explanatory message before the questionnaire. Edits to an
already visible question artifact remain allowed because they do not create a
new out-of-order prompt. Prompt sends, edits, and first-send deferrals are audit
events with `semantic_kind=interactive_question` rather than inferred from
Telegram message-id gaps. When
the OMX record provides a tmux return bridge, the bot closes the temporary
question pane and best-effort sends the normal `[omx question answered] ...`
continuation line through the bound runtime input path when a bound window is
known, so Codex submit/ACK handling applies. Pane-level tmux send remains only
a fallback when no bound window is available. A button or `Other` answer is
recorded as terminal `answered` only after the return bridge succeeds; when
Codex is busy or the bridge fails, the Telegram artifact stays retryable and no
terminal checkmark is emitted. While the record is active, ordinary
Telegram text/media input to the same bound window fails closed so it cannot
bypass the blocking control question. If the OMX record allows `Other`, a
free-text Telegram reply in the same bound thread is consumed as the `Other`
answer and follows the same bridge-before-terminal-state rule. A timeout/error terminal record is
not final while its same-window renderer pane is still alive and visibly
matches the record; in that case the Telegram question artifact remains or is
reopened as answerable, including `Other` recovery when allowed. A renderer pane
that exits before a Telegram answer bridge finishes is also treated as
temporarily recoverable when the durable record still names a same-window return
pane; during that bounded recovery window the existing Telegram artifact stays
retryable instead of being edited to a terminal renderer error. A renderer startup
failure is also recoverable when no helper pane exists yet but the session-scoped
OMX mode state names a same-window tmux return pane; Telegram then owns the
visible question artifact and best-effort bridges the answer to that return pane
instead of presenting the renderer error as final technical status. When safe,
the bot may also materialize a replacement same-window OMX question helper pane
so the local tmux operator view and Telegram artifact stay aligned. This artifact
is not a technical status artifact, not a user turn opener, and not a terminal
assistant answer.

This preserves the upstream Claude shape:

- status first
- tool lifecycle edits in order
- final answer last

This pipeline keeps the upstream-style rule that `tool_result` may edit the
earlier `tool_use` message in place when the runtime and delivery mode expose
tool lifecycle as ordinary content. In the default `compact` mode, that same
tool lifecycle is typically collapsed into the mutable status artifact instead.
The narrow terminal exception is generated-image success output with a saved artifact
path: textual generated-image success output when it substitutes for absent final
assistant text. Compact delivery can promote that safely validated
text result to a terminal media result artifact, sent as one Telegram photo
bubble with a caption; validation, file read, or media send failure falls back
to terminal saved-path text. Separately,
Codex `image_generation_end` and `view_image` / `Viewed Image` outputs are
pre-final runtime image preview artifacts when paired replay output embeds image
bytes. In compact mode they are a latest-only mutable Telegram media artifact:
the first preview sends a photo bubble and later same-turn previews edit that
media in place. They are authorized replay-proven disclosure to the active bound
control surface, use sanitized provenance captions, never read local path
arguments for media bytes in the MVP, and do not close the assistant turn.
They never read local path arguments as preview media sources.
Compact mode uses the first image only when a preview payload contains multiple images.
It then records a truncation audit event rather than attempting to mutate a
Telegram media group.

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

For eligible command/tool statuses, that mutable compact technical status
artifact has a stable visual shape: a bounded delivered technical-status history
followed by a fenced current detail panel. The history commits only after
Telegram accepts the send/edit/no-op for that exact status bubble and resets on
turn boundaries, final-answer closure, status clear, or window mismatch.

Codex terminal-control panels observed only in the live tmux pane are projected
through a distinct `terminal_control_panel` status class. The class is mutable
and scoped like technical status, but it is not closed merely because a prior
assistant-final already landed: `/goal` panels and `Conversation interrupted`
operator notices describe current operator control state, not assistant content
from the closed turn. Polling sanitizes these panels to human-facing fields such
as goal status/objective/time/tokens/commands and suppresses raw transport
metadata.

OMX workflow progress is a separate optional status artifact, not a Codex
terminal-control panel. When a bound window has recognized, fresh `.omx` state,
ccbot may project it as `omx_workflow_panel` / `omx_workflow_status`, rendered as
one compact latest-only status bubble such as `🧭 OMX ultragoal 1/6 · G002 ·
running` plus a clipped current-unit summary. The artifact is scoped to the same
delivery surface/window as a dedicated OMX workflow status lane; workflow
transitions edit or replace that lane and must not overwrite the ordinary
technical-status lane. Because this is telemetry rather than live terminal
control, it obeys final-answer closure: stale or new OMX workflow status must
not appear below an already delivered final answer unless a new user turn
reopens status delivery. Unknown, stale, corrupt, or unrelated `.omx` state is
suppressed silently; strict pane fallback recognizes only OMX statusline/footer
shapes and never ordinary assistant prose. Pure-Codex windows with no recognized
OMX state behave as before.

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
- targeted Stop-hook/Ralph `<hook_prompt>` continuations are also hidden
  opener scaffolds when they instruct the runtime to continue work after a
  terminal answer; they may reopen generation without creating a visible user
  echo, while still rendering the operator prompt itself as warning-family
  content
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
- or to a targeted Stop-hook/Ralph continuation prompt that actually starts a
  runtime continuation
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
- orchestration milestones such as spawned/waiting/completed subagent summaries; multi-agent wait lists render each agent as its own tree row
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
For long-wait reviewer/progress commentary where an in-place Telegram edit
would leave the update hidden above the chat tail, the bot may delete the old
latest commentary artifact and re-send the replacement at the tail. This still
preserves one logical latest-only commentary artifact rather than accumulating
a commentary stack.
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
- truncation markers such as `preview 5/91 lines` live outside the fenced block and count the visible post-cleanup preview rows over the original total
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
- if the bound tmux window is gone or its Codex input plane has fallen back to a
  shell, inbound Telegram input may first repair the same `binding_scope=tmux`
  chain from durable cwd/runtime/conversation proof; if repair is ambiguous or
  unproven, queued delivery for that binding is dropped/fails closed
- explicit `/unbind`, topic close, or stale-window cleanup clears the tracked
  status artifact before normal cleanup continues
- deleted or uneditable Telegram status messages fall back to sending a new
  message or clearing the stale tracking entry
- Telegram `RetryAfter` is a transport backpressure signal, not proof that the
  current queue task was delivered. Durable content and ingress-receipt tasks
  must stay queue-owned and retry after the advised cooldown instead of being
  consumed by `queue.task_done()`; mutable/ephemeral artifacts may be retried or
  explicitly suppressed only with retry audit evidence.
- While a user queue is backed up, compact mutable lanes (`status`,
  `commentary`, `plan`, and `pending-input`) are coalesced at enqueue time to
  the latest same-surface/window/turn/lane task after the most recent durable
  ordering barrier. Durable content, final answers, warnings, ingress receipts,
  and terminal close tasks are never coalesced away.
- When a canonical `assistant_final` enters the queue, it becomes a final
  barrier for the same surface/window/turn. Queued `status`, `commentary`,
  `plan_update`, and pre-final `image_preview` updates behind that barrier are dropped with
  `final_barrier_dropped_queued_mutable_progress` audit instead of being sent
  just before or after the final. Pending-input previews are not dropped by this
  barrier because they describe future input rather than current-turn output.

This prevents:

- late events posting into explicitly unbound topics
- progress artifacts surviving after teardown
- stale tool-result edits targeting an old topic binding
- local ACK of a Telegram send/edit task that was actually rate-limited before
  reaching Telegram

## Runtime Update Typing Indicator

For runtime-originated updates that ccbot will dispatch to Telegram, ccbot sends
a Telegram `typing` chat action to the same delivery surface before enqueueing
the update. This is a transport hint only; it does not create durable content and
does not change compact bubble ordering. Typing actions are throttled per
effective Telegram `chat_id`/`message_thread_id` control surface and co-budgeted
per Telegram `chat_id` to at most one runtime-update typing action every three
seconds. When Telegram returns `RetryAfter` or repeated transport timeouts, the
chat enters a short degraded-transport cooldown: typing/status probes are
suppressed and audited as `telegram_backpressure`, while durable content
delivery remains queue-owned. Suppressed or non-dispatched internal events do
not emit typing.

## Backlog Metrics

ccbot exposes payload-free backlog snapshots for operators and future health
surfaces:

- Telegram delivery backlog is queue-owned: per user it reports queue depth,
  in-flight task type/class, oldest queued age, mutable vs durable queued counts,
  flood cooldown remaining, and the cumulative mutable-coalesced count.
- Replay backlog is monitor-owned: per tracked replay source it reports file
  size, last read/accepted byte offset, byte/line delta, parsed-but-not-dispatched
  count, callback-in-flight count, pending rollout event count, and
  delivery-queued event count.
- Delivery audit rows for queue-backed sends/edits/suppression/retry decisions
  include payload-free queue context: task class, queued age in milliseconds,
  queue depth observed at enqueue and send/audit time, and structured transport
  error context such as `transport_error_type`, `error_class`, `retry_after`,
  and `backpressure_reason`. Error text is compact and credential/payload
  redacted; raw Telegram payloads and bot tokens are never audit evidence.

These counters intentionally do not include raw prompt, assistant, command, or
tool payload text. Telegram queue backlog and Codex replay backlog are distinct:
replay bytes may be fully read while Telegram delivery remains queued, and
Telegram may be empty while unread replay evidence remains on disk.

## Queue And Steer

Message-layer sources are equal:

- Telegram-submitted text
- routed human text submitted through the same message-routing surface

Source does not affect priority.

Routing mode affects semantics:

- `queue`
  - normal turn submission through the live runtime input plane
  - for Codex, queue mode sends the payload and then uses Codex's `Tab`
    queued-message gesture instead of the normal Enter/C-m submit path; it
    does not require the pane to be idle before ccbot queues the prompt
- `steer`
  - directed immediate intervention into the current turn when the runtime supports it
  - this is the default for Codex-bound Telegram text so ccbot preserves
    ACK-verified submit behavior unless the user switches modes

Telegram controls:

- `/switch` toggles the persisted mode for the current effective `chat_id`/`message_thread_id` control surface.
- `/switch steer` and `/switch queue` set the persisted mode explicitly.
- `/steer <prompt>` and `/queue <prompt>` send one prompt with the named
  semantics; without a prompt they set the persisted mode.

Raw terminal control is not part of this equal message layer.
Direct human `tmux` input remains a separate operator intervention surface and
is not modeled as an ordinary queued semantic message.

For writable live tmux bindings, queued Telegram text enters that operator
surface as an injection operation with distinct phases:

- payload delivery
- runtime submit-key delivery
- runtime acknowledgement from same-runtime-identity replay evidence

Payload/key success is not a turn opener. For Codex conversational input,
single-line and multiline alike, ccbot treats the currently attached Codex
runtime identity's appended replay evidence as the durable proof that a new turn
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
identity matching fails, the bot must return an explicit delivery failure. If
payload and submit-key delivery succeed but replay ACK does not arrive within
the bounded window, the bot must surface an explicit delivered-but-unconfirmed
state instead of a hard failure or silent success.

If the active tmux pane is in tmux copy-mode/scrollback when Telegram input
arrives, ccbot treats that as a recoverable tmux-local mode, not as Codex
input readiness. It sends `Escape`, revalidates that `pane_in_mode` cleared,
and only then delivers the payload plus Codex replay-ACK proof. If `Escape`
does not clear the mode, input fails closed before payload delivery. Ingress
receipts include mode/target/prompt in the compact shape
`↗ Steer → @9 · comfy-agent-ops · /path` or
`⏭ Queue → @9 · comfy-agent-ops · /path` followed by the prompt preview, so
operators can distinguish tmux internal window IDs such as `@9` from visible
tmux indexes such as `7`.

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
- `<hook_prompt>`: deliver as an operator warning artifact, never as user echo;
  only targeted Stop-hook/Ralph continuation prompts also carry a hidden
  turn-opener side effect.
- file-change events: show file paths plus available preview lines in `sh`.
- `update_plan`: update the dedicated plan artifact with the actual plan body,
  not merely a `Plan updated` label.

For self-improvement, every Telegram delivery lifecycle decision is appended to
`telegram_delivery_audit.jsonl` under the ccbot config directory. The audit is
schema-versioned and records both positive and negative lifecycle events.
Dispatchable final answers are not treated as safely consumed merely because
their Codex replay bytes were read: the monitor persists replay offsets only
after callback handoff finishes, and assistant-final send failures produce an
explicit retryable failure path instead of silently accepting the replay cursor.
`send`, `edit`, and `delete` rows describe Telegram API attempts; `suppress`
rows explain intentional non-delivery such as stale turn output or a poll-only
`write_stdin` update arriving when no mutable status artifact exists, including
post-final pre-final artifacts dropped because the same turn's lane is already
closed. Rows
include action, control surface, task/content/semantic class, message id when
available, success flag, reason/error, turn generation and tool-use correlation
where available, text length/hash, and a compact preview. Operator replay
repairs use `replay_backfill` rows with replay path, byte offsets, call id, and
media hash for media, or `replay_backfill_text` rows with replay path, byte
offsets, turn id, and text hash for assistant-final text. Duplicate prevention
does not depend on mutable monitor offsets. Queue-backed rows may add task class,
queue age/depth, collapsed-mutable count context, and structured transport
fields (`transport_error_type`, `error_class`, `retry_after`,
`backpressure_reason`) so RetryAfter/timeout/backpressure incidents are
diagnosable without reading raw Bot API payloads. MarkdownV2 text sends/edits
that fall back to plain text also record the effective `render_mode`, the
`transport_outcome`, and formatted-vs-plain fallback error classes/types so a
plain-text rescue can be distinguished from a formatted Telegram render and
from a total transport failure. Direct `!` bash-capture output remains a
non-queue `bot.py` path, but its human-visible sends/edits also emit audit rows
with `task_type=direct_bash_capture` and the same render/outcome fields. It
deliberately omits full raw payloads and secrets, and redacts
credential-shaped fragments from error text.
