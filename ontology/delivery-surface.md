# Delivery Surface Ontology

This note defines the Telegram-facing ontology for turn delivery.

Artifact ownership, deduplication, and closure are scoped to one Telegram
control surface at a time.

## Turn Artifacts

- **Terminal turn artifact**
  - `assistant_final`
  - the final assistant bubble that closes the turn

- **Pre-final visible artifact**
  - human-facing artifact that may appear before the terminal turn artifact,
    but never below it for the same turn
  - current examples:
    - `commentary`
    - `orchestration`
    - `plan_update`
    - runtime image preview artifacts explicitly promoted by product policy
    - any future surfaced preview bubble explicitly promoted by product policy

- **Technical status artifact**
  - mutable execution-status surface for ephemeral technical detail
  - examples:
    - reasoning/thinking
    - tool lifecycle
    - command execution progress
    - file-change churn

- **Terminal media result artifact**
  - `assistant_final` result whose primary user-facing payload is media rather
    than standalone text
  - current example: textual generated-image success output with a safely
    validated generated-image payload may be delivered as one Telegram photo bubble with a caption
    in compact mode when it substitutes for absent final assistant text
  - closes the same turn exactly like a final assistant text artifact, but only
    after media send acknowledgement or a terminal text fallback path completes
  - if path validation, file reading, or Telegram media send fails, the saved
    artifact path text remains the terminal fallback so the result is not lost
  - must be derived from replay evidence and must not become arbitrary local
    file disclosure

- **Runtime image preview artifact**
  - pre-final visible artifact whose primary payload is image media from runtime
    visual progress or inspection, such as Codex `image_generation_end` or
    `view_image` / `Viewed Image` output
  - sourced only from paired replay-embedded image bytes in the MVP; local path
    arguments are sanitized provenance, not authorization to read files
  - represents authorized replay-proven disclosure to the active bound control
    surface and must obey stale-turn, stale-binding, and pre-final-closed guards
  - does not close the assistant turn and must not be conflated with generated
    image terminal media results

- **Content attachment payload**
  - file payload attached to a content task rather than a standalone control
    artifact
  - current examples:
    - image data decoded from runtime `tool_result` blocks and delivered with
      `sendPhoto`
    - document/file data decoded from runtime `tool_result` blocks and delivered
      with `sendDocument`
  - follows the same stale-turn, stale-binding, and pre-final closure guards as
    its owning content task

- **External CLI result artifact**
  - operator/service-originated result delivery sent by the local
    `ccbot send_bot_message` CLI
  - scoped to a resolved Telegram control surface via stored Telegram group
    routing coordinates, or to explicit `chat_id` / `thread_id` overrides
  - may include document/file payloads such as result archives
  - is not runtime input, not replay evidence, not a user turn opener, and not
    an assistant-final artifact
  - must not create or mutate a tmux binding; it is a direct outbound Telegram
    delivery path for adjacent services built on top of a ccbot instance

- **Inbound media artifact path**
  - runtime input text produced from a Telegram media message after the control
    surface is already bound to a writable live runtime
  - only exists after Telegram Bot API download guardrails pass; oversized
    bot-inaccessible media must fail closed with a warning instead of creating
    a generic artifact-failure bubble
  - current examples:
    - photo/document ingress batches saved under `$CCBOT_DIR/images` or
      `$CCBOT_DIR/documents` and forwarded as a single `Attachments:` runtime
      input when the captured binding target still revalidates
    - audio original saved under `$CCBOT_DIR/media` and forwarded as
      `Audio artifact: /path`
    - video original saved under `$CCBOT_DIR/media` and forwarded as
      `Video artifact: /path`
  - may include optional enrichment such as transcript availability status or
    a video thumbnail line, but the saved original media artifact is the MVP
    payload
  - waiting/progress notices for inbound batches are Telegram ingress progress
    notices, not runtime user echoes, not replay evidence, not runtime turn
    openers, and not assistant-final artifacts
  - is not an outbound Telegram delivery artifact and must not use
    `ccbot send`

- **Pending input artifact**
  - mutable preview of future input already queued behind the current turn
  - examples:
    - queued follow-up messages
    - future queued-message edit hint
  - this artifact is not part of the current turn's visible output ordering
    contract and is not itself a user turn opener

