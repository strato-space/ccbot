# Codex Command Semantics

This document defines the Codex-specific control semantics used by `ccbot`.
The goal is determinism: the bot must resolve a single Codex thread or fail
closed with an explicit error or picker.

## Naming Precedence

`ccbot` keeps the following layers separate:

- Telegram topic title: the visible forum label
- tmux window display name: the live operator-facing terminal label
- persisted runtime identity: the resumable thread/session id or title

The precedence is:

1. explicit `/rename` updates the tmux window and syncs the Telegram topic
   title to the final tmux name
2. Telegram topic rename events update the tmux window display name
3. persisted runtime identity changes only when the runtime explicitly supports
   them

When collision suffixes are required, the applied tmux name is authoritative
and the Telegram topic title is normalized to that suffix-applied name.

## `/resume <thread-name|id>`

- The token must match exactly by persisted thread id or by exact thread name.
- If the token is ambiguous, including id/name collisions or duplicate names,
  the command must fail closed.
- If the token does not resolve to a persisted Codex thread, the command must
  fail closed.
- The runtime launch uses a tmux window as the live control surface.
- The actual launch command is `codex resume <resolved-thread-id>` after the
  unique persisted identity has been resolved.
- The bot may reuse an exact tmux window when the window name and directory
  match the requested target and the live runtime kind is compatible.

## Persisted Thread Picker

- The picker lists human-resumable interactive Codex sessions for the selected
  cwd, not every rollout file under `~/.codex/sessions`.
- Non-interactive helper sessions such as `originator=codex_exec` or
  `source=exec` are hidden from manual resume candidates.
- Candidate labels use persisted thread names first. If a session has no
  persisted name, the picker uses the first human user message from the rollout
  as a preview, skipping injected service context such as `AGENTS.md`.
- A raw thread id is only a last-resort label when neither a name nor a safe
  human preview exists.

## `/rename <new-name>`

- `/rename` always renames the tmux window.
- The visible Telegram topic title is synchronized to the final tmux window
  name.
- Persisted Codex identity rename is only allowed through a stable public or
  proven-safe surface.
- If no safe persisted rename surface is available, the bot must document the
  degraded mode explicitly and leave the persisted Codex identity unchanged.
- For Codex, the current safe answer is degraded mode:
  - tmux window rename: yes
  - Telegram topic title sync: yes
  - persisted identity rename: unsupported / unsupported_degraded
- For fast-agent, `/rename` updates the tmux title and also updates the
  persisted session title metadata, but not the persisted session id.

## Multiline Submit ACK

Codex multiline input is a two-step tmux operation: bracketed paste first, then
bare `Enter`. The initial post-paste delay is deliberately tiny; it is only a
readiness gap, not proof of delivery. ccbot reports success for multiline
Codex input only after the bound Codex rollout JSONL appends a turn-acceptance
record such as `turn_context` or a matching user message. If the visible Codex
surface is still busy, ccbot does not paste multiline Telegram input; it fails
closed because immediate JSONL ACK cannot be proven. If no persisted ACK appears
within the bounded retry window on an input-ready pane, the send path fails
closed and warns that the draft may still be waiting in the terminal composer.

## Fail-Closed Rules

The following situations must produce an explicit error or picker instead of a
best-effort guess:

- duplicate thread names
- id/name collisions
- cross-directory matches when the target window or persisted identity cannot
  be proven to refer to the same cwd
- runtime mismatches between the requested Codex action and the live tmux pane
- missing persisted rollout evidence

## Why This Is Not ACP-First

`ccbot` keeps `tmux` as the authoritative operator intervention surface.
In other words, tmux is the authoritative operator intervention surface.
Human observability and direct terminal control take priority over protocol
purity, so Codex command semantics are defined in tmux-first terms and then
mapped to deterministic Codex launch behavior.
