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

Fresh `/bind` is different from `/rename`: when the bot has captured a Telegram
surface title from topic create/edit events, that title may seed the new tmux
window name. If no title is known, the bot may fall back to cwd-derived tmux
naming, but it must not overwrite the existing Telegram topic title with the cwd
basename. The cwd is workspace metadata, not the surface name.

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
- A fresh Codex/OMX bind in a cwd that already has another active bound runtime
  must not adopt that older runtime's rollout by cwd or recent mtime alone. The
  new window stays replay-silent until its own runtime identity is proven.
- If two distinct tmux windows resolve to the same runtime thread id, delivery
  fails closed for the ambiguous duplicate instead of fanning one replay stream
  into multiple unrelated Telegram topics.

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

## Codex Conversational Submit ACK

Codex conversational input is a two-step tmux operation: deliver the payload,
then send the runtime-native submit key. Single-line payloads use literal text;
multiline payloads use bracketed paste followed by bare `Enter`. The initial
post-payload delay is deliberately tiny; it is only a readiness gap, not proof
of delivery. ccbot treats the same-runtime-identity rollout JSONL append as the
durable turn-acceptance proof. A matching user message is the strongest ACK;
bare `turn_context` can count only inside the per-window ACK guard after the
submit key has been sent. If the visible Codex surface is still busy, ccbot does
not inject Telegram input; it fails closed because immediate JSONL ACK cannot
be proven. If payload and submit-key delivery succeed but no persisted ACK
appears within the bounded retry window on an input-ready pane, the send path
surfaces an explicit delivered-but-unconfirmed state and continues matching a
later delayed replay user echo.

Local service automation that needs to submit Codex input must enter through
`ccbot runtime-input`, not through `ccbot send` and not through copied tmux
paste/send-key snippets. This keeps the local automation path on the same
live-input-plane checks and Codex conversational ACK contract as Telegram-originated text.

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
