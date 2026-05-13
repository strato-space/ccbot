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
  - for shared group surfaces, user-scoped persisted records are an
    implementation detail: the effective binding is shared by the
    chat/topic-facing control surface for every allowed participant
  - for named topics in shared groups, the effective surface must be checked
    against the Telegram `chat_id`; equal `thread_id` values in different groups are not the same control surface

- **Telegram group routing coordinates**
  - physical Telegram delivery coordinates needed after a product control
    surface has been resolved
  - for shared named topics, the current persisted shape is
    `group_chat_ids[user_id:thread_id] -> Telegram group chat_id`
  - for no-topics group main chat mode, the current persisted shape is
    `group_chat_ids[user_id:0] -> Telegram group chat_id`
  - these coordinates are not the product control-surface identity; they are
    the transport routing data used for topic title sync and outbound delivery

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
  - it may hold explicit pre-bind input for later auto-send exactly once after
    binding succeeds when an entry path deliberately captures such input
  - it is not current-turn runtime output

- **Addressed entry**
  - explicit user action that may open or continue bind flow on a surface
  - current examples:
    - `/bind`
    - `/resume <thread-name|id>` where the runtime lane allows it
  - bot-addressed `@mention` is not an addressed entry for shared group
    surfaces
  - photo/document/sticker/audio/video ingress is not an addressed entry either; if the
    surface is not actively bound to a writable runtime, media is ignored
    without downloading, replying, or mutating bind state

- **Runtime helper window**
  - a live tmux window whose persisted runtime conversation identity belongs to
    a parent-controlled helper session, such as a Codex native subagent thread
    spawned from another Codex session
  - it is observable evidence for the parent task, not an independent Telegram
    control surface
  - default bind pickers must hide it, and stale picker callbacks that still
    reference it must fail closed
  - stale persisted bindings to helper windows must be pruned fail-closed on
    state refresh; while awaiting cleanup, getters and binding iterators must
    treat them as unbound
  - a tmux-window binding with no live process descriptor is also treated as
    inactive/unbound because the bot cannot prove it is a writable user surface
  - if helper telemetry is ever exposed to Telegram, it must be projected as
    parent orchestration milestones, not as a separately writable topic binding

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
- shared group topics require an explicit command entry path before bind flow
  may start
- shared group topics do not treat bot-addressed `@mention` as a bind-flow
  opener
- command-only entry paths in shared group surfaces must persist Telegram group
  routing coordinates before they mutate binding state
- for named topics this means `(user_id, thread_id) -> chat_id`; for no-topics
  main-chat mode this means `(user_id, 0) -> chat_id`
- no-topics main-chat mode is its own control-surface species; it must not be
  modeled by collapsing the whole chat container into the topic noun

## Operational Invariants

- one control surface has at most one active binding at a time
- one `(user_id, surface_key)` pair identifies at most one control surface
- in shared group surfaces, allowed participants resolve to the same active
  binding for the same chat/topic surface instead of creating per-user windows
- shared named-topic binding lookup must reject bindings from a different
  Telegram group even when the numeric topic/thread id is equal
- command-only entry must not depend on prior text, mention, or callback input
  to populate `group_chat_ids`
- outbound topic delivery and topic title synchronization must resolve through
  stored Telegram group `chat_id` coordinates, not through the Telegram user id
- pending-slot state is owned by the control surface, not by a runtime turn
- explicitly captured text before writable activation may auto-send once after
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
