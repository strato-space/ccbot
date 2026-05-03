"""Telegram bot handlers — the main UI layer of CCBot.

Registers all command/callback/message handlers and manages the bot lifecycle.
The canonical runtime subject is a Telegram topic bound to live control.
The current handler implementation remains topic-first; a future no-topics mode
should be modeled explicitly as the `thread_id is None` main-chat path rather
than by claiming that chat and topic are identical.

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /bind, /kill,
    /unbind, plus forwarding supported raw /commands to Codex via tmux.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - Topic-based routing: each resolved Telegram topic binds to one tmux window.
    Unbound topics trigger the directory browser to create or resume a thread.
    The current handler path requires a resolved topic id; a future no-topics
    mode would need an explicit `thread_id is None` main-chat path.
  - Photo/document/sticker/audio/video handling: incoming media sent by user is
    downloaded and forwarded to Codex as runtime text with local artifact paths
    or image markers (photo_handler, document_handler, sticker_handler,
    audio_handler, video_handler).
  - Voice handling: voice messages are transcribed via OpenAI API and
    forwarded as text (voice_handler).
  - Automatic cleanup: closing a topic kills the associated window
    (topic_closed_handler). Unsupported content (video notes, animations, etc.)
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
import mimetypes
import os
import re
import shutil
import subprocess  # nosec B404 - ffmpeg is invoked with fixed argv and no shell
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    MessageEntity,
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
    enqueue_plan_update,
    enqueue_status_update,
    flush_terminal_artifacts_before_new_turn,
    get_message_queue,
    is_pre_final_visible_lane_closed,
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
from .handlers.omx_questions import (
    find_active_omx_question,
    handle_omx_question_callback,
    handle_omx_question_ui,
)
from .markdown_v2 import convert_markdown
from .handlers.response_builder import (
    build_commentary_parts,
    build_response_parts,
    build_status_text,
)
from .handlers.status_polling import mark_runtime_presence_active, status_poll_loop
from .runtime_discontinuity import is_codex_termination_summary_text
from .runtime_types import (
    ASSISTANT_FINAL_SEMANTIC_KIND,
    LIFECYCLE_SEMANTIC_KIND,
    PLAN_UPDATE_SEMANTIC_KIND,
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
from .session import BLOCKED_PROMPT_SEND_MESSAGE, PendingSurfaceSlot, session_manager
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
    "exit": "↗ Terminate the live Codex process in this window",
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

CODEX_REJECTED_COMMAND_HINTS: dict[str, str] = {
    "quit": "⚠️ `/quit` is no longer part of the supported Codex Telegram surface. Use `/exit` instead.",
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
    file_path: str = ""


@dataclass(frozen=True)
class ControlSurface:
    kind: str
    chat_id: int | None
    thread_id: int | None
    legacy_scope_id: int | None
    surface_key: str | None
    label: str
    is_shared_group: bool
    supports_bind_flow: bool

    @property
    def message_thread_id(self) -> int | None:
        return self.thread_id


@dataclass(frozen=True)
class _AttachmentInputTarget:
    user_id: int
    thread_id: int
    window_id: str


@dataclass(frozen=True)
class _StickerArtifacts:
    image_path: Path
    image_label: str
    original_path: Path | None = None
    gif_path: Path | None = None
    status_lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class AddressedEvent:
    is_addressed: bool
    is_bare: bool = False
    payload_text: str | None = None


PENDING_SURFACE_STATE_KEY = "_pending_surface"
PENDING_SURFACE_KEY = "_pending_surface_key"
PENDING_SURFACE_SLOTS_KEY = "_pending_surface_slots"

_TELEGRAM_PROXY_ENV_KEYS = (
    "CCBOT_TELEGRAM_PROXY",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "WSS_PROXY",
    "wss_proxy",
    "WS_PROXY",
    "ws_proxy",
)
_STICKER_GIF_FFMPEG_TIMEOUT_SECONDS = 30


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


def _build_unbound_input_message(
    user_id: int,
    surface: ControlSurface,
) -> str:
    """Explain how to re-enter the bind flow for an unbound control surface."""
    runtime_kind = _default_launch_runtime_kind()
    if (
        surface.legacy_scope_id is not None
        and _get_surface_policy(user_id, surface) == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    ):
        return _build_manual_bind_required_message(runtime_kind)
    subject = "chat" if surface.kind == "group_main_chat" else "topic"
    return (
        f"❌ No live tmux window is bound to this {subject}. "
        "Use /bind to start the bind flow."
    )


def _clear_same_thread_picker_state(
    user_data: dict | None,
    surface: ControlSurface | None,
) -> None:
    """Clear bind-flow UI state for the active control surface before an action."""
    if user_data is None or surface is None:
        return
    pending_surface = _pending_surface_from_user_data(user_data)
    if pending_surface is None or not _same_surface(pending_surface, surface):
        return
    clear_window_picker_state(user_data)
    clear_browse_state(user_data)
    clear_thread_picker_state(user_data)
    _set_pending_surface(user_data, None)
    user_data.pop("_pending_thread_text", None)
    user_data.pop("_selected_path", None)


def _clear_shared_group_peer_flow_state(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    surface: ControlSurface,
) -> None:
    """Drop stale per-user bind-flow residue after resolving a shared binding."""
    _clear_pending_slot(context, user_id, surface)
    if _get_surface_binding_state(user_id, surface) == BINDING_STATE_BIND_FLOW:
        _set_surface_binding_state(user_id, surface, BINDING_STATE_NONE)


def _callback_matches_pending_surface(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending_surface: ControlSurface | None,
) -> bool:
    if pending_surface is None:
        return True
    return _same_surface(control_surface_classifier(update), pending_surface)


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
                    file_path=str(getattr(candidate, "rollout_file", "") or ""),
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
    """Extract the resolved Telegram topic id for the current handler path.

    Returning ``None`` means this update is not currently on the topic-based
    surface handled by the existing flow. A no-topics path, if supported,
    should be modeled explicitly as the chat-wide `thread_id is None` mode
    rather than by collapsing chat and topic here.
    """
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


def control_surface_classifier(update: Update) -> ControlSurface:
    """Classify the current Telegram control surface for routing decisions."""
    chat = update.effective_chat
    if chat is None:
        return ControlSurface(
            kind="unsupported",
            chat_id=None,
            thread_id=None,
            legacy_scope_id=None,
            surface_key=None,
            label="topic",
            is_shared_group=False,
            supports_bind_flow=False,
        )

    thread_id = _get_thread_id(update)
    is_group_chat = chat.type in ("group", "supergroup")
    if is_group_chat and thread_id is None:
        return ControlSurface(
            kind="group_main_chat",
            chat_id=chat.id,
            thread_id=None,
            legacy_scope_id=chat.id,
            surface_key=f"c:{chat.id}",
            label="chat",
            is_shared_group=True,
            supports_bind_flow=True,
        )
    if thread_id is not None:
        return ControlSurface(
            kind="group_topic" if is_group_chat else "private_topic",
            chat_id=chat.id,
            thread_id=thread_id,
            legacy_scope_id=thread_id,
            surface_key=f"t:{thread_id}",
            label="topic",
            is_shared_group=is_group_chat,
            supports_bind_flow=True,
        )
    return ControlSurface(
        kind="unsupported",
        chat_id=chat.id,
        thread_id=None,
        legacy_scope_id=None,
        surface_key=None,
        label="topic",
        is_shared_group=False,
        supports_bind_flow=False,
    )


def surface_key_adapter(surface: ControlSurface) -> int | None:
    """Return the legacy numeric scope id used by pre-surface session APIs."""
    return surface.legacy_scope_id


def _remember_group_chat_id_for_surface(user_id: int, update: Update) -> None:
    """Persist Telegram group routing metadata for command-only entry paths."""
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user_id, _get_thread_id(update), chat.id)


def _surface_to_state(surface: ControlSurface) -> dict[str, object]:
    return {
        "kind": surface.kind,
        "chat_id": surface.chat_id,
        "thread_id": surface.thread_id,
        "legacy_scope_id": surface.legacy_scope_id,
        "surface_key": surface.surface_key,
        "label": surface.label,
        "is_shared_group": surface.is_shared_group,
        "supports_bind_flow": surface.supports_bind_flow,
    }


def _surface_from_state(state: object) -> ControlSurface | None:
    if not isinstance(state, dict):
        return None
    surface_key = state.get("surface_key")
    kind = state.get("kind")
    label = state.get("label")
    if (
        not isinstance(surface_key, str)
        or not isinstance(kind, str)
        or not isinstance(label, str)
    ):
        return None
    return ControlSurface(
        kind=kind,
        chat_id=state.get("chat_id") if isinstance(state.get("chat_id"), int) else None,
        thread_id=state.get("thread_id")
        if isinstance(state.get("thread_id"), int)
        else None,
        legacy_scope_id=state.get("legacy_scope_id")
        if isinstance(state.get("legacy_scope_id"), int)
        else None,
        surface_key=surface_key,
        label=label,
        is_shared_group=bool(state.get("is_shared_group")),
        supports_bind_flow=bool(state.get("supports_bind_flow", True)),
    )


def _session_has_method(name: str) -> bool:
    return callable(getattr(type(session_manager), name, None))


def _pending_surface_from_user_data(user_data: dict | None) -> ControlSurface | None:
    if user_data is None:
        return None
    return _surface_from_state(user_data.get(PENDING_SURFACE_STATE_KEY))


def _set_pending_surface(
    user_data: dict | None, surface: ControlSurface | None
) -> None:
    if user_data is None:
        return
    if surface is None:
        user_data.pop(PENDING_SURFACE_STATE_KEY, None)
        user_data.pop(PENDING_SURFACE_KEY, None)
        user_data.pop("_pending_thread_id", None)
        return
    user_data[PENDING_SURFACE_STATE_KEY] = _surface_to_state(surface)
    user_data[PENDING_SURFACE_KEY] = surface.surface_key
    user_data["_pending_thread_id"] = surface.legacy_scope_id


def _same_surface(a: ControlSurface | None, b: ControlSurface | None) -> bool:
    if a is None or b is None:
        return False
    if a.surface_key and b.surface_key:
        return a.surface_key == b.surface_key
    return a.legacy_scope_id == b.legacy_scope_id


def _pending_slots(user_data: dict | None) -> dict[str, dict[str, object]]:
    if user_data is None:
        return {}
    slots = user_data.get(PENDING_SURFACE_SLOTS_KEY)
    if not isinstance(slots, dict):
        slots = {}
        user_data[PENDING_SURFACE_SLOTS_KEY] = slots
    return slots


def _put_pending_slot(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    surface: ControlSurface,
    text: str,
) -> dict[str, object] | None:
    if surface.surface_key is None:
        return None
    if _session_has_method("set_surface_pending_slot"):
        return session_manager.set_surface_pending_slot(
            user_id,
            text,
            surface_key=surface.surface_key,
        )
    slots = _pending_slots(context.user_data)
    current = PendingSurfaceSlot.from_record(slots.get(surface.surface_key))
    revision = (current.revision if current is not None else 0) + 1
    record = PendingSurfaceSlot(text=text, revision=revision).to_dict()
    slots[surface.surface_key] = record
    return record


def _peek_pending_slot(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    surface: ControlSurface,
) -> dict[str, object] | None:
    if surface.surface_key is None:
        return None
    if _session_has_method("peek_surface_pending_slot"):
        return session_manager.peek_surface_pending_slot(
            user_id,
            surface_key=surface.surface_key,
        )
    slots = _pending_slots(context.user_data)
    record = slots.get(surface.surface_key)
    slot = PendingSurfaceSlot.from_record(record)
    return slot.to_dict() if slot is not None else None


def _clear_pending_slot(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    surface: ControlSurface,
) -> None:
    if surface.surface_key is None:
        return
    if _session_has_method("clear_surface_pending_slot"):
        session_manager.clear_surface_pending_slot(
            user_id,
            surface_key=surface.surface_key,
        )
        return
    slots = _pending_slots(context.user_data)
    slots.pop(surface.surface_key, None)


def _consume_pending_slot(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    surface: ControlSurface,
    activation_id: str,
) -> str | None:
    if surface.surface_key is None:
        return None
    if _session_has_method("consume_surface_pending_slot"):
        record = session_manager.consume_surface_pending_slot(
            user_id,
            activation_id,
            surface_key=surface.surface_key,
        )
        if not isinstance(record, dict):
            return None
        text = record.get("text")
        return text if isinstance(text, str) and text else None
    slots = _pending_slots(context.user_data)
    slot = PendingSurfaceSlot.from_record(slots.get(surface.surface_key))
    if slot is None:
        return None
    if slot.status != "pending":
        return None
    slots[surface.surface_key] = slot.consume(activation_id).to_dict()
    return slot.text


def _get_session_window_for_surface(
    user_id: int, surface: ControlSurface
) -> str | None:
    if surface.surface_key and _session_has_method("get_window_for_surface"):
        return session_manager.get_window_for_surface(
            user_id,
            surface_key=surface.surface_key,
        )
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return None
    return session_manager.get_window_for_thread(user_id, legacy_scope_id)


def _get_shared_group_binding_for_surface(
    user_id: int, surface: ControlSurface
) -> tuple[int, str] | None:
    """Resolve an existing group surface binding owned by another user."""
    if not surface.is_shared_group or not surface.surface_key:
        return None
    bindings_by_user = getattr(session_manager, "surface_bindings", None)
    if not isinstance(bindings_by_user, dict):
        return None

    for owner_id, bindings in bindings_by_user.items():
        if owner_id == user_id or not isinstance(bindings, dict):
            continue
        window_id = bindings.get(surface.surface_key)
        if isinstance(window_id, str) and window_id:
            try:
                binding_owner_id = int(owner_id)
            except (TypeError, ValueError):
                continue
            if not _shared_group_binding_matches_surface(
                binding_owner_id,
                surface,
            ):
                continue
            return binding_owner_id, window_id
    return None


def _shared_group_binding_matches_surface(
    owner_id: int,
    surface: ControlSurface,
) -> bool:
    """Reject same-thread-id bindings that belong to another Telegram chat."""
    if surface.kind == "group_main_chat":
        return True
    if surface.kind != "group_topic":
        return False
    if surface.chat_id is None or surface.message_thread_id is None:
        return False
    resolve_chat_id = getattr(session_manager, "resolve_chat_id", None)
    if not callable(resolve_chat_id):
        return False
    return resolve_chat_id(owner_id, surface.message_thread_id) == surface.chat_id


def _get_shared_group_window_for_surface(
    user_id: int, surface: ControlSurface
) -> str | None:
    binding = _get_shared_group_binding_for_surface(user_id, surface)
    if binding is None:
        return None
    owner_id, window_id = binding
    logger.info(
        "Resolved shared group surface %s via owner=%s window=%s for user=%d",
        surface.surface_key,
        owner_id,
        window_id,
        user_id,
    )
    return window_id


def _get_writable_window_for_surface(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    surface: ControlSurface,
) -> tuple[str | None, bool]:
    """Return the user's own binding or a shared group peer binding."""
    wid = _get_session_window_for_surface(user_id, surface)
    if wid is not None:
        return wid, False
    wid = _get_shared_group_window_for_surface(user_id, surface)
    if wid is not None:
        _clear_shared_group_peer_flow_state(context, user_id, surface)
        return wid, True
    return None, False


