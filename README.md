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
- **Run multiple conversations in parallel** — Each Telegram topic, or one explicitly bound no-topics group main chat, maps to its own control surface, so you can juggle multiple projects from one chat group.

Other Telegram bots often wrap a separate API session that cannot be resumed in your terminal. CCBot takes a different approach: it's just a thin control layer over tmux, so the terminal remains the live control surface and you never lose the ability to switch back.

In fact, CCBot itself was built this way — iterating on itself through terminal sessions monitored and driven from Telegram via CCBot.

## Runtime Model

The adaptation work uses an explicit runtime ontology to avoid collapsing live
tmux control, persisted conversation identity, and on-disk replay evidence into
a single "session" concept.

Start with the compact ontology index:
- [`ontology/README.md`](ontology/README.md)
- [`ontology/runtime.md`](ontology/runtime.md)
- [`ontology/topic-control.md`](ontology/topic-control.md)
- [`ontology/delivery-surface.md`](ontology/delivery-surface.md)
- [`ontology/boundaries.md`](ontology/boundaries.md)

Execution plans and companion specs live in:
- [`specs/README.md`](specs/README.md)
- [`specs/ccbot-codex-adaptation-plan.md`](specs/ccbot-codex-adaptation-plan.md)
- [`specs/ccbot-codex-adaptation-plan-2.md`](specs/ccbot-codex-adaptation-plan-2.md)
- [`specs/ccbot-codex-adaptation-plan-4.md`](specs/ccbot-codex-adaptation-plan-4.md)
- [`specs/ccbot-fast-agent-jsonl-spec.md`](specs/ccbot-fast-agent-jsonl-spec.md)

Then use the longer derived maintainer notes:
- [`doc/runtime-ontology.md`](doc/runtime-ontology.md)
- [`doc/runtime-event-contract.md`](doc/runtime-event-contract.md)
- [`doc/telegram-delivery-pipeline.md`](doc/telegram-delivery-pipeline.md)
- [`doc/state-migration.md`](doc/state-migration.md)
- [`doc/strato-ops-codex.md`](doc/strato-ops-codex.md)
- [`doc/multi-runtime-regression-matrix.md`](doc/multi-runtime-regression-matrix.md)
- [`doc/multi-runtime-rollout.md`](doc/multi-runtime-rollout.md)

The canonical shape is:

`Telegram control surface -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`

External replay-only shape is also supported:

`Telegram control surface -> binding(binding_scope=external) -> runtime conversation identity -> replay evidence`

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

- **Topic-based control** — Each Telegram topic binds to one delivery source at a time: either a live tmux window, or an external persisted Codex thread in read-only replay mode
- **Helper-window isolation** — Codex native subagent/helper tmux windows remain
  parent-owned evidence surfaces and are hidden from ordinary `/bind` pickers;
  stale callbacks that target them fail closed, and pre-existing bindings to
  helper or metadata-less inactive windows are pruned fail-closed on state
  refresh
- **Compact Telegram delivery** — In the default production surface, user echo,
  orchestration milestones, and final assistant answers remain ordinary content
  bubbles, the latest human-facing commentary stays visible as a dedicated
  artifact, and technical reasoning/tool/command/file-change churn stays in
  the mutable status artifact. Once the final assistant answer lands, the
  whole pre-final visible surface closes until the next user turn, and the
  mutable technical status surface closes with it, so no late commentary, orchestration
  milestone, or surfaced preview artifact appears below the
  final answer for the same turn, and no late status artifact appears below the
  final answer for the same turn. Put bluntly: no pre-final visible artifact
  or late technical status may leak below the terminal assistant bubble.
  When compactness and semantic clarity conflict, the delivery surface prefers
  visibility-first edit-in-place updates over ambiguous silence.
  Long-wait reviewer/progress commentary may be re-sent at the chat tail while
  replacing the previous commentary artifact, because editing an old Telegram
  bubble can make the update effectively invisible to the operator.
  If a new turn starts via hidden opener scaffolding, lifecycle `turn_started`
  can reopen the delivery lanes idempotently without creating a duplicate
  visible user-opener bubble; targeted Stop-hook/Ralph continuation prompts
  may do the same hidden reopen while still rendering only as operator
  warnings, never as ordinary user echo.
  Ordinary user echo remains visible; only explicit internal payload shapes
  such as `<subagent_notification>` or tagged command scaffolds stay hidden and
  non-turn-opening. For live Codex bindings, a newly discovered replay source
  may backfill only the bounded current-turn opener so tmux-originated user
  messages are visible without flooding historical replay.
  When command/tool/file previews are surfaced, they follow a Codex-style split:
  preview body in fenced `sh` / `json`, truncation metadata outside the fence,
  and no redundant `completed · output 1 line(s)` footer when the preview
  already conveys the result.
- **Queued follow-up preview** — When Codex is still running and later messages
  are queued behind the active turn, the bot may surface them as a separate
  mutable pending-input artifact (after-next-tool, end-of-turn, and queued
  follow-up sections) rather than mixing them into commentary or current-turn
  status. That artifact belongs to future input, so the terminal assistant
  answer does not clear it by itself; it closes only when the queue is empty,
  the binding goes stale, or an explicit clear path runs.
