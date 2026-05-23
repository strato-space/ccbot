# Runtime Ontology For Multi-Runtime Topic Control

This file is a derived maintainer note. The master ontology now lives in
[`/home/tools/ccbot/ontology/README.md`](/home/tools/ccbot/ontology/README.md).

If this note conflicts with anything under `ontology/`, `ontology/` wins.

## Canonical Ontology Entry Points

- [`/home/tools/ccbot/ontology/README.md`](/home/tools/ccbot/ontology/README.md)
- [`/home/tools/ccbot/ontology/runtime.md`](/home/tools/ccbot/ontology/runtime.md)
- [`/home/tools/ccbot/ontology/topic-control.md`](/home/tools/ccbot/ontology/topic-control.md)
- [`/home/tools/ccbot/ontology/delivery-surface.md`](/home/tools/ccbot/ontology/delivery-surface.md)
- [`/home/tools/ccbot/ontology/boundaries.md`](/home/tools/ccbot/ontology/boundaries.md)

## Why This Slave Note Exists

The older implementation and plan history used `session`, `topic`, `window`,
and `thread` too loosely. The master ontology now separates those kinds
explicitly. This file exists only as the expanded explanatory bridge from specs
and code to that master ontology.

## Runtime Layer Summary

The runtime layer must keep these kinds distinct:

- `control surface`
- `control-surface policy`
- `binding`
- `runtime process`
- `runtime conversation identity`
- `semantic emitter / supervisor`
- `live semantic stream`
- `persisted replay evidence`
- `input acknowledgement`

The controlling relation is:

`Telegram control surface --governed by control-surface policy--> may or may not enter a binding flow`

Then:

`binding_scope=tmux -> tmux window -> runtime process`

`binding_scope=external -> runtime conversation identity -> persisted replay evidence`

`runtime process -> semantic emitter / supervisor`

`semantic emitter / supervisor -> live semantic stream`

`semantic emitter / supervisor -> persisted replay evidence`

`runtime conversation identity scopes/indexes the live semantic stream and persisted replay evidence`

`live semantic stream and/or persisted replay evidence -> normalized events`

This remains compatible with the plan language in
[`/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md`](/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-2.md),
but the canonical nouns now live under `ontology/`, not under `doc/`.

## Topic And Main-Chat Control

The master ontology no longer treats `Telegram topic` as the universal genus.
The code now models a broader `control surface` with two species:

- named topic control surface
- no-topics main-chat control surface

That is the minimal repair required by real code:

- `surface_key=t:<chat_id>:<thread_id>` for named topics when Telegram
  coordinates are known; bare `t:<thread_id>` is legacy mirror/fallback data
- `surface_key=c:<chat_id>` for no-topics main-chat mode
- full persisted control-surface identity is `(user_id, surface_key)`; the
  `surface_key` alone is a local product key, not a globally unique surface
  identity

`thread_id is None` may canonically mark the no-topics main-chat mode in the
product surface, but this is still not the same claim as `chat == topic`.

## Capability And Control Notes

- `Input injection plane` exists only for live `tmux` bindings.
- `Input acknowledgement` is persisted proof that an injected message became a
  runtime turn. For Codex, the proof is an appended rollout JSONL event such as
  `turn_context` or a matching user-message record; pane reaction is diagnostic only.
- `binding_scope=external` may preserve replay delivery while staying
  read-only rather than pretending to send into tmux.
- `queue` and `steer` remain message-plane routing modes.
- `literal ACP-protocol-over-stdio` remains rejected as the primary operator
  surface because tmux stdio stays human-first.

## Forbidden Collapses

The master ontology rejects, among others:

- `policy == binding`
- `bind flow == message routing`
- `status notification == content delivery`
- `control surface == topic`
- `window == thread`
- `process == replay evidence`

## Related Derived Notes

- [`/home/tools/ccbot/doc/topic-control-state-machine.md`](/home/tools/ccbot/doc/topic-control-state-machine.md)
  explains the state machine derived from `ontology/topic-control.md`.
- [`/home/tools/ccbot/doc/runtime-event-contract.md`](/home/tools/ccbot/doc/runtime-event-contract.md)
  explains how normalized runtime semantics project onto delivery classes.
