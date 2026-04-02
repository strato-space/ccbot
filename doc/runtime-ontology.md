# Runtime Ontology For Codex Adaptation

This note defines the entities that the Codex adaptation work must keep distinct.
It is the contract for all implementation tasks in `/home/ccbot-codex-adaptation-plan.md`.

## Why this exists

The current codebase is Claude-oriented and often uses the word `session` as if a
single thing were being launched, controlled, resumed, and read back. That is too
coarse for Codex.

For Codex work, the implementation must distinguish the live terminal process from
the persisted conversation identity and from the rollout evidence on disk.

## Definitions

- **Telegram topic**
  - The user-facing control lane in Telegram.
  - It is where commands, text input, screenshots, and notifications are shown.

- **Binding**
  - The bot's persisted association from a Telegram topic to a live tmux window,
    plus the runtime metadata needed to route notifications and resume safely.

- **tmux window**
  - The live terminal container managed by the bot.
  - It is where the interactive `codex` process runs.

- **Codex process**
  - The currently running interactive `codex` CLI instance inside a tmux window.
  - The bot writes user input to this process through tmux keystrokes.

- **Codex thread**
  - The persisted conversation identity that Codex can resume later.
  - Thread metadata is exposed through the local Codex storage model and related tooling.

- **Rollout log**
  - The JSONL event stream on disk under `~/.codex/sessions/.../rollout-*.jsonl`.
  - This is read-only evidence emitted by Codex and used for history and notifications.

- **Runtime adapter**
  - The code layer that translates runtime-specific launch, identity, input, and
    event semantics into the bot's generic behavior.

## Canonical model

The adaptation must use this chain:

`Telegram topic -> binding -> tmux window -> Codex process -> Codex thread -> rollout log`

The entities above are related, but they are not interchangeable.

## Read path vs write path

### Write path

The bot writes to the live Codex process through tmux.

Allowed write target:
- `Telegram topic -> binding -> tmux window -> Codex process`

Not allowed:
- Writing "to a thread"
- Writing "to a rollout log"
- Treating `session_index.jsonl` or rollout JSONL as command targets

### Read path

The bot reads history and notification evidence from normalized rollout events on disk.

Allowed read target:
- `Telegram topic -> binding -> Codex thread -> rollout log -> normalized events`

Not allowed:
- Treating the live pane buffer as the primary history source
- Treating tmux pane text as the identity source for a thread

tmux pane capture may be used for:
- screenshots
- prompt-state hints
- fail-closed interactive state detection

tmux pane capture must not be used as the sole source of truth for:
- thread identity
- persisted history
- resume resolution

## Forbidden equalities

These equalities are false and must stay false in code, docs, and tests:

- `window == thread`
- `process == thread`
- `process == rollout log`
- `thread == rollout log`
- `topic == thread`
- `topic == window`

More precise statements:

- A tmux window hosts a live process, but it is not the persisted thread.
- A process may attach to an existing thread via resume, but it is not that thread.
- A rollout log is emitted evidence from process/thread activity, not the process itself.
- A Telegram topic is bound to a live control lane, not directly to a persisted log file.

## Operational invariants

- A topic may bind to at most one live tmux window at a time.
- A tmux window may host at most one active Codex process at a time.
- A live process may be associated with at most one primary Codex thread at a time.
- A thread may have multiple historical rollout logs over time.
- Resume attaches a new or reused live process to an existing thread; it does not restore
  the previous live process.
- History is reconstructed from normalized rollout evidence, not from the live process buffer.

## Resolution policy

Thread resolution must be deterministic and fail closed.

Preferred precedence:
1. Explicit thread id chosen by the operator
2. Explicit launcher-side registration record
3. Exact normalized cwd match with a single valid candidate
4. User-visible disambiguation

Never do this:
- silently choose between multiple same-cwd thread candidates
- guess thread identity from pane text alone
- assume `/root/.codex` is the only valid Codex home

## Scope guard

The Codex adaptation work does not introduce new `voice`, `task`, or `ACP` behavior.
Those flows are shared-surface compatibility constraints and must be protected by
non-regression tests while the runtime model changes underneath them.
