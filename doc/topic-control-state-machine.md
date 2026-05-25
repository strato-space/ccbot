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
- `surface_pending_slots[user_id][surface_key] -> deferred explicit pre-bind input`
- `surface_titles[title_surface_key] -> Telegram-visible title`
- `group_chat_ids[surface_key] -> Telegram group chat_id`

In shared group topics and no-topics group main chats, a persisted binding under
one allowed user is the effective binding for the whole chat/topic control
surface. Other allowed participants resolve that same `surface_key` to the same
tmux window instead of opening a parallel per-user window.

For named topics, shared lookup must still verify the Telegram `chat_id`.
Identical numeric `thread_id` values in different groups are different control
surfaces and must not share a binding.

The `group_chat_ids` map stores Telegram transport routing coordinates for
outbound delivery and topic title sync. It is not the control-surface identity.
Command-only entry must refresh it directly because shared group mentions are
not bind-flow openers.

The `surface_titles` map stores optional human-facing title metadata captured
from Telegram topic create/edit service updates. Named-topic title keys are
chat-qualified as `t:<chat_id>:<thread_id>` when Telegram coordinates are known,
so same-numbered topics in different groups cannot bleed display names into one
another. During state migration, legacy bare `t:<thread_id>` title entries are
backfilled to a chat-qualified key only when `group_chat_ids` proves exactly one
Telegram group chat coordinate for that topic id; ambiguous same-numbered topics
remain unpromoted until a fresh Telegram title event arrives. Telegram service
updates are delivered to an actor, but the title is surface-scoped: another
allowed actor may reuse the stored title only for the same exact chat-qualified
control surface. A stored title may seed a fresh tmux
window name, but it is not the binding, not the cwd, and not runtime/replay
identity. When no title is known, fresh bind must not rename the Telegram topic
to a cwd basename just because a directory was selected. When the final tmux
display name differs because of collision suffixing or reuse, successful
Telegram title sync updates the cached title metadata to that final name.

### User-scoped map audit

As of the 2026-05-24 ccbot-d4v audit, the already-flattened effective
surface-behavior maps are `surface_routing_modes`, `surface_titles`, and
`group_chat_ids`. A canonical `ControlSurfaceIdentity` helper now centralizes
surface-key parsing and title-key derivation for topic, chat, and legacy
compatibility forms. The remaining `user_id -> surface_key` maps are retained
intentionally because the helper makes owner/audit identity explicit without
flattening permission-bearing state:

- `surface_bindings` / `external_surface_bindings` record the actor that created
  or owns the persisted binding while shared group lookup may reuse the binding
  for other allowed participants on the same exact chat/topic surface.
- `surface_binding_states`, `surface_bind_flow_versions`, and
  `surface_bind_flow_nonces` are bind-flow credentials/state; flattening them
  would mix concurrent actors and stale callback protection.
- `surface_pending_slots` belongs to the actor-owned future-input lane and must
  not become a shared current-turn artifact.
- `surface_policies` currently looks like surface behavior, but its writes are
  still coupled to actor-owned bind/reset flows and legacy topic compatibility
  mirrors. It should not be flattened by string-key edits alone; do that only
  after the identity helper makes the permission/audit owner and effective
  chat/thread surface explicit.

Therefore no additional map is safe to flatten in-place by string-key edits
alone. Future flattening must continue using `ControlSurfaceIdentity` and must
preserve actor/audit ownership separately from effective shared chat/thread
behavior.

Compatibility topic mirrors still exist for topic-shaped callers:

- `topic_policies[user_id][thread_id]`
- `topic_binding_states[user_id][thread_id]`
- `thread_bindings[user_id][thread_id] -> window_id`

## Surface Kinds

### Named topic control surface

- canonical persisted key: `t:<chat_id>:<thread_id>` when Telegram
  coordinates are known; bare `t:<thread_id>` is legacy mirror/fallback data
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

- If the topic already has a live binding created by another allowed
  participant in the same Telegram group, text and explicit controls resolve to
  that shared binding.
- Ordinary non-addressed plain message in `none`
  - stays silent
  - does not open bind flow
- Bot-addressed `@mention` in `none`
  - stays silent
  - does not open bind flow
- Bot-instance foreign surface gate
  - when `CCBOT_OWNED_SURFACES` or `CCBOT_IGNORED_SURFACES` classifies a
    shared group surface as foreign to this bot instance, every update type is
    hard-ignored before typing, replies, downloads, runtime-input audit, or tmux
    input
- Explicit `/bind` or `/bind@<this-bot>`
  - first captures Telegram group routing metadata for this surface
  - may enter `bind_flow`
- Any other update in an unbound shared group named topic, including
  `/bind <args>`, `/resume <thread>`, `/steer`, `/queue`, `/switch`, bot-addressed mentions,
  ordinary text, media, and non-bind callbacks
  - stays silent
  - does not emit typing, replies, downloads, audit rows, state mutations, or
    tmux/runtime input
  - may become valid only after the surface has an active binding or in a
    non-shared/non-named-topic lane that explicitly supports it
- Fresh bind-flow picker callbacks are allowed only as continuations of the exact
  `/bind` or `/bind@<this-bot>` entry and must validate bind-flow credentials

### No-topics main-chat control surface

- behaves as one chat-wide control surface rather than as a hidden topic
- may bind canonically by `chat_id`
- follows the same policy/binding-state split as named topics

### Shared transitions

- Picker selection or directory confirm
  - enters `bound`
  - writes `surface_bindings`
  - may use stored `surface_titles` for the tmux display name
  - after successful Telegram title sync, records the final tmux display name
    as the latest surface title
  - keeps replay delivery silent until the bound runtime identity is proven
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
- `bind_flow` is transient control state, not a live delivery source; for
  runtime delivery it is still unbound.
- `bound` is the only state that may carry a live binding record.
- `surface_policy != binding_state` remains a hard distinction.
- In shared group topics, ordinary non-addressed user text is not itself a
  bind-flow opener.
- Bot-addressed `@mention` is also not a bind-flow opener in shared group
  surfaces.
- In shared group surfaces, allowed users are peers for one chat/topic binding;
  the user who created the binding does not own a separate runtime lane.
- Same-numbered topics in different Telegram groups are not peers and must not
  resolve to each other's binding.
- Command-only entry must not rely on a previous non-command text, mention, or
  callback update to resolve Telegram group chat routing.
- Read-only external binding must fail closed for new Telegram input instead of
  pretending to provide writable tmux control.
- Pending-slot state belongs to the control surface and must not execute early.
