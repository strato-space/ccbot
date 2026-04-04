# Changelog

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

### FEATURE IMPLEMENTED

- **08:21-08:46** Multi-runtime rollout policy, ontology-tail cleanup, and
  schema/consumer audit docs were completed and frozen for the current release.
- **10:56-11:14** Human-oriented compact delivery was finalized with shell
  payload code blocks, compact tool summaries, and commentary preserved as the
  visible execution narrative.
- **11:26-11:46** Latest-only commentary delivery and code-aware tool/file
  formatting were added to keep Telegram closer to Codex human output.

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
