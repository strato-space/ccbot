# Multi-Runtime Regression Matrix

This note freezes the verification surface for the multi-runtime rewrite.
It is intentionally narrower than a general QA checklist: every matrix row
must point to executable guards or to an explicit review gate.

This note is contract-tested by `tests/ccbot/test_docs_contracts.py`.

## Core rule

No task that changes runtime routing, topic policy, Telegram delivery, or
command semantics is complete until:

- targeted tests pass
- the relevant doc contracts pass
- independent code review is complete
- ontology review is repeated for changes that touch core nouns or state
  machines

See also:
- `doc/execution-review-policy.md`

## Matrix

| Area | Required coverage | Current executable / review surface |
|---|---|---|
| Per-runtime launch | Codex, Claude Code, fast-agent launch registration on tmux-first semantics | `tests/ccbot/test_bot_contracts.py`, `tests/ccbot/test_claude_runtime_adapter.py`, `tests/ccbot/test_runtime_registry.py` |
| Per-runtime resume | Codex positive path, Claude degraded path, fast-agent degraded path from unbound topic, runtime syntax contracts | `tests/ccbot/test_bot_contracts.py`, `tests/ccbot/test_codex_threads.py`, `tests/ccbot/test_fast_agent_sessions.py`, `doc/codex-command-semantics.md`, `doc/fast-agent-runtime-adapter.md`, `doc/claude-runtime-adapter.md` |
| Bind / unbind / topic policy | implicit bind, manual bind required, cancel, explicit `/bind`, explicit `/unbind` | `tests/ccbot/test_bot_contracts.py`, `tests/ccbot/test_state_migration.py`, `doc/topic-control-state-machine.md` |
| Progress / result delivery | live status artifact, first-content conversion, final result delivery, stale delivery guards | `tests/ccbot/handlers/test_message_queue.py`, `tests/ccbot/handlers/test_response_builder.py`, `doc/telegram-delivery-pipeline.md` |
| History pollution guards | lifecycle and ephemeral progress excluded from `/history` | `tests/ccbot/test_runtime_types.py`, `tests/ccbot/test_session_monitor.py`, `doc/runtime-event-contract.md` |
| Rename behavior | tmux rename, runtime title-only rename, unsupported degraded rename | `tests/ccbot/test_bot_contracts.py`, `tests/ccbot/test_tmux_manager.py`, `tests/ccbot/test_fast_agent_sessions.py` |
| Topic rename vs `/rename` precedence | topic edits sync tmux name; explicit `/rename` remains authoritative | `tests/ccbot/test_bot_contracts.py`, `doc/codex-command-semantics.md` |
| Stale callback invalidation | bind-flow version/nonce invalidation after restart, unbind, cancel | `tests/ccbot/test_bot_contracts.py`, `doc/topic-policy-migration.md` |
| Late-event / stale-binding guards | queued status/content must fail closed after unbind or dead window | `tests/ccbot/handlers/test_message_queue.py`, `doc/telegram-delivery-pipeline.md` |
| Claude parity against upstream | progress lifecycle, tool-result edit-in-place, final-result delivery | `tests/ccbot/test_claude_parity_contract.py`, `/home/tools/ccbot-upstream`, `doc/claude-parity-contract.md` |
| queue / steer semantics | equal message channels at message layer, source does not affect priority, mode affects routing semantics | `tests/ccbot/test_bot_contracts.py`, `ontology/runtime.md`, `doc/runtime-ontology.md`, `doc/runtime-event-contract.md`, `doc/telegram-bot-features.md` |
| Raw operator control separation | raw terminal control is never modeled as a queued semantic message | `tests/ccbot/test_docs_contracts.py`, `ontology/boundaries.md`, `doc/runtime-ontology.md`, `doc/telegram-delivery-pipeline.md` |
| Non-regression: `voice`, `task`, `ACP-module` | shared surfaces stay intact while runtime work lands | `tests/ccbot/test_bot_contracts.py`, `doc/strato-ops-codex.md` |
| Review gates | self-review, independent code review, ontology review when required | `doc/execution-review-policy.md` |

## Required runtime branches

- Codex explicit `/resume <thread-name|id>` succeeds only on exact persisted
  identity match and launches `codex resume <resolved-thread-id>` in tmux.
- Claude Code explicit `/resume` from an unbound topic is degraded, because the
  transcript id does not prove a reversible workspace path.
- fast-agent explicit `/resume` from an unbound topic is degraded, because the
  persisted session id is scoped by the workspace `.fast-agent` root.

## Guardrail notes

- `queue` is the default Telegram message routing mode.
- `steer` changes routing semantics for explicit runtime-aware control paths.
- Raw terminal takeover remains a separate operator layer.
- The matrix is intentionally tmux-first and does not treat literal
  ACP-over-stdio as an acceptable primary control plane.
