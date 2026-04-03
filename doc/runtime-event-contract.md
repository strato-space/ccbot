# Runtime-Neutral Event Contract

This note closes `T25` for the multi-runtime topic-control plan.

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
- `commentary`
- `reasoning`
- `tool_start`
- `tool_progress`
- `tool_result`
- `command_execution`
- `file_change`
- `assistant_final`
- `lifecycle`

## Delivery classes

The semantic kinds collapse into three delivery classes:

- `history`
  - user-visible content that may appear in `/history`
- `progress`
  - live semantic progress that may drive Telegram status/progress behavior
- `lifecycle`
  - ephemeral control/lifecycle markers that must not pollute `/history`

## Current policy

- `lifecycle`
  - not dispatched to Telegram content handling
  - not included in `/history`
- `tool_progress`
  - dispatched as live semantic progress
  - not included in `/history`
- `commentary`, `reasoning`, `tool_start`, `tool_progress`
  - eligible to drive status/progress handling
- `user_echo`, `assistant_final`, `tool_result`, `command_execution`, `file_change`
  - treated as history-worthy content

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
