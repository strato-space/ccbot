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
- Warning delivery is latest-warning dedup by control surface: identical warning
  text reuses one bubble and adds a visible `×N` counter only when `N > 2`.
- If compactness conflicts with semantic clarity, prefer visibility-first
  mutable updates over ambiguous suppression.
- Ordinary user echo remains visible in `compact`; only explicit internal
  payload shapes stay hidden and non-turn-opening.
- Topic binding scope may be `tmux` or `external`; external bindings are
  replay-delivery first and may be read-only for input injection.
- If no live tmux input plane is attached, Telegram input must fail closed
  with an explicit read-only warning and a reattach hint.
- Pending-input preview belongs to the queue-owned future-input lane and closes
  on `queue-empty`, `binding-stale`, or explicit clear rather than on
  assistant-final alone.
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

## Recent Updates

- 2026-04-26: Made `ontology/` the master source for runtime/topic-control,
  delivery-surface, and boundary nouns; README, `doc/`, and `specs/` are now
  derived witnesses that must stay aligned through docs-contract tests.
- 2026-04-26: Hardened runtime-discontinuity polling so active Codex panes
  running as `node` no longer produce repeated screenshot warnings when the
  visible footer is unclassified; stable warning identities keep repeated
  notices deduplicated.
- 2026-04-06: Added surface-keyed topic/chat binding state so shared
  group topics stay silent until explicitly addressed, no-topics main-chat
  mode binds canonically by `chat_id`, and pending addressed text auto-sends
  exactly once only after writable activation succeeds.
- 2026-04-06: Synced README/topic-control/ontology docs and migration
  regressions with the new group bind gate, read-only external bind guardrails,
  and stale status-poll cleanup for no-topics main-chat surfaces.
- 2026-04-05: Tightened compact delivery so ordinary user echo always remains
  visible, late commentary/orchestration cannot reopen a closed pre-final lane,
  and pending-input preview keeps queue-owned closure semantics across polling
  and retry paths.
- 2026-04-05: Hardened warning fallback/retry behavior, removed redundant
  preview output footers when a valid preview footer already exists, and synced
  README/docs/ontology contract text with the implemented Telegram surface.
