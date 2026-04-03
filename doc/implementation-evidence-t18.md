# T18 Evidence Note

Scope: capture concrete implementation evidence for the multi-runtime rewrite.
This note is source-backed and is intended to remove undocumented assumptions
before the next tasks start.

## 1. Current bot: progress/result delivery gaps and regressions

The current Telegram delivery path still relies on a mixed model:
status lines are polled from terminal surfaces, then converted into chat content,
while tool output is edited in place only when the bot has a recorded
`tool_use_id`.

Concrete evidence:
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1915-L1989) routes
  monitor events into `enqueue_status_update()` and `enqueue_content_message()`.
- [src/ccbot/handlers/message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py#L214-L247)
  drops status work while the queue is busy and processes content after queue
  drain.
- [src/ccbot/handlers/message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py#L310-L389)
  edits tool results only if a matching `tool_use_id` message was tracked, then
  converts the status message into the first content part.
- [src/ccbot/handlers/message_queue.py](/home/tools/ccbot/src/ccbot/handlers/message_queue.py#L449-L516)
  keeps status updates as a separate mutable message keyed by
  `(user_id, thread_id)`.

Observed consequence:
- live progress can be delayed or suppressed when the queue is not empty;
- the first content part can overwrite the status message;
- tool results can fall back to a new message if the original tool_use message
  was not recorded or could not be edited.

The bot also uses terminal capture as a gate before sending text:
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1054-L1088)
  cancels any bash capture, captures the tmux pane, classifies blocked prompts,
  and aborts the send path if a prompt surface is visible.

This is the current regression surface for delivery: Telegram output depends on
queue state and terminal visibility, not just on semantic runtime events.

## 2. Current bot: implicit bind after `/unbind` and cancel flows

The current `/unbind` flow clears the binding, clears topic state, and tells the
user to send a message to bind again:
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L329-L356)

When an unbound topic receives a new message, the bot does not stay inert. It
immediately enters the bind flow:
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L984-L1034)
  shows the window picker if unbound windows exist, otherwise shows the
  directory browser and stores `_pending_thread_id` / `_pending_thread_text`.
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1189-L1235)
  binds the new thread/window and forwards pending text automatically after a
  selection is made.
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1599-L1637)
  repeats the same automatic bind-and-forward pattern for existing windows.

The cancel callbacks only clear pending UI state:
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1478-L1482)
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1557-L1561)
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L1678-L1682)

Implication:
- `/unbind` is not a hard stop state;
- the next message re-enters implicit bind behavior;
- cancel removes the picker/browse state but does not establish a persistent
  "unbound, do not auto-bind" sentinel.

## 3. Upstream Claude baseline: message, status, and tool-update behavior

