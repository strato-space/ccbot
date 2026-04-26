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

- **Pending input artifact**
  - mutable preview of future input already queued behind the current turn
  - examples:
    - queued follow-up messages
    - future queued-message edit hint
  - this artifact is not part of the current turn's visible output ordering
    contract and is not itself a user turn opener

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

In addition:

- latest commentary stays visible as a dedicated artifact
- one logical commentary artifact may be serialized into multiple Telegram
  messages when needed to preserve the full text
- latest Codex plan update stays visible as a separate mutable artifact and is
  updated only by newer `plan_update` events
- latest pending input preview may stay visible as a separate mutable artifact
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

## Ordering Invariants

- pre-final visible artifacts already queued may land before the terminal final
  answer
- the terminal turn artifact is always delivered as a fresh message sequence;
  it must not replace the visible commentary artifact
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
  parts must abort rather than leaking below the new boundary
- pending input preview remains outside this terminal ordering barrier; it
  describes future queued input rather than current-turn output
- pending input preview closes on queue-owned lifecycle transitions such as
  queue-empty, binding-stale, or explicit clear, not on terminal assistant
  closure alone

## Preview Contract

When command/tool/file previews are surfaced:

- fenced code blocks contain only preview body lines
- truncation metadata lives outside the fenced block
- outcome metadata is a separate footer
- shell and file previews prefer `sh`
- structured payloads that are genuinely JSON prefer `json`
- the UI should not add a redundant footer like `completed · output 1 line(s)`
  when the preview already makes the outcome obvious
