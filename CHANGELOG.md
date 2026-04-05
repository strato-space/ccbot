# Changelog

## 2026-04-05

### PROBLEM SOLVED

- **00:35-01:56** Repeated warning notices could flood Telegram topics and
  obscure operator signal. Warning delivery now deduplicates identical warning
  text in-place and keeps one visible warning artifact per topic.
- **00:35-01:56** Topics bound to external Codex threads had no explicit
  modality boundary between replay delivery and input injection. External bind
  is now first-class and input injection fails closed as read-only when no live
  tmux plane is attached.
- **00:35-01:56** Documentation/ontology drift around external bind semantics
  and warning behavior increased operational ambiguity. Ontology, delivery
  docs, README, and doc contracts are now synchronized to the implemented model.
- **11:05-11:41** Compact delivery still had semantic drift across queue,
  policy, parser, and preview layers: ordinary user echo could be hidden,
  post-final commentary/orchestration could reappear after closure, pending
  input could over-merge older poll slices or get stuck after send failure, and
  preview bubbles could retain redundant output-footers. The Telegram surface
  now behaves as a clearer semantic ledger without adding new bubble classes.

### FEATURE IMPLEMENTED

- **00:35-01:56** Added warning artifact dedup with mutable repeat counter:
  the counter is rendered only when repetition cardinality is strictly greater
  than 2 (`×N` for `N > 2`).
- **00:35-01:56** Added external Codex bind without tmux via `/bind <thread-name|id>`
  in Codex lane, with persisted external binding metadata and replay-source
  resolution in monitor flow.
- **00:35-01:56** Added explicit read-only guardrails for external bindings
  across text/key/input actions and user-facing reattach hints.
- **00:35-01:56** Finalized ontology/spec tranche T68-T72 as completed and
  pinned new guarantees in doc contract tests.
- **11:05-11:41** Implemented the Telegram delivery integration wave across the
  existing five delivery surfaces: warning dedup/counter hardening, visible
  ordinary user echo in compact mode, post-final orchestration/commentary
  non-reopen, queue-owned pending-input preview hardening, and preview/footer
  convergence without duplicate fenced output decoration.
- **11:05-11:41** Added an execution-ready wave plan artifact in
  `specs/telegram-delivery-integration-wave-plan.md` so the delivery contract,
  rollout gate, and verification scope are recorded alongside the codebase.

### CHANGES

- **00:35-01:56** Implemented warning dedup flow in
  `src/ccbot/handlers/message_queue.py` (new warning routing, repeat tracking,
  in-place edits, and shutdown cleanup) with regressions in
  `tests/ccbot/handlers/test_message_queue.py`.
- **00:35-01:56** Extended binding model with external scope in
  `src/ccbot/session.py`, `src/ccbot/runtime_types.py`,
  `src/ccbot/state_schema.py`, and `src/ccbot/session_monitor.py`; added
  coverage in `tests/ccbot/test_session.py` and
  `tests/ccbot/test_state_migration.py`.
- **00:35-01:56** Added external bind and read-only delivery behavior in
  `src/ccbot/bot.py` with contract coverage in
  `tests/ccbot/test_bot_contracts.py`.
- **00:35-01:56** Updated ontology/docs:
  `ontology/runtime.md`, `ontology/delivery-surface.md`,
  `ontology/README.md`, `doc/runtime-ontology.md`,
  `doc/runtime-event-contract.md`, `doc/telegram-delivery-pipeline.md`,
  `doc/telegram-bot-features.md`, `README.md`.
- **00:35-01:56** Updated spec status/log for T68-T72 in
  `specs/ccbot-codex-adaptation-plan-4.md` and expanded
  `tests/ccbot/test_docs_contracts.py` to pin external-bind and warning
  semantics.
- **01:40-01:50** Validation: `uv run --extra dev pytest -q
  tests/ccbot/test_docs_contracts.py tests/ccbot/handlers/test_message_queue.py
  tests/ccbot/test_session.py tests/ccbot/test_state_migration.py
  tests/ccbot/test_bot_contracts.py` (`175 passed`), and
  `uv run --extra dev ruff check src tests` (`All checks passed`).
- **11:05-11:41** Hardened queue lifecycle behavior in
  `src/ccbot/handlers/message_queue.py`: stale-binding cleanup now clears
  warning dedupe state, pending-input send failures clear stale dedupe pins for
  retry, and regressions were added in
  `tests/ccbot/handlers/test_message_queue.py` for markdown fallback paths,
  stale binding cleanup, and retry-safe pending-input resend behavior.