- **OMX interactive questions** — Runtime-owned `omx.question/v1` records under
  `.omx/state/questions/`, `.omx/state/sessions/*/questions/`, or the explicit
  `--state-path` of a same-window OMX question renderer pane are rendered as a
  separate mutable Telegram artifact with inline option buttons. A newly
  discovered first prompt is held while the current turn's pre-final lane is
  open so explanatory final/info content lands before the questionnaire; edits
  to an already visible prompt still happen in place and are not delayed by that
  first-send gate. Prompt sends, edits, and first-send deferrals are written to
  the delivery audit as interactive-question events. The temporary renderer
  pane belongs to the bound tmux window; it is not promoted to a bindable
  control surface or delivery source. Choosing an option must first bridge the
  normal `[omx question answered] ...` continuation line through the bound
  runtime input path when a bound window is known (so Codex submit/ACK handling
  applies) or to the recorded tmux return pane as a fallback. The durable
  record is written as `answered` only after that bridge succeeds; if Codex is
  busy or the bridge fails, Telegram keeps the question retryable instead of
  showing a terminal answered state. If the record allows `Other`,
  a free-text Telegram reply to the same bound thread is recorded as the
  `Other` answer and follows the same return-pane bridge. If the record times
  out with an error while the same-window renderer pane is still alive and
  visibly matches the record, Telegram keeps/reopens the question artifact as
  answerable instead of treating the timeout as final. If the renderer pane exits
  before a Telegram answer bridge finishes and the record still names a bound
  return pane, Telegram keeps the artifact retryable for a short recovery window
  instead of flashing a terminal renderer error. If renderer startup fails before
  a helper pane is created but the session-scoped OMX mode state still names a
  same-window tmux return pane, Telegram may recover the question using that
  return bridge instead of surfacing the renderer error as final technical
  status; when safe, it may also materialize a replacement same-window helper
  pane for the local tmux operator view. While a question is active or recoverable,
  ordinary Telegram input to that window fails closed unless it is consumed as
  an allowed `Other` answer.
- **Heads-up warnings stay visible without breaking turn closure** — Operator
  warning notices remain visible in Telegram while assistant-final semantics
  and post-final artifact closure remain intact. Repeated identical warning
  text reuses one warning bubble and adds a repeat counter only when `N > 2`.
- **Runtime discontinuity guardrails** — True runtime termination or live tmux
  surface loss is delivered as a warning artifact with replay-native evidence
  first and screenshot fallback only for real loss. Active Codex panes that
  render as `node` processes with unclassified footers are treated as live, so
  status polling does not turn ordinary footer churn into repeated screenshots.
- **Prompt-safe control lane** — Detect `input ready`, `busy`, and `blocked prompt` terminal states before sending input
- **Voice messages** — Voice messages are transcribed through a configurable
  STT provider (OpenAI by default, or a host-local command) and forwarded as text
- **Audio/video messages** — Telegram audio/video files within the configured Telegram bot download cap are saved under `$CCBOT_DIR/media` and forwarded artifact-first to the runtime as local paths plus metadata; transcription is optional future enrichment
- **Photo/document messages** — Telegram photos and documents/files such as `tar.gz` archives are downloaded and forwarded to the runtime as local file paths; Telegram media groups and same-surface orphan attachment bursts are batched into one runtime input when safe
- **Sticker messages** — Telegram stickers are normalized to image attachments for the runtime; animated/video stickers use their Telegram thumbnail when available
- **Generated-image terminal media result** — successful textual image-generation
  tool output with a safely validated generated-image artifact may be delivered
  in compact mode as one terminal Telegram photo bubble with a caption; if
  validation, reading, or media send fails, the saved-path text remains the
  terminal fallback
- **Runtime image preview artifact** — Codex `image_generation_end` and
  `view_image` / `Viewed Image` replay output with paired embedded image bytes
  is delivered as a latest-only pre-final mutable Telegram photo bubble with a
  sanitized caption; the first preview sends the bubble and later same-turn
  previews edit that media in place. If a preview carries multiple images,
  compact mode uses the first image and audits the truncation.
  It is authorized replay-proven disclosure to the active bound control surface.
  It is never a local-path file read and does not close the assistant turn.
- **Send messages** — Forward text to Codex via tmux keystrokes
- **Simple text fast path** — eligible one-line Codex text gets an immediate
  Telegram ingress receipt and starts a runtime injection attempt before the
  replay ACK finishes; after matching Codex/OMX replay confirms the same
  Telegram-originated text, that receipt remains as the durable user-input
  bubble and the duplicate `👤` replay echo is suppressed
