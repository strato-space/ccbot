# CCBot

[中文文档](README_CN.md)
[Русская документация](README_RU.md)

Control Codex tmux sessions remotely via Telegram — monitor, interact, and manage AI coding work without leaving the live terminal surface.

https://github.com/user-attachments/assets/15ffb38e-5eb9-4720-93b9-412e4961dc93

## Why CCBot?

Codex runs in your terminal. When you step away from your computer — commuting, on the couch, or just away from your desk — the session keeps working, but you lose visibility and control.

CCBot solves this by letting you **seamlessly continue the same terminal-backed conversation from Telegram**. The key insight is that it operates on **tmux**, not a hosted agent API. Your Codex process stays exactly where it is, in a tmux window on your machine. CCBot simply reads its output and sends keystrokes to it. This means:

- **Switch from desktop to phone mid-conversation** — Codex is working on a refactor? Walk away, keep monitoring and responding from Telegram.
- **Switch back to desktop anytime** — Since the tmux session was never interrupted, just `tmux attach` and you're back in the terminal with full scrollback and context.
- **Run multiple conversations in parallel** — Each Telegram topic maps to a separate tmux window, so you can juggle multiple projects from one chat group.

Other Telegram bots often wrap a separate API session that cannot be resumed in your terminal. CCBot takes a different approach: it's just a thin control layer over tmux, so the terminal remains the live control surface and you never lose the ability to switch back.

In fact, CCBot itself was built this way — iterating on itself through terminal sessions monitored and driven from Telegram via CCBot.

## Runtime Model

The adaptation work uses an explicit runtime ontology to avoid collapsing live
tmux control, persisted conversation identity, and on-disk replay evidence into
a single "session" concept.

The maintainer note is:
- [`doc/runtime-ontology.md`](doc/runtime-ontology.md)
- [`doc/state-migration.md`](doc/state-migration.md)
- [`doc/strato-ops-codex.md`](doc/strato-ops-codex.md)
- [`doc/multi-runtime-regression-matrix.md`](doc/multi-runtime-regression-matrix.md)
- [`doc/multi-runtime-rollout.md`](doc/multi-runtime-rollout.md)

The canonical shape is:

`Telegram topic -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`

Maintainer reference:
- [`doc/runtime-ontology.md`](doc/runtime-ontology.md)
- [`doc/state-migration.md`](doc/state-migration.md)
- [`doc/strato-ops-codex.md`](doc/strato-ops-codex.md)
- [`doc/multi-runtime-regression-matrix.md`](doc/multi-runtime-regression-matrix.md)
- [`doc/multi-runtime-rollout.md`](doc/multi-runtime-rollout.md)

## Strato Ops

For the Strato fork, use the operator runbook in
[`doc/strato-ops-codex.md`](doc/strato-ops-codex.md) for the current Codex
production lane, and [`doc/multi-runtime-rollout.md`](doc/multi-runtime-rollout.md)
for staged Claude Code restore / fast-agent enablement. Together they document:

- the live `tmux -> runtime process -> replay evidence` operating path
- the legacy `CLAUDE_COMMAND` env var name that now launches `codex`
- one-time state migration and reversible rollback via `*.v1.bak`
- the operator tooling path `/home/tools/codex-tools/codex-session-scout`
- the release-scope boundary: `voice`, `task`, and `ACP-module` are preserved but not expanded in this release
- staged enablement rules so partial runtime rollout does not silently change semantics in production topics

## Features

- **Topic-based control** — Each Telegram topic binds to one tmux window at a time, while the live process in that window may start or resume a persisted conversation identity
- **Compact Telegram delivery** — In the default production surface, user echo,
  orchestration milestones, and final assistant answers remain ordinary content
  bubbles, the latest human-facing commentary stays visible as a dedicated
  artifact, and technical reasoning/tool/command/file-change churn stays in
  the mutable status artifact. Once the final assistant answer lands, the
  commentary lane closes until the next user turn so no late commentary
  appears below the final answer
