# Telegram Bot Advanced Features Research

## Release Note

The current Codex adaptation only advertises the supported Telegram core lane:

- control surface -> live tmux window binding
- control surface -> external Codex persisted-thread bind (read-only replay mode)
- control surface -> live tmux or external replay binding in the master
  ontology
- directory / thread picker
- text / voice / photo / document / sticker / audio / video forwarding
- history and screenshot inspection
- a small supported Codex slash-command menu

Raw slash commands can still be typed manually and are forwarded best-effort, but the documented and registered menu surface stays narrower than the full Codex TUI command set.

## 1. Telegram Bot API Feature Overview

### 1.1 Rich Text & Formatting

| Feature | Description |
|---------|-------------|
| **MarkdownV2 parse_mode** | Bold `*`, italic `_`, underline `__`, strikethrough `~`, spoiler `\|\|`, code `` ` ``, code block `` ``` ``, links `[text](url)`. Special chars must be escaped |
| **HTML parse_mode** | `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<pre language="python">` syntax highlighting, `<blockquote>`, `<tg-spoiler>` |
| **Expandable Blockquote** | `<blockquote expandable>` in HTML / `**>` in MarkdownV2 — collapsed by default, tap to expand (Bot API 7.3+) |
| **Spoiler text** | Hidden text revealed on tap: `\|\|spoiler\|\|` in MarkdownV2, `<tg-spoiler>` in HTML |
| **Custom Emoji** | `<tg-emoji emoji-id="...">` in HTML — use premium custom emoji inline |
| **MessageEntity** | Structured entity objects: mention, hashtag, URL, code, pre, text_link, custom_emoji, blockquote, expandable_blockquote, etc. |
| **link_preview_options** | Control link previews: disable, prefer small/large media, position above/below text (Bot API 7.0+) |

### 1.2 Interactive Components

| Feature | Description |
|---------|-------------|
| **InlineKeyboardMarkup** | Inline buttons attached to messages. Button types: callback_data, url, web_app, login_url, switch_inline_query, pay, copy_text |
| **ReplyKeyboardMarkup** | Persistent keyboard at the bottom, supports resize, one_time, input_field_placeholder |
| **ReplyKeyboardRemove** | Remove a previously sent ReplyKeyboardMarkup |
| **callback_query.answer(text, show_alert)** | Toast notification or modal alert after button click. Must be called within 10 seconds |
| **copy_text button** | InlineKeyboardButton with `copy_text` field — one-tap copy to clipboard (Bot API 7.10+) |
| **WebApp (Mini App)** | Embed web pages via `WebAppInfo`. Support for keyboard button, inline button, menu button modes |

### 1.3 Media & Files

| Feature | Description |
|---------|-------------|
| **sendPhoto / sendDocument / sendAnimation** | Send images, files, GIFs with optional caption |
| **sendVideo / sendAudio / sendVoice / sendVideoNote** | Video, audio, voice messages, video notes (circles) |
| **sendMediaGroup** | Album-mode batch send (2-10 items). Supports photos, videos, documents, audio |
| **sendSticker** | Send static/animated/video stickers as outbound Telegram delivery |
| **sendPaidMedia** | Send media behind Telegram Star paywall (Bot API 7.6+) |
| **InputMediaPhoto / InputMediaDocument** | Edit media of sent messages via `editMessageMedia` |
| **sendDice** | Send animated emoji dice (🎲🎯🏀⚽🎳🎰) |

### 1.4 Conversation Management

| Feature | Description |
|---------|-------------|
| **ConversationHandler** | python-telegram-bot multi-step state machine dialog (library feature, not Bot API) |
| **send_chat_action("typing")** | "Typing..." status indicator. Auto-expires after 5 seconds, must resend for longer operations |
| **reply_parameters** | Reply to messages with optional quote text. Replaces old `reply_to_message_id` parameter (Bot API 7.0+) |
| **pin_chat_message / unpin** | Pin messages to top of chat. `unpin_all_chat_messages` to clear all |
| **forwardMessage / forwardMessages** | Forward single/multiple messages. `copyMessage` / `copyMessages` forwards without source attribution |
| **deleteMessage / deleteMessages** | Delete single or multiple messages at once |
| **message_effect_id** | Add visual effect animation to sent messages (Bot API 7.5+) |

### 1.5 Inline Mode