Upstream Claude still provides the reference shape for the legacy lane:
- [upstream message_queue.py](/home/tools/ccbot-upstream/src/ccbot/handlers/message_queue.py#L306-L385)
  edits `tool_result` into the previously sent `tool_use` message and converts
  the status message into the first content part.
- [upstream message_queue.py](/home/tools/ccbot-upstream/src/ccbot/handlers/message_queue.py#L449-L516)
  keeps a mutable status message keyed by topic and edits it in place.
- [upstream session_monitor.py](/home/tools/ccbot-upstream/src/ccbot/session_monitor.py#L1-L12)
  reads Claude JSONL session files with byte-offset tracking.
- [upstream transcript_parser.py](/home/tools/ccbot-upstream/src/ccbot/transcript_parser.py#L1-L11)
  documents the Claude JSONL message model and tool pairing contract.
- [upstream transcript_parser.py](/home/tools/ccbot-upstream/src/ccbot/transcript_parser.py#L504-L714)
  parses `tool_use` and `tool_result` blocks, including error and interruption
  handling.

This is the baseline we need to preserve for Claude progress/result UX.

## 4. Codex surfaces: resume, naming, and event delivery

Codex exposes persisted thread identity and resumable thread state:
- [sdk/typescript/README.md](/home/tools/codex/sdk/typescript/README.md#L98-L105)
  says threads live in `~/.codex/sessions` and can be reconstructed with
  `resumeThread()`.
- [codex-rs/mcp-server/src/codex_tool_runner.rs](/home/tools/codex/codex-rs/mcp-server/src/codex_tool_runner.rs#L33-L46)
  includes `threadId` in structured tool-call results.
- [codex-rs/mcp-server/src/outgoing_message.rs](/home/tools/codex/codex-rs/mcp-server/src/outgoing_message.rs#L203-L215)
  includes `threadId` in notification meta for multiplexed sessions.
- [codex-rs/mcp-server/src/outgoing_message.rs](/home/tools/codex/codex-rs/mcp-server/src/outgoing_message.rs#L293-L306)
  shows `SessionConfiguredEvent` carrying `session_id` and `thread_name`.
- [codex-rs/tui/src/resume_picker.rs](/home/tools/codex/codex-rs/tui/src/resume_picker.rs#L78-L90)
  exposes the "resume" and "fork" picker actions.

Operational support tooling also exists:
- [codex-tools/README.md](/home/tools/codex-tools/README.md#L3-L16)
  lists live session inspection, title/id search, and raw event-stream fetch.
- [codex-tools/skills/codex-session-scout/SKILL.md](/home/tools/codex-tools/skills/codex-session-scout/SKILL.md#L8-L16)
  says the tool is for live thread activity and raw event inspection.
- [codex-tools/skills/codex-session-scout/scripts/codex-session-scout](/home/tools/codex-tools/skills/codex-session-scout/scripts/codex-session-scout#L34-L40)
  supports `live`, `status`, `id`, `title`, and `path` columns.
- [codex-tools/skills/codex-session-scout/scripts/codex-session-scout](/home/tools/codex-tools/skills/codex-session-scout/scripts/codex-session-scout#L171-L189)
  reads thread names from `session_index.jsonl`.

This gives the Codex capability surface needed for runtime-aware commands and
degraded-mode handling.

## 5. fast-agent surfaces: identity, live stream, replay, and ACP

fast-agent exposes both persisted identity and ACP live delivery:
- [src/fast_agent/mcp/mcp_agent_client_session.py](/home/tools/fast-agent/src/fast_agent/mcp/mcp_agent_client_session.py#L290-L309)
  exposes `experimental_session_id` and `experimental_session_title`.
- [src/fast_agent/mcp/mcp_agent_client_session.py](/home/tools/fast-agent/src/fast_agent/mcp/mcp_agent_client_session.py#L348-L382)
  deletes sessions by `session_id` and clears the local cookie when the active
  session is deleted.
- [src/fast_agent/mcp/prompt_serialization.py](/home/tools/fast-agent/src/fast_agent/mcp/prompt_serialization.py#L103-L107)
  preserves `tool_calls`, `tool_results`, `channels`, and `stop_reason`.
- [src/fast_agent/mcp/prompt_serialization.py](/home/tools/fast-agent/src/fast_agent/mcp/prompt_serialization.py#L179-L200)
  persists and reloads the enhanced JSON replay format.
- [docs-internal/ACP_TOOL_CALLS.md](/home/tools/fast-agent/docs-internal/ACP_TOOL_CALLS.md#L13-L45)
  documents `tool_call`, `tool_call_update`, and permission handling in ACP
  mode.
- [src/fast_agent/acp/server/agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L1317-L1323)
  emits status-line `session_update` traffic.
- [src/fast_agent/acp/server/agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L2322-L2485)
  streams reasoning/message chunks and final updates through
  `session_update`.
- [src/fast_agent/acp/server/agent_acp_server.py](/home/tools/fast-agent/src/fast_agent/acp/server/agent_acp_server.py#L2577-L2638)
  handles `cancel` and runs the ACP server over stdio.

Implication:
- fast-agent already has a live ACP semantic surface and a replay-oriented JSON
  persistence surface, but the stdio transport is still the host boundary.

## 6. Unsupported or risky operations that must degrade

The current codebase already marks several operations as runtime-specific or
read-only:
- [src/ccbot/bot.py](/home/tools/ccbot/src/ccbot/bot.py#L389-L405)
  makes `/usage` Claude-only and explicitly tells Codex windows to use `/status`.
- [src/ccbot/terminal_parser.py](/home/tools/ccbot/src/ccbot/terminal_parser.py#L264-L286)
  classifies visible prompts and interactive surfaces as `blocked_prompt`.
- [src/ccbot/session.py](/home/tools/ccbot/src/ccbot/session.py#L968-L972)
  rejects sends while a blocked prompt is visible.
- [src/ccbot/handlers/interactive_ui.py](/home/tools/ccbot/src/ccbot/handlers/interactive_ui.py#L43-L64)
  marks several prompt types as core-lane restrictions and notes that remote
  controls are disabled for those prompts.
- [docs-internal/ACP_TOOL_CALLS.md](/home/tools/fast-agent/docs-internal/ACP_TOOL_CALLS.md#L38-L45)
  says permission checking exists, but is not integrated into the default flow.

These are the operations that should enter degraded mode rather than assume
full control-plane availability.

## 7. Implementation consequence

The next tasks must assume:
- Telegram message delivery is a semantic delivery problem, not a tmux scrape
  problem;
- `/unbind` needs an explicit persistent unbound state if we want to stop
  implicit rebinding;
- Claude, Codex, and fast-agent all have different identity surfaces, but the
  same general requirement: a live semantic stream plus a replay evidence path;
- blocked prompts, capability-specific commands, and missing permission support
  need explicit degraded-mode handling.