- **Prompt-safe control lane** — Detect `input ready`, `busy`, and `blocked prompt` terminal states before sending input
- **Voice messages** — Voice messages are transcribed via OpenAI and forwarded as text
- **Send messages** — Forward text to Codex via tmux keystrokes
- **Codex command forwarding** — Forward raw Codex slash commands, with a small supported menu surface for `/clear`, `/compact`, `/diff`, `/init`, `/review`, and `/status`
- **Create new conversations** — Start Codex conversations from Telegram via directory browser
- **Resume conversations** — Pick up where you left off by resuming an existing Codex identity in a directory
- **Kill bindings** — Close a topic to auto-kill the associated tmux window
- **Message history** — Browse conversation history with pagination (newest first)
- **Explicit process registration** — Auto-associates tmux windows with Codex processes at launch time
- **Persistent state** — Thread bindings and read offsets survive restarts

## Prerequisites

- **tmux** — must be installed and available in PATH
- **Codex CLI** — the `codex` binary must be installed

## Installation

### Option 1: Install from GitHub (Recommended)

```bash
# Using uv (recommended)
uv tool install git+https://github.com/strato-space/ccbot.git

# Or using pipx
pipx install git+https://github.com/strato-space/ccbot.git
```

### Option 2: Install from source

```bash
git clone https://github.com/strato-space/ccbot.git
cd ccbot
uv sync
```

## Configuration

**1. Create a Telegram bot and enable Threaded Mode:**

