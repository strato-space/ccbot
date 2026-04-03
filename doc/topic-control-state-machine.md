# Topic Control State Machine

`ccbot` keeps two topic-control axes separate:

- `topic_policy`: whether a topic may implicitly bind on a plain incoming
  message, or whether it must be explicitly bound again with `/bind`
- `binding_state`: whether a topic currently has no binding, is in a bind flow,
  or is bound to a live tmux window

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

### Transitions

- Plain message in `implicit_bind_allowed + none`
  - enters `bind_flow`
  - picker/window selection is shown
- Explicit `/bind`
  - sets `topic_policy = implicit_bind_allowed`
  - enters `bind_flow`
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