- **11:05-11:41** Tightened compact policy and bot routing in
  `src/ccbot/telegram_delivery_policy.py` and `src/ccbot/bot.py` so ordinary
  user echo always remains Telegram-visible while hidden internal payloads stay
  suppressed, and complete commentary/orchestration events are dropped once the
  pre-final lane is closed. Added coverage in
  `tests/ccbot/test_bot_contracts.py`.
- **11:05-11:41** Converged preview/footer formatting in
  `src/ccbot/handlers/response_builder.py` and
  `src/ccbot/codex_rollout.py`: existing fenced previews are preserved without
  double-wrap, valid `preview X/Y lines` footers stay outside the fence, and
  redundant trailing `output N line(s)` noise is stripped. Added regressions in
  `tests/ccbot/handlers/test_response_builder.py` and
  `tests/ccbot/test_codex_rollout.py`.
- **11:05-11:41** Hardened pending-input parsing/rendering in
  `src/ccbot/terminal_parser.py` and `src/ccbot/handlers/status_polling.py`
  with stricter stop conditions for prompt/status noise, non-merging repeated
  same-header blocks across older poll slices, and clipped edit-hint rendering.
  Added coverage in `tests/ccbot/test_terminal_parser.py` and
  `tests/ccbot/test_pending_input_status_polling.py`.
- **11:05-11:41** Synced documentation and ontology with the implemented
  delivery contract in `AGENTS.md`, `README.md`,
  `doc/runtime-event-contract.md`, `doc/telegram-bot-features.md`,
  `doc/telegram-delivery-pipeline.md`, `ontology/delivery-surface.md`, and
  expanded `tests/ccbot/test_docs_contracts.py` to pin the new wording.
- **11:05-11:41** Validation: `uv run --extra dev pytest -q
  tests/ccbot/handlers/test_message_queue.py
  tests/ccbot/test_pending_input_status_polling.py
  tests/ccbot/test_terminal_parser.py
  tests/ccbot/test_bot_contracts.py
  tests/ccbot/test_codex_rollout.py
  tests/ccbot/handlers/test_response_builder.py
  tests/ccbot/test_docs_contracts.py` (`248 passed`), and
  `uv run --extra dev ruff check src tests README.md doc ontology`
  (`All checks passed`).

## 2026-04-04

### PROBLEM SOLVED

- **09:38-10:06** Existing-window bind and Codex replay tracking no longer
  leave Telegram in a send-only state or duplicate user/final messages.
- **10:37-11:14** The production Telegram surface no longer leaks large raw
  reasoning, tool, command, and file-output bubbles by default, and visible
  commentary no longer disappears under status churn.
- **11:26-11:46** Compact commentary is now latest-only and visible, while
  tool, tool-output, and file-change surfaces gained codex-style code-aware
  formatting instead of raw JSON/arg dumps.
- **12:00-12:13** Compact delivery no longer allows late commentary to appear
  below the final assistant answer; the commentary lane now closes in queue
  order and respects the public turn boundary.
- **22:20-22:40** Crash-recovery review found and removed a turn-order risk
  where `Heads up` text could be misclassified as warning/finally-visible
  status, potentially weakening final-turn closure guarantees.
- **22:20-22:40** Status polling no longer silently drops active-turn status
  updates due to generation mismatch, and pending-input preview no longer
  sticks stale after flood-control drops.
- **22:20-22:40** Pending-input parsing now prefers the newest visible queue
  block, preventing stale scrollback lines from being surfaced as live queued
  user input.

### FEATURE IMPLEMENTED

- **08:21-08:46** Multi-runtime rollout policy, ontology-tail cleanup, and
  schema/consumer audit docs were completed and frozen for the current release.
- **10:56-11:14** Human-oriented compact delivery was finalized with shell
  payload code blocks, compact tool summaries, and commentary preserved as the
  visible execution narrative.
- **11:26-11:46** Latest-only commentary delivery and code-aware tool/file
  formatting were added to keep Telegram closer to Codex human output.
- **12:00-12:13** A queue-serialized commentary-close primitive was added so
  already-queued human narrative can still land before the final answer, while
  any later commentary is suppressed until the next user turn.
- **13:00-13:20** Added a dedicated `ontology/` source-of-truth folder so
  runtime nouns, delivery-surface nouns, and ACP/human-control boundaries no
  longer depend on scattered maintainer notes alone.
- **17:10-17:20** Fixed two remaining turn-order correctness bugs: final
  surface closure now waits for full multipart final delivery, and hidden
  internal payloads no longer reopen turns unless they are real hidden turn
  openers.
- **17:25-17:30** Overlapping `wait_agent` lifecycles no longer collapse into
  one visible wait, and legitimate user prompts that quote repository
  instructions are no longer hidden by broad payload heuristics.