1. Chat with [@BotFather](https://t.me/BotFather) to create a new bot and get your bot token
2. Open @BotFather's profile page, tap **Open App** to launch the mini app
3. Select your bot, then go to **Settings** > **Bot Settings**
4. Enable **Threaded Mode**

**2. Configure environment variables:**

Create `~/.ccbot/.env`:

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

**Required:**

| Variable             | Description                       |
| -------------------- | --------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather         |
| `ALLOWED_USERS`      | Comma-separated Telegram user IDs |

**Optional:**

| Variable                | Default    | Description                                      |
| ----------------------- | ---------- | ------------------------------------------------ |
| `CCBOT_DIR`             | `~/.ccbot` | Config/state directory (`.env` loaded from here) |
| `TMUX_SESSION_NAME`     | `ccbot`    | Tmux session name                                |
| `CLAUDE_COMMAND`        | `claude`   | Legacy env var name for the command run in new windows; set this explicitly to `codex` or your Codex wrapper in this fork |
| `MONITOR_POLL_INTERVAL` | `2.0`      | Polling interval in seconds                      |
| `CCBOT_SHOW_HIDDEN_DIRS` | `false` | Show hidden (dot) directories in directory browser |
| `OPENAI_API_KEY` | _(none)_ | OpenAI API key for voice message transcription |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API base URL (for proxies or compatible APIs) |

Message formatting is always HTML via `chatgpt-md-converter` (`chatgpt_md_converter` package).
There is no runtime formatter switch to MarkdownV2.

> For Codex, prefer setting approval and sandbox policy explicitly in the launch command rather than relying on interactive approval prompts in a detached terminal.

## Launch Behavior

CCBot registers live processes at launch time and then resolves them onto runtime conversation identities. The tmux window is the live write target; the runtime conversation identity and replay evidence remain separate persisted objects.

## Usage

```bash
# If installed via uv tool / pipx
ccbot

# If installed from source
uv run ccbot
```

### Commands

**Bot commands:**

| Command       | Description                     |
| ------------- | ------------------------------- |
| `/start`      | Show welcome message |
| `/history`    | Message history for this topic |
| `/screenshot` | Capture terminal screenshot |
| `/esc`        | Send Escape to interrupt the active runtime |
| `/bind`       | Start an explicit bind flow for this topic |
| `/unbind`     | Detach this topic from its live window |
| `/resume`     | Bind this topic to a persisted runtime thread when the configured lane supports deterministic explicit resume |
| `/rename`     | Rename the current tmux window and sync the topic title |

**Supported Codex core-lane commands shown in the Telegram menu when the configured launch lane is Codex:**

| Command    | Description                  |
| ---------- | ---------------------------- |
| `/clear`   | Start a fresh chat in the bound window |
| `/compact` | Compact the current thread context |
| `/diff`    | Show git diff |
| `/init`    | Create `AGENTS.md` for Codex |
| `/review`  | Review current changes |
| `/status`  | Show Codex session status |

Other raw `/command` inputs are still forwarded best-effort to the active tmux-hosted runtime, but they are not part of the supported Telegram command surface unless documented above. This is intentional: commands that depend on prompt selection or other unsupported remote controls are not advertised in the menu even if a runtime can handle them locally. Claude-only commands such as `/cost`, `/help`, `/memory`, and `/usage` are not part of the supported Codex lane.

### Topic Workflow

**1 Topic = 1 live tmux binding at a time.** The bot runs in Telegram Forum (topics) mode.

Each topic controls one tmux window at a time. The process inside that window may start a fresh conversation or resume an existing persisted identity. The concrete runtime lane depends on `CLAUDE_COMMAND`.

**Creating a new session:**

1. Create a new topic in the Telegram group
2. Send any plain text message in the topic
3. A directory browser appears — select the project directory
4. If the directory has existing Codex identities, an identity picker appears — choose one to resume or start fresh
5. A tmux window is created, the configured runtime starts there (with resume wiring if resuming), and your pending message is forwarded

**Explicit bind, explicit resume, and manual unbind:**

- The first plain text message in a fresh topic may still trigger the bind flow automatically.
- After an explicit `/unbind` or a picker cancel, the topic enters `manual_bind_required`.
- In `manual_bind_required`, plain messages do not restart binding implicitly.
- Use `/bind` to choose a live window or workspace again.
- Use `/resume <thread-name|id>` only when the configured runtime lane supports deterministic explicit resume from an unbound topic.
  - Codex: supported by exact persisted thread id or exact thread name.
  - Claude Code: degraded from an unbound topic because transcript ids do not prove the workspace path.
  - fast-agent: degraded from an unbound topic because session ids are scoped by the workspace `.fast-agent` root.

**Sending messages:**

Once a topic is bound to a window, plain text and voice messages are forwarded to the active tmux-hosted runtime. Voice is transcribed first, then routed like plain text.

Routing note:
- Telegram text and voice inputs enter the equal message layer in `queue` mode by default.
- `steer` is a routing semantic for runtime-aware control flows; it is not the same thing as raw terminal takeover.
- Raw terminal control in tmux remains a separate operator layer and is never modeled as an ordinary queued message.

**Killing a session:**

Close (or delete) the topic in Telegram. The associated tmux window is automatically killed and the binding is removed.

### Message History

Navigate with inline buttons:

```
📋 [project-name] Messages (42 total)

───── 14:32 ─────

👤 fix the login bug

───── 14:33 ─────

I'll look into the login bug...

[◀ Older]    [2/9]    [Newer ▶]
```

### Notifications

The monitor polls replay evidence every 2 seconds and projects it onto the
Telegram delivery surface.

In the default production-facing `compact` mode, the visible bubble surface is
intentionally narrow:

- **User echo** — The submitted Telegram message is echoed back into the topic
- **Orchestration milestones** — Spawned/waiting/completed subagent status is
  rendered as Codex-style human-facing milestone bubbles instead of raw
  `spawn_agent` / `wait_agent` / `<subagent_notification>` payloads
- **Commentary** — Human-facing progress narrative remains visible as ordinary
  content so execution context does not disappear under mutable status churn
- **Final assistant responses** — The completed assistant answer lands as
  ordinary content

Technical execution classes stay out of permanent bubbles by default:

- **Reasoning / thinking** — Routed through the mutable status artifact or
  suppressed when they are placeholder-only
- **Tool lifecycle** — Summarized into the mutable status artifact
- **Command execution / local command** — Summarized into the mutable status
  artifact with compact command text rather than raw shell dumps
- **File-change summaries** — Routed through the mutable status artifact

Verbose/debug paths may expose more raw execution surface, but that is not the
default product contract.

Notifications are delivered to the topic bound to the window.

Formatting note:
- Telegram messages are rendered with parse mode `HTML` using `chatgpt-md-converter`
- Long messages are split with HTML tag awareness to preserve code blocks and formatting

## Running Codex in tmux

### Option 1: Create via Telegram (Recommended)

1. Create a new topic in the Telegram group
2. Send any message
3. Select the project directory from the browser

### Option 2: Create Manually

```bash
tmux attach -t ccbot
tmux new-window -n myproject -c ~/Code/myproject
# Then start Codex in the new window
codex
```

The window must be in the `ccbot` tmux session (configurable via `TMUX_SESSION_NAME`). CCBot registers the live process when it launches the window and then resolves the persisted identity from local Codex state.

## Data Storage

| Path                            | Description                                                             |
| ------------------------------- | ----------------------------------------------------------------------- |
| `$CCBOT_DIR/state.json`         | Topic bindings, window states, display names, and per-user read offsets |
| `$CCBOT_DIR/session_map.json`   | Versioned live process registrations and identity hints per tmux window |
| `$CCBOT_DIR/monitor_state.json` | Monitor byte offsets per replay source (prevents duplicate notifications) |
| `~/.codex/session_index.jsonl`  | Persisted Codex identity index (read-only)                               |
| `~/.codex/sessions/`            | Codex rollout logs and persisted identity state (read-only)             |

## File Structure

```
src/ccbot/
├── __init__.py            # Package entry point
├── main.py                # CLI dispatcher (hook subcommand + bot bootstrap)
├── hook.py                # Hook subcommand for session tracking (+ --install)
├── config.py              # Configuration from environment variables
├── bot.py                 # Telegram bot setup, command handlers, topic routing
├── session.py             # Session management, state persistence, message history
├── session_monitor.py     # JSONL file monitoring (polling + change detection)
├── monitor_state.py       # Monitor state persistence (byte offsets)
├── transcript_parser.py   # Legacy transcript parsing + normalized rollout event shaping
├── terminal_parser.py     # Terminal pane parsing (interactive UI + status line)
├── html_converter.py      # Markdown → Telegram HTML conversion + HTML-aware splitting
├── screenshot.py          # Terminal text → PNG image with ANSI color support
├── transcribe.py          # Voice-to-text transcription via OpenAI API
├── utils.py               # Shared utilities (atomic JSON writes, JSONL helpers)
├── tmux_manager.py        # Tmux window management (list, create, send keys, kill)
├── fonts/                 # Bundled fonts for screenshot rendering
└── handlers/
    ├── __init__.py        # Handler module exports
    ├── callback_data.py   # Callback data constants (CB_* prefixes)
    ├── directory_browser.py # Directory browser inline keyboard UI
    ├── history.py         # Message history pagination
    ├── interactive_ui.py  # Interactive UI handling (AskUser, ExitPlan, Permissions)
    ├── message_queue.py   # Per-user message queue + worker (merge, rate limit)
    ├── message_sender.py  # safe_reply / safe_edit / safe_send helpers
    ├── response_builder.py # Response message building (format tool_use, thinking, etc.)
    └── status_polling.py  # Terminal status line polling
```

## Contributors

Thanks to all the people who contribute! We encourage using Codex to collaborate on contributions.

<a href="https://github.com/strato-space/ccbot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=strato-space/ccbot" />
</a>
