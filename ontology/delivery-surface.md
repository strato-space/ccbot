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
    - any future surfaced preview bubble explicitly promoted by product policy

- **Technical status artifact**
  - mutable execution-status surface for ephemeral technical detail
  - examples:
    - reasoning/thinking
    - tool lifecycle
    - command execution progress
    - file-change churn

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

- **Pending input artifact**
  - mutable preview of future input already queued behind the current turn
  - examples:
    - queued follow-up messages
    - future queued-message edit hint
  - this artifact is not part of the current turn's visible output ordering
    contract and is not itself a user turn opener

- **Interactive question artifact**
  - mutable Telegram projection of a runtime-owned blocking question
  - current source:
    - OMX durable `omx.question/v1` records under
      `.omx/state/questions/` or `.omx/state/sessions/*/questions/`
  - not a technical status artifact and not an assistant-final artifact
  - belongs to the control surface whose live tmux window/cwd/renderer return
    or target pane produced the record
  - a temporary question renderer pane is an implementation detail inside the
    parent tmux window; it is not a bindable control surface, not a delivery
    source, and not a separate turn source
  - uses Telegram inline buttons for predefined options and updates in place
    until answered or no longer active
  - answering writes the durable question record to terminal state
    `answered`, then best-effort bridges the answer back to the recorded tmux
    return pane and closes the temporary question pane
  - while the durable record is active, ordinary Telegram input to the same
    bound tmux window fails closed and must not bypass the question artifact
  - unsupported free-text `Other` answers remain available in the tmux UI until
    a Telegram free-text answer lane is explicitly designed

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
- generated-image success text with a saved artifact path, when it substitutes
  for the absent final assistant text; this is not an automatic image
  attachment claim

In addition:

- latest commentary stays visible as a dedicated artifact
- one logical commentary artifact may be serialized into multiple Telegram
  messages when needed to preserve the full text
- latest Codex plan update stays visible as a separate mutable artifact and is
  updated only by newer `plan_update` events
- latest pending input preview may stay visible as a separate mutable artifact
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