- **22:20-22:40** Added a dedicated pending-input artifact lane with safer
  queue dedupe behavior under flood-control and explicit parser coverage for
  multi-section pending-input previews.
- **22:20-22:40** Added warning normalization guardrails so `Heads up`
  detection applies to commentary warnings without stealing assistant-final
  semantics.

### CHANGES

- **08:21-08:46** Added and tightened rollout/docs surfaces including
  `doc/multi-runtime-rollout.md`, `doc/consumer-audit-by-kind.md`,
  `doc/runtime-naming-audit.md`, and `doc/monitor-state-schema-strategy.md`.
- **09:38-09:53** Fixed bind/read-path recovery in
  `src/ccbot/bot.py` and `src/ccbot/session_monitor.py` so existing live Codex
  windows register runtime metadata and resolve replay sources correctly.
- **10:06** Suppressed duplicate Codex `event_msg` delivery in
  `src/ccbot/codex_rollout.py`.
- **10:37-11:14** Reworked compact Telegram delivery in
  `src/ccbot/telegram_delivery_policy.py` and
  `src/ccbot/codex_rollout.py`, then synchronized the contract in
  `doc/telegram-delivery-pipeline.md`, `README.md`,
  `doc/telegram-bot-features.md`, and doc contract tests.
- **11:26-11:46** Added latest-only commentary artifact handling in
  `src/ccbot/handlers/message_queue.py` and `src/ccbot/bot.py`, improved
  code-aware formatting for tool/file surfaces in
  `src/ccbot/handlers/response_builder.py` and `src/ccbot/codex_rollout.py`,
  and resynchronized the delivery docs/specs.
- **12:00-12:13** Added a queue-ordered `commentary_close` delivery primitive
  in `src/ccbot/handlers/message_queue.py`, switched final-answer handling in
  `src/ccbot/bot.py` from immediate lane closure to queued commentary fencing,
  tightened teardown in `src/ccbot/handlers/cleanup.py`, and extended delivery
  contract tests for the no-commentary-after-final invariant.
- **13:00-13:20** Added `ontology/README.md`, `ontology/runtime.md`,
  `ontology/delivery-surface.md`, and `ontology/boundaries.md`; updated
  `README.md`, `doc/runtime-ontology.md`, `doc/runtime-event-contract.md`,
  `doc/telegram-delivery-pipeline.md`, and doc-contract tests so the ontology
  layer is explicit and discoverable.
- **17:10-17:20** Tightened delivery ordering in
  `src/ccbot/handlers/message_queue.py`, narrowed hidden turn-opening policy in
  `src/ccbot/telegram_delivery_policy.py` and `src/ccbot/bot.py`, prevented
  hidden `<subagent_notification>` from mutating Codex user-turn state in
  `src/ccbot/codex_rollout.py`, and added regressions in
  `tests/ccbot/handlers/test_message_queue.py`,
  `tests/ccbot/test_bot_contracts.py`, and
  `tests/ccbot/test_codex_rollout.py`.
- **17:25-17:30** Scoped `wait_agent` lifecycle tracking to the invocation in
  `src/ccbot/codex_rollout.py`, removed broad instruction-text suppression from
  `src/ccbot/telegram_delivery_policy.py`, and added regressions for
  overlapping waits plus visible quoted-instructions user prompts in
  `tests/ccbot/test_codex_rollout.py` and `tests/ccbot/test_bot_contracts.py`.
- **22:20-22:40** Hardened warning normalization in
  `src/ccbot/codex_rollout.py` so `Heads up` warning mapping is commentary-only;
  added regression in `tests/ccbot/test_codex_rollout.py` for assistant-phase
  `Heads up` text.
- **22:20-22:40** Restored generation-scoped status polling in
  `src/ccbot/handlers/status_polling.py` and added assertions in
  `tests/ccbot/handlers/test_status_polling.py`.
- **22:20-22:40** Improved newest-block pending-input extraction in
  `src/ccbot/terminal_parser.py`, added multi-header parser regressions in
  `tests/ccbot/test_terminal_parser.py`, and kept pending-input surface
  rendering covered in `tests/ccbot/test_pending_input_status_polling.py`.
- **22:20-22:40** Fixed flood-window dedupe stickiness for pending-input tasks
  in `src/ccbot/handlers/message_queue.py` and added flood-drop regression in
  `tests/ccbot/handlers/test_message_queue.py`.
- **22:20-22:40** Tightened tool-output formatting in
  `src/ccbot/handlers/response_builder.py` so incidental inline backticks do
  not disable fenced preview formatting; added regression in
  `tests/ccbot/handlers/test_response_builder.py`.
