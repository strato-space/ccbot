"""Telegram bot handlers — the main UI layer of CCBot.

Registers all command/callback/message handlers and manages the bot lifecycle.
Each Telegram topic maps 1:1 to a tmux window running a live agent process.

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /bind, /kill,
    /unbind, plus forwarding supported raw /commands to Codex via tmux.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - Topic-based routing: each named topic binds to one tmux window.
    Unbound topics trigger the directory browser to create or resume a thread.
  - Photo handling: photos sent by user are downloaded and forwarded
    to Codex as file paths (photo_handler).
  - Voice handling: voice messages are transcribed via OpenAI API and
    forwarded as text (voice_handler).
  - Automatic cleanup: closing a topic kills the associated window
    (topic_closed_handler). Unsupported content (stickers, etc.)
    is rejected with a warning (unsupported_content_handler).
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Handler modules (in handlers/):
  - callback_data: Callback data constants
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers
  - history: Message history pagination
  - directory_browser: Directory browser UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import io
import logging
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_THREAD_CANCEL,
    CB_THREAD_NEW,
    CB_THREAD_SELECT,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
    split_bind_flow_token,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    THREADS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_THREAD,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_thread_picker,
    build_window_picker,
    clear_browse_state,
    clear_thread_picker_state,
    clear_window_picker_state,
)
from .handlers.cleanup import clear_topic_state
from .handlers.history import send_history
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    get_interactive_window,
    handle_interactive_ui,
    set_interactive_mode,
)
from .handlers.message_queue import (
    clear_status_msg_info,
    current_turn_generation,
    enqueue_commentary_update,
    enqueue_content_message,
    enqueue_status_update,
    get_message_queue,
    open_new_turn_generation,
    shutdown_workers,
)
from .launcher_registration import infer_runtime_kind_from_command
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    safe_send,
    send_with_fallback,
)
from .markdown_v2 import convert_markdown
from .handlers.response_builder import build_response_parts, build_status_text
from .handlers.status_polling import status_poll_loop
from .runtime_types import (
    USER_ECHO_SEMANTIC_KIND,
    runtime_capability_registry,
)
from .screenshot import text_to_image
from .state_schema import (
    BINDING_STATE_BIND_FLOW,
    BINDING_STATE_BOUND,
    BINDING_STATE_NONE,
    TOPIC_POLICY_MANUAL_BIND_REQUIRED,
)
from .session import BLOCKED_PROMPT_SEND_MESSAGE, session_manager
from .session_monitor import NewMessage, SessionMonitor
from .telegram_delivery_policy import apply_telegram_delivery_policy
from .telegram_delivery_policy import is_non_turn_user_notification
from .terminal_parser import classify_input_surface, extract_bash_output
from .tmux_manager import tmux_manager
from .transcribe import close_client as close_transcribe_client
from .transcribe import transcribe_voice
from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

# Codex passthrough commands intentionally advertised in the Telegram menu.
# Keep this list limited to the supported core lane; other raw slash commands
# may still be typed manually and are forwarded best-effort.
CODEX_MENU_COMMANDS: dict[str, str] = {
    "clear": "↗ Start a fresh Codex chat in this window",
    "compact": "↗ Compact the current Codex thread",
    "diff": "↗ Show git diff in the current workspace",
    "init": "↗ Create AGENTS.md for Codex",
    "review": "↗ Review current changes",
    "status": "↗ Show Codex session status",
}

CLAUDE_ONLY_COMMAND_HINTS: dict[str, str] = {
    "cost": "⚠️ `/cost` is Claude-only. Use `/status` in Codex windows.",
    "help": "⚠️ `/help` is Claude-only. Use `/status` or `/init` in Codex windows.",
    "memory": (
        "⚠️ `/memory` is Claude-only. Use `/init` or edit `AGENTS.md` directly in Codex projects."
    ),
}

UNBOUND_TOPIC_MESSAGE = "❌ No live tmux window is bound to this topic."
BIND_FLOW_ACTIVE_MESSAGE = (
    "⚠️ This topic is already in a bind flow. Use the picker or /unbind to cancel it."
)


@dataclass(frozen=True)
class _ResumeCommandTarget:
    runtime_kind: str
    thread_id: str
    summary: str
    cwd: str


def _default_launch_runtime_kind() -> str:
    """Return the configured runtime lane for new explicit launches."""
    return infer_runtime_kind_from_command(config.claude_command)


def _build_resume_usage(runtime_kind: str) -> str:
    if runtime_kind == "codex":
        return "❌ Usage: /resume <thread-name|id>"
    if runtime_kind == "claude":
        return "❌ Usage: /resume <session-id>"
    return "❌ Usage: /resume <session-id>"


def _build_manual_bind_required_message(runtime_kind: str) -> str:
    if runtime_kind == "codex":
        return (
            "❌ This topic is manually unbound. Use /bind to choose a window, "
            "or /resume <thread-name|id> to bind a persisted Codex thread explicitly."
        )
    if runtime_kind == "claude":
        return (
            "❌ This topic is manually unbound. Use /bind to choose a workspace. "
            "Claude explicit /resume from an unbound topic is degraded because the "
            "persisted transcript id does not prove the workspace path."
        )
    if runtime_kind == "fast-agent":
        return (
            "❌ This topic is manually unbound. Use /bind to choose a workspace. "
            "fast-agent explicit /resume from an unbound topic is degraded because "
            "session ids are scoped by the workspace `.fast-agent` root."
        )
    return "❌ This topic is manually unbound. Use /bind to choose a window."


def _build_resume_degraded_message(runtime_kind: str) -> str:
    if runtime_kind == "claude":
        return (
            "⚠️ Claude explicit `/resume` is not available from an unbound topic. "
            "Claude transcript ids do not carry a reversible workspace path, so "
            "ccbot cannot launch the correct tmux window safely. Use /bind to "
            "choose the workspace first."
        )
    if runtime_kind == "fast-agent":
        return (
            "⚠️ fast-agent explicit `/resume` is not available from an unbound topic. "
            "Persisted sessions are scoped by the workspace `.fast-agent` root, so "
            "ccbot must see the workspace first. Use /bind to choose the workspace."
        )
    return "⚠️ Explicit /resume is not available for the configured runtime lane."


def _build_start_resume_note(runtime_kind: str) -> str:
    if runtime_kind == "codex":
        return (
            "In the current Codex lane, explicit `/resume <thread-name|id>` from an "
            "unbound topic is supported when the persisted identity resolves exactly."
        )
    return _build_resume_degraded_message(runtime_kind)


def _build_unbound_input_message(user_id: int, thread_id: int | None) -> str:
    """Explain how to re-enter the bind flow for unbound topics."""
    runtime_kind = _default_launch_runtime_kind()
    if (
        thread_id is not None
        and session_manager.get_topic_policy(user_id, thread_id)
        == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    ):
        return _build_manual_bind_required_message(runtime_kind)
    return (
        f"{UNBOUND_TOPIC_MESSAGE} "
        "Send a plain text message to start the bind flow, or use /bind."
    )


def _clear_same_thread_picker_state(
    user_data: dict | None,
    thread_id: int | None,
) -> None:
    """Clear bind-flow UI state for the active topic before an explicit action."""
    if user_data is None or thread_id is None:
        return
    if user_data.get("_pending_thread_id") != thread_id:
        return
    clear_window_picker_state(user_data)
    clear_browse_state(user_data)
    clear_thread_picker_state(user_data)
    user_data.pop("_pending_thread_id", None)
    user_data.pop("_pending_thread_text", None)
    user_data.pop("_selected_path", None)


async def _resolve_resume_command_target(
    runtime_kind: str,
    token: str,
) -> tuple[_ResumeCommandTarget | None, str | None]:
    """Resolve an explicit `/resume` token for the configured runtime lane."""
    normalized_runtime = runtime_kind or _default_launch_runtime_kind()

    if normalized_runtime == "codex":
        if session_manager.codex_thread_catalog is None:
            return None, "❌ Codex thread catalog is unavailable."
        session_manager.codex_thread_catalog.refresh()
        resolution = await asyncio.to_thread(
            session_manager.codex_thread_catalog.resolve_resume_target,
            token,
        )
        if resolution.status == "selected" and resolution.selected is not None:
            candidate = resolution.selected
            return (
                _ResumeCommandTarget(
                    runtime_kind="codex",
                    thread_id=candidate.thread_id,
                    summary=candidate.summary,
                    cwd=candidate.cwd,
                ),
                None,
            )
        if resolution.status == "ambiguous":
            return (
                None,
                "❌ Codex resume target is ambiguous. Use the exact persisted thread id.",
            )
        return (
            None,
            f"❌ No Codex thread matched '{token}'. Use the exact thread id or exact thread name.",
        )

    return None, _build_resume_degraded_message(normalized_runtime)


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


def _get_window_runtime_kind(window_id: str) -> str | None:
    """Return the persisted runtime kind for a bound window.

    This intentionally fails closed for windows that have no persisted runtime
    metadata yet. ``SessionManager.get_window_state()`` auto-creates a default
    descriptor, which would incorrectly classify an unknown Codex window as
    legacy Claude if we called it unconditionally here.
    """
    window_states = getattr(session_manager, "window_states", None)
    if isinstance(window_states, dict):
        state = window_states.get(window_id)
        if state is None:
            return None
        runtime_kind = getattr(state, "runtime_kind", None)
    else:
        state = session_manager.get_window_state(window_id)
        runtime_kind = getattr(state, "runtime_kind", None)

    if isinstance(runtime_kind, str) and runtime_kind:
        return runtime_kind
    return None


def _infer_known_runtime_kind_from_pane_command(command: str) -> str | None:
    """Infer a runtime from an active pane command without default fallback."""
    normalized = (command or "").strip()
    if not normalized:
        return None
    try:
        tokens = shlex.split(normalized)
    except ValueError:
        tokens = normalized.split()
    if not tokens:
        return None

    executable = Path(tokens[0]).name.casefold()
    for runtime_kind, capability in runtime_capability_registry.items():
        aliases = {
            capability.launch_command_name.casefold(),
            *(alias.casefold() for alias in capability.command_aliases),
        }
        if executable in aliases:
            return runtime_kind
    return None


def _get_registered_window_runtime_kind(window_id: str) -> str | None:
    """Return a trusted persisted runtime kind for an already-registered window."""
    window_states = getattr(session_manager, "window_states", None)
    if not isinstance(window_states, dict):
        return _get_window_runtime_kind(window_id)

    state = window_states.get(window_id)
    if state is None:
        return None

    # Ignore placeholder descriptors that only contain the legacy default runtime.
    if not getattr(state, "cwd", "") and not getattr(state, "thread_id", ""):
        return None

    runtime_kind = getattr(state, "runtime_kind", None)
    if isinstance(runtime_kind, str) and runtime_kind:
        return runtime_kind
    return None


def _resolve_existing_window_runtime_kind(window_id: str, pane_command: str) -> str:
    """Resolve the runtime kind for a live tmux window selected via /bind."""
    return (
        _infer_known_runtime_kind_from_pane_command(pane_command)
        or _get_registered_window_runtime_kind(window_id)
        or _default_launch_runtime_kind()
    )


def build_bot_commands() -> list[BotCommand]:
    """Build the advertised Telegram command surface.

    The stable bot-level surface is always present. Codex passthrough commands
    are only advertised when the configured default launch lane is Codex.
    """
    default_runtime_kind = _default_launch_runtime_kind()
    commands = [
        BotCommand("start", "Show the tmux topic workflow"),
        BotCommand("history", "Show thread history for this topic"),
        BotCommand("screenshot", "Capture the active tmux pane"),
        BotCommand("esc", "Interrupt the active runtime task"),
        BotCommand("bind", "Start or resume the topic bind flow"),
        BotCommand("unbind", "Detach this topic from its live window"),
        BotCommand("resume", "Resume a persisted thread or session in this topic"),
        BotCommand("rename", "Rename the current tmux window and topic"),
    ]
    if default_runtime_kind == "codex":
        for cmd_name, desc in CODEX_MENU_COMMANDS.items():
            commands.append(BotCommand(cmd_name, desc))
    return commands


async def _surface_blocked_prompt_state(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    *,
    reply_message: object | None = None,
    chat_id: int | None = None,
) -> None:
    """Show the current blocked prompt as a read-only snapshot."""
    await handle_interactive_ui(bot, user_id, window_id, thread_id)
    notice = (
        "⚠️ Terminal prompt is waiting for a decision. "
        "Remote input is disabled for this state."
    )
    if reply_message is not None:
        await safe_reply(reply_message, notice)
        return
    if chat_id is not None:
        await safe_send(
            bot,
            chat_id,
            notice,
            message_thread_id=thread_id,
        )


async def _sync_topic_title(
    bot: Bot,
    user_id: int,
    thread_id: int,
    topic_title: str,
) -> bool:
    """Best-effort sync of a forum topic title to the new window name."""
    resolved_chat = session_manager.resolve_chat_id(user_id, thread_id)
    try:
        await bot.edit_forum_topic(
            chat_id=resolved_chat,
            message_thread_id=thread_id,
            name=topic_title,
        )
        return True
    except Exception as e:
        logger.debug("Failed to sync forum topic title: %s", e)
        return False


async def _start_bind_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    thread_id: int,
    *,
    explicit: bool,
    pending_text: str | None = None,
) -> None:
    """Open the bind chooser and mark the topic as being in a bind flow."""
    if update.message is None:
        return

    if explicit:
        session_manager.allow_implicit_bind(user.id, thread_id)
    session_manager.start_topic_bind_flow(user.id, thread_id)
    bind_flow_version, bind_flow_nonce = session_manager.get_topic_bind_flow_credentials(
        user.id, thread_id
    )

    all_windows = await tmux_manager.list_windows()
    bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
    unbound = [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_ids
    ]

    logger.debug(
        "Bind flow start (explicit=%s): all=%s, bound=%s, unbound=%s",
        explicit,
        [w.window_name for w in all_windows],
        bound_ids,
        [name for _, name, _ in unbound],
    )

    if unbound:
        msg_text, keyboard, win_ids = build_window_picker(
            unbound,
            bind_flow_version=bind_flow_version,
            bind_flow_nonce=bind_flow_nonce,
        )
        if context.user_data is not None:
            clear_thread_picker_state(context.user_data)
            clear_window_picker_state(context.user_data)
            clear_browse_state(context.user_data)
            context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
            context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
            context.user_data["_pending_thread_id"] = thread_id
            if pending_text is not None:
                context.user_data["_pending_thread_text"] = pending_text
            else:
                context.user_data.pop("_pending_thread_text", None)
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    start_path = str(Path.cwd())
    msg_text, keyboard, subdirs = build_directory_browser(
        start_path,
        bind_flow_version=bind_flow_version,
        bind_flow_nonce=bind_flow_nonce,
    )
    if context.user_data is not None:
        clear_thread_picker_state(context.user_data)
        clear_window_picker_state(context.user_data)
        clear_browse_state(context.user_data)
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
        context.user_data["_pending_thread_id"] = thread_id
        if pending_text is not None:
            context.user_data["_pending_thread_text"] = pending_text
        else:
            context.user_data.pop("_pending_thread_text", None)
    await safe_reply(update.message, msg_text, reply_markup=keyboard)


def _get_current_bind_flow_credentials(
    user_id: int,
    thread_id: int,
) -> tuple[int, str]:
    """Return the active bind-flow credentials for a topic."""
    return session_manager.get_topic_bind_flow_credentials(user_id, thread_id)


def _resolve_bind_flow_callback_thread_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:
    """Recover the topic id for bind-flow callbacks after unrelated traffic.

    The visible picker message still belongs to the original topic even if
    `_pending_thread_id` was cleared by text in another topic. Use the callback
    message context as a safe fallback before treating the callback as stale.
    """
    if context.user_data:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid is not None:
            return pending_tid
    return _get_thread_id(update)


async def _validate_bind_flow_callback(
    query: object,
    *,
    user_id: int,
    thread_id: int | None,
    version: int,
    nonce: str,
) -> bool:
    """Fail closed for stale bind-flow callbacks after restart/unbind/cancel."""
    if thread_id is None:
        await query.answer("Stale bind flow, use /bind again", show_alert=True)
        return False
    if not session_manager.validate_topic_bind_flow_callback(
        user_id,
        thread_id,
        version,
        nonce,
    ):
        await query.answer("Stale bind flow, use /bind again", show_alert=True)
        return False
    return True


# --- Command handlers ---


async def bind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explicitly start a bind flow for the current topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is not None:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"ℹ️ This topic is already bound to '{display}'. "
            "Use /unbind first if you want to bind a different window.",
        )
        return

    await _start_bind_flow(update, context, user, thread_id, explicit=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    clear_browse_state(context.user_data)

    runtime_kind = _default_launch_runtime_kind()
    capability = session_manager.get_runtime_capability(runtime_kind)

    if update.message:
        await safe_reply(
            update.message,
            "🤖 *tmux runtime control*\n\n"
            "Each topic controls one live tmux window.\n"
            f"This bot launches the configured runtime lane in tmux: {capability.display_name}.\n"
            "The first plain message in a fresh topic may trigger implicit bind. "
            "After /unbind or Cancel, plain messages stop rebinding until you use "
            "/bind or a supported /resume.\n"
            "Telegram text enters the equal message layer in queue mode by default. "
            "steer changes routing semantics for explicit runtime controls; raw "
            "tmux terminal control stays separate and is never treated as a queued message.\n"
            f"{_build_start_resume_note(runtime_kind)}\n"
            "Use /bind when you want to choose a workspace explicitly, and use "
            "/resume when the current runtime lane supports deterministic explicit resume.\n"
            "The menu only advertises the stable bot surface and any supported runtime core lane.",
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, UNBOUND_TOPIC_MESSAGE)
        return

    await send_history(update.message, wid)


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, UNBOUND_TOPIC_MESSAGE)
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(text, with_ansi=True)
    keyboard = _build_screenshot_keyboard(wid)
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its live Codex window without killing it."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    runtime_kind = _default_launch_runtime_kind()
    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        session_manager.require_manual_bind(user.id, thread_id)
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
        _clear_same_thread_picker_state(context.user_data, thread_id)
        await safe_reply(
            update.message,
            _build_manual_bind_required_message(runtime_kind),
        )
        return

    display = session_manager.get_display_name(wid)
    session_manager.unbind_thread(user.id, thread_id)
    session_manager.require_manual_bind(user.id, thread_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    await safe_reply(
        update.message,
        f"✅ Topic unbound from window '{display}'.\n"
        "The live tmux window is still running.\n"
        f"{_build_manual_bind_required_message(runtime_kind)}",
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bind the current topic to a persisted runtime thread when supported."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is not None:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"ℹ️ This topic is already bound to '{display}'. Use /unbind first if you want to /resume a different persisted thread.",
        )
        return

    runtime_kind = _default_launch_runtime_kind()
    raw_text = (update.message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await safe_reply(update.message, _build_resume_usage(runtime_kind))
        return

    token = " ".join(parts[1].split())
    target, error_message = await _resolve_resume_command_target(runtime_kind, token)
    if target is None:
        await safe_reply(update.message, error_message or _build_resume_degraded_message(runtime_kind))
        return

    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    _clear_same_thread_picker_state(context.user_data, thread_id)
    session_manager.allow_implicit_bind(user.id, thread_id)

    success, message, final_name, created_wid, reused_existing = await tmux_manager.create_or_reuse_window(
        target.cwd,
        start_claude=True,
        resume_session_id=target.thread_id,
        runtime_kind=target.runtime_kind,
        reuse_existing=True,
    )
    if not success or not created_wid:
        await safe_reply(update.message, f"❌ {message}")
        return

    await _register_bound_window(
        context,
        user,
        thread_id,
        window_id=created_wid,
        window_name=final_name or Path(target.cwd).name,
        selected_path=target.cwd,
        runtime_kind=target.runtime_kind,
        resume_session_id=target.thread_id,
    )

    capability = session_manager.get_runtime_capability(target.runtime_kind)
    action = "Reused" if reused_existing else "Created"
    await safe_reply(
        update.message,
        "\n".join(
            [
                f"✅ {message}",
                f"✅ {action} {capability.display_name} window for '{target.summary}'.",
                f"ℹ️ Topic is now bound to the resumed {capability.display_name} thread.",
            ]
        ),
    )


async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Rename the current bound window and sync the visible topic title."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, UNBOUND_TOPIC_MESSAGE)
        return

    raw_text = (update.message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await safe_reply(update.message, "❌ Usage: /rename <new-name>")
        return

    desired_name = " ".join(parts[1].split())
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    success, message, final_name = await tmux_manager.rename_window_with_suffixes(
        wid,
        desired_name,
    )
    if not success or not final_name:
        await safe_reply(update.message, f"❌ {message}")
        return

    session_manager.update_display_name(wid, final_name)
    topic_synced = await _sync_topic_title(context.bot, user.id, thread_id, final_name)

    runtime_kind = _get_window_runtime_kind(wid)
    capability = session_manager.get_runtime_capability(runtime_kind)
    identity_changed, identity_note = await session_manager.rename_runtime_identity_for_window(
        wid,
        final_name,
    )

    response_lines = [f"✅ {message}"]
    if topic_synced:
        response_lines.append(f"✅ Telegram topic title synced to '{final_name}'.")
    else:
        response_lines.append(
            f"⚠️ Telegram topic title could not be synced; tmux window is '{final_name}'."
        )

    if capability.rename_identity_mode == "title_only" and identity_changed:
        response_lines.append(
            f"✅ Persisted {capability.display_name} title metadata updated to '{final_name}'."
        )
        response_lines.append("ℹ Persisted conversation id stayed the same.")
    else:
        if capability.rename_identity_mode in {"unsupported", "unsupported_degraded"}:
            response_lines.append("ℹ Persisted runtime identity was not changed.")
        else:
            response_lines.append(f"ℹ {identity_note}.")

    if final_name != desired_name:
        response_lines.append(
            f"ℹ Requested name '{desired_name}' was adjusted deterministically."
        )

    await safe_reply(update.message, "\n".join(response_lines))


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt the active runtime."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, UNBOUND_TOPIC_MESSAGE)
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    success, message = await session_manager.send_special_key_to_window(
        w.window_id, "Escape"
    )
    if success:
        await safe_reply(update.message, "⎋ Sent Escape")
    else:
        await safe_reply(update.message, f"❌ {message}")


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy Claude-only usage helper; Codex windows should use `/status`."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, UNBOUND_TOPIC_MESSAGE)
        return

    if _get_window_runtime_kind(wid) != "claude":
        await safe_reply(
            update.message,
            "⚠️ `/usage` is only available for Claude windows with registered runtime metadata. Use `/status` in Codex windows.",
        )
        return

    success, message = await session_manager.send_to_window(wid, "/usage")
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                user.id,
                wid,
                thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ Failed to capture usage info: {message}")
        return
    await safe_reply(update.message, "ℹ️ Claude usage modal requested in the terminal.")


# --- Screenshot keyboard with quick control keys ---

# key_id → tmux key name
_KEYS_SEND_MAP: dict[str, str] = {
    "up": "Up",
    "dn": "Down",
    "lt": "Left",
    "rt": "Right",
    "esc": "Escape",
    "ent": "Enter",
    "spc": "Space",
    "tab": "Tab",
    "cc": "C-c",
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_KEYS_PREFIX}{key_id}:{window_id}"[:64],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"[:64],
                )
            ],
        ]
    )


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid:
        display = session_manager.get_display_name(wid)
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        session_manager.unbind_thread(user.id, thread_id)
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def topic_edited_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and internal state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        # Icon-only change, no rename needed
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        logger.debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    old_name = session_manager.get_display_name(wid)
    success, message, final_name = await tmux_manager.rename_window_with_suffixes(
        wid,
        new_name,
    )
    if not success or not final_name:
        logger.debug(
            "Topic edited rename failed (user=%d, thread=%d, window=%s): %s",
            user.id,
            thread_id,
            wid,
            message,
        )
        return

    session_manager.update_display_name(wid, final_name)
    if final_name != new_name:
        await _sync_topic_title(context.bot, user.id, thread_id, final_name)
    runtime_kind = _get_window_runtime_kind(wid)
    capability = session_manager.get_runtime_capability(runtime_kind)
    identity_changed, identity_note = await session_manager.rename_runtime_identity_for_window(
        wid,
        final_name,
    )
    if identity_changed:
        logger.info(
            "Runtime identity renamed for topic edit (runtime=%s, window=%s)",
            capability.display_name,
            wid,
        )
    else:
        logger.debug(
            "Runtime identity unchanged for topic edit (runtime=%s, window=%s): %s",
            capability.display_name,
            wid,
            identity_note,
        )
    logger.info(
        "Topic renamed: '%s' -> '%s' (window=%s, user=%d, thread=%d)",
        old_name,
        final_name,
        wid,
        user.id,
        thread_id,
    )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward slash commands to the active tmux-backed runtime."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, UNBOUND_TOPIC_MESSAGE)
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    display = session_manager.get_display_name(wid)
    runtime_kind = _get_window_runtime_kind(wid)
    command_name = cc_slash[1:].split(None, 1)[0].lower()
    if runtime_kind == "codex" and command_name in CLAUDE_ONLY_COMMAND_HINTS:
        await safe_reply(update.message, CLAUDE_ONLY_COMMAND_HINTS[command_name])
        return

    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # If /clear command was sent, clear the persisted identity binding
        # so we can detect the next runtime-provided identity after first input.
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing persisted binding for window %s after /clear", display)
            session_manager.clear_window_binding(wid)

        # Prompt-producing commands are surfaced by the status poller when the
        # runtime exposes a detectable prompt surface.
    else:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                user.id,
                wid,
                thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text, photo, and voice messages are supported. Stickers, video, and other media cannot be forwarded to Codex.",
    )


# --- Image directory for incoming photos ---
_IMAGES_DIR = ccbot_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(update.message, _build_unbound_input_message(user.id, thread_id))
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    # Save to ~/.ccbot/images/<timestamp>_<file_unique_id>.jpg
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)

    # Build the message to send to Codex
    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(image attached: {file_path})"
    else:
        text_to_send = f"(image attached: {file_path})"

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, thread_id)

    success, message = await session_manager.send_to_window(wid, text_to_send)
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                user.id,
                wid,
                thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Codex.")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    if not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ Voice transcription requires an OpenAI API key.\n"
            "Set `OPENAI_API_KEY` in your `.env` file and restart the bot.",
        )
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        await safe_reply(update.message, _build_unbound_input_message(user.id, thread_id))
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, thread_id)

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                user.id,
                wid,
                thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(update.message, f'🎤 "{text}"')


# Active bash capture tasks: (user_id, thread_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


async def _register_bound_window(
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    thread_id: int,
    *,
    window_id: str,
    window_name: str,
    selected_path: str,
    runtime_kind: str,
    resume_session_id: str | None = None,
) -> bool:
    """Register a launched window, then bind the Telegram topic to it."""
    session_manager.register_live_process(
        window_id,
        selected_path,
        window_name=window_name,
        runtime_kind=runtime_kind,
        thread_id=resume_session_id or "",
    )

    if runtime_kind == "claude":
        hook_timeout = 15.0 if resume_session_id else 5.0
        hook_ok = await session_manager.wait_for_session_map_entry(
            window_id, timeout=hook_timeout
        )
        if resume_session_id:
            ws = session_manager.get_window_state(window_id)
            if not hook_ok:
                logger.warning(
                    "Hook timed out for resume window %s, manually setting session_id=%s cwd=%s",
                    window_id,
                    resume_session_id,
                    selected_path,
                )
                ws.session_id = resume_session_id
                ws.cwd = str(selected_path)
                ws.window_name = window_name
                session_manager._save_state()
            elif ws.session_id != resume_session_id:
                logger.info(
                    "Resume override: window %s session_id %s -> %s",
                    window_id,
                    ws.session_id,
                    resume_session_id,
                )
                ws.session_id = resume_session_id
                session_manager._save_state()

    session_manager.allow_implicit_bind(user.id, thread_id)
    session_manager.bind_thread(
        user.id, thread_id, window_id, window_name=window_name
    )
    return await _sync_topic_title(context.bot, user.id, thread_id, window_name)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = update.message.text

    # Ignore text in window picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_WINDOW:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the window picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_window_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the directory browser above, or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        clear_browse_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in thread-picker mode (only for the same topic)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_SELECTING_THREAD
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the thread picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_thread_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop("_selected_path", None)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        binding_state = session_manager.get_topic_binding_state(user.id, thread_id)
        topic_policy = session_manager.get_topic_policy(user.id, thread_id)

        if binding_state == BINDING_STATE_BIND_FLOW:
            await safe_reply(update.message, BIND_FLOW_ACTIVE_MESSAGE)
            return

        if binding_state == BINDING_STATE_BOUND:
            logger.info(
                "Detected stale bound state without a live window, clearing state "
                "(user=%d, thread=%d)",
                user.id,
                thread_id,
            )
            session_manager.set_topic_binding_state(
                user.id, thread_id, BINDING_STATE_NONE
            )
            binding_state = BINDING_STATE_NONE

        if topic_policy == TOPIC_POLICY_MANUAL_BIND_REQUIRED:
            await safe_reply(
                update.message,
                _build_manual_bind_required_message(_default_launch_runtime_kind()),
            )
            return

        logger.info(
            "Implicit bind allowed: showing bind flow (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        await _start_bind_flow(
            update,
            context,
            user,
            thread_id,
            explicit=False,
            pending_text=text,
        )
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    await enqueue_status_update(context.bot, user.id, wid, None, thread_id=thread_id)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, thread_id)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    pane_text = await tmux_manager.capture_pane(w.window_id)
    surface = classify_input_surface(pane_text) if pane_text else None
    if surface and surface.kind == "blocked_prompt":
        logger.info(
            "Detected blocked prompt before sending text (user=%d, thread=%s)",
            user.id,
            thread_id,
        )
        await _surface_blocked_prompt_state(
            context.bot,
            user.id,
            wid,
            thread_id,
            reply_message=update.message,
        )
        return

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                user.id,
                wid,
                thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)


# --- Window creation helper ---


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None = None,
    *,
    runtime_kind: str | None = None,
    launch_command: str | None = None,
    reuse_existing: bool = False,
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by directory-confirm, fresh-thread, and thread-resume actions.
    """
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    resolved_runtime_kind = runtime_kind or _default_launch_runtime_kind()
    if reuse_existing:
        success, message, created_wname, created_wid, _reused_existing = (
            await tmux_manager.create_or_reuse_window(
                selected_path,
                start_claude=True,
                resume_session_id=resume_session_id,
                runtime_kind=resolved_runtime_kind,
                launch_command=launch_command,
                reuse_existing=True,
            )
        )
    else:
        success, message, created_wname, created_wid = await tmux_manager.create_window(
            selected_path,
            resume_session_id=resume_session_id,
            runtime_kind=resolved_runtime_kind,
            launch_command=launch_command,
        )
    if success:
        logger.info(
            "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s)",
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
            resume_session_id,
        )
        if pending_thread_id is None:
            session_manager.register_live_process(
                created_wid,
                selected_path,
                window_name=created_wname,
                runtime_kind=resolved_runtime_kind,
                thread_id=resume_session_id or "",
            )
            if resolved_runtime_kind == "claude":
                hook_timeout = 15.0 if resume_session_id else 5.0
                await session_manager.wait_for_session_map_entry(
                    created_wid, timeout=hook_timeout
                )
        if pending_thread_id is not None:
            topic_synced = await _register_bound_window(
                context,
                user,
                pending_thread_id,
                window_id=created_wid,
                window_name=created_wname,
                selected_path=selected_path,
                runtime_kind=resolved_runtime_kind,
                resume_session_id=resume_session_id,
            )
            resolved_chat = session_manager.resolve_chat_id(user.id, pending_thread_id)

            status = "Resumed thread" if resume_session_id else "Started fresh thread"
            response_lines = [f"✅ {message}", "", f"{status}. Send messages here."]
            if not topic_synced:
                response_lines.append(
                    f"⚠️ Telegram topic title could not be synced; tmux window is '{created_wname}'."
                )
            await safe_edit(query, "\n".join(response_lines))

            # Send pending text if any
            pending_text = (
                context.user_data.get("_pending_thread_text")
                if context.user_data
                else None
            )
            if pending_text:
                logger.debug(
                    "Forwarding pending text to window %s (len=%d)",
                    created_wname,
                    len(pending_text),
                )
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_text", None)
                    context.user_data.pop("_pending_thread_id", None)
                send_ok, send_msg = await session_manager.send_to_window(
                    created_wid,
                    pending_text,
                )
                if not send_ok:
                    logger.warning("Failed to forward pending text: %s", send_msg)
                    if send_msg == BLOCKED_PROMPT_SEND_MESSAGE:
                        await _surface_blocked_prompt_state(
                            context.bot,
                            user.id,
                            created_wid,
                            pending_thread_id,
                            chat_id=resolved_chat,
                        )
                        return
                    await safe_send(
                        context.bot,
                        resolved_chat,
                        f"❌ Failed to send pending message: {send_msg}",
                        message_thread_id=pending_thread_id,
                    )
            elif context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
    await query.answer("Created" if success else "Failed")


