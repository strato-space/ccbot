# Topic Control State Machine

This file is a derived state-machine note. The master ontology for this layer
now lives in:

- [`/home/tools/ccbot/ontology/topic-control.md`](/home/tools/ccbot/ontology/topic-control.md)
- [`/home/tools/ccbot/ontology/runtime.md`](/home/tools/ccbot/ontology/runtime.md)

If this file conflicts with those ontology notes, the ontology notes win.

## Derived Model

`ccbot` keeps two control axes separate for one canonical `control surface`:

- `surface_policy`: whether a surface may implicitly enter bind flow on plain
  incoming text, or whether it must be explicitly rebound
- `binding_state`: whether the surface currently has no binding, is inside bind
  flow, or is already bound

This note remains state-machine centric. It does not redefine the nouns.

## Storage Shape

Canonical persisted control-state maps are surface-scoped:

- `surface_policies[user_id][surface_key]`
- `surface_binding_states[user_id][surface_key]`
- `surface_bindings[user_id][surface_key] -> window_id`
- `surface_pending_slots[user_id][surface_key] -> deferred addressed input`

Compatibility topic mirrors still exist for topic-shaped callers:

- `topic_policies[user_id][thread_id]`
- `topic_binding_states[user_id][thread_id]`
- `thread_bindings[user_id][thread_id] -> window_id`

## Surface Kinds

### Named topic control surface

- canonical persisted key: `t:<thread_id>`
- corresponds to a forum topic in a topic-enabled chat

### No-topics main-chat control surface

- canonical persisted key: `c:<chat_id>`
- corresponds to a shared main-chat control surface when forum topics are
  unavailable
- `thread_id is None` is the product-side marker for this mode

## State Chart

### Policy axis

- `implicit_bind_allowed`
- `manual_bind_required`

### Binding axis

- `none`
- `bind_flow`
- `bound`

## Transition Rules

### Private named topics

- Plain message in `implicit_bind_allowed + none`
  - may enter `bind_flow`
  - picker/window selection is shown

### Shared group topics

- Ordinary non-addressed plain message in `none`
  - stays silent
  - does not open bind flow
- Explicit `/bind`
  - may enter `bind_flow`
- Explicit `/resume <thread>`
  - remains an allowed explicit entry path where the runtime lane supports it
- Bot-addressed `@mention`
  - may open bind/help flow
  - may populate one surface-scoped pending slot
  - does not itself execute runtime work until writable activation succeeds

### No-topics main-chat control surface

- behaves as one chat-wide control surface rather than as a hidden topic
- may bind canonically by `chat_id`
- follows the same policy/binding-state split as named topics

### Shared transitions

- Picker selection or directory confirm
  - enters `bound`
  - writes `surface_bindings`
- Explicit `/unbind`
  - clears `surface_bindings`
  - sets `surface_policy = manual_bind_required`
  - sets `binding_state = none`
- Picker cancel
  - clears bind-flow UI state
  - sets `surface_policy = manual_bind_required`
  - sets `binding_state = none`
- Stale binding cleanup
  - clears `surface_bindings`
  - sets `binding_state = none`
  - leaves `surface_policy` unchanged
- Topic/chat reopen
  - does not reset `surface_policy`
  - does not re-enable implicit bind after explicit unbind/cancel

## Invariants

- `manual_bind_required` must never silently revert to implicit binding.
- `bind_flow` is transient control state, not a live delivery source.
- `bound` is the only state that may carry a live binding record.
- `surface_policy != binding_state` remains a hard distinction.
- In shared group topics, ordinary non-addressed user text is not itself a
  bind-flow opener.
- Read-only external binding must fail closed for new Telegram input instead of
  pretending to provide writable tmux control.
- Pending-slot state belongs to the control surface and must not execute early.