- **Telegram ingress receipt**
  - immediate mutable acknowledgement for the current Telegram update on a
    bound writable surface
  - current example: `↗ Получил, отправляю в runtime…` for eligible simple
    Codex text while asynchronous replay proof is pending
  - not a replay-backed runtime user echo before ACK, not runtime proof, not a
    pending-input artifact unless the input is actually queued behind an active
    turn, and not an assistant-final artifact
  - may pass stale technical-status churn in the queue, but must not leapfrog an
    already queued terminal assistant-final artifact for the same control
    surface
  - on replay ACK success it may be edited/promoted into a confirmed user-input
    display and the later duplicate replay user echo is suppressed only after
    ordinary user-turn reopening side effects run
  - on a short ACK miss after payload/submit delivery it is edited or paired
    with a delivered-but-unconfirmed state; on hard delivery failure it is
    edited or paired with an explicit failure so it never remains
    indistinguishable from a successful runtime user echo
  - `send_chat_action("typing")` is only a transient Telegram transport signal:
    it is not this receipt, not a turn artifact, and not runtime proof

- **Interactive question artifact**
  - mutable Telegram projection of a runtime-owned blocking question
  - current source:
    - OMX durable `omx.question/v1` records under
      `.omx/state/questions/` or `.omx/state/sessions/*/questions/`
    - the explicit `--state-path` carried by a same-window OMX question
      renderer pane when the runtime is launched from an `.omx-runs` state root
  - not a technical status artifact and not an assistant-final artifact
  - belongs to the control surface whose live tmux window/cwd/renderer return
    or target pane produced the record
  - a temporary question renderer pane is an implementation detail inside the
    parent tmux window; it is not a bindable control surface, not a delivery
    source, and not a separate turn source
  - uses Telegram inline buttons for predefined options and updates in place
    until answered or no longer active
  - the first visible prompt must not jump ahead of the current turn's
    explanatory terminal/informational artifact while the pre-final lane remains
    open; status-poll discovery may defer creating a new question artifact until
    that lane closes, and may also wait for already queued informational or
    commentary content to drain
  - edits to an existing question artifact may proceed in place because they
    mutate an already visible control artifact rather than creating a new
    out-of-order prompt
  - prompt sends, edits, and first-send deferrals are delivery-audit events with
    interactive-question semantics; missing Telegram message ids are not proof
    of correct ordering
  - if the record allows `Other`, a free-text Telegram reply on the same bound
    surface is consumed as the `Other` answer instead of ordinary runtime input
  - a timeout/error record with a same-window renderer pane that is still alive
    and visibly matches the record remains recoverable and answerable; the
    timeout is not by itself final technical status
  - a renderer-start failure before a helper pane exists remains recoverable
    when session-scoped OMX mode state names a same-window tmux return pane;
    the return pane is a continuation bridge, not a bindable question surface
  - a replacement renderer pane may be materialized inside the same parent tmux
    window to restore the local operator view; this does not create a new
    control surface
  - answering writes the durable question record to terminal state
    `answered`, closes the temporary question pane, then best-effort bridges
    the answer through the bound runtime input path when a bound window is
    known (so runtime submit/ACK semantics apply), with recorded/session-state
    tmux return-pane send only as a fallback
  - while the durable record is active or recoverable, ordinary Telegram input
    to the same bound tmux window fails closed and must not bypass the question
    artifact
    unless it is consumed through the allowed `Other` lane

- **Warning artifact**
  - durable system notice
  - not a user turn opener
  - not a technical status artifact
  - repeated warning text on the same control surface deduplicates into one
    latest warning bubble, with a visible repeat counter only when `N > 2`
  - usage-limit / quota-exhaustion notices are warning-artifact subtypes
  - runtime-discontinuity system summaries are a warning-artifact subtype;
    they may send screenshot evidence before raw text and may opt into a
    distinct warning identity so separate exit/loss events do not collapse

- **User turn opener**
  - semantic fact that a new user turn has begun
  - this may be a visible user echo or a hidden internal prompt scaffold that
    still starts a real runtime turn
  - if the opener is hidden and the turn boundary would otherwise be missed by
    delivery, lifecycle `turn_started` may reopen the lanes idempotently, but
    only while the lanes are still closed
  - targeted Stop-hook/Ralph operator prompts that instruct the runtime to
    continue after a terminal answer are hidden opener scaffolds for ordering
    purposes only; they still render as warning-family operator prompts and
    never as ordinary user echo