| Feature | Description |
|---------|-------------|
| **InlineQueryHandler** | @bot triggers search in any chat. Returns up to 50 results |
| **ChosenInlineResultHandler** | Track which result the user selected (requires /setinlinefeedback via BotFather) |
| **switch_inline_query_chosen_chat** | Button that opens inline query in a specific chat type |

### 1.6 Bot Commands & Menu

| Feature | Description |
|---------|-------------|
| **BotCommand + set_my_commands** | Register `/` command menu. Supports per-language and per-scope (all chats, group, private) |
| **MenuButton** | Custom bottom-left menu button: default, commands list, or web_app |
| **BotName / BotDescription / BotShortDescription** | Programmatically set bot name, description, and short description per language |

### 1.7 Message Editing & Lifecycle

| Feature | Description |
|---------|-------------|
| **editMessageText** | Edit text of sent messages. Supports parse_mode, inline_keyboard |
| **editMessageMedia** | Replace attached media |
| **editMessageCaption** | Modify caption |
| **editMessageReplyMarkup** | Update inline keyboard only |
| **deleteMessage / deleteMessages** | Remove messages (bot's own or in groups with admin rights) |

### 1.8 Message Streaming (Bot API 9.3+, Dec 2025)

| Feature | Description |
|---------|-------------|
| **sendMessageDraft** | Stream partial messages while generating — the message is progressively updated. Ideal for AI/LLM bots that produce long responses incrementally |

### 1.9 Forum Topics

| Feature | Description |
|---------|-------------|
| **Forum Topics** | Group superchats can enable topics. Bots can create/edit/close/reopen/delete topics |
| **Topics in Private Chats** | Bots can enable forum mode in private chats (`has_topics_enabled` on User). Messages support `message_thread_id` (Bot API 9.3+) |

### 1.10 Payments & Stars

| Feature | Description |
|---------|-------------|
| **sendInvoice** | Send payment invoices to users |
| **Telegram Stars** | Digital currency for in-app payments. `getMyStarBalance` to check balance (Bot API 9.1+) |
| **sendPaidMedia** | Content behind Star paywall (up to 25,000 Stars, Bot API 9.3+) |

### 1.11 Other Capabilities

| Feature | Description |
|---------|-------------|
| **Message Reactions** | `setMessageReaction` — set emoji/custom emoji reactions on messages (Bot API 7.2+) |
| **sendPoll** | Polls with up to 12 options (expanded from 10 in Bot API 9.1+), quiz mode with explanations |
| **Checklists** | `sendChecklist` / `editMessageChecklist` — structured task lists (Bot API 9.1+) |
| **Job Queue** | python-telegram-bot scheduled/delayed tasks (library feature, not Bot API) |
| **Webhook / getUpdates** | Two modes for receiving updates |

---

## 2. Feature Implementation Status in ccbot

### Already Implemented

| Feature | Status | Notes |
|---------|--------|-------|
| **HTML formatting** | ✅ | Messages render via `chatgpt-md-converter`; MarkdownV2 is no longer the active runtime formatter |
| **send_chat_action("typing")** | ✅ | Shown while processing user messages and during long operations |
| **sendMessageDraft transport preview** | ✅ / guarded | Optional `CCBOT_TELEGRAM_DRAFT_PREVIEW=probe|on` path for draft-eligible high-frequency transient partial frames. It is transport-only audit evidence, not a durable artifact or final-answer proof; group/topic use requires explicit surface allowlist and persisted live capability evidence |
| **Telegram ingress receipt** | ✅ | Eligible simple Codex text sends a distinct current-update receipt before replay ACK; it is edited to confirmed, delivered-but-unconfirmed, or failed and is not a runtime user echo before proof |
| **InlineKeyboardMarkup** | ✅ | Used extensively: thread picker, history pagination, directory browser, prompt snapshots, screenshot refresh |
| **callback_query.answer()** | ✅ | Instant feedback on all callback button clicks |
| **editMessageText** | ✅ | Status-to-content conversion in compact mode, plus verbose/fallback `tool_result` editing into `tool_use` messages |
| **Compact delivery policy** | ✅ | Default production surface keeps ordinary user echo, orchestration milestones, and final assistant text as durable bubbles; the latest commentary stays visible as a dedicated artifact while technical execution classes collapse into mutable status, and visibility wins when silence would make runtime state ambiguous |
| **Queued follow-up preview** | ✅ | Pending queued user messages may surface as a separate mutable artifact modeled after the Codex pending-input preview rather than being mixed into commentary or status; it closes on queue-empty, binding-stale, or explicit clear rather than on assistant-final alone |
| **OMX interactive question artifacts** | ✅ | Durable root/session `omx.question/v1` records are rendered as a separate mutable Telegram artifact with inline option buttons; a newly discovered first prompt is held while the current turn's pre-final lane is open so explanatory final/info content appears before the questionnaire; existing prompt edits remain in-place; split-pane renderers remain inside the parent bound tmux window rather than becoming bindable surfaces; button answers and allowed free-text `Other` replies update the state record only after bridging back to the bound runtime/return pane succeeds; busy bridges stay retryable, and recent renderer-exited records with a same-window return pane remain recoverable instead of flashing a terminal error |
| **Pre-final terminal surface barrier** | ✅ | `commentary`, orchestration milestones, any surfaced preview bubble, and the mutable technical status artifact may appear before `assistant_final`, but never below it for the same turn |
| **Codex-style command/tool previews** | ✅ | When command/tool/file previews are surfaced, they prefer extracted shell payloads, fenced `sh` / `json` blocks, truncation footers outside the code block body, and non-redundant outcome footers |
| **Codex-style orchestration milestones** | ✅ | Subagent spawn/wait/finished-waiting/completion is rendered as human-facing milestones instead of raw `spawn_agent` / `wait_agent` / `<subagent_notification>` payloads; each `wait_agent` invocation keeps its own waiting/finished lifecycle even when targets overlap |
| **editMessageMedia** | ✅ | Screenshot refresh replaces image in-place |
| **deleteMessage** | ✅ | Status message cleanup, interactive UI cleanup |
| **BotCommand + set_my_commands** | ✅ | Bot menu is limited to the supported Codex core lane plus a small passthrough subset |
| **sendDocument** | ✅ | Screenshots and runtime document/file attachments sent as Telegram documents |
| **Sticker ingress** | ✅ | Inbound Telegram stickers are normalized to runtime image attachments; animated/video stickers use Telegram thumbnails as visual input and preserve original animation artifacts for direct result delivery |
| **Photo/document batching** | ✅ | Inbound Telegram photo/document media groups and same-surface orphan attachment bursts are saved under `$CCBOT_DIR/images` / `$CCBOT_DIR/documents` and coalesced into one runtime input with an `Attachments:` list when binding proof still revalidates |
| **Simple text fast path** | ✅ | One-line Codex text without attachment intent, shell-command prefix, active question, blocked prompt, stale safety cache, or open attachment batch bypasses the 0.75s text lead-hold and waits for replay ACK asynchronously; a short ACK miss is shown as delivered-but-unconfirmed rather than a hard input failure |
| **Voice STT provider switch** | ✅ | Inbound Telegram voice messages resolve a writable runtime binding before download/STT, then transcribe through `CCBOT_VOICE_STT_PROVIDER`: `openai` by default, `local_command` via a host command template, `auto` for local-first/cloud-fallback, or `disabled` |
| **Audio/video ingress** | ✅ | Inbound Telegram audio/video messages are artifact-first runtime inputs: originals within the effective Telegram bot download cap are saved under `$CCBOT_DIR/media`, audio/video paths and metadata are sent to the bound runtime, video previews are best-effort, and transcription is optional future enrichment rather than an OpenAI gate |
| **ccbot send file delivery** | ✅ | Local `ccbot send --file-path --file-type photo\|animation\|audio\|video` returns generated artifacts to the Telegram surface without using runtime-input/TUI injection; outbound video sends auto-probe or accept explicit width/height/duration/thumbnail metadata and `--json` returns Telegram video/thumbnail geometry for final-preview QA |
| **Polling liveness guard** | ✅ | Telegram long polling uses explicit getUpdates pool/timeouts and a pending-update watchdog so service-alive-but-polling-dead processes exit for systemd restart instead of silently accumulating updates |
| **Backlog metrics** | ✅ | Payload-free counters distinguish Telegram delivery queue/in-flight/flood backlog from Codex replay unread/read-but-not-dispatched backlog |
| **Generated-image terminal media result** | ✅ | Textual image-generation tool output with a safely validated generated-image artifact may be delivered as one compact terminal photo bubble with caption when it substitutes for absent final assistant text; validation/read/send failure falls back to terminal saved-path text |
| **Runtime image preview artifact** | ✅ | Codex `image_generation_end` and `view_image` / `Viewed Image` replay output with paired embedded image bytes is delivered as a latest-only pre-final mutable Telegram photo bubble with sanitized caption; the first preview sends the bubble and later same-turn previews edit that media in place, and multi-image previews use the first image with a truncation audit. It is authorized replay-proven disclosure to the active bound control surface, never a local-path file read, and does not close the assistant turn |
| **ReplyKeyboardRemove** | ✅ | Used when switching away from reply keyboard |
| **Codex command forwarding** | ✅ | Raw `/command` input is forwarded to tmux; the documented menu only exposes the supported Codex subset |
| **Message rate limiting** | ✅ | 1.1s minimum interval per user to avoid flood control; Telegram `RetryAfter` keeps durable queue tasks pending for retry rather than acknowledging them as delivered; runtime typing/status probes share chat-level degraded-transport backpressure |
| **Per-user message queues** | ✅ | FIFO ordering, content/status task separation, message merging, RetryAfter retry accounting for durable delivery tasks, and enqueue-time coalescing for mutable compact lanes |
| **Status message deduplication** | ✅ | Skip edit if status text unchanged |
| **Codex terminal-control panel mirroring** | ✅ | `/goal` panels and `Conversation interrupted` notices visible only in tmux are mirrored to Telegram as scoped mutable operator-status artifacts rather than user echo or assistant-final content |

### Potential Improvements (Prioritized)

| # | Feature | Impact | Effort | Notes |
|---|---------|--------|--------|-------|
| 1 | **Draft answer artifact** | High | Medium | Stream partial assistant answers progressively. In private chats this may use `sendMessageDraft`; in forum topics the safer contract is a normal message plus `editMessageText` so topic-mode behavior does not depend on a private-only transport primitive |
| 2 | **Expandable blockquote for debug reasoning** | Medium | Low | Use `<blockquote expandable>` only in verbose/debug lanes where reasoning is intentionally exposed; the default compact surface should stay quiet |
| 3 | **reply_parameters with quote** | Medium | Low | Quote the specific user message when replying, providing clear message association |
| 4 | **copy_text button** | Medium | Low | Add "Copy" button to code block messages for one-tap clipboard copy |
| 5 | **link_preview_options** | Low | Low | Disable or minimize link previews in Codex responses to reduce visual noise |
| 6 | **message_effect_id** | Low | Low | Add subtle animation effects on completion or error messages |
| 7 | **Forum Topics in Private Chat** | Medium | High | Organize per-session conversations as topics in a single private chat instead of interleaving |
| 8 | **Checklists** | Low | Medium | Display Codex task lists as native Telegram checklists |
| 9 | **WebApp dashboard** | Medium | High | Real-time terminal view, session management UI via Mini App |
| 10 | **pinChatMessage** | Low | Low | Pin summary or active session info |

---

## 3. Codex Slash Commands

### Currently Advertised by ccbot

These commands are registered in the Telegram bot menu as the stable topic-control surface:

| Command | Bot Menu Description | Function |
|---------|---------------------|----------|
| `/bind` | Start or resume the topic bind flow | Explicitly choose a live window/workspace, or use `/bind <thread-name|id>` in Codex lane to attach external read-only replay |
| `/unbind` | Detach this topic from its live window | Leaves the tmux window running but moves the topic to `manual_bind_required` |
| `/resume <token>` | Bind this topic to a persisted runtime thread | Works only when the configured launch lane supports deterministic explicit resume from an unbound topic |
| `/rename <name>` | Rename the current tmux window and topic | Sync the live tmux label, forum topic title, and supported runtime title metadata |
| `/esc` | Interrupt the active runtime task | Sends Escape to the current tmux pane |

When the configured launch lane is Codex, ccbot also advertises the Codex core lane:

| Command | Bot Menu Description | Function |
|---------|---------------------|----------|
| `/clear` | ↗ Start a fresh Codex chat in this window | Wipes the current chat and starts fresh. ccbot also clears session association |
| `/compact` | ↗ Compact the current Codex thread | Summarize/compress context to free token budget |
| `/diff` | ↗ Show git diff | Show the current workspace diff |
| `/init` | ↗ Create AGENTS.md for Codex | Bootstrap project instructions for Codex |
| `/review` | ↗ Review current changes | Start a code review against the current workspace |
| `/status` | ↗ Show Codex session status | Display current session configuration and token usage |

`/usage` still exists as a legacy Claude helper, but it is intentionally not advertised in the Telegram menu. In Codex windows the bot now points users to `/status` instead of rewriting the command silently.

### Other Codex Commands (Raw Passthrough, Not Menu-Supported)

| Command | Parameterless | Interactive | Suitable for Telegram | Notes |
|---------|:---:|:---:|:---:|-------|
| `/new` | ✅ | No | ⚠️ Possible | Starts a fresh chat in the same live window; useful but not necessary in the Telegram menu |
| `/rename <name>` | ✅ | No | ✅ Supported | Renames the tmux window, syncs the topic title, and updates supported runtime title metadata |
| `/init` | ✅ | No | ✅ Supported in menu | Creates `AGENTS.md` for Codex projects |
| `/plan` | ✅ / args | No | ⚠️ Caution | Useful, but may create long plan output |
| `/model` | ✅ | Yes | ✅ Optional lane only | Requires positively identified model picker prompt and remote prompt controls |
| `/approvals` | ✅ | Yes | ✅ Optional lane only | Requires positively identified approvals popup and remote prompt controls |
| `/permissions` | ✅ | Yes | ✅ Optional lane only | Same as `/approvals`; TUI prompt driven |
| `/logout` | ✅ | No | ❌ No | Destructive; should not be advertised in Telegram |
| `/exit` | ✅ | No | ✅ Supported in menu | Terminates the live Codex process |

### Surface Policy

- Registered bot commands should describe only the stable topic-control surface and the configured runtime lane.
- Prompt-driven commands stay out of the advertised menu unless the prompt parser can positively identify and drive the resulting TUI state.
- Raw passthrough remains available for expert use, but undocumented commands are best-effort rather than release-contract behavior; `/quit` is runtime-rejected in favor of `/exit`.
- In compact delivery, the visible pre-final surface is deliberately narrow:
  - latest commentary artifact
  - orchestration milestone bubbles
  - mutable Codex plan-update artifact
  - any future surfaced preview bubble explicitly promoted by product policy
- Once the final assistant answer lands, that whole pre-final visible surface is
  closed until the next user turn, and the mutable technical status artifact
  are both closed with it. In other words, the mutable technical status artifact
  are both closed until the next user turn once the terminal assistant bubble lands.
- Exact invariant: `mutable technical status artifact are both closed` once the
  terminal assistant bubble lands for that turn.

### Compact Telegram Delivery Contract

- Durable bubbles in compact mode are intentionally narrow:
  - user echo
  - orchestration milestones
  - final assistant text
- Latest commentary remains visible as a dedicated artifact, but it is not a
  durable ordinary content bubble.
- Codex `update_plan` calls render as a separate mutable plan artifact; new plan
  events edit that artifact and commentary/status/tool output must not replace it.
- Commentary is not clipped by the internal status-helper limit; if it exceeds
  one Telegram message it may span multiple Telegram messages while remaining
  one logical commentary artifact.
- Ordinary user echo stays visible in compact mode; hidden internal payloads
  stay suppressed only when they match explicit tagged/internal shapes.
- Warning artifacts remain durable and visible; repeated identical warning text
  on one control surface deduplicates into one bubble with a `×N` counter when `N > 2`.
- Usage-limit / quota-exhaustion notices are warning artifacts too; they are
  not ephemeral technical status and not assistant-final content.
- Queued follow-up messages may remain visible as a separate pending-input
  artifact. They preview future input and are therefore not part of the
  current turn's pre-final visible artifact class.
- Technical execution classes stay out of permanent bubbles by default:
  - reasoning / thinking
  - tool lifecycle
  - command execution
  - file-change churn
- When compact/verbose lanes surface command or tool previews, they follow the
  Codex-style preview contract:
  - code block body contains only preview lines
  - truncation footer lives outside the fenced block
  - outcome footer is separate and should not redundantly say
    `completed · output 1 line(s)` when the preview already conveys the result
  - shell commands are one mutable command artifact: bare command previews are
    `sh` fenced, and `exec_command` output edits the command bubble while
    dropping Codex/developer transport metadata before
    showing the real output preview
  - Codex parsed read/list/search commands can surface as `• Explored` so
    operators see which files/searches were inspected without raw shell noise

### Topic Control Policy

This section is a derived summary of
[`ontology/topic-control.md`](/home/tools/ccbot/ontology/topic-control.md).
The ontology files remain the master source for these nouns:

- `Telegram control surface -> binding -> tmux window -> runtime process -> runtime conversation identity -> replay evidence`
- `surface_policy` and `binding_state` remain separate persisted axes.
- `topic_policy` remains the legacy topic-shaped compatibility view.
- Full persisted control-surface identity is `(user_id, surface_key)`; a
  `surface_key` alone is a local product key. Code paths derive topic/chat keys
  through `ControlSurfaceIdentity` so binding lookup, title lookup, and outbound
  replay routing stay aligned.
- In shared group topics, allowed users are peers for the same chat/topic
  binding. Identical numeric `thread_id` values in different groups are different control surfaces and must not share a binding.
- A no-topics group chat may expose one shared main-chat mode described by the
  ontology as `thread_id is None`; this is not a claim that `chat == topic`.

Named-topic behavior:

- In **private chats with topics enabled**, a fresh topic may still start with implicit bind from the first plain message.
- In **group/supergroup topics**, ordinary messages and bot-addressed `@mention` messages in an unbound topic must stay silent; they do not open bind flow.
- In **group/supergroup topics**, unbound photo, document, sticker, audio, and video messages must also
  stay silent; they do not download media, reply with bind guidance, call
  runtime input, or mutate bind-flow state.
- In **group/supergroup topics**, explicit `/bind` and explicit `/resume` remain valid explicit entry paths.
- Explicit command entry paths store Telegram group routing metadata, including
  the group `chat_id`, so later topic title sync and delivery use the group
  transport coordinates rather than a user chat id.
- After explicit `/unbind` or picker cancel, the topic moves to `manual_bind_required`.
- In `manual_bind_required`, plain messages do not re-trigger bind implicitly.
- In Codex lane, `/bind <thread-name|id>` may attach an external persisted
  thread without tmux. This binding is replay-delivery first and marked
  read-only.
- In external read-only bind mode, Telegram input must fail closed with a
  read-only warning and a hint to reattach writable live control via `/bind`
  or `/resume`.

### queue vs steer

- Telegram text enters the equal message layer in `queue` mode by default.
- `steer` is a routing semantic for runtime-aware control actions, not a claim that tmux keystrokes are ordinary chat messages.
- Raw terminal control remains a separate operator layer. A human typing directly in tmux is not modeled as an equal queued message channel.
- Telegram text delivered to a writable live tmux runtime uses a payload
  followed by a separate submit key. Codex conversational input, whether
  single-line or multiline, is reported successful only after same-identity
  replay evidence proves a new turn; submit-key success alone can leave the
  payload in the composer. Multiline Codex payloads remain bracketed-paste
  plus bare `Enter`, but the ACK invariant is no longer multiline-only.

### Compact Bubble Semantics

- `assistant_final` is the terminal turn artifact.
- `assistant_final` is always delivered as a fresh Telegram message sequence
  and never by replacing the visible commentary artifact.
- `commentary`, orchestration milestones, `plan_update`, and any future surfaced
  preview bubble belong to the broader `pre-final visible artifact` class.
- Once the terminal assistant bubble lands, no later member of that class may
  appear below it for the same turn.
- The same turn boundary also closes the mutable technical status artifact.
- Post-final commentary or orchestration milestones are dropped once the
  pre-final lane is closed; they do not reopen that lane retroactively.
- If a visible multipart send has already started when the boundary closes, the
  remaining parts fail closed rather than leaking below the final answer.

### Runtime-specific `/resume` notes

- Codex: explicit `/resume <thread-name|id>` is supported by exact persisted identity resolution and launches `codex resume <resolved-thread-id>` in tmux.
- Claude Code: explicit `/resume` from an unbound topic is degraded because the persisted transcript id does not prove a reversible workspace path.
- fast-agent: explicit `/resume` from an unbound topic is degraded because persisted sessions are scoped by the workspace `.fast-agent` root.

---

## 4. Telegram Bot API Version Reference

| Version | Date | Key Features for Bots |
|---------|------|----------------------|
| 7.0 | Dec 2023 | `reply_parameters`, `link_preview_options`, reactions |
| 7.2 | Mar 2024 | `setMessageReaction`, business connections |
| 7.3 | May 2024 | Expandable blockquotes |
| 7.5 | Jun 2024 | Message effects, paid media |
| 7.10 | Sep 2024 | `copy_text` button |
| 8.0 | Nov 2024 | Gifts, verified accounts |
| 9.0 | Mar 2025 | Business branding, Star transactions |
| 9.1 | Jul 2025 | Checklists, 12-option polls, `getMyStarBalance` |
| 9.2 | Aug 2025 | Suggested posts, direct messages in channels |
| 9.3 | Dec 2025 | **`sendMessageDraft` (streaming)**, topics in private chats, gift upgrades |

Sources: [Bot API Changelog](https://core.telegram.org/bots/api-changelog), [Bot API Documentation](https://core.telegram.org/bots/api)
