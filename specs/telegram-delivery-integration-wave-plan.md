# Plan: Telegram Delivery Integration Wave

**Generated**: 2026-04-05
**Estimated Complexity**: High

## Overview

This plan executes one integrated delivery wave for Telegram surface behavior in
`ccbot`, not five isolated fixes. The wave covers:

1. warning dedup + repeat counter
2. user echo visibility contract
3. subagent orchestration bubble collapse
4. queued follow-up as pending-input artifact
5. command/tool preview rendering and footer cleanup

The goal is to make the public Telegram surface semantically correct, compact,
and stable under polling, retries, and turn boundaries.

## Scholastic Ontology Normalization (Gate)

### Key terms (normalized)

- **Terminal turn artifact**: final assistant message that closes the current
  turn.
- **Pre-final visible artifact**: user-visible narrative before terminal
  artifact (`commentary`, `orchestration`, visible `user_echo`, warning).
- **Technical status artifact**: mutable execution-status lane (`command`,
  `tool_use`, `tool_result`, `file_change`, reasoning/status progress).
- **Pending-input artifact**: mutable preview of queued future input.
- **Warning artifact**: durable system notice, not a turn opener and not a
  technical status item.
- **User echo artifact**: visible reflection of ordinary user input and a turn
  opener fact.

### Ontology check (category failures to avoid)

- **Categorical failure A**: treating warning as technical status.
  - Counterexample: repeated warning should remain a durable notice, while
    status text is ephemeral by design.
- **Categorical failure B**: treating hidden/internal user payloads as ordinary
  user turn openers.
  - Counterexample: `<subagent_notification>` and scaffold tags must not open a
    new turn.
- **Categorical failure C**: treating subagent lifecycle as permanent content
  bubbles.
  - Counterexample: spawn/wait/finish bursts flood chat and destroy narrative
    readability if not collapsed into mutable orchestration surface.

### Minimal repair

- Keep distinct artifact classes and lane ownership.
- Apply mutable update semantics per class.
- Enforce terminal ordering: no pre-final/status artifacts after successful
  final delivery until next real user opener.

### Modality separation

- **Necessary invariants**:
  - warning remains durable and deduplicated
  - ordinary user echo remains visible
  - hidden/internal user payloads remain suppressed
  - commentary/orchestration/status lanes close after final artifact
  - pending-input lane follows queue-state closure
    (`queue-empty | binding-stale | explicit clear`)
- **Optional tuning knobs**:
  - preview size (lines/chars)
  - orchestration verbosity profile
  - footer compactness style

## Prerequisites

- Repo: `/home/tools/ccbot` on branch `feat/multi-runtime-topic-control`
- Existing delivery stack available:
  - `src/ccbot/handlers/message_queue.py`
  - `src/ccbot/handlers/response_builder.py`
  - `src/ccbot/codex_rollout.py`
  - `src/ccbot/handlers/status_polling.py`
  - `src/ccbot/bot.py`
  - `src/ccbot/telegram_delivery_policy.py`
- Existing contracts:
  - `tests/ccbot/handlers/test_message_queue.py`
  - `tests/ccbot/test_bot_contracts.py`
  - `tests/ccbot/test_codex_rollout.py`
  - `tests/ccbot/test_pending_input_status_polling.py`
  - `tests/ccbot/test_terminal_parser.py`
  - `tests/ccbot/test_docs_contracts.py`

No new external dependency is required for this wave; implementation is
contained to current runtime and Telegram delivery modules.

## Dependency Graph

```text
T1 ──┬── T4 ── T8 ── T11 ──┬── T10 ── T9 ──┬── T12 ── T12A ──┬── T13 ──┬── T16 ── T17 ── T18
     │                      │                │                  │         │
T2 ──┼── T5 ────────────────┴────────────────┘                  ├── T14 ──┤
     │                                                          │         │
T3 ──┴── T6 ─────────────────────────────────────────────────────┴── T15 ──┘
              └── T7 ────────────────────────────────┘
```

## Sprint 1: Contract & Ontology Baseline
**Goal**: Freeze target semantics before touching queue/renderer code.
**Demo/Validation**:
- explicit invariants listed and mapped to existing/open issue clusters
- no category conflation in runtime/delivery ontology notes

### T1: Capture Current Delivery Matrix
- **id**: T1
- **depends_on**: []
- **location**:
  - `/home/tools/ccbot/src/ccbot/*`
  - `/home/tools/ccbot/tests/ccbot/*`
- **description**:
  - Build a behavior matrix for the 5 surfaces (warning, user echo,
    orchestration, queued follow-up, previews) from current code and tests.
  - Map known regressions to canonical clusters.
