# AGENTS.md

This repository implements a Telegram bot that controls tmux-hosted coding
runtimes without surrendering the live terminal surface.

## Core invariants

- `tmux` is the live human control surface.
- Runtime semantics come from replay evidence and runtime-native event
  normalization, not from pane scraping as the primary source of truth.
- The default Telegram surface is `compact`.
- In `compact`, only user echo and final assistant text should survive as
  ordinary content bubbles.
- In `compact`, the latest human-facing commentary should stay visible as a
  dedicated artifact without accumulating a long stack of commentary bubbles.
- Reasoning, tool lifecycle, command execution, and file-change summaries
  belong in the mutable status artifact unless a debug/verbose path explicitly
  opts into richer delivery.

## When changing delivery behavior

- Update [`doc/telegram-delivery-pipeline.md`](doc/telegram-delivery-pipeline.md).
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
