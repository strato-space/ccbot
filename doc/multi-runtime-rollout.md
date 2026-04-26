# Multi-Runtime Rollout And Cutover

This note defines how the multi-runtime rewrite lands in production without
silently changing topic semantics.

It complements:

- `doc/strato-ops-codex.md` for the current Codex production lane
- `ontology/README.md` for the canonical nouns and `doc/runtime-ontology.md` for the derived maintainer explainer
- `doc/multi-runtime-regression-matrix.md` for the frozen verification surface

## Scope

This rollout note covers:

- staged Claude Code restore on the new runtime-neutral base
- staged fast-agent enablement on the same base
- operator instructions for runtime capability differences
- fallback and cutover rules when one runtime path is not production-ready

This note does not expand:

- `voice`
- `task`
- `ACP-module`

## Core deployment rule

`ccbot` remains a single configured launch lane per bot instance.

In the current codebase, the configured lane is selected by the legacy
`CLAUDE_COMMAND` environment variable and resolved into a runtime capability
profile at startup.

Operational consequence:

- do not treat one running bot instance as a simultaneous multi-lane router
- stage new runtimes by dedicated bot instances, hosts, or maintenance windows
- keep runtime choice explicit at deploy time, not implicit at topic-message time

## Why staged rollout is required

The runtime capability surface is intentionally non-uniform:

- Codex supports deterministic explicit `/resume <thread-name|id>` from an
  unbound topic.
- Claude Code restore is first-class for existing bound flows, but explicit
  unbound-topic `/resume` stays degraded because transcript ids do not prove a
  reversible workspace path.
- fast-agent supports persisted `session_id` and title metadata, but unbound-topic
  `/resume` stays degraded because persisted session ids are scoped by the local
  workspace `.fast-agent` root.
- `/rename` is capability-aware:
  - Codex: tmux rename only unless a public-safe persisted-identity rename is
    proved
  - Claude Code: tmux/topic rename only
  - fast-agent: tmux/topic rename plus title-metadata update, but not
    persisted `session_id` rename

Because runtime semantics differ, rollout must preserve two invariants:

- no topic silently changes meaning after deployment
- no operator assumes every runtime supports the Codex command surface

## Runtime rollout rings

### Ring 0: Codex production baseline

Use this ring for the currently supported production lane.

Configuration:

- `CLAUDE_COMMAND=codex`

Required operator guarantees:

- fresh bind works
- explicit `/resume <thread-name|id>` works
- `/rename` is deterministic
- progress and final-result delivery are stable
- `voice`, `task`, and `ACP-module` remain unchanged

Primary runbook:

- `doc/strato-ops-codex.md`

Promotion gate:

- `tests/ccbot/test_bot_contracts.py`
- `tests/ccbot/handlers/test_message_queue.py`
- `tests/ccbot/test_codex_threads.py`
- `tests/ccbot/test_runtime_types.py`
- `tests/ccbot/test_session_monitor.py`
- `tests/ccbot/test_docs_contracts.py`
- the Codex and shared rows in `doc/multi-runtime-regression-matrix.md`

### Ring 1: Claude Code restore canary

Use a dedicated bot instance or host-scoped deployment when restoring Claude
Code on the new base.

Configuration example:

- `CLAUDE_COMMAND=claude`

Required operator expectations:

- bound-topic Claude flows remain supported
- transcript-backed progress/result delivery matches the upstream parity
  contract
- explicit unbound-topic `/resume` remains degraded by design
- help text and `/start` must advertise the degraded path explicitly

Promotion gate:

- `tests/ccbot/test_claude_parity_contract.py`
- `tests/ccbot/test_claude_runtime_adapter.py`
- the relevant rows in `doc/multi-runtime-regression-matrix.md`

### Ring 2: fast-agent canary

Use a dedicated bot instance or host-scoped deployment when enabling
fast-agent.

Configuration example:

- `CLAUDE_COMMAND=fast-agent`

Required operator expectations:

- launch starts a tmux-first fast-agent lane
- persisted session discovery works for already bound topics
- `acp_log.jsonl` is preferred as replay evidence when present
- explicit unbound-topic `/resume` remains degraded by design
- `/rename` updates tmux/topic state and fast-agent title metadata only

Promotion gate:

- `tests/ccbot/test_fast_agent_sessions.py`
- `tests/ccbot/test_runtime_registry.py`
- the relevant rows in `doc/multi-runtime-regression-matrix.md`

## Partial enablement policy

Partial enablement is allowed, but it must be explicit.

Allowed:

- one production bot instance stays on Codex
- a separate canary bot instance or host runs Claude Code
- a separate canary bot instance or host runs fast-agent
- documentation and help copy differ by configured runtime lane