def _resolve_session_window_for_surface(
    user_id: int, surface: ControlSurface
) -> str | None:
    if surface.surface_key and _session_has_method("resolve_window_for_surface"):
        wid = session_manager.resolve_window_for_surface(
            user_id,
            surface_key=surface.surface_key,
        )
        return wid or _get_shared_group_window_for_surface(user_id, surface)
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return None
    wid = session_manager.resolve_window_for_thread(
        user_id,
        legacy_scope_id,
        chat_id=surface.chat_id,
    )
    return wid or _get_shared_group_window_for_surface(user_id, surface)


def _get_surface_policy(user_id: int, surface: ControlSurface) -> str:
    if surface.surface_key and _session_has_method("get_surface_policy"):
        return session_manager.get_surface_policy(
            user_id,
            surface_key=surface.surface_key,
        )
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return TOPIC_POLICY_MANUAL_BIND_REQUIRED
    return session_manager.get_topic_policy(user_id, legacy_scope_id)


def _get_surface_binding_state(user_id: int, surface: ControlSurface) -> str:
    if surface.surface_key and _session_has_method("get_surface_binding_state"):
        return session_manager.get_surface_binding_state(
            user_id,
            surface_key=surface.surface_key,
        )
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return BINDING_STATE_NONE
    return session_manager.get_topic_binding_state(user_id, legacy_scope_id)


def _set_surface_binding_state(
    user_id: int,
    surface: ControlSurface,
    binding_state: str,
) -> None:
    if surface.surface_key and _session_has_method("set_surface_binding_state"):
        session_manager.set_surface_binding_state(
            user_id,
            binding_state,
            surface_key=surface.surface_key,
        )
        return
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is not None:
        session_manager.set_topic_binding_state(user_id, legacy_scope_id, binding_state)


def _require_manual_bind(user_id: int, surface: ControlSurface) -> None:
    if surface.surface_key and _session_has_method("require_manual_bind_for_surface"):
        session_manager.require_manual_bind_for_surface(
            user_id,
            surface_key=surface.surface_key,
        )
        return
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is not None:
        session_manager.require_manual_bind(user_id, legacy_scope_id)


def _allow_implicit_bind(user_id: int, surface: ControlSurface) -> None:
    if surface.surface_key and _session_has_method("allow_implicit_bind_for_surface"):
        session_manager.allow_implicit_bind_for_surface(
            user_id,
            surface_key=surface.surface_key,
        )
        return
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is not None:
        session_manager.allow_implicit_bind(user_id, legacy_scope_id)


def _start_surface_bind_flow_state(user_id: int, surface: ControlSurface) -> None:
    if surface.surface_key and _session_has_method("start_surface_bind_flow"):
        session_manager.start_surface_bind_flow(
            user_id,
            surface_key=surface.surface_key,
        )
        return
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is not None:
        session_manager.start_topic_bind_flow(user_id, legacy_scope_id)


def _get_surface_bind_flow_credentials(
    user_id: int,
    surface: ControlSurface,
) -> tuple[int, str]:
    if surface.surface_key and _session_has_method("get_surface_bind_flow_credentials"):
        return session_manager.get_surface_bind_flow_credentials(
            user_id,
            surface_key=surface.surface_key,
        )
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return (0, "")
    return session_manager.get_topic_bind_flow_credentials(user_id, legacy_scope_id)


def _validate_surface_bind_flow_callback(
    user_id: int,
    surface: ControlSurface,
    version: int,
    nonce: str,
) -> bool:
    if surface.surface_key and _session_has_method(
        "validate_surface_bind_flow_callback"
    ):
        return session_manager.validate_surface_bind_flow_callback(
            user_id,
            version,
            nonce,
            surface_key=surface.surface_key,
        )
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return False
    return session_manager.validate_topic_bind_flow_callback(
        user_id,
        legacy_scope_id,
        version,
        nonce,
    )


def _surface_external_binding(
    user_id: int,
    surface: ControlSurface,
) -> dict[str, object] | None:
    if surface.surface_key and _session_has_method("get_external_surface_binding"):
        return session_manager.get_external_surface_binding(
            user_id,
            surface_key=surface.surface_key,
        )
    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return None
    return session_manager.get_external_topic_binding(user_id, legacy_scope_id)


