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

## Replay Evidence Write Ownership

Replay evidence is written by:

- the runtime process
- or the semantic emitter / supervisor

Replay evidence is not a bot-side control-path write target.

Runtime conversation identity does not emit replay evidence. It scopes and
indexes it.

## Forbidden Equalities

These equalities are false:

- `policy == binding`
- `bind flow == message routing`
- `status notification == content delivery`
- `window == thread`
- `process == thread`
- `process == replay evidence`
- `thread == replay evidence`
- `topic == thread`
- `topic == window`
- `literal ACP-protocol-over-stdio == acceptable primary control plane`

More precise statements:

- a tmux window hosts a live process, but is not the persisted thread
- a process may attach to an existing thread via resume, but is not that
  thread
- replay evidence is emitted evidence, not the process itself
- a Telegram topic is governed by policy and binding, not directly by a
  persisted log file
- a status artifact is not the same kind of thing as final content
