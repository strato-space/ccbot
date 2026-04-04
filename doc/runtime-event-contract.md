# Runtime-Neutral Event Contract

This note closes `T25` for the multi-runtime topic-control plan.

The compact ontology companion for this note is
[`/home/tools/ccbot/ontology/delivery-surface.md`](/home/tools/ccbot/ontology/delivery-surface.md).
That file names the core delivery nouns; this note expands them into the
runtime-neutral contract.

## Goal

Telegram delivery must consume one runtime-neutral event contract even though
the underlying runtimes expose different evidence surfaces:

- Claude Code transcript events
- Codex rollout events
- fast-agent ACP-equivalent stream events with mirrored replay

The contract is semantic, not transport-specific. It does not require literal
`ACP-protocol` ownership of the live runtime stdio.

## Core fields

`NormalizedEvent` now carries two layers:

- legacy compatibility fields already used by the bot
  - `content_type`
  - `event_kind`
  - `role`
  - `tool_use_id`
- contract fields used for runtime-neutral delivery policy
  - `semantic_kind`
  - `delivery_class`
  - `include_in_history`
  - `dispatch_to_telegram`
  - `status_message_eligible`

## Semantic kinds

Required semantic kinds:

- `user_echo`
- `orchestration`
- `commentary`
- `reasoning`
- `tool_start`
- `tool_progress`
- `tool_result`
- `command_execution`
- `file_change`
- `assistant_final`
- `lifecycle`

Two higher-order ontological classes matter at delivery time:

- `terminal turn artifact`
  - currently `assistant_final`
- `pre-final visible artifact`
  - visible assistant-side artifacts that may appear before the terminal turn
    artifact, but never below it for the same turn
  - today this includes:
    - `commentary`
    - `orchestration`
    - any future surfaced preview bubble the product chooses to expose
- `technical status artifact`
  - mutable progress/status surface for ephemeral execution detail
  - may appear while the turn is running, but must not reappear below the
    terminal turn artifact for the same turn
- `user turn opener`
  - semantic fact that a new user turn has begun, regardless of whether the
    corresponding payload is visible in Telegram
  - may be represented by a visible user echo or by a hidden internal prompt
    scaffold that still begins a real runtime turn
- `turn generation`
  - per-topic ordering generation used by the bot layer so stale close tasks
    from an older turn cannot re-close the surface of a newer turn
  - the same generation barrier also drops stale pre-final artifacts, stale
    technical-status artifacts, and stale terminal turn artifacts once a newer
    turn has already opened
- `turn identity`
  - runtime-side identity for a specific user turn
  - in Codex rollout this may be an explicit `turn_id` from `turn_context`, or
    a surrogate turn key before the canonical id arrives
  - duplicate suppression and canonical-message preference must be scoped to
    turn identity rather than to bare `(role, phase, text)` alone

## Delivery classes

The semantic kinds collapse into three delivery classes:

- `history`
  - user-visible content that may appear in `/history`
- `progress`
  - live semantic progress that may drive Telegram status/progress behavior
- `lifecycle`
  - ephemeral control/lifecycle markers that must not pollute `/history`

## Current policy

Two levels must remain distinct:

- semantic eligibility in the runtime-neutral contract
- product projection onto the Telegram delivery surface

At the contract level:

- `lifecycle`
  - not dispatched to Telegram content handling
  - not included in `/history`
- `tool_progress`
  - dispatched as live semantic progress
  - not included in `/history`
- `commentary`, `reasoning`, `tool_start`, `tool_progress`
  - eligible to drive status/progress handling
- `user_echo`, `orchestration`, `commentary`, `assistant_final`
  - user-facing content candidates
- `tool_result`, `command_execution`, `file_change`
  - history-worthy semantic facts even when the product surface chooses to
    collapse them into compact status delivery
- `assistant_final`
  - is the terminal turn artifact
- `user_echo`
  - may act as the user turn opener
  - hidden internal prompt scaffolds may also act as user turn openers when
    they begin a real runtime turn
  - hidden internal technical payloads and hidden notifications are not user
    turn openers merely because they are suppressed from Telegram
- `commentary`, `orchestration`, and any surfaced preview bubble
  - are pre-final visible artifacts
  - they may be delivered before `assistant_final`
  - they must be suppressed or dropped if they would otherwise appear below
    `assistant_final` for the same turn
- if a pre-final visible artifact has already started a multipart send when a
  newer turn opens or the terminal turn artifact lands, the remaining parts of
  that send must abort rather than leaking below the new boundary
- the terminal turn artifact closes the surface only after the final assistant
  content has been delivered successfully in full, not after the first
  successful fragment of a multipart final send
- `reasoning`, `tool_start`, `tool_progress`, `tool_result`,
  `command_execution`, and `file_change`
  - may drive the mutable technical status artifact
  - once `assistant_final` lands for a turn, that technical status artifact
    must also close until the next user turn
- poll-driven technical status updates must carry the current topic turn
  generation, so background status polling cannot resurrect or clear the wrong
  turn after reopen

At the default product-facing `compact` Telegram surface:

- `user_echo` and `assistant_final`
  - remain ordinary content bubbles
- `orchestration`
  - remains a durable human-facing milestone bubble for multi-agent and
    supervisor coordination events such as spawned agent, waiting, and
    completed subagent summaries
- `commentary`
  - remains visible as a latest-only human-facing commentary artifact
- `reasoning`, `tool_start`, `tool_result`, `command_execution`, `file_change`
  - are typically projected into the mutable status artifact or suppressed when
    they are placeholder-only / raw-payload-only

For Codex rollout specifically:

- canonical `response_item.message` wins over duplicate lightweight `event_msg`
  message copies on Telegram/history
- cross-poll normalization may briefly buffer lightweight `event_msg` copies so
  a later canonical `response_item.message` can win without duplicate delivery
- if the canonical copy never arrives, the buffered lightweight copy may flush
  on a later idle poll rather than on an unrelated non-idle poll, so canonical
  preference survives cross-poll monitor churn
- `event_msg.user_message` is different:
  - in incremental monitor mode it may open the new turn immediately
  - restart/state hydration must preserve active-turn duplicate-suppression
    state long enough for the later canonical user copy to collapse into that
    same turn instead of reopening it a second time
  - if a later canonical `response_item.message(role=user)` is only a
    duplicate of that same opener inside the duplicate window, it is dropped
    rather than reopening the turn a second time
- `wait_agent` is also different:
  - `Waiting for ...` means the wait cycle is active
  - `Finished waiting ...` means the wait tool returned
  - any per-agent completion/failure statuses and timeout summaries are
    distinct follow-on orchestration facts, not substitutes for the finished
    waiting milestone

This preserves current Codex and Claude behavior while giving `fast-agent`
room to emit explicit `tool_progress` later without changing the queue layer
again.

## Input-plane boundary

The event contract is intentionally read-path only.

It does not collapse:

- equal message channels
- raw terminal operator control

`queue` and `steer` are routing semantics for submitted messages, not semantic
output kinds.
