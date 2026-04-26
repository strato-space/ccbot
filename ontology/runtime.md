# Runtime Ontology

This note defines the core runtime nouns for `ccbot`.

## Definitions

- **Telegram control surface**
  - the canonical user-facing routing lane in Telegram
  - current species:
    - named topic control surface
    - no-topics main-chat control surface

- **Telegram topic**
  - the named-topic species of Telegram control surface
  - realized as a forum topic in a topic-enabled chat

- **No-topics main-chat control surface**
  - a chat-wide control surface used when forum topics are unavailable
  - canonically represented in the current product surface by `thread_id is None`
  - is not a claim that `chat == topic`

- **Surface key**
  - the canonical persisted local key for a Telegram control surface under one
    user scope
  - concrete shapes in the current code:
    - `t:<thread_id>`
    - `c:<chat_id>`
  - this is a product persistence noun, not a Telegram API noun
  - it is not the full control-surface identity by itself

- **Control-surface identity**
  - the full persisted identity for a control surface in the current storage
    model
  - current concrete shape: `(user_id, surface_key)`
  - this prevents `t:<thread_id>` values from being treated as globally unique
    outside their persisted user scope

- **Topic transport identifier**
  - Telegram transport token such as `message_thread_id`
  - identifies a topic at the API/storage boundary
  - is not identical to the topic itself

- **Control-surface policy**
  - persisted rule that governs whether a control surface may trigger implicit
    bind or instead requires explicit bind
  - current canonical values:
    - `implicit_bind_allowed`
    - `manual_bind_required`

- **Topic control policy**
  - the topic-shaped legacy compatibility wrapper around the canonical
    control-surface policy

- **Binding**
  - persisted association from a Telegram control surface to a delivery source,
    together with runtime metadata needed for safe routing and delivery
  - binding scope is explicit:
    - `tmux` for a live terminal container
    - `external` for a persisted runtime thread without live tmux attachment

- **tmux window**
  - the live terminal container managed by the bot

- **Runtime process**
  - the live interactive CLI process inside the tmux window
  - examples: `codex`, `claude`, `fast-agent`

- **Runtime conversation identity**
  - the persisted conversation object that can later be resumed
  - examples: Codex thread, Claude Code session, fast-agent `session_id`

- **Semantic emitter / supervisor**
  - the runtime-side or wrapper-side layer that emits machine-readable semantic
    events without taking over the live human CLI stdio

- **Live semantic stream**
  - machine-readable event stream observed while the runtime is active

- **Persisted replay evidence**
  - append-only or otherwise replayable persisted evidence used for restart
    recovery, history reconstruction, and deterministic testing

- **Normalized event**
  - runtime-neutral event object consumed by Telegram delivery and history
    layers

- **Message channel**
  - routed text-producing source that submits atomic messages through the
    message plane
  - Telegram is mandatory
  - human-routed text submission may be equal at the message layer

- **Operator control layer**
  - raw human terminal action outside the message plane
  - examples: direct tmux attach, Ctrl+C, shell recovery

- **Routing mode**
  - semantic handling mode for a submitted message
  - current required modes: `queue`, `steer`

- **Input injection plane**
  - capability to inject text/keys into a live runtime process
  - available only when the control surface is bound to a live tmux scope
  - external-thread binding may stay read-only when no live injection plane is
    attached

## Canonical Model

`Telegram control surface --governed by control-surface policy--> may or may not enter a binding flow`

`binding -> delivery source`

`binding_scope=tmux -> tmux window -> runtime process`

`binding_scope=external -> runtime conversation identity -> persisted replay evidence`

`runtime process -> binds to runtime conversation identity`

`runtime process -> semantic emitter / supervisor`

`semantic emitter / supervisor -> live semantic stream`

`semantic emitter / supervisor -> persisted replay evidence`

`runtime conversation identity scopes/indexes the live semantic stream and persisted replay evidence`

`live semantic stream and/or persisted replay evidence -> normalized events`

`normalized events -> Telegram notifications / history views`

Separate no-topics mode:

`chat without forum topics -> no-topics main-chat control surface (thread_id is None) -> binding -> ...`

## Message Plane vs Operator Layer

Equal message channels:

- Telegram
- human text routed through the same atomic message surface

Rules:

- channels are equal at the message layer
- source does not affect priority
- mode affects routing semantics

Raw operator control is different:

- direct tmux keystrokes are not ordinary message-channel events
- human terminal takeover remains a separate operator layer

## Operational Invariants

- a topic may bind to at most one delivery source at a time
- a control surface may bind to at most one delivery source at a time
- a chat without forum topics may expose one shared no-topics main-chat mode
  for the control plane
- a tmux window may host at most one active runtime process at a time
- a live process may be associated with at most one primary runtime
  conversation identity at a time
- a runtime conversation identity may have multiple historical replay artifacts
- resume attaches a new or reused live process to an existing identity; it does
  not restore the previous live process
- history is reconstructed from normalized replay evidence, not from the live
  process buffer
- surface-scoped maps are canonical; topic-scoped maps are compatibility
  mirrors
- surface-scoped policy/binding-state maps are canonical; topic-scoped maps are
  compatibility mirrors
- external-thread bind may deliver replay events without exposing a live input
  injection plane
- when a tmux window disappears but replay evidence remains readable, the
  control surface may transition from `binding_scope=tmux` to
  `binding_scope=external` instead of being silently cleared
- live-runtime presence is semantic rather than banner-bound: a visible active
  Codex footer/status surface or input prompt still counts as a live runtime
  surface even after the initial startup banner has scrolled out of the pane
- if no live input injection plane exists, Telegram text/keys must fail closed
  as read-only rather than pretending to send into tmux