- **validation**:
  - matrix links each surface to emitting module, queue lane, and test coverage.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T2: Apply Scholastic Ontology Fix To Spec/Docs
- **id**: T2
- **depends_on**: []
- **location**:
  - `/home/tools/ccbot/ontology/runtime.md`
  - `/home/tools/ccbot/ontology/delivery-surface.md`
  - `/home/tools/ccbot/doc/runtime-event-contract.md`
- **description**:
  - Encode normalized artifact classes and prohibited category collapses.
  - Explicitly separate durable notices, turn openers, and status lane.
- **validation**:
  - docs state warning/user-echo/orchestration/pending-input/status separation.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T3: Consolidate Issue Taxonomy Into One Wave Scope
- **id**: T3
- **depends_on**: []
- **location**:
  - `/home/strato-space/settings/.beads/issues.jsonl`
  - `/home/tools/ccbot/specs/telegram-delivery-integration-wave-plan.md`
- **description**:
  - Define canonical issue umbrella and duplicate clusters so implementation
    follows one scope boundary.
- **validation**:
  - one canonical issue references all wave surfaces; duplicates are linked.
- **status**: Not Completed
- **log**:
- **files edited/created**:

## Sprint 2: Delivery Design (Queue + Render + Rollout Contracts)
**Goal**: Finalize dataflow and update semantics before code mutation.
**Demo/Validation**:
- dependency-safe design notes for mutable artifact keys and render policy
- no ambiguous ownership between rollout normalizer and queue worker

### T4: Define Mutable Artifact Key Strategy
- **id**: T4
- **depends_on**: [T1, T2]
- **location**:
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
- **description**:
  - Specify keying and lifecycle for warning/commentary/orchestration/pending
    input/status artifacts to maximize edit-in-place reuse and minimize churn.
- **validation**:
  - key model covers cross-poll dedup, flood-drop reset, and shutdown cleanup.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T5: Define Preview Policy v2
- **id**: T5
- **depends_on**: [T1, T2]
- **location**:
  - `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`
  - `/home/tools/ccbot/src/ccbot/codex_rollout.py`
  - `/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py`
- **description**:
  - Set unified preview limits (approximately 2x current footprint), code-block
    language rules (`sh/json/text`), and footer suppression rules.
- **validation**:
  - policy defines when to show preview footer and when to suppress redundant
    `output N line(s)` tails.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T6: Define Orchestration Collapse Model
- **id**: T6
- **depends_on**: [T1, T2, T3]
- **location**:
  - `/home/tools/ccbot/src/ccbot/codex_rollout.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
- **description**:
  - Specify aggregation of spawn/wait/finish/timeout into one mutable
    orchestration artifact per topic/turn, with stable human-readable milestones.
- **validation**:
  - model prevents burst spam while preserving state transitions.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T7: Define Turn-Boundary Closure Invariants
- **id**: T7
- **depends_on**: [T2]
- **location**:
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
- **description**:
  - Pin hard invariant: no commentary/status/orchestration after successful
    final assistant delivery for the same generation.
  - Pin pending-input closure rule separately: clear only on
    `queue-empty | binding-stale | explicit clear`, not merely on final answer.
- **validation**:
  - explicit generation/closure rules documented and testable with no
    contradiction between T7 and pending-input behavior.
- **status**: Not Completed
- **log**:
- **files edited/created**:

## Sprint 3: Implementation Wave
**Goal**: Implement integrated behavior in one code wave across queue/normalizer/renderer.
**Demo/Validation**:
- telegram chat shows compact, stable, non-duplicated artifact behavior
- no regression in turn ordering
- implementation order is serialized across shared hotspots
  (`message_queue.py`, `bot.py`, `codex_rollout.py`, `telegram_delivery_policy.py`)

### T8: Implement Warning Dedup And Counter Policy
- **id**: T8
- **depends_on**: [T4]
- **location**:
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
- **description**:
  - Enforce same-warning edit-in-place, counter rendering only for `N > 2`, and
    proper reset on new warning text.
- **validation**:
  - repeated warning does not create new bubbles; threshold behavior holds.
  - failure-path behavior is covered for `RetryAfter` and stale edit/delete
    failures with safe reset/re-send semantics.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T9: Restore And Protect Visible User Echo Contract
- **id**: T9
- **depends_on**: [T4, T7, T10]
- **location**:
  - `/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
- **description**:
  - Keep visible `👤` echo for ordinary user input while preserving suppression
    of hidden/internal payload classes.
- **validation**:
  - visible user text emits one echo bubble; hidden payloads stay non-visible
    and non-turn-opening where applicable.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T10: Collapse Subagent/Orchestration Bursts Into Mutable Milestones