- **Turn generation**
  - per-control-surface ordering generation used to prevent stale close tasks
    and stale artifacts from reopening or reclosing a newer turn

- **Turn identity**
  - runtime-side identity for a specific user turn
  - duplicate suppression and canonical-message preference must be scoped to
    turn identity rather than to bare text alone

## Delivery Classes

- **history**
  - user-visible content that may appear in `/history`

- **progress**
  - live semantic progress that may drive Telegram status behavior

- **lifecycle**
  - ephemeral control markers that must not pollute `/history`

## Compact Telegram Contract

Durable bubbles in `compact` mode are intentionally narrow:

- user echo
- orchestration milestones
- warning artifacts
- final assistant text
- terminal media result artifacts such as generated-image preview/photo bubbles
  with captions, when a safely validated local generated artifact substitutes
  for absent final assistant text
- runtime image preview artifacts such as Codex `image_generation_end` and
  `Viewed Image` preview/photo bubbles, when paired replay-embedded image bytes
  are available for the active bound control surface

In addition:

- latest commentary stays visible as a dedicated artifact
- one logical commentary artifact may be serialized into multiple Telegram
  messages when needed to preserve the full text
- long-wait reviewer/progress commentary may re-emit the latest commentary
  artifact at the chat tail instead of editing an older Telegram message in
  place, because Telegram edits do not make an old bubble visibly current; this
  remains one latest-only commentary artifact, not a stack
- latest Codex plan update stays visible as a separate mutable artifact and is
  updated only by newer `plan_update` events
- latest pending input preview may stay visible as a separate mutable artifact
- current-update Telegram ingress receipts may stay visible until their
  runtime-input proof confirms or fails; they remain distinct from pending
  input previews unless the input is genuinely queued for a future turn
- active runtime-owned questions stay visible as separate interactive question
  artifacts rather than being collapsed into the technical status artifact
- technical execution classes stay out of permanent bubbles by default
- warning artifacts use latest-warning dedup semantics rather than technical
  status churn semantics; distinct runtime-discontinuity warnings may opt into
  a separate warning identity so repeated exit/loss events do not collapse into
  one bubble solely by identical text
- when compactness and semantic clarity conflict, visibility-first mutable
  updates are preferred over ambiguous suppression

Technical execution classes include:

- reasoning / thinking
- tool lifecycle
- command execution
- file-change churn

Command-like tool output belongs to command execution even when the output
record arrives without its paired tool-call record in the current poll slice.
Developer transport metadata such as `Chunk ID`, wall time, token count, and
`Output:` is not user-facing content. Genuine non-command tool results remain
tool results rather than being forced into command execution.

## Ordering Invariants

- pre-final visible artifacts already queued may land before the terminal final
  answer
- the terminal turn artifact is always delivered as a fresh message sequence;
  it must not replace the visible commentary artifact
- before a new user turn advances the control-surface generation, any already
  queued terminal turn artifact for the previous generation must be flushed so
  it is delivered or explicitly send-failed, never silently stale-dropped
- only after final assistant content has been delivered successfully does the
  pre-final visible surface close
- only after final assistant content has been delivered successfully does the
  technical status artifact close
- no late pre-final visible artifact may appear below the final answer for the
  same turn
- no late technical status artifact may appear below the final answer for the
  same turn
- warning artifacts are outside the current-turn pre-final/status closure
  barrier and may remain visible across turns
- warning dedup is keyed by control surface and latest warning text, not by turn
- once the pre-final visible lane is closed, later commentary/orchestration/plan
  facts for that same generation must drop rather than reopen the lane
- lifecycle markers are not visible content by default, but `turn_started`
  may act as a lane-reopen fallback when hidden opener scaffolding already
  started a real turn and the pre-final/status lanes remained closed
- targeted Stop-hook/Ralph operator prompts may also reopen the lanes exactly
  once when the runtime continuation is the hidden opener; generic operator
  warnings do not reopen turn generation
- if an already-started multipart send becomes stale mid-flight, the remaining
  parts and trailing attachment payloads must abort rather than leaking below
  the new boundary
- pending input preview remains outside this terminal ordering barrier; it
  describes future queued input rather than current-turn output