# --- Callback query handler ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    raw_data = query.data
    data, bind_flow_version, bind_flow_nonce = split_bind_flow_token(raw_data)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _get_thread_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, cb_thread_id, chat.id)

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window_id
                offset_str, window_id = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window_id:start:end (window_id may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_id = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await query.answer("Invalid data")
            return

        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await send_history(
                query,
                window_id,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await safe_edit(query, "Window no longer exists.")
        await query.answer("Page updated")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        # Validate: callback must come from the same topic that started browsing
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        current_version, current_nonce = _get_current_bind_flow_credentials(
            user.id, pending_tid
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            new_path_str,
            bind_flow_version=current_version,
            bind_flow_nonce=current_nonce,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        current_version, current_nonce = _get_current_bind_flow_credentials(
            user.id, pending_tid
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            parent_path,
            bind_flow_version=current_version,
            bind_flow_nonce=current_nonce,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        current_version, current_nonce = _get_current_bind_flow_credentials(
            user.id, pending_tid
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            current_path,
            pg,
            bind_flow_version=current_version,
            bind_flow_nonce=current_nonce,
        )
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        # Check if this was initiated from a thread bind flow
        pending_thread_id = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_thread_id,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return

        # Validate: confirm button must come from the same topic that started browsing
        confirm_thread_id = _get_thread_id(update)
        if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
            clear_browse_state(context.user_data)
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return

        clear_browse_state(context.user_data)

        # Check for existing persisted threads in this directory.
        threads = await session_manager.list_threads_for_directory(selected_path)
        if threads:
            # Show thread picker — store state for later.
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_THREAD
                context.user_data[THREADS_KEY] = threads
                context.user_data["_selected_path"] = selected_path
            current_version, current_nonce = _get_current_bind_flow_credentials(
                user.id, pending_thread_id
            )
            text, keyboard = build_thread_picker(
                threads,
                bind_flow_version=current_version,
                bind_flow_nonce=current_nonce,
            )
            await safe_edit(query, text, reply_markup=keyboard)
            await query.answer()
            return

        # No existing persisted threads — create a fresh window directly.
        await _create_and_bind_window(
            query, context, user, selected_path, pending_thread_id
        )

    elif data == CB_DIR_CANCEL:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        active_tid = pending_tid if pending_tid is not None else _get_thread_id(update)
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        if active_tid is not None:
            session_manager.require_manual_bind(user.id, active_tid)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Thread picker: resume an existing persisted thread.
    elif data.startswith(CB_THREAD_SELECT):
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_THREAD_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_threads = (
            context.user_data.get(THREADS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_threads):
            await query.answer("Thread not found")
            return

        thread = cached_threads[idx]
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_thread_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            resume_session_id=thread.thread_id,
        )

    elif data == CB_THREAD_NEW:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        clear_thread_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(query, context, user, selected_path, pending_tid)

    elif data == CB_THREAD_CANCEL:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        active_tid = pending_tid if pending_tid is not None else _get_thread_id(update)
        clear_thread_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_selected_path", None)
        if active_tid is not None:
            session_manager.require_manual_bind(user.id, active_tid)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await query.answer("Not in a topic", show_alert=True)
            return

        display = w.window_name
        if not w.cwd:
            await query.answer(
                f"Window '{display}' has no detectable workspace path",
                show_alert=True,
            )
            return

        clear_window_picker_state(context.user_data)
        runtime_kind = _resolve_existing_window_runtime_kind(
            selected_wid,
            w.pane_current_command,
        )
        await _register_bound_window(
            context,
            user,
            thread_id,
            window_id=selected_wid,
            window_name=display,
            selected_path=w.cwd,
            runtime_kind=runtime_kind,
        )

        await safe_edit(
            query,
            f"✅ Bound this topic to live window `{display}`",
        )

        # Forward pending text if any
        resolved_chat = session_manager.resolve_chat_id(user.id, thread_id)
        pending_text = (
            context.user_data.get("_pending_thread_text") if context.user_data else None
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_pending_thread_id", None)
        if pending_text:
            send_ok, send_msg = await session_manager.send_to_window(
                selected_wid, pending_text
            )
            if not send_ok:
                logger.warning("Failed to forward pending text: %s", send_msg)
                if send_msg == BLOCKED_PROMPT_SEND_MESSAGE:
                    await _surface_blocked_prompt_state(
                        context.bot,
                        user.id,
                        selected_wid,
                        thread_id,
                        chat_id=resolved_chat,
                    )
                    return
                await safe_send(
                    context.bot,
                    resolved_chat,
                    f"❌ Failed to send pending message: {send_msg}",
                    message_thread_id=thread_id,
                )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        start_path = str(Path.cwd())
        current_version, current_nonce = _get_current_bind_flow_credentials(
            user.id, pending_tid
        )
        msg_text, keyboard, subdirs = build_directory_browser(
            start_path,
            bind_flow_version=current_version,
            bind_flow_nonce=current_nonce,
        )
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            thread_id=pending_tid,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        active_tid = pending_tid if pending_tid is not None else _get_thread_id(update)
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        if active_tid is not None:
            session_manager.require_manual_bind(user.id, active_tid)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = _build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)

    elif data == "noop":
        await query.answer()

    # Interactive UI: Up arrow
    elif data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Up"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Down"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Left"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Right"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer()

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Escape"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await clear_interactive_msg(user.id, context.bot, thread_id)
        await query.answer("⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Enter"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Space"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        thread_id = _get_thread_id(update)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return
        success, message = await session_manager.send_special_key_to_window(
            w.window_id, "Tab"
        )
        if not success:
            await query.answer(message, show_alert=True)
            return
        await asyncio.sleep(0.5)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("⇥ Tab")

    # Interactive UI: refresh display
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        thread_id = _get_thread_id(update)
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await query.answer("🔄")

    # Screenshot quick keys: send key to tmux window
    elif data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return

        tmux_key = key_info
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        success, message = await session_manager.send_special_key_to_window(
            w.window_id, tmux_key
        )
        if success:
            await query.answer(_KEY_LABELS.get(key_id, key_id))
        else:
            await query.answer(message, show_alert=True)
            return

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = _build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # Screenshot unchanged or message too old


# --- Streaming response / notifications ---


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    msg = apply_telegram_delivery_policy(
        msg,
        mode=config.telegram_delivery_mode,
    )

    opens_new_turn = (
        msg.semantic_kind == USER_ECHO_SEMANTIC_KIND
        and not is_non_turn_user_notification(msg.text)
    )

    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session
    active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    for user_id, wid, thread_id in active_users:
        turn_generation = current_turn_generation(user_id, thread_id)
        if opens_new_turn:
            turn_generation = open_new_turn_generation(user_id, thread_id)

        if not msg.dispatch_to_telegram:
            logger.debug(
                "Skipping non-dispatched event thread=%s semantic=%s",
                msg.thread_id,
                msg.semantic_kind,
            )
            continue

        # Handle interactive tools specially - capture terminal and send UI
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Flush pending messages (e.g. plan content) before sending interactive UI
            queue = get_message_queue(user_id)
            if queue:
                await queue.join()
            # Wait briefly for Claude Code to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid, thread_id)
            if handled:
                # Update user's read offset
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue  # Don't send the normal tool_use message
            else:
                # UI not rendered — clear the early-set mode
                clear_interactive_mode(user_id, thread_id)

        # Any non-interactive message means the interaction is complete — delete the UI message
        if get_interactive_msg_id(user_id, thread_id):
            await clear_interactive_msg(user_id, bot, thread_id)

        # Skip tool call notifications when CCBOT_SHOW_TOOL_CALLS=false
        if not config.show_tool_calls and msg.content_type in ("tool_use", "tool_result"):
            continue

        if (
            msg.status_message_eligible
            and (not msg.is_complete or msg.content_type == "tool_progress")
        ):
            status_text = build_status_text(
                msg.text,
                is_complete=msg.is_complete,
                content_type=msg.content_type,
                role=msg.role,
            )
            await enqueue_status_update(
                bot,
                user_id,
                wid,
                status_text,
                thread_id=thread_id,
                turn_generation=turn_generation,
            )
            continue

        if (
            config.telegram_delivery_mode == "compact"
            and msg.is_complete
            and msg.content_type in {"commentary", "orchestration"}
        ):
            commentary_text = build_status_text(
                msg.text,
                is_complete=True,
                content_type=msg.content_type,
                role=msg.role,
            )
            await enqueue_commentary_update(
                bot,
                user_id,
                wid,
                commentary_text,
                thread_id=thread_id,
                turn_generation=turn_generation,
            )

            session = await session_manager.resolve_session_for_window(wid)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass
            continue

        parts = build_response_parts(
            msg.text,
            msg.is_complete,
            msg.content_type,
            msg.role,
        )

        if msg.is_complete:
            # Enqueue content message task
            # Note: tool_result editing is handled inside _process_content_task
            # to ensure sequential processing with tool_use message sending
            await enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_id=wid,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                content_type=msg.content_type,
                semantic_kind=msg.semantic_kind,
                text=msg.text,
                thread_id=thread_id,
                image_data=msg.image_data,
                turn_generation=turn_generation,
            )

            # Update user's read offset to current file position
            # This marks these messages as "read" for this user
            session = await session_manager.resolve_session_for_window(wid)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass


# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task

    await application.bot.delete_my_commands()
    await application.bot.set_my_commands(build_bot_commands())

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")


async def post_shutdown(application: Application) -> None:
    global _status_poll_task

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop all queue workers
    await shutdown_workers()

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")

    await close_transcribe_client()


def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("bind", bind_command))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("rename", rename_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    # Topic edited event — sync renamed topic to tmux window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED,
            topic_edited_handler,
        )
    )
    # Forward any other raw /command to Codex
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Codex
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Voice: transcribe via OpenAI and forward text to Codex
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: non-text content (stickers, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