- **Codex command forwarding** — Forward raw Codex slash commands, with a small supported menu surface for `/clear`, `/compact`, `/diff`, `/exit`, `/init`, `/review`, and `/status`
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
| `CCBOT_COMMAND`         | `claude`   | Runtime launcher command for new windows; set to `codex`, `omx --madmax`, or a host-specific wrapper |
| `CLAUDE_COMMAND`        | `claude`   | Legacy fallback used only when `CCBOT_COMMAND` is unset |
| `CCBOT_OWNED_SURFACES` | _(none)_ | Optional comma-separated allow list of chat-qualified surfaces (`t:<chat_id>:<thread_id>`, `c:<chat_id>`). When set, shared group updates outside these surfaces are hard-ignored before typing/reply/download/runtime side effects |
| `CCBOT_IGNORED_SURFACES` | _(none)_ | Optional comma-separated deny list of surfaces to hard-ignore; supports canonical keys plus legacy `t:<thread_id>` aliases for migrations |
| `MONITOR_POLL_INTERVAL` | `2.0`      | Polling interval in seconds                      |
| `CCBOT_SHOW_HIDDEN_DIRS` | `false` | Show hidden (dot) directories in directory browser |
| `OPENAI_API_KEY` | _(none)_ | OpenAI API key for voice message transcription when using `openai` or `auto` fallback |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API base URL (for proxies or compatible APIs) |
| `CCBOT_VOICE_STT_PROVIDER` | `openai` | Voice STT provider: `openai`, `local_command`, `auto`, or `disabled` |
| `CCBOT_LOCAL_STT_COMMAND` | _(none)_ | Local STT command template for `local_command`/`auto`; placeholders: `{input_path}`, `{output_dir}`, `{model}`, `{language}`, `{run_id}` |
| `CCBOT_LOCAL_STT_MODEL` | `antony66/whisper-large-v3-russian` | Local STT model identifier passed to the command template |
| `CCBOT_LOCAL_STT_LANGUAGE` | `ru` | Local STT language passed to the command template |
| `CCBOT_LOCAL_STT_TIMEOUT_SECONDS` | `300` | Timeout for one local STT command invocation |
| `CCBOT_LOCAL_STT_MAX_CONCURRENCY` | `1` | Max concurrent local STT command invocations |
| `CCBOT_LOCAL_STT_KEEP_ARTIFACTS` | `false` | Keep generated local STT run directories under `$CCBOT_DIR/media/stt` for debugging |
| `CCBOT_MAX_AUDIO_BYTES` | `52428800` | Maximum inbound Telegram audio artifact size before refusing download/forward |
| `CCBOT_MAX_VIDEO_BYTES` | `104857600` | Maximum inbound Telegram video artifact size before refusing download/forward |
| `CCBOT_MAX_TELEGRAM_DOWNLOAD_BYTES` | `20971520` | Maximum Bot API `getFile`/download size for inbound media; effective audio/video preflight uses the lower of this cap and the media-specific cap |
| `CCBOT_TELEGRAM_POOL_TIMEOUT` | `10.0` | HTTPX connection-pool wait timeout for ordinary Telegram Bot API requests |
| `CCBOT_TELEGRAM_GET_UPDATES_POOL_SIZE` | `4` | Dedicated Telegram `getUpdates` connection pool size; keep above PTB's single-connection default for long-poll resilience |
| `CCBOT_TELEGRAM_GET_UPDATES_POOL_TIMEOUT` | `10.0` | Connection-pool wait timeout for `getUpdates` requests |
| `CCBOT_TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT` | `10.0` | Connect timeout for Telegram long-poll requests |
| `CCBOT_TELEGRAM_GET_UPDATES_READ_TIMEOUT` | `30.0` | Read timeout for Telegram long-poll requests; should exceed `CCBOT_TELEGRAM_POLL_TIMEOUT` |
| `CCBOT_TELEGRAM_GET_UPDATES_WRITE_TIMEOUT` | `10.0` | Write timeout for Telegram long-poll requests |
| `CCBOT_TELEGRAM_POLL_TIMEOUT` | `10` | Telegram long-poll timeout passed to `run_polling` |
| `CCBOT_TELEGRAM_BOOTSTRAP_RETRIES` | `-1` | PTB polling bootstrap retries; negative values retry indefinitely so transient Bot API/proxy timeouts do not restart-loop the service before polling starts |
| `CCBOT_TELEGRAM_POLL_HEALTH_ENABLED` | `true` | Enable watchdog that exits the process when Bot API has pending updates but no Telegram update handler has run recently |
| `CCBOT_TELEGRAM_POLL_HEALTH_INTERVAL` | `60.0` | Watchdog check interval in seconds |
| `CCBOT_TELEGRAM_POLL_STALE_SECONDS` | `180.0` | Stale dispatcher age that allows watchdog restart when pending updates exist |
| `CCBOT_TELEGRAM_POLL_PENDING_THRESHOLD` | `1` | Pending update count threshold for watchdog restart |
| `CCBOT_TELEGRAM_POLL_HEALTH_FAILURE_THRESHOLD` | `3` | Consecutive timeout-like watchdog health failures before restart when dispatcher progress is stale |
| `CCBOT_TELEGRAM_POLL_WATCHDOG_EXIT_CODE` | `75` | Exit code used by the watchdog so systemd restarts a polling-dead service |

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

