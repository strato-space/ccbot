# Topic And Surface Control Ontology

This note defines the control-surface nouns that govern bind entry, binding
state, and deferred user input.

If this note conflicts with any explanatory note in `doc/`, this note wins.

## Definitions

- **Telegram control surface**
  - the canonical user-facing routing target in `ccbot`
  - every persisted delivery and bind decision is scoped to exactly one control
    surface
  - current species:
    - named Telegram topic
    - no-topics main-chat control surface

- **Named topic control surface**
  - a forum topic in a topic-enabled chat
  - uses a non-`None` topic transport id

- **No-topics main-chat control surface**
  - a chat-wide control surface used when forum topics are unavailable
  - canonically represented by chat identity plus `thread_id is None`
  - is not a claim that `chat == topic`

- **Surface key**
  - the canonical persisted local key for a control surface under one user scope
  - current concrete encodings:
    - `t:<thread_id>` for named topics
    - `c:<chat_id>` for no-topics main-chat surfaces
  - this is a product-side persistence key, not a Telegram domain noun
  - it is not the full control-surface identity by itself

- **Control-surface identity**
  - the full persisted identity for a control surface in the current storage
    model
  - current concrete shape: `(user_id, surface_key)`
  - this is the identity used when reasoning about uniqueness across persisted
    bot state

- **Surface policy**
  - persisted rule governing whether plain user text may enter bind flow or
    whether explicit bind is required
  - current canonical values:
    - `implicit_bind_allowed`
    - `manual_bind_required`

- **Binding state**
  - persisted control state describing whether a surface currently has no
    binding, is inside bind flow, or is already bound
  - current canonical values:
    - `none`
    - `bind_flow`
    - `bound`

- **Bind-flow credentials**
  - freshness markers for callback/UI continuity within one bind-flow session
  - current fields:
    - version
    - nonce
  - stale callbacks must fail closed rather than mutating a newer bind flow

- **Pending slot**
  - surface-scoped deferred user intent captured before writable activation is
    complete
  - it may hold addressed text for later auto-send exactly once after binding
    succeeds
  - it is not current-turn runtime output

- **Addressed entry**
  - explicit user action that may open or continue bind flow on a surface
  - current examples:
    - `/bind`
    - `/resume <thread-name|id>` where the runtime lane allows it
    - bot-addressed `@mention`

## Canonical Model

`Telegram control surface --governed by surface policy--> may or may not enter bind flow`

`bind flow --freshened by bind-flow credentials--> explicit picker / selection state`

`binding state == bound -> control surface -> binding -> delivery source`

`pending slot -> deferred user input for one control surface`

Named-topic variant:

`(user_id, surface_key=t:<thread_id>) -> named topic control surface`

No-topics main-chat variant:

`(user_id, surface_key=c:<chat_id>) + thread_id is None -> no-topics main-chat control surface`

## State Semantics

- `surface policy` and `binding state` are distinct axes
- `bind_flow` is transient control state, not a delivery source
- `bound` is the only binding state that may own an active binding record
- stale binding cleanup may clear the binding while leaving the surface policy
  unchanged
- explicit `/unbind` or bind-flow cancel may force `manual_bind_required`
  without destroying the distinction between policy and binding state

## Entry Rules

- named private topics may allow implicit bind when policy permits it
- shared group topics do not treat ordinary non-addressed text as a bind-flow
  opener
- shared group topics require an addressed or explicit entry path before bind
  flow may start
- no-topics main-chat mode is its own control-surface species; it must not be
  modeled by collapsing the whole chat container into the topic noun

## Operational Invariants

- one control surface has at most one active binding at a time
- one `(user_id, surface_key)` pair identifies at most one control surface
- pending-slot state is owned by the control surface, not by a runtime turn
- addressed text captured before writable activation may auto-send once after
  activation succeeds, but must not execute early
- legacy `topic_*` maps are compatibility mirrors over the canonical
  surface-scoped maps

## Legacy Compatibility

The codebase still exposes topic-shaped wrappers such as:

- `topic_policy`
- `topic_binding_state`
- `thread_bindings`

These are compatibility views for topic-shaped callers. The canonical persisted
model is surface-scoped, not topic-scoped in the general case.
