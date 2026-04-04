# Project Ontology

This folder is the compact source of truth for `ccbot` core ontology.

Use it when you need the shortest project-level restatement of:

- what kinds of thing the system contains
- which relations between those things are real
- which equalities are false
- which delivery and ordering invariants the Telegram surface must preserve

The longer design and rollout notes still live under [`doc/`](/home/tools/ccbot/doc),
but this folder is the canonical ontology entrypoint.

## Files

- [`runtime.md`](/home/tools/ccbot/ontology/runtime.md)
  - live control plane, persisted identity, replay evidence, and routing nouns
- [`delivery-surface.md`](/home/tools/ccbot/ontology/delivery-surface.md)
  - turn artifacts, Telegram surface classes, and ordering invariants
- [`boundaries.md`](/home/tools/ccbot/ontology/boundaries.md)
  - ACP distinctions, human-first transport rule, and forbidden equalities

## Canonical Shape

`Telegram topic -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`

External replay-only variant:

`Telegram topic -> binding(binding_scope=external) -> runtime conversation identity -> replay evidence`

More precisely:

- `binding_scope=tmux -> tmux window -> runtime process`
- `binding_scope=external -> runtime conversation identity -> replay evidence`
- `runtime process -> semantic emitter / supervisor`
- `semantic emitter / supervisor -> live semantic stream`
- `semantic emitter / supervisor -> persisted replay evidence`
- `runtime conversation identity scopes/indexes live semantic stream and persisted replay evidence`
- `live semantic stream and/or persisted replay evidence -> normalized events`
- `normalized events -> Telegram delivery and history views`

## Why this folder exists

The project cannot afford to collapse these distinct kinds of thing into one
flat "session" idea:

- live terminal control
- persisted conversation identity
- replay evidence on disk
- semantic delivery policy
- raw operator intervention

That collapse caused real implementation bugs before. This folder exists so the
project keeps one explicit ontology surface that code, tests, specs, and docs
can all point to.