# Show top-level CLI help without loading bot secrets or starting polling
ccbot --help
```

### Commands

**Bot commands:**

| Command       | Description                     |
| ------------- | ------------------------------- |
| `/start`      | Show welcome message |
| `/history`    | Message history for this topic or supported main chat |
| `/screenshot` | Capture terminal screenshot |
| `/esc`        | Send Escape to interrupt the active runtime |
| `/bind`       | Start an explicit bind flow for this topic or supported main chat (`/bind <thread-name|id>` in Codex lane attaches external read-only replay) |
| `/unbind`     | Detach this topic or supported main chat from its live window |
| `/resume`     | Bind this topic or supported main chat to a persisted runtime thread when the configured lane supports deterministic explicit resume |
| `/rename`     | Rename the current tmux window and sync the topic title |
| `/switch [steer|queue]` | Toggle this Telegram `chat_id`/`message_thread_id` surface between `steer` and `queue`, or set the named mode explicitly |
| `/steer <prompt>` | Send one prompt with immediate steer semantics; without a prompt, set this surface to `steer` |
| `/queue <prompt>` | Send one prompt with runtime queue semantics; without a prompt, set this surface to `queue` |

**Supported Codex core-lane commands shown in the Telegram menu when the configured launch lane is Codex:**

| Command    | Description                  |
| ---------- | ---------------------------- |
| `/clear`   | Start a fresh chat in the bound window |
| `/compact` | Compact the current thread context |
| `/diff`    | Show git diff |
| `/exit`    | Terminate the live Codex process in the bound window |
| `/init`    | Create `AGENTS.md` for Codex |
| `/review`  | Review current changes |
| `/status`  | Show Codex session status |

Other raw `/command` inputs are still forwarded best-effort to the active tmux-hosted runtime, but they are not part of the supported Telegram command surface unless documented above. This is intentional: commands that depend on prompt selection or other unsupported remote controls are not advertised in the menu even if a runtime can handle them locally. Claude-only commands such as `/cost`, `/help`, `/memory`, and `/usage` are not part of the supported Codex lane, and `/quit` is explicitly rejected in favor of `/exit`.

**Local CLI result delivery:**

Services running in the same bot instance context can send results back to the
instance's Telegram chat without injecting anything into tmux:

```bash
# Defaults to the target resolved from $CCBOT_DIR/state.json
ccbot send "Job finished" --file-path ./result.tar.gz

# Explicit override when state has multiple possible surfaces
ccbot send "Job finished" \
  --chat-id -1001234567890 \
  --thread-id 42 \
  --file-path ./result.tar.gz
```

Targeting is hybrid: explicit `--chat-id` / `--thread-id` wins; otherwise the
CLI resolves the persisted Telegram control-surface routing coordinates from
`$CCBOT_DIR/state.json`. Ambiguous state fails closed and requires an explicit
target.


**Operator replay/backfill for missed terminal artifacts:**

If an older Codex rollout event was already consumed before generated-image
preview delivery was enabled, or a text assistant final was missed after a
tracking bug, do not rewind the global monitor offset. Use an explicit
operator-selected replay slice, call id, turn id, text hash, or byte range
instead:

```bash
# Dry-run first; prints selected generated-image candidates only
ccbot replay-backfill --replay-path ~/.codex/sessions/.../rollout.jsonl \
  --thread-id 019e... --call-id ig_... --json

# Deliver only the selected terminal media to the resolved Telegram surface
ccbot replay-backfill --replay-path ~/.codex/sessions/.../rollout.jsonl \
  --thread-id 019e... --call-id ig_... --deliver --json

# Dry-run a missed assistant-final text repair by explicit turn or byte range
ccbot replay-backfill --text-final \
  --replay-path ~/.codex/sessions/.../rollout.jsonl \
  --thread-id 019e... --turn-id turn_... --chat-id -100... \
  --message-thread-id 555 --json
```

`ccbot replay-backfill` never mutates monitor offsets. By default it
re-normalizes only selected `image_generation_end` records and writes a
`replay_backfill` audit row with replay path, byte offsets, call id, and media
hash. With `--text-final`, it requires `--byte-range`, `--turn-id`, or
`--text-sha256`, refuses ambiguous multi-final selections, skips candidates
already recorded unless `--force` is used, and writes a distinct
`replay_backfill_text` audit row with replay path, byte offsets, turn id, and
text hash for duplicate prevention.

**Local CLI runtime input injection:**

Services that need to submit text to the live tmux-hosted runtime must use the
runtime input plane, not `ccbot send` and not ad-hoc `tmux paste-buffer` logic:

```bash
# Resolve through persisted control-surface state
ccbot runtime-input --user-id 12345 --thread-id 42 "continue"

# Or target an operator-known live tmux window explicitly
ccbot runtime-input --window-id @7 "continue"
```

`ccbot runtime-input` uses the same `SessionManager` / `RuntimeInputDriver`
path as Telegram text: external replay-only bindings are read-only, inactive
or helper windows fail closed, blocked prompts are not bypassed, and Codex
conversational input uses runtime-native submit plus same-identity
replay-evidence ACK. Multiline payloads still use bracketed paste before
submit. `ccbot send` remains Telegram delivery only.

**Read-only binding/workspace preflight:**

Before live automation injects runtime input or treats workspace-sensitive media
delivery evidence as final, validate the expected binding without sending text
to tmux or Telegram:

```bash
CCBOT_DIR=/data/iqdoctor/.ccbot \
  ccbot binding-preflight --json \
  --user-id 3045664 \
  --surface-key t:-1003685295814:555 \
  --expected-user-id 3045664 \
  --expected-surface-key t:-1003685295814:555 \
  --expected-window-name comfy-agent \
  --expected-runtime-kind codex \
  --expected-cwd /home/tools/mediagen-comfy