- pending input preview closes on queue-owned lifecycle transitions such as
  queue-empty, binding-stale, or explicit clear, not on terminal assistant
  closure alone
- interactive question artifacts close when their durable record reaches a
  terminal status, when the binding disappears, or when the prompt is answered
  through Telegram; they are not closed merely by technical-status churn

## Preview Contract

When command/tool/file previews are surfaced:

- fenced code blocks contain only preview body lines
- truncation metadata lives outside the fenced block
- outcome metadata is a separate footer
- shell and file previews prefer `sh`
- structured payloads that are genuinely JSON prefer `json`
- the UI should not add a redundant footer like `completed · output 1 line(s)`
  when the preview already makes the outcome obvious
- `exec_command` / `functions.exec_command` is a shell-command artifact, not a
  generic tool artifact: its call and completion share the same `tool_use_id`,
  so completion should update the command surface instead of opening a separate
  `Tool Output` bubble
- developer/runtime wrapper metadata such as `Chunk ID`, `Wall time`,
  `Process exited with code`, `Original token count`, and the `Output:` marker
  is transport evidence and must be stripped before rendering; only the real
  stdout/stderr body belongs inside the command output preview
- Codex read/list/search summaries are valid human exploration artifacts; when
  the runtime emits parsed read/search/list command metadata, Telegram may
  render the Codex-style `• Explored` surface rather than a raw shell command

## Human Surface Audit And Operator Prompt Repair

The Telegram artifact is not the raw runtime payload. A runtime may emit
`exec_command`, `write_stdin`, `omx_state.state_write`, `<hook_prompt>`, file
change, or warning data as JSON or XML-shaped transport, but Telegram must
render the semantic fact first and keep the technical payload only as compact
preview evidence.

Additional artifact rules:

- **Operator prompt artifact**
  - hook/control prompts such as `<hook_prompt ...>...</hook_prompt>` are
    system/operator notices, not user messages
  - they must not be echoed as `👤 ...`
  - they render as warning-family artifacts with the hook body, not the raw XML
  - targeted Stop-hook/Ralph continuation prompts may additionally carry a
    hidden turn-opener side effect; this does not make the warning itself a
    user message
- **State update artifact**
  - OMX state writes render as a short semantic state transition such as
    `state_write: ralph`, phase, active flag, iteration, task summary, and
    context snapshot path
  - raw state JSON is debug evidence, not the default human artifact
- **Delivery audit artifact**
  - every Telegram send/edit/delete/suppress attempt should be recorded in a local JSONL audit
    with schema version, action, topic, artifact class, turn/tool correlation
    where available, text length/hash, compact preview, reason, and success/error
  - negative lifecycle rows (`suppress`, failed `delete`, failed send/edit) are
    first-class evidence, not debug noise; without them the audit cannot explain
    why a Codex/tmux-visible artifact did not appear in Telegram
  - the audit is not itself Telegram content; it is a self-improvement ledger
    for comparing Telegram readability with the Codex/tmux surface

Preview refinements:

- command and tool previews should be long enough to be useful before saying
  `preview N/M lines`; the default target is about twenty lines, not one-line
  summaries when a small real preview is available
- `write_stdin` with empty chars is a poll, not meaningful user content; it
  should summarize as a poll against a session rather than a raw JSON blob
- poll-only `write_stdin` updates must not create a new Telegram bubble and
  must not overwrite a more useful already-visible technical status artifact;
  suppress the poll and record whether it had no existing status or was kept
  from replacing an existing status
- status-polled `Tool Output` wrapper text is not a human artifact; strip
  transport metadata (`Chunk ID`, `Wall time`, process status, token counts,
  and the `Output:` marker) and render only the real command output preview
- Telegram `message is not modified` responses for mutable technical status
  edits are idempotent success, not send failures; they update local tracking
  and audit as `edit_noop` instead of creating a replacement bubble
- plan-update artifacts are latest-only within one assistant turn, not across
  turns; a new user turn must open a fresh plan artifact instead of editing the
  previous turn's plan bubble up-thread
- final assistant artifacts are terminal turn barriers; once a final answer is
  observed it must be delivered before a later queued user turn can advance the
  surface generation, and a user echo must never be rendered by editing an old
  technical status bubble
