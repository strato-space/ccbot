# AGENTS.md

This repository implements a Telegram bot that controls tmux-hosted coding
runtimes without surrendering the live terminal surface.

## Core invariants

- `tmux` is the live human control surface.
- `ontology/` is the compact source-of-truth for runtime nouns, delivery-surface
  nouns, and boundary claims.
- Runtime semantics come from replay evidence and runtime-native event
  normalization, not from pane scraping as the primary source of truth.
- The default Telegram surface is `compact`.
- In `compact`, user echo, orchestration milestones, and final assistant text
  are the durable ordinary content bubbles.
- In `compact`, the latest human-facing commentary should stay visible as a
  dedicated artifact without accumulating a long stack of commentary bubbles.
- In `compact`, queued follow-up input may stay visible as a separate mutable
  pending-input artifact (after-next-tool, end-of-turn, queued-follow-up
  sections) and is not part of current-turn output ordering.
- In `compact`, once the final assistant answer is delivered successfully in
  full, the whole pre-final visible surface and the mutable technical status
  surface must close until the next user turn.
- No late pre-final visible artifact and no late technical status artifact may
  appear below the final answer for the same turn.
- Hidden internal user payloads do not reopen a turn unless they are explicit
  hidden turn openers, and user-visible text must never be hidden by broad
  instruction-looking heuristics alone.
- Heads-up warnings are Telegram-visible system notices, but they must not
  steal assistant-final turn semantics.
- Reasoning, tool lifecycle, command execution, and file-change summaries
  belong in the mutable status artifact unless a debug/verbose path explicitly
  opts into richer delivery.

## When changing delivery behavior

- Update [`doc/telegram-delivery-pipeline.md`](doc/telegram-delivery-pipeline.md).
- Update [`ontology/`](ontology/README.md) when core nouns or boundary claims
  change.
- Update any affected public docs in [`README.md`](README.md) and
  [`doc/telegram-bot-features.md`](doc/telegram-bot-features.md).
- Keep [`tests/ccbot/test_docs_contracts.py`](tests/ccbot/test_docs_contracts.py)
  aligned with the documented delivery contract.

## Validation

- Run `uv run --extra dev pytest` for the touched test slice.
- Run `uv run --extra dev ruff check` on touched Python modules and tests.
- If Telegram delivery changes, validate both:
  - semantic normalization / unit tests
  - a live deploy smoke check against the bot service
- If polling enqueues status artifacts, keep turn-generation scoping aligned
  with queue stale-drop guards.
