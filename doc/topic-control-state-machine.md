# Topic Control State Machine

`ccbot` keeps two topic-control axes separate for the canonical topic-based
runtime surface:

- `topic_policy`: whether a topic may implicitly bind on a plain incoming
  message, or whether it must be explicitly bound again with `/bind`
- `binding_state`: whether a topic currently has no binding, is in a bind flow,
  or is bound to a live tmux window

This note is intentionally **topic-centric** and follows the canonical runtime
ontology:

`Telegram topic -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`

A group chat **without topics** may expose one shared **main-chat mode**
canonically marked by `thread_id is None` in the current product surface.
That is not a claim that `chat == topic`; it is a separate chat-wide control
mode that coexists with the named-topic state machine below.

## Storage Shape

Persisted topic control state lives in `state.json` alongside the existing
`thread_bindings` map:

- `topic_policies[user_id][thread_id]`
- `topic_binding_states[user_id][thread_id]`
- `thread_bindings[user_id][thread_id] -> window_id`

The persisted policy and binding-state maps are the source of truth. The live
binding map remains the concrete window association.

## State Chart

### Policy axis

- `implicit_bind_allowed`
- `manual_bind_required`

### Binding axis

- `none`
- `bind_flow`
- `bound`

## Transition Rules For Named Topics

### Private topic-capable chats

- Plain message in `implicit_bind_allowed + none`
  - may enter `bind_flow`
  - picker/window selection is shown

### Group/supergroup topics

- Ordinary non-addressed plain message in `none`
  - stays silent
  - does **not** open bind flow
- Explicit `/bind`
  - sets `topic_policy = implicit_bind_allowed`
  - enters `bind_flow`
- Explicit `/resume <thread>`
  - is an allowed explicit re-entry path from `none`
- Bot-addressed `@mention`
  - may open bind/help flow
  - may populate one topic-scoped pending slot
  - does not itself execute runtime work until writable activation succeeds

### Shared transitions

- Picker selection or directory confirm
  - enters `bound`
  - `thread_bindings` is written
- Explicit `/unbind`
  - clears `thread_bindings`
  - sets `topic_policy = manual_bind_required`
  - sets `binding_state = none`
- Picker cancel
  - clears bind-flow UI state
  - sets `topic_policy = manual_bind_required`
  - sets `binding_state = none`
- Stale binding cleanup
  - clears `thread_bindings`
  - sets `binding_state = none`
  - leaves `topic_policy` unchanged
- Topic reopen
  - does not reset `topic_policy`
  - does not re-enable implicit bind after an explicit unbind/cancel

## Invariants

- `manual_bind_required` must never silently revert to implicit binding.
- `bind_flow` is a transient control state, not a live window binding.
- `bound` is the only state that may carry a live `thread_bindings` entry.
- `topic_policy != binding_state` remains a hard distinction.
- In shared group topics, ordinary non-addressed user text is not itself a bind-flow opener.
- Read-only external binding must fail closed for new Telegram input instead of pretending to provide writable tmux control.