- **id**: T10
- **depends_on**: [T4, T6, T7, T11]
- **location**:
  - `/home/tools/ccbot/src/ccbot/codex_rollout.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
- **description**:
  - Replace multi-bubble orchestration bursts with single mutable milestone
    stream per topic/turn.
- **validation**:
  - spawn/wait/finish/timeout transitions update one visible bubble.
  - cross-poll duplicate suppression is verified when `event_msg` and canonical
    `response_item` records arrive in different polling slices.
  - failure-path behavior is covered for `RetryAfter` and stale edit/delete
    failures with safe reset/re-send semantics.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T11: Harden Pending-Input Artifact Delivery
- **id**: T11
- **depends_on**: [T4, T7, T8]
- **location**:
  - `/home/tools/ccbot/src/ccbot/handlers/status_polling.py`
  - `/home/tools/ccbot/src/ccbot/terminal_parser.py`
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
- **description**:
  - Ensure queued follow-up previews are delivered as mutable pending-input
    artifact with robust parser extraction and dedupe.
- **validation**:
  - pending-input updates reuse same bubble and clear correctly on closure.
  - closure semantics follow `queue-empty | binding-stale | explicit clear`.
  - failure-path behavior is covered for `RetryAfter` and stale edit/delete
    failures with safe reset/re-send semantics.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T12: Implement Preview Formatting v2
- **id**: T12
- **depends_on**: [T5, T9]
- **location**:
  - `/home/tools/ccbot/src/ccbot/handlers/response_builder.py`
  - `/home/tools/ccbot/src/ccbot/codex_rollout.py`
  - `/home/tools/ccbot/src/ccbot/telegram_delivery_policy.py`
- **description**:
  - Increase preview footprint, enforce code-fence formatting by content class,
    and suppress redundant footer lines after code previews.
- **validation**:
  - command/tool/tool-output bubbles render consistent code blocks and concise
    footer semantics.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T12A: Integration Hardening Across Delivery Lanes
- **id**: T12A
- **depends_on**: [T8, T9, T10, T11, T12]
- **location**:
  - `/home/tools/ccbot/src/ccbot/handlers/message_queue.py`
  - `/home/tools/ccbot/src/ccbot/bot.py`
  - `/home/tools/ccbot/src/ccbot/codex_rollout.py`
  - `/home/tools/ccbot/src/ccbot/handlers/status_polling.py`
- **description**:
  - Run one explicit hardening pass for cross-lane consistency after all
    surface-specific changes land, focusing on generation gates, stale-task
    cleanup, and duplicate suppression coherence.
- **validation**:
  - synthetic replay and queue scenarios prove no inter-lane contradiction.
- **status**: Not Completed
- **log**:
- **files edited/created**:

## Sprint 4: Regression Matrix + Docs/Spec Sync
**Goal**: Lock behavior with contracts and sync ontology/spec/docs to implementation.
**Demo/Validation**:
- all modified surfaces pinned by tests
- docs/specs describe actual behavior

### T13: Extend Queue And Polling Regressions
- **id**: T13
- **depends_on**: [T12A]
- **location**:
  - `/home/tools/ccbot/tests/ccbot/handlers/test_message_queue.py`
  - `/home/tools/ccbot/tests/ccbot/test_pending_input_status_polling.py`
  - `/home/tools/ccbot/tests/ccbot/test_terminal_parser.py`
- **description**:
  - Add and adjust regressions for warning dedup, orchestration collapse, and
    pending-input artifact reuse/clear behavior.
- **validation**:
  - test matrix catches duplicate bursts and stale pending-input artifacts.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T14: Extend Bot And Rollout Delivery Contracts
- **id**: T14
- **depends_on**: [T8, T9, T10, T12A]
- **location**:
  - `/home/tools/ccbot/tests/ccbot/test_bot_contracts.py`
  - `/home/tools/ccbot/tests/ccbot/test_codex_rollout.py`
  - `/home/tools/ccbot/tests/ccbot/handlers/test_response_builder.py`
- **description**:
  - Pin user echo visibility, orchestration milestone compactness, preview v2
    formatting, and warning semantics.
- **validation**:
  - contracts fail on duplicate orchestration spam or preview regressions.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T15: Sync Ontology/Docs/Specs With Final Semantics
- **id**: T15
- **depends_on**: [T12A]
- **location**:
  - `/home/tools/ccbot/ontology/*.md`
  - `/home/tools/ccbot/doc/*.md`
  - `/home/tools/ccbot/specs/ccbot-codex-adaptation-plan-4.md`
  - `/home/tools/ccbot/README.md`
- **description**:
  - Update ontology and operator docs to reflect integrated delivery behavior
    and close any remaining drift.
- **validation**:
  - docs contract tests pass with updated terminology and invariants.
- **status**: Not Completed
- **log**:
- **files edited/created**:

## Sprint 5: Validation, Rollout, Issue Closure
**Goal**: Prove production behavior and close issue tail safely.
**Demo/Validation**:
- local tests and lint clean
- production smoke in Telegram topic confirms integrated behavior

### T16: Local Validation Matrix
- **id**: T16
- **depends_on**: [T13, T14, T15]
- **location**:
  - `/home/tools/ccbot/tests/ccbot/*`
- **description**:
  - Run targeted regression bundle and lint; ensure no delivery-surface
    contract failures.
- **validation**:
  - `uv run --extra dev pytest ...` targeted suites pass
  - `uv run --extra dev pytest -q tests/ccbot/test_docs_contracts.py` passes
  - `uv run --extra dev ruff check src tests` passes
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T17: Deploy And Telegram E2E Smoke
- **id**: T17
- **depends_on**: [T15, T16]
- **location**:
  - `str` host runtime (`/tools/ccbot`)
- **description**:
  - Deploy branch on `str`, restart exact unit
    `systemctl --user restart ccbot.service` under user `iqdoctor`.
  - Before smoke, clear stale mutable artifacts in the target topic so
    verification is not polluted by old state.
  - Run live Telegram smoke for all 5 surfaces.
- **validation**:
  - no duplicate bursts, expected bubble reuse/edit behavior, final-order
    invariants hold.
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T18: Beads Closure And Executive Closeout
- **id**: T18
- **depends_on**: [T17]
- **location**:
  - `/home/strato-space/settings/.beads/issues.jsonl`
  - `/home/tools/ccbot/CHANGELOG.md`
- **description**:
  - Close duplicates under canonical umbrella issue, update changelog and send
    concise executive update.
- **validation**:
  - duplicate issues closed/linked, canonical issue resolved with evidence.
- **status**: Not Completed
- **log**:
- **files edited/created**:

## Parallel Execution Groups

| Wave | Tasks | Can Start When |
|------|-------|----------------|
| 1 | T1, T2, T3 | Immediately |
| 2 | T4, T5, T6, T7 | T1/T2/T3 prerequisites satisfied |
| 3 | T8 | T4 complete |
| 4 | T11 | T8, T4, T7 complete |
| 5 | T10 | T11, T4, T6, T7 complete |
| 6 | T9 | T10, T4, T7 complete |
| 7 | T12 | T9, T5 complete |
| 8 | T12A | T8, T9, T10, T11, T12 complete |
| 9 | T13, T14, T15 | T12A complete |
| 10 | T16 | T13, T14, T15 complete |
| 11 | T17 | T16 complete |
| 12 | T18 | T17 complete |

## Testing Strategy

- Queue/lanes:
  - `tests/ccbot/handlers/test_message_queue.py`
  - `tests/ccbot/test_pending_input_status_polling.py`
  - `tests/ccbot/test_terminal_parser.py`
- Delivery contracts:
  - `tests/ccbot/test_bot_contracts.py`
  - `tests/ccbot/test_codex_rollout.py`
  - `tests/ccbot/handlers/test_response_builder.py`
- Docs/ontology contracts:
  - `tests/ccbot/test_docs_contracts.py`
- Quality gate:
  - `uv run --extra dev pytest -q <targeted suites>`
  - `uv run --extra dev ruff check src tests`

## Risks & Mitigations

- **Risk**: mutable artifact key collisions across generations.
  - **Mitigation**: include turn generation in lane-closure and stale-task
    checks; keep artifact key scoped by `(user_id, thread_id)`.
- **Risk**: orchestration collapse loses critical operator milestones.
  - **Mitigation**: enforce minimum milestone set (spawn/wait/finish/timeout)
    and preserve one-line state transitions.
- **Risk**: preview expansion reintroduces Telegram truncation/fence imbalance.
  - **Mitigation**: keep fenced-block balancing tests and explicit max-chars
    constraints in compact policy.
- **Risk**: close-session loses issue traceability under context compaction.
  - **Mitigation**: canonical issue mapping in T3 and mandatory closure in T18.

## Rollback Plan

- Revert implementation commits for T8-T12 as one wave if smoke fails.
- Keep ontology/docs updates only if they still match rolled-back runtime
  behavior; otherwise revert together with code.
- Restore previous production behavior by redeploying last known good commit
  and re-running Telegram smoke.