```

`ccbot runtime-status` is the same read-only status/preflight surface under a
runtime-oriented name. Its JSON includes both the detailed `classification` and
a compact `status` such as `input_ready`, `no_live_input_plane`,
`binding_mismatch`, or `ambiguous` so external supervisors can gate
`ccbot runtime-input` without scraping human text.

For ComfyCodexBot, `/home/tools/mediagen-comfy` is the primary Codex workspace
for the Telegram control surface. `/home/tools/server/comfy` is historical
runtime/runbook context only and must not be used as the primary ComfyCodexBot
runtime-input or final media-evidence workspace. The preflight also fails
closed for inactive tmux bindings, Codex helper/subagent windows, and placeholder
runtime metadata that lacks a live cwd/runtime-kind proof.

**Polling liveness:**

The bot uses Telegram long polling. ccbot configures a dedicated `getUpdates`
connection pool and timeouts explicitly, then runs a watchdog that checks
`pending_update_count`. If Telegram reports pending updates while no inbound
Telegram handler has run for `CCBOT_TELEGRAM_POLL_STALE_SECONDS`, the watchdog
logs the stale state and exits with `CCBOT_TELEGRAM_POLL_WATCHDOG_EXIT_CODE` so
systemd can restart the service. The same fail-fast path is used after
`CCBOT_TELEGRAM_POLL_HEALTH_FAILURE_THRESHOLD` consecutive timeout-like
watchdog health failures while dispatcher progress is stale, covering pool
starvation cases where the health probe itself cannot complete. Logs include
key/value fields such as `event=telegram_polling_pending_stalled`,
`event=telegram_polling_health_timeout_stalled`, failure counts, age, thresholds,
and exit code; token/proxy credentials are redacted from health error text. The
initial PTB polling bootstrap uses `CCBOT_TELEGRAM_BOOTSTRAP_RETRIES=-1` by
default, so a temporary Bot API/proxy timeout during `deleteWebhook` is retried
in-process instead of creating a user-service restart loop. The watchdog does
not drain updates and does not mutate topic bindings; it only recovers a
service-alive-but-polling-dead process.

### Topic Workflow

**1 control surface = 1 binding at a time.**

The canonical runtime ontology is control-surface centric:

`Telegram control surface -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`

In a shared group topic or no-topics group main chat, the binding belongs to the
control surface, not to the person who created it. Any allowed group member who
writes in the same bound surface uses the same tmux window; `/bind`, `/resume`,
and `/unbind` operate on that shared surface binding.

For forum topics, "same surface" means the same Telegram group plus the same
topic/thread id. A topic with the same numeric thread id in another group is a
different control surface. Bot instances that share a Telegram group should set
`CCBOT_OWNED_SURFACES`; when that allow list is present, foreign group topics
are ignored before ccbot emits typing indicators, replies, media downloads,
runtime-input audit rows, or tmux input.

For shared groups without topics, the current product surface may expose one
explicit main-chat mode:

`no-topics main-chat control surface -> binding -> tmux window`

This no-topics path is **not** a claim that `chat == topic`; it is a separate
chat-wide control surface that coexists with named-topic behavior.

Each supported surface controls one delivery source at a time:

- live tmux window (writable control lane)
- external persisted Codex thread (read-only replay lane)

For fresh topic binds, Telegram topic titles captured from topic create/edit
events may seed the tmux window display name. In shared groups that title is a
chat/topic surface fact, not an actor fact, so another allowed participant may
reuse it for the same exact chat-qualified surface. If no title is known, ccbot
must not rename the Telegram topic to the selected cwd basename. After a
successful collision-suffixed title sync, the cached title is updated to the
final tmux name. Replay delivery also requires a proven runtime identity: a
fresh same-cwd Codex/OMX window stays silent until its own thread is known, and
one replay stream is not fanned out to distinct tmux windows/topics.

The concrete runtime lane depends on `CCBOT_COMMAND` (`CLAUDE_COMMAND` remains
a legacy fallback when `CCBOT_COMMAND` is unset).

Optional startup restore intent may be declared per bot instance with
`CCBOT_RESTORE_*` variables. These variables declare intended window/cwd/runtime
identity/control-surface coordinates only; canonical `surface_bindings` state is
still written only after startup validates the runtime identity, full
`(user_id, surface_key)` control-surface identity, and any required group
`chat_id` routing coordinates.  Restore treats a standalone `chat_id` as a
Telegram routing coordinate: it may derive or validate the canonical
chat-qualified surface key, but it is not itself a complete control-surface
identity.

For Codex-backed restore, the controller service environment must also include
the Codex replay root and non-interactive OMX setting, for example:

```env
CCBOT_COMMAND=omx --madmax
CODEX_HOME=/data/iqdoctor/.codex
OMX_AUTO_UPDATE=0
CCBOT_RESTORE_ENABLED=1
CCBOT_RESTORE_WINDOW=comfy-agent
CCBOT_RESTORE_CWD=/home/tools/mediagen-comfy
CCBOT_RESTORE_RUNTIME_ID=019d6825-88ba-7f10-948e-eaaf162ea2a9
CCBOT_RESTORE_USER_ID=3045664
CCBOT_RESTORE_SURFACE_KEY=t:-1003685295814:555
CCBOT_RESTORE_CHAT_ID=-1003685295814
CCBOT_RESTORE_SHARED_GROUP=true
CCBOT_RESTORE_COMMAND=omx --madmax
```

Runtime launches scrub controller-only `CCBOT_RESTORE_*`, Telegram token, and
controller selection variables from child process env. If OpenAI auth still
requires a secret wrapper for a particular host, that wrapper must be auth-only
and cwd-neutral; the selected workspace remains the explicit bind path.

Startup restore is non-destructive in v1: it inventories the tmux
session/window/panes before acting, distinguishes `LiveRuntimeProof` from
`ResumeTargetProof`, ignores OMX HUD/question/update/helper panes as bindable
runtime surfaces, and fails closed rather than killing tmux or restarting
services when live identity is ambiguous.  `CCBOT_RESTORE_*` remains restore
intent, not proof; when current live Codex fd proof disagrees with stale
restore intent for the same window, the live fd-proven identity wins.  Local
automation and live smoke validation must use `ccbot binding-preflight`, `ccbot runtime-input`,
and same-runtime replay-evidence ACK; `ccbot runtime-status` is the equivalent
read-only status alias for supervisors; do not use `ccbot send` or copied `tmux paste-buffer`
commands as a runtime input path.
The service startup path does not inject a smoke message automatically; its
bind-time gate stops at `LiveRuntimeProof`, while the operator live-ops gate
must prove `ccbot runtime-input` replay ACK for both configured bots.

On `str`, autonomous controller restart scope is limited to the two known bot
controller/tmux surfaces below.  Both controller services now carry the
tmux-preserving systemd drop-in `tmux-preserve.conf` with
`KillMode=process`; that drop-in reduces restart blast radius, but it is not
itself proof that tmux survived.  Both services must also carry the generic
Telegram token scrub drop-in
`deploy/systemd/user/telegram-token-env-scrub.conf` with
`UnsetEnvironment=TELEGRAM_BOT_TOKEN TELEGRAM_TOKEN` so a stale user-manager
environment cannot override the token loaded from the instance `CCBOT_DIR/.env`.
Before and after any approved controller restart, operators/automation must
record the tmux server PID and `tmux
list-sessions` output.  Non-target tmux sessions/windows/panes must not be
restarted or killed by this recovery path.

| Bot controller | systemd user service | `CCBOT_DIR` | tmux session/window | Telegram identity/routing | runtime cwd | `CODEX_HOME` |
| --- | --- | --- | --- | --- | --- | --- |
| ComfyCodexBot | `ccbot.service` | `/data/iqdoctor/.ccbot` | `comfy` / `comfy-agent` | user `3045664`, surface `t:-1003685295814:555`, chat `-1003685295814` | `/home/tools/mediagen-comfy` | `/data/iqdoctor/.codex` |
| ImmArenaBot | `imm_arena_bot.service` | `/data/iqdoctor/.ccbot-imm_arena_bot` | `imm_arena_bot` / `imm` | user `3045664`, surface `t:-1003974721114:3`, chat `-1003974721114` | `/home/tools/imm` | `/home/tools/imm/.codex` |

Older `/home/tools/server/comfy` references are historical/runtime-runbook
context only; they are not the primary ComfyCodexBot Codex workspace.

OMX HUD/helper panes are operator telemetry, not work-runtime panes.  A HUD
should remain a small bottom pane in its parent window and must never be chosen
as the restored Telegram binding target.

**Creating a new session:**

1. Create a new topic in the Telegram group, or use the main chat in a group where topics are disabled
2. Enter via a valid opener for that surface
   - private chats with topics enabled: a first plain text message may still open bind flow
   - shared group topics: ordinary text and `@bot` mentions stay silent until a command is used; use `/bind` or `/resume`
   - no-topics group main chat: ordinary text and `@bot` mentions stay silent until a command is used; use `/bind` or `/resume`
3. A directory browser appears from a cwd-neutral root such as
   `CCBOT_BIND_DEFAULT_ROOT`, `CCBOT_WORKSPACE_ROOT`, or `/home/tools` — select
   the project directory. Controller restore state and the controller service
   `WorkingDirectory` are never workspace selections by themselves.
4. If the directory has existing Codex identities, an identity picker appears — choose one to resume or start fresh
5. A tmux window is created, the configured runtime starts there (with resume wiring if resuming), and Telegram input starts routing only after the surface is bound

Forum-topic bindings are stored by chat-qualified surface identity
(`t:<chat_id>:<thread_id>`). Older bare `t:<thread_id>` records are treated as
legacy mirrors and are used only when the saved chat id proves the same
Telegram group, so equal numeric topic ids in different groups cannot share a
runtime binding.

Command entry paths also capture the Telegram group `chat_id` needed for later
topic delivery and title sync. Bot-addressed `@mention` is not used as a
routing warm-up in shared group surfaces.

**Explicit bind, explicit resume, and manual unbind:**

- In **private chats with topics enabled**, the first plain text message in a fresh topic may still trigger the bind flow automatically.
- In **group/supergroup topics**, ordinary text and bot-addressed `@mention` in an unbound topic stay silent.
- In **group/supergroup topics**, unbound photo, document, and sticker ingress also stays
  silent: the bot does not download the media, reply with bind guidance, or
  mutate bind state.
- In **no-topics group main chat mode**, ordinary text and bot-addressed `@mention` stay silent.
- Explicit `/bind` and `/resume` remain the valid explicit re-entry paths in shared group surfaces.
- Command handlers persist group routing metadata before binding, resuming,
  unbinding, renaming, history lookup, screenshot capture, interrupt, or usage
  actions that address the shared surface.
- After an explicit `/unbind` or a picker cancel, the topic enters `manual_bind_required`.
- In `manual_bind_required`, plain messages do not restart binding implicitly.
- Use `/bind` to choose a live window or workspace again.
- In Codex lane, `/bind <thread-name|id>` can attach external persisted replay
  without tmux. This path is read-only.
- Use `/resume <thread-name|id>` only when the configured runtime lane supports deterministic explicit resume from an unbound topic.
  - Codex: supported by exact persisted thread id or exact thread name.
  - Claude Code: degraded from an unbound topic because transcript ids do not prove the workspace path.
  - fast-agent: degraded from an unbound topic because session ids are scoped by the workspace `.fast-agent` root.

**Sending messages:**

Once a topic is bound to a live tmux window, plain text, voice, photo,
document, sticker, audio, and video messages are forwarded to the active
runtime for every allowed participant in that bound surface. Voice is
transcribed first through `CCBOT_VOICE_STT_PROVIDER` (`openai` by default,
`local_command` for host-local ASR, `auto` for local-first/cloud-fallback, or
`disabled`). Photos are downloaded under
`$CCBOT_DIR/images`; documents are downloaded under `$CCBOT_DIR/documents`; photo/document media groups and orphan attachment bursts are coalesced into one runtime input with an `Attachments:` list when the same surface/binding proof remains valid;
simple one-line Codex text with no attachment intent, no active/recoverable OMX
question, no blocked prompt, no open attachment batch, and a fresh writable
binding proof may bypass the attachment text lead-hold and synchronous ACK wait.
If the target pane is in tmux copy-mode/scrollback, ccbot first sends `Escape`
and verifies that tmux mode cleared before injecting text; if the mode remains,
it fails closed before payload delivery. The fast path sends a Telegram ingress
receipt that explicitly names the routing mode and resolved target, for example
`↗ Steer → @9 · comfy-agent-ops · /home/tools/mediagen-comfy` or
`⏭ Queue → @9 · comfy-agent-ops · /home/tools/mediagen-comfy`, followed by the
prompt preview. It then updates the receipt to confirmed, delayed
(`⏳ Delivered; waiting for Codex replay ACK`), or failed after the bounded ACK
window. After ACK, the receipt is the durable Telegram-originated user-input
bubble; replay-only/tmux-originated prompts still render ordinary `👤` echo.
audio/video originals are downloaded under `$CCBOT_DIR/media` and forwarded
artifact-first as local paths plus metadata. Static stickers are normalized to
PNG image attachments. Animated/video stickers use their Telegram thumbnail as
the runtime visual input when available, also preserving the original
animation artifact path for direct result delivery. Video stickers may get a
GIF sibling when `ffmpeg` is available; `.tgs` stickers keep the original
`.tgs` artifact without pretending it is an image/GIF. For regular videos, a
Telegram thumbnail or `ffmpeg` frame preview is attached when available; if no
preview can be produced, the video artifact path is still delivered with
`Preview unavailable`. Audio/video artifact transcription is not attempted in
the MVP; voice-message STT can use OpenAI or a host-local command and is
configured independently from audio/video artifact ingress. The default remote
Telegram Bot API download cap is
`CCBOT_MAX_TELEGRAM_DOWNLOAD_BYTES=20971520`; audio/video files above the
effective cap fail before download with a clear “too large for Telegram bot
download” warning rather than a generic artifact failure.

If photo, document, sticker, audio, or video media arrives before the topic has an
active writable runtime binding, it is ignored silently. Use `/bind` or
`/resume` first; media ingress does not open or repair bind flow by itself.

If the topic is bound to an external persisted thread without live tmux, input
injection fails closed with an explicit read-only warning and a reattach hint.

Transport note: for runtime updates that are actually dispatched to Telegram,
ccbot sends a `typing` chat action to the same topic/chat as a non-durable
activity hint. This hint is rate-limited per effective `chat_id`/`message_thread_id` delivery
surface to no more than once every three seconds and is not emitted for
suppressed internal events.

Routing note:
- Codex-bound Telegram text uses `steer` mode by default, preserving the
  existing ACK-verified submit behavior.
- `/switch` toggles the current Telegram surface between `steer` and `queue`;
  `/switch steer` and `/switch queue` set the mode explicitly.
- `/steer <prompt>` and `/queue <prompt>` are one-shot prompt sends that use the
  named semantics without needing to toggle first. Without a prompt, `/steer`
  and `/queue` set the persisted surface mode.
- `queue` mode sends through the same live tmux input plane but, for Codex,
  uses the `Tab` queued-message gesture instead of the normal Enter/C-m submit
  path; visible blocked prompts and dead input planes still fail closed.
- `steer` is a routing semantic for immediate runtime-aware control flows; it
  is not the same thing as raw terminal takeover.
- Raw terminal control in tmux remains a separate operator layer and is never modeled as an ordinary queued message.
- Text sent to a writable live tmux runtime is delivered as payload plus
  a separate runtime submit key; payload/key success is not considered
  replay-confirmed message delivery. For Codex conversational input
  (single-line and multiline), same-runtime-identity persisted JSONL turn event
  / replay evidence is the durable ACK that proves a new turn. Multiline Codex
  payloads are still bracketed-pasted before bare `Enter`; if tmux payload and
  submit-key delivery succeed but no persisted ACK appears within the bounded
  retry window, ccbot surfaces an explicit delivered-but-unconfirmed state
  instead of a hard failure or silent success.
- A Codex-bound tmux window that has fallen back to a shell prompt is read as a
  dead input plane; Telegram input fails closed instead of being pasted into
  `bash`.
- Codex `Conversation interrupted` surfaces stay writable: they are normal
  next-instruction prompts, not read-only approval prompts.
- Pending-input previews preserve queued message text literally (except explicit
  Codex checkbox marker glyph stripping), so command-like user text does not
  get normalized away.

**Returning generated files to Telegram:**

Use `ccbot send` for fast outbound delivery of generated artifacts. This is the
Telegram delivery alias and is separate from runtime/TUI input (`ccbot
runtime-input` / `ccbot inject`). For IMM on `str`, use the IMM bot state dir:

```bash
CCBOT_DIR=/data/iqdoctor/.ccbot-imm_arena_bot \
  /tools/ccbot/.venv/bin/ccbot send \
  --thread-id 3 \
  --file-path /path/thumb.png \
  --file-type photo \
  --message "thumbnail"

