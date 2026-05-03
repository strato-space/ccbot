# Ontological Boundaries

This note records the project boundaries that must stay explicit in code and
docs.

## ACP Distinction

- **ACP-protocol**
  - the agent protocol and semantic reference vocabulary

- **ACP-module**
  - the existing `ccbot` product surface named `ACP`
  - this is a legacy module boundary, not the same thing as the protocol

These two must not be conflated.

## Human-First Transport Rule

The live runtime `stdin/stdout` belongs to the human-observable CLI surface
hosted in tmux.

Therefore `ccbot` rejects `ACP-first transport over live stdio` as the primary
operator model.

Accepted order of priority:

- tmux stdio CLI interface first
- semantic transport second
- ACP-equivalent event model as the target meaning layer

This is not anti-ACP. It is a boundary claim: human observability, injection,
and operator control outrank protocol purity for the live execution surface.

Telegram text injection into a writable live tmux binding is a two-step
operator-layer act: deliver the payload, then deliver the runtime submit key.
Raw paste success alone is not a message-layer turn opener. Paste failure or
submit failure must surface as explicit delivery failure rather than as a
successful queued message. For multiline Codex payloads, field evidence shows
that tmux `C-m` can leave the pasted payload in the composer; the post-paste
turn opener is therefore the bare `Enter` key path, while single-line typed
input may keep the normal submit-key path. The Codex multiline boundary also
includes a post-paste readiness gap: the bot must not treat "paste accepted by
tmux" and "Codex composer is ready for submit" as the same event.

Local automation must not duplicate this with ad-hoc tmux commands. The
automation boundary is `ccbot runtime-input`, which resolves the same
control-surface/window state and calls the same runtime input driver as
Telegram text. `ccbot send` is the opposite direction: outbound Telegram
delivery for service results, not a runtime/TUI injection command.

Inbound Telegram audio/video handling is a third boundary: after a writable
runtime binding is resolved, ccbot saves the original media under
`$CCBOT_DIR/media` and sends a text payload containing the local artifact path
to the runtime. That artifact-first ingress is not outbound `ccbot send`, and
optional transcript/preview enrichment must not become a gate for delivering
the original artifact path.

## Replay Evidence Write Ownership

Replay evidence is written by:

- the runtime process
- or the semantic emitter / supervisor

Replay evidence is not a bot-side control-path write target.

Runtime conversation identity does not emit replay evidence. It scopes and
indexes it.

## Forbidden Equalities

These equalities are false:

- `control surface == topic`
- `policy == binding`
- `surface policy == binding state`
- `bind flow == message routing`
- `status notification == content delivery`
- `window == thread`
- `process == thread`
- `process == replay evidence`
- `thread == replay evidence`
- `chat == topic`
- `topic == thread`
- `topic == window`
- `pane == window`
- `question renderer pane == control surface`
- `question renderer pane == delivery source`
- `surface key == Telegram transport identifier`
- `surface key == full control-surface identity`
- `literal ACP-protocol-over-stdio == acceptable primary control plane`

More precise statements:

- a tmux window hosts a live process, but is not the persisted thread
- a process may attach to an existing thread via resume, but is not that
  thread
- replay evidence is emitted evidence, not the process itself
- a chat container may expose one shared no-topics main-chat mode when forum
  topics are unavailable, but the raw chat container is not itself the topic
  object
- a control surface is the product-side routing genus; `topic` is only one
  species of that genus
- a surface key is only the local persisted key inside the current user-scoped
  state map; full identity is `(user_id, surface_key)`
- `thread_id is None` may canonically mark that no-topics main-chat mode in the
  product surface, but `None` is still not the same kind of thing as a topic
- a Telegram topic is governed by policy and binding, not directly by a
  persisted log file
- a status artifact is not the same kind of thing as final content
- a temporary question renderer pane is tmux topology inside a parent window;
  it may render a blocking control question but must not be treated as a
  bindable Telegram surface or as the source of runtime delivery