def parse_addressed_event(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> AddressedEvent:
    """Parse @bot mentions for non-shared surfaces without raw text heuristics."""
    message = update.message
    if message is None or not message.text:
        return AddressedEvent(is_addressed=False)
    username = getattr(context.bot, "username", None)
    if not isinstance(username, str) or not username:
        return AddressedEvent(is_addressed=False)
    entities = list(message.entities or [])
    if not entities:
        return AddressedEvent(is_addressed=False)
    first = min(entities, key=lambda entity: entity.offset)
    if first.offset != 0:
        return AddressedEvent(is_addressed=False)

    if first.type == MessageEntity.MENTION:
        mention_text = message.text[first.offset : first.offset + first.length]
        if mention_text.casefold() != f"@{username}".casefold():
            return AddressedEvent(is_addressed=False)
    elif first.type == MessageEntity.TEXT_MENTION:
        bot_id = getattr(context.bot, "id", None)
        entity_user = getattr(first, "user", None)
        if (
            bot_id is None
            or entity_user is None
            or getattr(entity_user, "id", None) != bot_id
        ):
            return AddressedEvent(is_addressed=False)
    else:
        return AddressedEvent(is_addressed=False)

    remainder = message.text[first.offset + first.length :]
    if not remainder.strip():
        return AddressedEvent(is_addressed=True, is_bare=True)
    if not remainder[:1].isspace():
        return AddressedEvent(is_addressed=False)
    payload_text = remainder.strip()
    if not payload_text:
        return AddressedEvent(is_addressed=True, is_bare=True)
    return AddressedEvent(
        is_addressed=True,
        is_bare=False,
        payload_text=payload_text,
    )


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


def _is_non_bindable_codex_helper_window(window_id: str) -> bool:
    """Return True for Codex helper/subagent windows that users must not bind."""
    window_states = getattr(session_manager, "window_states", None)
    if not isinstance(window_states, dict):
        return False

    state = window_states.get(window_id)
    if state is None:
        return False
    if getattr(state, "runtime_kind", "") != "codex":
        return False

    thread_id = str(getattr(state, "thread_id", "") or "").strip()
    if not thread_id:
        return False

    catalog = getattr(session_manager, "codex_thread_catalog", None)
    if catalog is None or not hasattr(catalog, "is_helper_thread_fast"):
        return False
    try:
        return bool(catalog.is_helper_thread_fast(thread_id))
    except Exception as exc:
        logger.debug(
            "Unable to classify Codex helper window %s (%s): %s",
            window_id,
            thread_id,
            exc,
        )
        return False


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


def _resolve_existing_window_runtime_kind(
    window_id: str,
    pane_command: str,
) -> str | None:
    """Resolve the runtime kind for a live tmux window selected via /bind."""
    return (
        runtime_capability_registry.known_runtime_kind_from_command(pane_command)
        or _get_registered_window_runtime_kind(window_id)
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


def _telegram_proxy_from_env() -> str | None:
    """Return the first configured Telegram HTTP proxy URL from the environment."""
    for key in _TELEGRAM_PROXY_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            return value
    return None


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


async def _surface_omx_question_state(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    *,
    reply_message: object | None = None,
    chat_id: int | None = None,
    window: object | None = None,
) -> bool:
    """Show an active OMX question and fail closed for ordinary remote input."""
    w = (
        window
        if window is not None
        else await tmux_manager.find_window_by_id(window_id)
    )
    if not w:
        return False
    if not isinstance(getattr(w, "cwd", ""), str) or not getattr(w, "cwd", ""):
        return False
    record = find_active_omx_question(w)
    if record is None:
        return False
    shown = await handle_omx_question_ui(
        bot,
        user_id,
        window_id,
        thread_id,
        record=record,
    )
    if not shown:
        logger.warning(
            "Active OMX question detected but Telegram artifact was not delivered "
            "(user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
    notice = (
        "⚠️ OMX question is waiting for an answer. "
        "Use the Telegram question buttons (or answer in tmux) before sending normal input."
    )
    if reply_message is not None:
        await safe_reply(reply_message, notice)
    elif chat_id is not None:
        await safe_send(
            bot,
            chat_id,
            notice,
            message_thread_id=thread_id,
        )
    return True


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


def _surface_subject(surface: ControlSurface) -> str:
    return "chat" if surface.kind == "group_main_chat" else "topic"


def _unsupported_surface_message(surface: ControlSurface) -> str:
    if surface.kind == "unsupported":
        return (
            "❌ This build currently requires a Telegram topic, or a group/supergroup "
            "main chat when topics are disabled."
        )
    return f"❌ This command only works in a {_surface_subject(surface)}."


async def _maybe_autosend_pending_after_activation(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    user: object,
    surface: ControlSurface,
    window_id: str,
    chat_id: int | None = None,
) -> None:
    activation_id = f"{window_id}:{time.time_ns()}"
    pending_text = _consume_pending_slot(
        context,
        user.id,
        surface,
        activation_id,
    )
    if not pending_text:
        if context.user_data is not None:
            _set_pending_surface(context.user_data, None)
            context.user_data.pop("_pending_thread_text", None)
        return
    send_ok, send_msg = await session_manager.send_to_window(window_id, pending_text)
    if context.user_data is not None:
        _set_pending_surface(context.user_data, None)
        context.user_data.pop("_pending_thread_text", None)
    if send_ok:
        return
    logger.warning("Failed to forward pending text after activation: %s", send_msg)
    resolved_chat = chat_id or session_manager.resolve_chat_id(
        user.id,
        surface.legacy_scope_id,
    )
    if send_msg == BLOCKED_PROMPT_SEND_MESSAGE:
        await _surface_blocked_prompt_state(
            context.bot,
            user.id,
            window_id,
            surface.message_thread_id,
            chat_id=resolved_chat,
        )
        return
    await safe_send(
        context.bot,
        resolved_chat,
        f"❌ Failed to send pending message: {send_msg}",
        message_thread_id=surface.message_thread_id,
    )


async def _start_bind_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    surface: ControlSurface,
    *,
    explicit: bool,
    pending_text: str | None = None,
) -> None:
    """Open the bind chooser and mark the control surface as being in a bind flow."""
    if update.message is None:
        return

    legacy_scope_id = surface_key_adapter(surface)
    if legacy_scope_id is None:
        return
    if explicit:
        _allow_implicit_bind(user.id, surface)
    _start_surface_bind_flow_state(user.id, surface)
    if pending_text:
        _put_pending_slot(context, user.id, surface, pending_text)
    bind_flow_version, bind_flow_nonce = _get_surface_bind_flow_credentials(
        user.id, surface
    )

    all_windows = await tmux_manager.list_windows()
    bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
    unbound: list[tuple[str, str, str]] = []
    hidden_helper_windows: list[str] = []
    for w in all_windows:
        if w.window_id in bound_ids:
            continue
        if _is_non_bindable_codex_helper_window(w.window_id):
            hidden_helper_windows.append(w.window_name)
            continue
        unbound.append((w.window_id, w.window_name, w.cwd))

    logger.debug(
        "Bind flow start (explicit=%s): all=%s, bound=%s, unbound=%s, hidden_helpers=%s",
        explicit,
        [w.window_name for w in all_windows],
        bound_ids,
        [name for _, name, _ in unbound],
        hidden_helper_windows,
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
            _set_pending_surface(context.user_data, surface)
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
        _set_pending_surface(context.user_data, surface)
        if pending_text is not None:
            context.user_data["_pending_thread_text"] = pending_text
        else:
            context.user_data.pop("_pending_thread_text", None)
    await safe_reply(update.message, msg_text, reply_markup=keyboard)


def _get_current_bind_flow_credentials(
    user_id: int,
    surface: ControlSurface,
) -> tuple[int, str]:
    """Return the active bind-flow credentials for the current control surface."""
    return _get_surface_bind_flow_credentials(user_id, surface)


def _resolve_bind_flow_callback_surface(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> ControlSurface | None:
    """Recover the initiating control surface for bind-flow callbacks."""
    pending_surface = _pending_surface_from_user_data(context.user_data)
    if pending_surface is not None:
        return pending_surface
    surface = control_surface_classifier(update)
    return surface if surface.supports_bind_flow else None


def _resolve_bind_flow_callback_thread_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:
    """Recover the topic id for bind-flow callbacks after unrelated traffic.

    The visible picker message still belongs to the original topic even if
    `_pending_thread_id` was cleared by text in another topic. Use the callback
    message context as a safe fallback before treating the callback as stale.
    """
    pending_surface = _resolve_bind_flow_callback_surface(update, context)
    if pending_surface is None:
        return None
    return pending_surface.legacy_scope_id


async def _validate_bind_flow_callback(
    query: object,
    *,
    user_id: int,
    surface: ControlSurface | None,
    version: int,
    nonce: str,
) -> bool:
    """Fail closed for stale bind-flow callbacks after restart/unbind/cancel."""
    if surface is None:
        await query.answer("Stale bind flow, use /bind again", show_alert=True)
        return False
    if not _validate_surface_bind_flow_callback(user_id, surface, version, nonce):
        await query.answer("Stale bind flow, use /bind again", show_alert=True)
        return False
    return True


# --- Command handlers ---


async def bind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explicitly start a bind flow for the current control surface."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if not surface.supports_bind_flow:
        await safe_reply(update.message, _unsupported_surface_message(surface))
        return

    wid, _ = _get_writable_window_for_surface(context, user.id, surface)
    if wid is not None:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"ℹ️ This {_surface_subject(surface)} is already bound to '{display}'. "
            "Use /unbind first if you want to bind a different window.",
        )
        return

    raw_text = (update.message.text or "").strip()
    parts = raw_text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        runtime_kind = _default_launch_runtime_kind()
        if runtime_kind != "codex":
            await safe_reply(
                update.message, _build_resume_degraded_message(runtime_kind)
            )
            return

        token = " ".join(parts[1].split())
        target, error_message = await _resolve_resume_command_target(
            runtime_kind, token
        )
        if target is None:
            await safe_reply(
                update.message,
                error_message
                or "❌ No Codex thread matched the token for external bind.",
            )
            return

        await clear_topic_state(
            user.id,
            surface.legacy_scope_id,
            context.bot,
            context.user_data,
        )
        _clear_same_thread_picker_state(context.user_data, surface)
        _allow_implicit_bind(user.id, surface)
        session_manager.bind_external_surface(
            user.id,
            runtime_kind=target.runtime_kind,
            source_thread_id=target.thread_id,
            summary=target.summary,
            cwd=target.cwd,
            file_path=target.file_path,
            read_only=True,
            surface_key=surface.surface_key,
        )
        topic_synced = (
            await _sync_topic_title(
                context.bot,
                user.id,
                surface.message_thread_id,
                target.summary or target.thread_id,
            )
            if surface.message_thread_id is not None
            else True
        )

        response_lines = [
            f"✅ Bound this {_surface_subject(surface)} to persisted Codex replay.",
            f"• thread: `{target.thread_id}`",
            "• mode: read-only (no live tmux injection plane attached)",
            "Use `/unbind` then `/resume <thread-name|id>` (or `/bind`) to attach writable control.",
        ]
        if not topic_synced:
            response_lines.append("⚠️ Topic title sync failed; binding is still active.")
        await safe_reply(update.message, "\n".join(response_lines))
        return

    await _start_bind_flow(update, context, user, surface, explicit=True)


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
            "Each bound topic or supported group main chat controls one live tmux window.\n"
            f"This bot launches the configured runtime lane in tmux: {capability.display_name}.\n"
            "Shared group topics and no-topics main chats stay silent until you use "
            "/bind or /resume. After /unbind or Cancel, plain messages stop rebinding "
            "until you explicitly re-open control.\n"
            "Telegram text enters the equal message layer in queue mode by default. "
            "steer changes routing semantics for explicit runtime controls; raw "
            "tmux terminal control stays separate and is never treated as a queued message.\n"
            f"{_build_start_resume_note(runtime_kind)}\n"
            "Use /bind when you want to choose a workspace explicitly, and use "
            "/resume when the current runtime lane supports deterministic explicit resume.\n"
            "The menu only advertises the stable bot surface and any supported runtime core lane. "
            "In the Codex lane, `/exit` is the supported public termination command.",
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias /help to the documented /start surface."""
    await start_command(update, context)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if not surface.supports_bind_flow:
        await safe_reply(update.message, _unsupported_surface_message(surface))
        return
    wid = _resolve_session_window_for_surface(user.id, surface)
    if not wid:
        await safe_reply(update.message, _build_unbound_input_message(user.id, surface))
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

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if not surface.supports_bind_flow:
        await safe_reply(update.message, _unsupported_surface_message(surface))
        return
    wid = _resolve_session_window_for_surface(user.id, surface)
    if not wid:
        await safe_reply(update.message, _build_unbound_input_message(user.id, surface))
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
    """Unbind this control surface from its live window without killing it."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if not surface.supports_bind_flow:
        await safe_reply(update.message, _unsupported_surface_message(surface))
        return

    runtime_kind = _default_launch_runtime_kind()
    wid = _get_session_window_for_surface(user.id, surface)
    binding_owner_id = user.id
    if not wid:
        shared_binding = _get_shared_group_binding_for_surface(user.id, surface)
        if shared_binding is not None:
            binding_owner_id, wid = shared_binding
    if not wid:
        _require_manual_bind(user.id, surface)
        await clear_topic_state(
            user.id,
            surface.legacy_scope_id,
            context.bot,
            context.user_data,
        )
        _clear_same_thread_picker_state(context.user_data, surface)
        await safe_reply(
            update.message,
            _build_manual_bind_required_message(runtime_kind),
        )
        return

    display = session_manager.get_display_name(wid)
    session_manager.unbind_surface(
        binding_owner_id,
        surface_key=surface.surface_key,
    )
    _require_manual_bind(user.id, surface)
    if binding_owner_id != user.id:
        _require_manual_bind(binding_owner_id, surface)
    await clear_topic_state(
        user.id,
        surface.legacy_scope_id,
        context.bot,
        context.user_data,
    )
    if binding_owner_id != user.id:
        await clear_topic_state(
            binding_owner_id,
            surface.legacy_scope_id,
            context.bot,
            None,
        )

    await safe_reply(
        update.message,
        f"✅ {_surface_subject(surface).capitalize()} unbound from window '{display}'.\n"
        "The live tmux window is still running.\n"
        f"{_build_manual_bind_required_message(runtime_kind)}",
    )


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bind the current control surface to a persisted runtime thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if not surface.supports_bind_flow:
        await safe_reply(update.message, _unsupported_surface_message(surface))
        return

    wid, _ = _get_writable_window_for_surface(context, user.id, surface)
    if wid is not None:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"ℹ️ This {_surface_subject(surface)} is already bound to '{display}'. Use /unbind first if you want to /resume a different persisted thread.",
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
        await safe_reply(
            update.message,
            error_message or _build_resume_degraded_message(runtime_kind),
        )
        return

    await clear_topic_state(
        user.id,
        surface.legacy_scope_id,
        context.bot,
        context.user_data,
    )
    _clear_same_thread_picker_state(context.user_data, surface)
    _allow_implicit_bind(user.id, surface)

    (
        success,
        message,
        final_name,
        created_wid,
        reused_existing,
    ) = await tmux_manager.create_or_reuse_window(
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
        surface.legacy_scope_id,
        window_id=created_wid,
        window_name=final_name or Path(target.cwd).name,
        selected_path=target.cwd,
        runtime_kind=target.runtime_kind,
        surface=surface,
        resume_session_id=target.thread_id,
        sync_topic_title=surface.message_thread_id is not None,
    )
    await _maybe_autosend_pending_after_activation(
        context,
        user=user,
        surface=surface,
        window_id=created_wid,
    )

    capability = session_manager.get_runtime_capability(target.runtime_kind)
    action = "Reused" if reused_existing else "Created"
    await safe_reply(
        update.message,
        "\n".join(
            [
                f"✅ {message}",
                f"✅ {action} {capability.display_name} window for '{target.summary}'.",
                f"ℹ️ This {_surface_subject(surface)} is now bound to the resumed {capability.display_name} thread.",
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

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if surface.message_thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = _resolve_session_window_for_surface(user.id, surface)
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
    topic_synced = await _sync_topic_title(
        context.bot,
        user.id,
        surface.message_thread_id,
        final_name,
    )

    runtime_kind = _get_window_runtime_kind(wid)
    capability = session_manager.get_runtime_capability(runtime_kind)
    (
        identity_changed,
        identity_note,
    ) = await session_manager.rename_runtime_identity_for_window(
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

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if not surface.supports_bind_flow:
        await safe_reply(update.message, _unsupported_surface_message(surface))
        return
    wid = _resolve_session_window_for_surface(user.id, surface)
    if not wid:
        await safe_reply(update.message, _build_unbound_input_message(user.id, surface))
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

    surface = control_surface_classifier(update)
    _remember_group_chat_id_for_surface(user.id, update)
    if not surface.supports_bind_flow:
        await safe_reply(update.message, _unsupported_surface_message(surface))
        return
    wid = _resolve_session_window_for_surface(user.id, surface)
    if not wid:
        await safe_reply(update.message, _build_unbound_input_message(user.id, surface))
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return
    if await _surface_omx_question_state(
        context.bot,
        user.id,
        wid,
        surface.message_thread_id,
        reply_message=update.message,
        window=w,
    ):
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
                surface.thread_id,
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

    surface = control_surface_classifier(update)
    wid = session_manager.get_window_for_thread(user.id, thread_id)
    binding_owner_id = user.id
    if not wid:
        shared_binding = _get_shared_group_binding_for_surface(user.id, surface)
        if shared_binding is not None:
            binding_owner_id, wid = shared_binding
    if wid:
        display = session_manager.get_display_name(wid)
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, owner=%d, thread=%d)",
                display,
                user.id,
                binding_owner_id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, owner=%d, thread=%d)",
                display,
                user.id,
                binding_owner_id,
                thread_id,
            )
        session_manager.unbind_thread(binding_owner_id, thread_id)
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
        if binding_owner_id != user.id:
            await clear_topic_state(binding_owner_id, thread_id, context.bot, None)
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

    surface = control_surface_classifier(update)
    wid, _ = _get_writable_window_for_surface(context, user.id, surface)
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
    (
        identity_changed,
        identity_note,
    ) = await session_manager.rename_runtime_identity_for_window(
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
    surface = control_surface_classifier(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = _resolve_session_window_for_surface(user.id, surface)
    if not wid:
        await safe_reply(update.message, UNBOUND_TOPIC_MESSAGE)
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    if await _surface_omx_question_state(
        context.bot,
        user.id,
        wid,
        surface.message_thread_id,
        reply_message=update.message,
        window=w,
    ):
        return

    display = session_manager.get_display_name(wid)
    runtime_kind = _get_window_runtime_kind(wid)
    command_name = cc_slash[1:].split(None, 1)[0].lower()
    if runtime_kind == "codex" and command_name in CLAUDE_ONLY_COMMAND_HINTS:
        await safe_reply(update.message, CLAUDE_ONLY_COMMAND_HINTS[command_name])
        return
    if runtime_kind == "codex" and command_name in CODEX_REJECTED_COMMAND_HINTS:
        await safe_reply(update.message, CODEX_REJECTED_COMMAND_HINTS[command_name])
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
            logger.info(
                "Clearing persisted binding for window %s after /clear", display
            )
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
    """Reply to unsupported non-text messages (video notes, animations, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text, photo, document, sticker, voice, audio, and video messages are supported. Video notes, animations, and other media cannot be forwarded to Codex.",
    )


# --- Attachment directories for incoming media ---
_IMAGES_DIR = ccbot_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
_DOCUMENTS_DIR = ccbot_dir() / "documents"
_DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
_MEDIA_DIR = ccbot_dir() / "media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_MAX_AUDIO_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_VIDEO_BYTES = 100 * 1024 * 1024
_VIDEO_PREVIEW_FFMPEG_TIMEOUT_SECONDS = 20
_AUDIO_SUFFIXES = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
_VIDEO_SUFFIXES = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}
_AUDIO_MIME_ALLOWLIST = {
    "application/ogg",
    "application/octet-stream",
}
_VIDEO_MIME_ALLOWLIST = {
    "application/octet-stream",
}


def _sanitize_attachment_filename(filename: str | None, fallback: str) -> str:
    """Sanitize an incoming Telegram filename for local storage."""
    candidate = (filename or "").replace("\x00", "").strip()
    if candidate:
        candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not candidate or candidate in {".", ".."}:
        candidate = fallback
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", candidate).strip(" .")
    if not safe or safe in {".", ".."}:
        safe = fallback
    if len(safe) > 180:
        stem, dot, suffix = safe.rpartition(".")
        if dot and stem:
            safe = f"{stem[: max(1, 179 - len(suffix))]}.{suffix}"
        else:
            safe = safe[:180]
    return safe


def _optional_str_attr(obj: Any, name: str) -> str | None:
    value = getattr(obj, name, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int_attr(obj: Any, name: str) -> int | None:
    value = getattr(obj, name, None)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _max_media_bytes(env_name: str, default: int) -> int:
    value = os.getenv(env_name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", env_name, value, default)
        return default
    return max(0, parsed)


def _media_mime_allowed(mime_type: str | None, *, kind: str) -> bool:
    if not mime_type:
        return True
    normalized = mime_type.lower()
    if kind == "audio":
        return normalized.startswith("audio/") or normalized in _AUDIO_MIME_ALLOWLIST
    if kind == "video":
        return normalized.startswith("video/") or normalized in _VIDEO_MIME_ALLOWLIST
    return False


def _media_suffix_allowed(suffix: str, *, kind: str) -> bool:
    if kind == "audio":
        return suffix in _AUDIO_SUFFIXES
    if kind == "video":
        return suffix in _VIDEO_SUFFIXES
    return False


def _mime_fallback_suffix(mime_type: str | None, fallback: str) -> str:
    if not mime_type:
        return fallback
    guessed = mimetypes.guess_extension(mime_type.split(";", 1)[0].strip().lower())
    if guessed and re.fullmatch(r"\.[a-z0-9]{1,10}", guessed):
        return guessed.lower()
    return fallback


def _media_preflight_warning(media: Any, *, kind: str) -> str | None:
    max_bytes = _max_media_bytes(
        "CCBOT_MAX_AUDIO_BYTES" if kind == "audio" else "CCBOT_MAX_VIDEO_BYTES",
        _DEFAULT_MAX_AUDIO_BYTES if kind == "audio" else _DEFAULT_MAX_VIDEO_BYTES,
    )
    file_size = _optional_int_attr(media, "file_size")
    if max_bytes and file_size is not None and file_size > max_bytes:
        return (
            f"⚠ {kind.capitalize()} file is too large "
            f"({file_size} bytes; limit {max_bytes} bytes)."
        )

    mime_type = _optional_str_attr(media, "mime_type")
    if not _media_mime_allowed(mime_type, kind=kind):
        return (
            f"⚠ Unsupported {kind} MIME type: {mime_type}. "
            "Send a standard audio/video file."
        )
    return None


def _build_media_artifact_path(
    media: Any,
    tg_file: Any,
    *,
    kind: str,
    fallback_suffix: str,
) -> Path:
    unique_id = _sanitize_attachment_filename(
        _optional_str_attr(media, "file_unique_id"),
        kind,
    )
    original_name = _optional_str_attr(media, "file_name")
    tg_suffix = _telegram_file_suffix(tg_file, fallback_suffix)
    mime_suffix = _mime_fallback_suffix(
        _optional_str_attr(media, "mime_type"),
        fallback_suffix,
    )
    suffix = tg_suffix if tg_suffix != fallback_suffix else mime_suffix

    if original_name:
        original_safe = _sanitize_attachment_filename(original_name, f"{kind}{suffix}")
        if not Path(original_safe).suffix and suffix:
            original_safe = f"{original_safe}{suffix}"
        filename = f"{int(time.time())}_{unique_id}_{original_safe}"
    else:
        filename = f"{int(time.time())}_{unique_id}{suffix}"

    return _MEDIA_DIR / filename


def _post_download_media_warning(path: Path, *, kind: str) -> str | None:
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        logger.warning("Could not stat downloaded %s artifact %s: %s", kind, path, exc)
        return f"⚠ Could not verify downloaded {kind} artifact."

    max_bytes = _max_media_bytes(
        "CCBOT_MAX_AUDIO_BYTES" if kind == "audio" else "CCBOT_MAX_VIDEO_BYTES",
        _DEFAULT_MAX_AUDIO_BYTES if kind == "audio" else _DEFAULT_MAX_VIDEO_BYTES,
    )
    if max_bytes and file_size > max_bytes:
        return (
            f"⚠ {kind.capitalize()} file is too large "
            f"({file_size} bytes; limit {max_bytes} bytes)."
        )
    return None


async def _download_media_artifact(
    media: Any,
    *,
    kind: str,
    fallback_suffix: str,
) -> tuple[Path | None, str | None]:
    warning = _media_preflight_warning(media, kind=kind)
    if warning:
        return None, warning

    try:
        tg_file = await media.get_file()
        file_path = _build_media_artifact_path(
            media,
            tg_file,
            kind=kind,
            fallback_suffix=fallback_suffix,
        )
        suffix = file_path.suffix.lower()
        if suffix and not _media_suffix_allowed(suffix, kind=kind):
            return (
                None,
                f"⚠ Unsupported {kind} file extension: {suffix}. "
                "Send a standard audio/video file.",
            )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        await tg_file.download_to_drive(file_path)
    except Exception as exc:
        logger.warning("Failed to download Telegram %s artifact: %s", kind, exc)
        return None, f"⚠ Could not download {kind} artifact for Codex."

    warning = _post_download_media_warning(file_path, kind=kind)
    if warning:
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove rejected %s artifact %s", kind, file_path)
        return None, warning
    return file_path, None


def _format_media_metadata(prefix: str, media: Any, artifact_path: Path) -> str:
    parts: list[str] = []
    mime_type = _optional_str_attr(media, "mime_type")
    duration = _optional_int_attr(media, "duration")
    file_size = _optional_int_attr(media, "file_size")
    if mime_type:
        parts.append(f"mime={mime_type}")
    if duration is not None:
        parts.append(f"duration={duration}")
    if file_size is None:
        try:
            file_size = artifact_path.stat().st_size
        except OSError:
            file_size = None
    if file_size is not None:
        parts.append(f"size={file_size}")
    if not parts:
        parts.append("unavailable")
    return f"{prefix} metadata: " + ", ".join(parts)


def _build_audio_runtime_text(audio: Any, artifact_path: Path) -> str:
    return "\n".join(
        [
            "Audio artifact received.",
            f"Audio artifact: {artifact_path}",
            _format_media_metadata("Audio", audio, artifact_path),
            (
                "Transcript: unavailable (OpenAI API key not configured/invalid; "
                "local OSS ASR integration pending)"
            ),
        ]
    )


def _build_video_runtime_text(
    video: Any,
    artifact_path: Path,
    preview_path: Path | None,
    preview_warning: str | None,
) -> str:
    lines = [
        "Video artifact received.",
        f"Video artifact: {artifact_path}",
    ]
    if preview_path is not None:
        lines.append(f"Video thumbnail: (image attached: {preview_path})")
    else:
        lines.append(preview_warning or "Preview unavailable")
    lines.extend(
        [
            _format_media_metadata("Video", video, artifact_path),
            "Transcript: not attempted in MVP (local OSS video/audio extraction pending)",
        ]
    )
    return "\n".join(lines)


async def _send_attachment_runtime_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target: _AttachmentInputTarget,
    text_to_send: str,
    success_reply: str,
) -> None:
    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(target.user_id, target.thread_id)

    success, message = await session_manager.send_to_window(
        target.window_id,
        text_to_send,
    )
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                target.user_id,
                target.window_id,
                target.thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(update.message, success_reply)


async def _resolve_attachment_input_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    silent_unbound: bool = False,
) -> _AttachmentInputTarget | None:
    """Resolve a media attachment to the bound live runtime input target."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return None

    if not update.message:
        return None

    chat = update.message.chat
    control_surface = control_surface_classifier(update)
    thread_id = _get_thread_id(update)
    if (
        not silent_unbound
        and chat.type in ("group", "supergroup")
        and thread_id is not None
    ):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Current attachment path requires a resolved Telegram topic.
    if thread_id is None:
        if silent_unbound:
            return None
        await safe_reply(
            update.message,
            "❌ This build currently requires a Telegram topic. Create a named topic to start a session.",
        )
        return None

    wid, using_shared_group_binding = _get_writable_window_for_surface(
        context,
        user.id,
        control_surface,
    )
    if wid is None:
        if silent_unbound:
            return None
        await safe_reply(
            update.message,
            _build_unbound_input_message(user.id, control_surface),
        )
        return None

    if silent_unbound and chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        if silent_unbound:
            return None
        display = session_manager.get_display_name(wid)
        if not using_shared_group_binding:
            session_manager.unbind_thread(user.id, thread_id)
            await safe_reply(
                update.message,
                f"❌ Window '{display}' no longer exists. Binding removed.\n"
                "Send a message to start a new session.",
            )
        else:
            await safe_reply(
                update.message,
                f"❌ Shared window '{display}' no longer exists. Use /unbind, "
                "then start a new session.",
            )
        return None

    if await _surface_omx_question_state(
        context.bot,
        user.id,
        wid,
        control_surface.message_thread_id,
        reply_message=update.message,
        window=w,
    ):
        return None

    return _AttachmentInputTarget(
        user_id=user.id,
        thread_id=thread_id,
        window_id=wid,
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Codex."""
    if not update.message or not update.message.photo:
        return

    target = await _resolve_attachment_input_target(
        update,
        context,
        silent_unbound=True,
    )
    if target is None:
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
    clear_status_msg_info(target.user_id, target.thread_id)

    success, message = await session_manager.send_to_window(
        target.window_id,
        text_to_send,
    )
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                target.user_id,
                target.window_id,
                target.thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Codex.")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram documents: download and forward path to Codex."""
    if not update.message or not update.message.document:
        return

    target = await _resolve_attachment_input_target(update, context)
    if target is None:
        return

    document = update.message.document
    tg_file = await document.get_file()

    unique_id = _sanitize_attachment_filename(
        getattr(document, "file_unique_id", None),
        "document",
    )
    original_name = _sanitize_attachment_filename(
        getattr(document, "file_name", None),
        f"{unique_id}.bin",
    )
    filename = f"{int(time.time())}_{unique_id}_{original_name}"
    file_path = _DOCUMENTS_DIR / filename
    await tg_file.download_to_drive(file_path)

    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(document attached: {file_path})"
    else:
        text_to_send = f"(document attached: {file_path})"

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(target.user_id, target.thread_id)

    success, message = await session_manager.send_to_window(
        target.window_id,
        text_to_send,
    )
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                target.user_id,
                target.window_id,
                target.thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(update.message, "📎 Document sent to Codex.")


def _convert_attachment_image_to_png(source_path: Path, png_path: Path) -> None:
    """Convert a downloaded Telegram raster attachment to PNG."""
    from PIL import Image

    with Image.open(source_path) as image:
        image.convert("RGBA").save(png_path, format="PNG")


async def _download_attachment_image_as_png(
    file_source: Any,
    filename_stem: str,
) -> Path:
    """Download a Telegram image-like file and normalize it to PNG."""
    source_path = _IMAGES_DIR / f"{filename_stem}.webp"
    png_path = _IMAGES_DIR / f"{filename_stem}.png"
    tg_file = await file_source.get_file()
    await tg_file.download_to_drive(source_path)
    await asyncio.to_thread(
        _convert_attachment_image_to_png,
        source_path,
        png_path,
    )
    return png_path


def _telegram_file_suffix(tg_file: Any, fallback: str) -> str:
    """Infer a safe suffix from a Telegram File path."""
    file_path = getattr(tg_file, "file_path", None)
    suffix = Path(str(file_path or "")).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        return suffix
    return fallback


def _extract_video_preview_frame(source_path: Path, preview_path: Path) -> str | None:
    """Best-effort video preview extraction; returns a status on failure."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "ffmpeg not found"

    try:
        subprocess.run(  # nosec B603 - fixed argv, no shell, local sanitized paths
            [
                ffmpeg,
                "-y",
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                str(preview_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_VIDEO_PREVIEW_FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ffmpeg failed: {exc.__class__.__name__}"

    if not preview_path.exists() or preview_path.stat().st_size == 0:
        return "ffmpeg produced no preview"
    return None


async def _maybe_video_preview(
    video: Any,
    artifact_path: Path,
    filename_stem: str,
) -> tuple[Path | None, str | None]:
    """Create a best-effort image preview without gating video artifact delivery."""
    thumbnail = getattr(video, "thumbnail", None)
    if thumbnail is not None:
        thumbnail_id = _sanitize_attachment_filename(
            _optional_str_attr(thumbnail, "file_unique_id"),
            "thumbnail",
        )
        try:
            return (
                await _download_attachment_image_as_png(
                    thumbnail,
                    f"{filename_stem}_{thumbnail_id}_video_thumbnail",
                ),
                None,
            )
        except Exception as exc:
            logger.warning("Failed to normalize Telegram video thumbnail: %s", exc)

    preview_path = _IMAGES_DIR / f"{filename_stem}_video_preview.png"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    failure = await asyncio.to_thread(
        _extract_video_preview_frame,
        artifact_path,
        preview_path,
    )
    if failure:
        return None, f"Preview unavailable ({failure})"
    return preview_path, None


async def _download_sticker_original(
    sticker: Any,
    filename_stem: str,
    fallback_suffix: str,
) -> Path:
    """Download the original animated/video sticker artifact."""
    tg_file = await sticker.get_file()
    suffix = _telegram_file_suffix(tg_file, fallback_suffix)
    artifact_path = _IMAGES_DIR / f"{filename_stem}{suffix}"
    await tg_file.download_to_drive(artifact_path)
    return artifact_path


def _convert_video_sticker_to_gif(source_path: Path, gif_path: Path) -> str | None:
    """Best-effort video sticker GIF conversion; returns a status on failure."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "ffmpeg not found"

    try:
        subprocess.run(  # nosec B603 - fixed argv, no shell, local sanitized paths
            [
                ffmpeg,
                "-y",
                "-i",
                str(source_path),
                "-vf",
                "fps=15,scale=512:-1:flags=lanczos",
                str(gif_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_STICKER_GIF_FFMPEG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ffmpeg failed: {exc.__class__.__name__}"

    if not gif_path.exists() or gif_path.stat().st_size == 0:
        return "ffmpeg produced no GIF"
    return None


async def _maybe_video_sticker_gif(source_path: Path) -> tuple[Path | None, str | None]:
    """Create a GIF sibling for a video sticker when ffmpeg is available."""
    gif_path = source_path.with_suffix(".gif")
    failure = await asyncio.to_thread(
        _convert_video_sticker_to_gif,
        source_path,
        gif_path,
    )
    if failure:
        return None, failure
    return gif_path, None


async def _sticker_artifacts(sticker: Any) -> tuple[_StickerArtifacts | None, str | None]:
    """Persist a Telegram sticker without collapsing animation into image semantics."""
    unique_id = _sanitize_attachment_filename(
        getattr(sticker, "file_unique_id", None),
        "sticker",
    )
    timestamp = int(time.time())
    is_animated = bool(getattr(sticker, "is_animated", False))
    is_video = bool(getattr(sticker, "is_video", False))

    if is_animated or is_video:
        thumbnail = getattr(sticker, "thumbnail", None)
        if thumbnail is None:
            return (
                None,
                "⚠ Animated/video sticker has no thumbnail; cannot forward it as an image yet.",
            )
        thumbnail_id = _sanitize_attachment_filename(
            getattr(thumbnail, "file_unique_id", None),
            "thumbnail",
        )
        try:
            image_path = await _download_attachment_image_as_png(
                thumbnail,
                f"{timestamp}_{unique_id}_{thumbnail_id}_thumbnail",
            )
        except Exception as exc:
            logger.warning("Failed to normalize Telegram sticker thumbnail: %s", exc)
            return None, "⚠ Could not convert sticker thumbnail to an image for Codex."

        status_lines: list[str] = []
        original_path: Path | None = None
        gif_path: Path | None = None
        fallback_suffix = ".webm" if is_video else ".tgs"
        try:
            original_path = await _download_sticker_original(
                sticker,
                f"{timestamp}_{unique_id}_original",
                fallback_suffix,
            )
        except Exception as exc:
            logger.warning("Failed to download Telegram sticker animation artifact: %s", exc)
            status_lines.append(
                f"Sticker animation artifact: unavailable ({exc.__class__.__name__})"
            )

        if is_video and original_path is not None:
            gif_path, gif_failure = await _maybe_video_sticker_gif(original_path)
            if gif_failure:
                status_lines.append(f"Sticker animation GIF: unavailable ({gif_failure})")
        elif is_animated and original_path is not None:
            status_lines.append(
                "Sticker animation GIF: not generated for .tgs stickers"
            )

        return (
            _StickerArtifacts(
                image_path=image_path,
                image_label="thumbnail",
                original_path=original_path,
                gif_path=gif_path,
                status_lines=tuple(status_lines),
            ),
            None,
        )

    filename_stem = f"{timestamp}_{unique_id}"
    try:
        image_path = await _download_attachment_image_as_png(sticker, filename_stem)
        return _StickerArtifacts(image_path=image_path, image_label="image"), None
    except Exception as exc:
        logger.warning("Failed to normalize Telegram sticker as image: %s", exc)
        return None, "⚠ Could not convert sticker to an image for Codex."


def _build_sticker_runtime_text(sticker: Any, artifacts: _StickerArtifacts) -> str:
    """Build the runtime message while preserving the image attachment contract."""
    lines: list[str] = []
    emoji = getattr(sticker, "emoji", None)
    if isinstance(emoji, str) and emoji.strip():
        lines.append(f"Sticker emoji: {emoji.strip()}")
    lines.append(
        f"Sticker {artifacts.image_label}: (image attached: {artifacts.image_path})"
    )
    if artifacts.original_path is not None:
        lines.append(f"Sticker animation artifact: {artifacts.original_path}")
    if artifacts.gif_path is not None:
        lines.append(f"Sticker animation GIF: {artifacts.gif_path}")
    lines.extend(artifacts.status_lines)
    return "\n".join(lines)


async def sticker_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram stickers as image-equivalent runtime attachments."""
    if not update.message or not update.message.sticker:
        return

    target = await _resolve_attachment_input_target(
        update,
        context,
        silent_unbound=True,
    )
    if target is None:
        return

    sticker = update.message.sticker
    artifacts, warning = await _sticker_artifacts(sticker)
    if artifacts is None:
        await safe_reply(
            update.message,
            warning or "⚠ Could not forward sticker as an image.",
        )
        return

    text_to_send = _build_sticker_runtime_text(sticker, artifacts)

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(target.user_id, target.thread_id)

    success, message = await session_manager.send_to_window(
        target.window_id,
        text_to_send,
    )
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                target.user_id,
                target.window_id,
                target.thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(update.message, "🏷 Sticker sent to Codex as image.")


async def audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram audio as artifact-first runtime input."""
    if not update.message or not update.message.audio:
        return

    target = await _resolve_attachment_input_target(
        update,
        context,
        silent_unbound=True,
    )
    if target is None:
        return

    audio = update.message.audio
    artifact_path, warning = await _download_media_artifact(
        audio,
        kind="audio",
        fallback_suffix=".mp3",
    )
    if artifact_path is None:
        await safe_reply(
            update.message,
            warning or "⚠ Could not forward audio artifact to Codex.",
        )
        return

    await _send_attachment_runtime_text(
        update,
        context,
        target,
        _build_audio_runtime_text(audio, artifact_path),
        "🎧 Audio artifact sent to Codex.",
    )


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Telegram video as artifact-first runtime input."""
    if not update.message or not update.message.video:
        return

    target = await _resolve_attachment_input_target(
        update,
        context,
        silent_unbound=True,
    )
    if target is None:
        return

    video = update.message.video
    artifact_path, warning = await _download_media_artifact(
        video,
        kind="video",
        fallback_suffix=".mp4",
    )
    if artifact_path is None:
        await safe_reply(
            update.message,
            warning or "⚠ Could not forward video artifact to Codex.",
        )
        return

    filename_stem = artifact_path.stem
    preview_path, preview_warning = await _maybe_video_preview(
        video,
        artifact_path,
        filename_stem,
    )
    await _send_attachment_runtime_text(
        update,
        context,
        target,
        _build_video_runtime_text(
            video,
            artifact_path,
            preview_path,
            preview_warning,
        ),
        "🎬 Video artifact sent to Codex.",
    )


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
    control_surface = control_surface_classifier(update)
    thread_id = _get_thread_id(update)
    if chat.type in ("group", "supergroup") and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ This build currently requires a Telegram topic. Create a named topic to start a session.",
        )
        return

    wid, using_shared_group_binding = _get_writable_window_for_surface(
        context,
        user.id,
        control_surface,
    )
    if wid is None:
        await safe_reply(
            update.message,
            _build_unbound_input_message(user.id, control_surface),
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        if not using_shared_group_binding:
            session_manager.unbind_thread(user.id, thread_id)
            await safe_reply(
                update.message,
                f"❌ Window '{display}' no longer exists. Binding removed.\n"
                "Send a message to start a new session.",
            )
        else:
            await safe_reply(
                update.message,
                f"❌ Shared window '{display}' no longer exists. Use /unbind, "
                "then start a new session.",
            )
        return

    if await _surface_omx_question_state(
        context.bot,
        user.id,
        wid,
        control_surface.message_thread_id,
        reply_message=update.message,
        window=w,
    ):
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
    scope_id: int,
    *,
    window_id: str,
    window_name: str,
    selected_path: str,
    runtime_kind: str,
    surface: ControlSurface | None = None,
    resume_session_id: str | None = None,
    sync_topic_title: bool = True,
) -> bool:
    """Register a launched window, then bind the Telegram control surface to it."""
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

    if surface is None:
        surface = ControlSurface(
            kind="group_topic" if sync_topic_title else "group_main_chat",
            chat_id=None if sync_topic_title else scope_id,
            thread_id=scope_id if sync_topic_title else None,
            legacy_scope_id=scope_id,
            surface_key=(f"t:{scope_id}" if sync_topic_title else f"c:{scope_id}"),
            label="topic" if sync_topic_title else "chat",
            is_shared_group=True,
            supports_bind_flow=True,
        )
    _allow_implicit_bind(user.id, surface)
    session_manager.bind_surface(
        user.id,
        window_id,
        surface_key=surface.surface_key,
        window_name=window_name,
    )
    if not sync_topic_title:
        return True
    assert surface.message_thread_id is not None
    return await _sync_topic_title(
        context.bot,
        user.id,
        surface.message_thread_id,
        window_name,
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    surface = control_surface_classifier(update)
    thread_id = surface.message_thread_id

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = update.message.text

    # Ignore text in window picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_WINDOW:
        pending_surface = _pending_surface_from_user_data(context.user_data)
        if _same_surface(pending_surface, surface):
            await safe_reply(
                update.message,
                "Please use the window picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_window_picker_state(context.user_data)
        _set_pending_surface(context.user_data, None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_surface = _pending_surface_from_user_data(context.user_data)
        if _same_surface(pending_surface, surface):
            await safe_reply(
                update.message,
                "Please use the directory browser above, or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        clear_browse_state(context.user_data)
        _set_pending_surface(context.user_data, None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in thread-picker mode (only for the same topic)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_THREAD:
        pending_surface = _pending_surface_from_user_data(context.user_data)
        if _same_surface(pending_surface, surface):
            await safe_reply(
                update.message,
                "Please use the thread picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_thread_picker_state(context.user_data)
        _set_pending_surface(context.user_data, None)
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop("_selected_path", None)

    if not surface.supports_bind_flow:
        await safe_reply(
            update.message,
            _unsupported_surface_message(surface),
        )
        return

    wid, using_shared_group_binding = _get_writable_window_for_surface(
        context,
        user.id,
        surface,
    )
    if wid is None:
        binding_state = _get_surface_binding_state(user.id, surface)
        topic_policy = _get_surface_policy(user.id, surface)
        addressed_event = (
            AddressedEvent(is_addressed=False)
            if surface.is_shared_group
            else parse_addressed_event(update, context)
        )

        if binding_state == BINDING_STATE_BIND_FLOW:
            await safe_reply(update.message, BIND_FLOW_ACTIVE_MESSAGE)
            return

        if binding_state == BINDING_STATE_BOUND:
            logger.info(
                "Detected stale bound state without a live window, clearing state "
                "(user=%d, thread=%d)",
                user.id,
                surface.legacy_scope_id,
            )
            _set_surface_binding_state(user.id, surface, BINDING_STATE_NONE)
            binding_state = BINDING_STATE_NONE

        if surface.is_shared_group:
            return

        if topic_policy == TOPIC_POLICY_MANUAL_BIND_REQUIRED:
            await safe_reply(
                update.message,
                _build_manual_bind_required_message(_default_launch_runtime_kind()),
            )
            return

        logger.info(
            "Implicit bind allowed: showing bind flow (user=%d, scope=%s)",
            user.id,
            surface.surface_key or surface.legacy_scope_id,
        )
        pending_text = text
        explicit = False
        if addressed_event.is_addressed:
            explicit = True
            if addressed_event.is_bare:
                _clear_pending_slot(context, user.id, surface)
                pending_text = None
            else:
                pending_text = addressed_event.payload_text
        await _start_bind_flow(
            update,
            context,
            user,
            surface,
            explicit=explicit,
            pending_text=pending_text,
        )
        return

    if session_manager.is_external_binding_window_id(wid) is True:
        success, message = await session_manager.send_to_window(wid, text)
        if success:
            return
        external = _surface_external_binding(user.id, surface) or {}
        source_thread_id = str(external.get("source_thread_id") or "").strip()
        runtime_kind = str(external.get("runtime_kind") or "codex").strip() or "codex"
        hint_lines = [f"⚠️ {message}"]
        if source_thread_id:
            hint_lines.append(f"• bound {runtime_kind} thread: `{source_thread_id}`")
        hint_lines.append(
            "Use `/unbind` then `/resume <thread-name|id>` (or `/bind`) to switch to writable live control."
        )
        await safe_reply(update.message, "\n".join(hint_lines))
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d, shared=%s)",
            display,
            user.id,
            surface.legacy_scope_id,
            using_shared_group_binding,
        )
        if not using_shared_group_binding:
            session_manager.unbind_surface(
                user.id,
                surface_key=surface.surface_key,
            )
        if using_shared_group_binding:
            await safe_reply(
                update.message,
                f"❌ Shared window '{display}' no longer exists. Use /unbind, "
                "then start a new session.",
            )
        else:
            await safe_reply(
                update.message,
                f"❌ Window '{display}' no longer exists. Binding removed.\n"
                "Send a message to start a new session.",
            )
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    await enqueue_status_update(
        context.bot,
        user.id,
        wid,
        None,
        thread_id=surface.message_thread_id,
    )

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, surface.legacy_scope_id)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    pane_text = await tmux_manager.capture_pane(w.window_id)
    input_surface = classify_input_surface(pane_text) if pane_text else None
    if input_surface and input_surface.kind == "blocked_prompt":
        logger.info(
            "Detected blocked prompt before sending text (user=%d, thread=%s)",
            user.id,
            surface.legacy_scope_id,
        )
        await _surface_blocked_prompt_state(
            context.bot,
            user.id,
            wid,
            surface.message_thread_id,
            reply_message=update.message,
        )
        return

    if await _surface_omx_question_state(
        context.bot,
        user.id,
        wid,
        surface.message_thread_id,
        reply_message=update.message,
        window=w,
    ):
        return

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        if message == BLOCKED_PROMPT_SEND_MESSAGE:
            await _surface_blocked_prompt_state(
                context.bot,
                user.id,
                wid,
                surface.message_thread_id,
                reply_message=update.message,
            )
            return
        await safe_reply(update.message, f"❌ {message}")
        return

    if (
        _get_window_runtime_kind(wid) == "codex"
        and input_surface
        and input_surface.kind in {"busy", "input_ready", "blocked_prompt"}
    ):
        mark_runtime_presence_active(user.id, surface.message_thread_id, wid)

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(
                context.bot,
                user.id,
                surface.legacy_scope_id,
                wid,
                bash_cmd,
            )
        )
        _bash_capture_tasks[(user.id, surface.legacy_scope_id)] = task

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
    pending_surface: ControlSurface | None = None,
    runtime_kind: str | None = None,
    launch_command: str | None = None,
    reuse_existing: bool = False,
) -> None:
    """Create a tmux window, bind it to a surface, and forward pending text.

    Shared by directory-confirm, fresh-thread, and thread-resume actions.
    """
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)
    pending_surface = pending_surface or _pending_surface_from_user_data(
        context.user_data
    )

    resolved_runtime_kind = runtime_kind or _default_launch_runtime_kind()
    if reuse_existing:
        (
            success,
            message,
            created_wname,
            created_wid,
            _reused_existing,
        ) = await tmux_manager.create_or_reuse_window(
            selected_path,
            start_claude=True,
            resume_session_id=resume_session_id,
            runtime_kind=resolved_runtime_kind,
            launch_command=launch_command,
            reuse_existing=True,
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
                surface=pending_surface,
                resume_session_id=resume_session_id,
                sync_topic_title=(
                    pending_surface.message_thread_id is not None
                    if pending_surface is not None
                    else True
                ),
            )
            resolved_chat = session_manager.resolve_chat_id(user.id, pending_thread_id)

            status = "Resumed thread" if resume_session_id else "Started fresh thread"
            response_lines = [f"✅ {message}", "", f"{status}. Send messages here."]
            if (
                pending_surface is not None
                and pending_surface.message_thread_id is not None
                and not topic_synced
            ):
                response_lines.append(
                    f"⚠️ Telegram topic title could not be synced; tmux window is '{created_wname}'."
                )
            await safe_edit(query, "\n".join(response_lines))
            if pending_surface is not None:
                await _maybe_autosend_pending_after_activation(
                    context,
                    user=user,
                    surface=pending_surface,
                    window_id=created_wid,
                    chat_id=resolved_chat,
                )
            elif context.user_data is not None:
                _set_pending_surface(context.user_data, None)
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            _set_pending_surface(context.user_data, None)
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
    if await handle_omx_question_callback(update, context):
        return
    current_surface = control_surface_classifier(update)

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
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
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
            user.id, pending_surface
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
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
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
            user.id, pending_surface
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
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
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
            user.id, pending_surface
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
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return

        # Validate: confirm button must come from the same topic that started browsing
        if not _callback_matches_pending_surface(update, context, pending_surface):
            clear_browse_state(context.user_data)
            if context.user_data is not None:
                _set_pending_surface(context.user_data, None)
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
                user.id, pending_surface
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
            query,
            context,
            user,
            selected_path,
            pending_thread_id,
            pending_surface=pending_surface,
        )

    elif data == CB_DIR_CANCEL:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
            await query.answer("Stale browser (topic mismatch)", show_alert=True)
            return
        active_surface = pending_surface or current_surface
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            _set_pending_surface(context.user_data, None)
            context.user_data.pop("_pending_thread_text", None)
        if active_surface is not None:
            _clear_pending_slot(context, user.id, active_surface)
            _require_manual_bind(user.id, active_surface)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Thread picker: resume an existing persisted thread.
    elif data.startswith(CB_THREAD_SELECT):
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
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
            pending_surface=pending_surface,
            resume_session_id=thread.thread_id,
        )

    elif data == CB_THREAD_NEW:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
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

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            pending_surface=pending_surface,
        )

    elif data == CB_THREAD_CANCEL:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        active_surface = pending_surface or current_surface
        clear_thread_picker_state(context.user_data)
        if context.user_data is not None:
            _set_pending_surface(context.user_data, None)
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_selected_path", None)
        if active_surface is not None:
            _clear_pending_slot(context, user.id, active_surface)
            _require_manual_bind(user.id, active_surface)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
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

        if _is_non_bindable_codex_helper_window(selected_wid):
            await query.answer(
                "This Codex subagent/helper window belongs to its parent session "
                "and cannot be bound directly.",
                show_alert=True,
            )
            return

        if pending_surface is None:
            await query.answer("Stale bind flow, use /bind again", show_alert=True)
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
        if runtime_kind is None:
            await query.answer(
                f"Window '{display}' is not running a known runtime",
                show_alert=True,
            )
            return
        await _register_bound_window(
            context,
            user,
            pending_surface.legacy_scope_id,
            window_id=selected_wid,
            window_name=display,
            selected_path=w.cwd,
            runtime_kind=runtime_kind,
            surface=pending_surface,
            sync_topic_title=pending_surface.message_thread_id is not None,
        )

        await safe_edit(
            query,
            f"✅ Bound this {_surface_subject(pending_surface)} to live window `{display}`",
        )
        await _maybe_autosend_pending_after_activation(
            context,
            user=user,
            surface=pending_surface,
            window_id=selected_wid,
        )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        pending_tid = _resolve_bind_flow_callback_thread_id(update, context)
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        start_path = str(Path.cwd())
        current_version, current_nonce = _get_current_bind_flow_credentials(
            user.id, pending_surface
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
        pending_surface = _resolve_bind_flow_callback_surface(update, context)
        if not await _validate_bind_flow_callback(
            query,
            user_id=user.id,
            surface=pending_surface,
            version=bind_flow_version,
            nonce=bind_flow_nonce,
        ):
            return
        if not _callback_matches_pending_surface(update, context, pending_surface):
            await query.answer("Stale picker (topic mismatch)", show_alert=True)
            return
        active_surface = pending_surface or current_surface
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            _set_pending_surface(context.user_data, None)
            context.user_data.pop("_pending_thread_text", None)
        if active_surface is not None:
            _clear_pending_slot(context, user.id, active_surface)
            _require_manual_bind(user.id, active_surface)
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
    is_turn_started_lifecycle = (
        msg.semantic_kind == LIFECYCLE_SEMANTIC_KIND
        and msg.text.strip() == "turn_started"
    )

    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    if (
        msg.runtime_kind == "codex"
        and msg.role == "assistant"
        and msg.is_complete
        and is_codex_termination_summary_text(msg.text)
    ):
        logger.info(
            "Suppressing direct Telegram delivery of Codex termination summary for session %s; "
            "status polling will re-deliver it as a discontinuity warning.",
            msg.session_id,
        )
        return

    # Find users whose thread-bound window matches this session
    active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        logger.info(f"No active users for session {msg.session_id}")
        return

    for user_id, wid, thread_id in active_users:
        if msg.runtime_kind == "codex":
            mark_runtime_presence_active(user_id, thread_id, wid)

        turn_generation = current_turn_generation(user_id, thread_id)
        if opens_new_turn:
            await flush_terminal_artifacts_before_new_turn(bot, user_id, thread_id)
            turn_generation = open_new_turn_generation(user_id, thread_id)
        elif is_turn_started_lifecycle and is_pre_final_visible_lane_closed(
            user_id, thread_id
        ):
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
        if not config.show_tool_calls and msg.content_type in (
            "tool_use",
            "tool_result",
        ):
            continue

        if msg.status_message_eligible and (
            not msg.is_complete or msg.content_type == "tool_progress"
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
            and msg.semantic_kind == PLAN_UPDATE_SEMANTIC_KIND
        ):
            if is_pre_final_visible_lane_closed(user_id, thread_id):
                logger.debug(
                    "Skipping post-final plan update artifact: user=%d thread=%s",
                    user_id,
                    thread_id,
                )
                continue
            await enqueue_plan_update(
                bot,
                user_id,
                wid,
                msg.text,
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

        if (
            config.telegram_delivery_mode == "compact"
            and msg.is_complete
            and msg.content_type in {"commentary", "orchestration"}
        ):
            if is_pre_final_visible_lane_closed(user_id, thread_id):
                logger.debug(
                    "Skipping post-final pre-final visible artifact: user=%d thread=%s content_type=%s",
                    user_id,
                    thread_id,
                    msg.content_type,
                )
                continue
            commentary_parts = build_commentary_parts(
                msg.text,
                content_type=msg.content_type,
                role=msg.role,
            )
            commentary_text = "\n\n".join(commentary_parts)
            await enqueue_commentary_update(
                bot,
                user_id,
                wid,
                commentary_text,
                parts=commentary_parts,
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
                document_data=msg.document_data,
                turn_generation=turn_generation,
            )

            if msg.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND:
                queue = get_message_queue(user_id)
                if queue is not None:
                    await queue.join()

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
    builder = Application.builder().token(config.telegram_bot_token).rate_limiter(
        AIORateLimiter(max_retries=5)
    )
    telegram_proxy = _telegram_proxy_from_env()
    if telegram_proxy:
        builder = builder.proxy(telegram_proxy).get_updates_proxy(telegram_proxy)

    application = builder.post_init(post_init).post_shutdown(post_shutdown).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
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
    # Documents/files: download and forward file path to Codex
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    # Stickers: normalize static stickers/thumbnails to image attachments
    application.add_handler(MessageHandler(filters.Sticker.ALL, sticker_handler))
    # Audio: save original artifact and forward path/metadata to Codex
    application.add_handler(MessageHandler(filters.AUDIO, audio_handler))
    # Video: save original artifact and optional preview path to Codex
    application.add_handler(MessageHandler(filters.VIDEO, video_handler))
    # Voice: transcribe via OpenAI and forward text to Codex
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: unsupported non-text content (video notes, animations, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