CCBOT_DIR=/data/iqdoctor/.ccbot-imm_arena_bot \
  /tools/ccbot/.venv/bin/ccbot send \
  --thread-id 3 \
  --file-path /path/anim.gif \
  --file-type animation \
  --message "animation"
```

`--file-type gif` is accepted as an alias for `animation`; outbound `audio`
and `video` file types are supported for generated/service artifacts. For
outbound `video`, `ccbot send` auto-probes local `--file-path` uploads with a
bounded `ffprobe` call and passes Telegram `send_video` width, height, duration,
and `supports_streaming` metadata when available. Geometry-sensitive final
previews can also pass `--video-width`, `--video-height`, `--video-duration`,
and `--thumbnail-path`; `--json` includes the request method, requested video
metadata, thumbnail path when provided, and Telegram-returned video/thumbnail
geometry so a generic
`status/message_id/url` success is not treated as final preview evidence. This
is separate from inbound Telegram audio/video ingress, which forwards local
media artifact paths into the runtime.

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
  content so execution context does not disappear under mutable status churn;
  commentary may span multiple Telegram messages when needed to preserve the
  full text
- **Final assistant responses** — The completed assistant answer lands as
  ordinary content as a fresh last message; it never replaces the visible
  commentary artifact
- **Turn ordering barrier** — Once a newer turn opens, stale pre-final,
  technical-status, and final artifacts from the older turn fail closed rather
  than surfacing below the newer turn

Technical execution classes stay out of permanent bubbles by default:

- **Reasoning / thinking** — Routed through the mutable status artifact or
  suppressed when they are placeholder-only
- **Tool lifecycle** — Summarized into the mutable status artifact
- **Command execution / local command** — Summarized into the mutable status
  artifact with compact command text rather than raw shell dumps
  Bare command previews are normalized into `sh` fences, while command output
  remains a separate output category with `text`/`json` fences. The active
  status bubble identity is persisted by surface key and window id so restarts
  can resume editing or intentionally replace the same artifact instead of
  leaving duplicates.
- **Status-polled command output** — Raw wrapper metadata such as `Chunk ID`,
  `Wall time`, process status, token counts, and the literal `Output:` marker
  is stripped before Telegram rendering; poll-only `write_stdin` checks do not
  overwrite richer visible status
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
├── transcribe.py          # Voice-to-text transcription via OpenAI/local STT providers
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

### Telegram delivery audit

CCBot writes a compact local audit of Telegram delivery attempts to
`telegram_delivery_audit.jsonl` under `CCBOT_DIR`. Each row records the send/edit
action, topic/control surface, semantic class, success flag, message id when
available, and a short hash/preview of the rendered artifact. This is used to
compare what Telegram actually showed with the Codex/tmux human surface without
storing full raw tool payloads. For outbound video delivery, audit rows may also
include sanitized requested video geometry, request method, provided thumbnail
path, and Telegram-returned video/thumbnail geometry so final-preview gates can
reject status/message-id/url-only evidence.