Forbidden:

- changing `CLAUDE_COMMAND` in place on a shared production bot without
  operator notice
- treating degraded `/resume` on Claude Code or fast-agent as an implementation
  bug to be hidden from users
- silently reinterpreting existing production topics under a new runtime lane

If a lane is not ready, keep it disabled rather than exposing a half-working
surface on the primary bot.

## Current rollout inventory

| Ring | Status | Host / instance | Service / bot surface | Owner | Change window | Rollback target |
|---|---|---|---|---|---|---|
| Ring 0: Codex production baseline | Active | `str` (`/home/tools/server/.production/production.md`, user `iqdoctor`) | `systemd --user ccbot.service`, bot `@ComfyCodexBot` | `@ViLco_O` | Explicit production maintenance window on the primary bot instance | Same host/service with previous known-good Codex launcher and existing `*.v1.bak` state backups |
| Ring 1: Claude Code restore canary | Reserved, not yet deployed | Separate canary bot instance or host, not the Ring 0 production service | Reserve `ccbot-claude.service` or an equivalent dedicated bot instance | Assign before cutover | Dedicated Claude canary window only | Disable the canary and keep Ring 0 on Codex |
| Ring 2: fast-agent canary | Reserved, not yet deployed | Separate canary bot instance or host, not the Ring 0 production service | Reserve `ccbot-fast-agent.service` or an equivalent dedicated bot instance | Assign before cutover | Dedicated fast-agent canary window only | Disable the canary and keep Ring 0 on Codex |

Inventory rules:

- do not reuse the Ring 0 production service for Claude Code or fast-agent canaries
- do not promote a canary without naming the owner and rollback target in this table
- if the concrete canary host/service changes, update this table in the same change as the rollout decision

## Operator capability differences

### Codex

- explicit `/resume <thread-name|id>` from an unbound topic: supported
- replay evidence: `~/.codex/sessions/**/rollout-*.jsonl`
- operator tool: `/home/tools/codex-tools/codex-session-scout`

### Claude Code

- explicit `/resume` from an unbound topic: degraded by design
- replay evidence: transcript JSONL under `~/.claude/projects/`
- parity reference: `/home/tools/ccbot-upstream`

### fast-agent

- explicit `/resume` from an unbound topic: degraded by design
- replay evidence: `acp_log.jsonl` when present, otherwise session/history files
- persisted rename: title metadata only

## Cutover procedure

When promoting a runtime lane:

1. Freeze the target host/bot instance to one intended runtime lane.
2. Set `CLAUDE_COMMAND` to the target launcher.
3. Run targeted regression tests for that lane plus shared topic-policy and
   Telegram-delivery coverage.
4. Restart only the scoped bot process.
5. Smoke-test:
   - fresh bind
   - follow-up queued text
   - `/history`
   - runtime-appropriate `/resume`
   - `/rename`
   - explicit `/unbind` -> `manual_bind_required`
6. Verify no regression in preserved `voice`, `task`, and `ACP-module` surfaces.

Minimum cutover checklist:

- the target ring in the inventory table is assigned and current
- the runtime-specific promotion gate has passed
- help text and `/start` match the degraded or supported `/resume` semantics of that lane
- the production topic semantics for any existing bot instance are unchanged unless the deploy explicitly changes the configured lane

## Fallback and rollback

If a canary lane is not production-ready:

- keep the lane disabled on the main production bot
- revert the canary instance to the previous launcher or stop the canary
  instance entirely
- do not rewrite topic state solely to accommodate the failed runtime

If a deployed lane must roll back:

1. Stop the scoped bot instance.
2. Restore bot-side persisted state from the existing migration backups if the
   failure is schema-related.
3. Reset `CLAUDE_COMMAND` to the previous known-good launcher.
4. Restart the scoped bot instance.
5. Re-run the smoke checklist for the restored lane.

Rollback checklist:

- confirm the rollback target named in the inventory table
- restore only the scoped bot instance; do not reboot the host
- verify `manual_bind_required` topics stay manually unbound after the rollback
- verify preserved `voice`, `task`, and `ACP-module` behavior before reopening the lane

## Release decision

`GO` for a runtime lane requires all of:

- lane-specific regression tests pass
- shared topic-policy and Telegram-delivery tests pass
- matrix rows for that lane are satisfied
- operator docs match the actual degraded/supported command surface

`NO GO` if any of the following remain true:

- the lane changes topic semantics without an explicit deploy-time switch
- help text implies `/resume` or `/rename` support that the runtime does not have
- progress/result delivery is not proved against the frozen matrix
- `voice`, `task`, or `ACP-module` regress on the same bot instance
