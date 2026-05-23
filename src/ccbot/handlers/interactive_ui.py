"""Interactive UI handling for Claude Code prompts.

Handles interactive terminal UIs displayed by Claude Code:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by canonical delivery surface when available.
"""

import logging
from itertools import islice

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..session import session_manager
from ..terminal_parser import (
    classify_input_surface,
    extract_interactive_content,
)
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .message_sender import NO_LINK_PREVIEW

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
INTERACTIVE_TOOL_NAMES = frozenset({"AskUserQuestion", "ExitPlanMode"})

InteractiveKey = tuple[int, int | str]


def _interactive_token(
    thread_id: int | None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> int | str:
    if surface_key:
        return f"surface:{surface_key}"
    if chat_id is not None:
        if thread_id is not None:
            return f"topic:{chat_id}:{thread_id}"
        return f"chat:{chat_id}"
    return thread_id or 0


def _interactive_key(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> InteractiveKey:
    return (
        user_id,
        _interactive_token(thread_id, chat_id=chat_id, surface_key=surface_key),
    )


# Track interactive UI message IDs: delivery surface -> message_id
_interactive_msgs: dict[InteractiveKey, int] = {}

# Track interactive mode: delivery surface -> window_id
_interactive_mode: dict[InteractiveKey, str] = {}

READ_ONLY_PROMPT_NOTE = (
    "Remote controls are disabled for this prompt in the core lane."
)

VERTICAL_PROMPTS = frozenset(
    {
        "CodexExecApproval",
        "CodexPatchApproval",
        "CodexPermissionsPopup",
        "CodexModelPicker",
        "CodexReasoningPicker",
        "CodexTrustPrompt",
    }
)

TAB_SPACE_PROMPTS = frozenset({"Settings"})


def get_interactive_window(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get(
        _interactive_key(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )
    )


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[
        _interactive_key(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )
    ] = window_id


def clear_interactive_mode(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop(
        _interactive_key(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        ),
        None,
    )


def get_interactive_msg_id(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get(
        _interactive_key(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )
    )


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    ``ui_name`` controls the layout: ``RestoreCheckpoint`` omits ←/→ keys
    since only vertical selection is needed.
    """
    vertical_only = ui_name == "RestoreCheckpoint" or ui_name in VERTICAL_PROMPTS
    include_tab_space = ui_name in TAB_SPACE_PROMPTS

    rows: list[list[InlineKeyboardButton]] = []
    # Row 1: primary navigation keys
    if include_tab_space:
        rows.append(
            [
                InlineKeyboardButton(
                    "␣ Space", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "⇥ Tab", callback_data=f"{CB_ASK_TAB}{window_id}"[:64]
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]
                ),
            ]
        )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "🔄", callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "⏎ Enter", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _build_prompt_message(
    pane_text: str, surface_name: str, *, read_only: bool = True
) -> str:
    """Build a prompt snapshot for Telegram."""
    content = extract_interactive_content(pane_text)
    if content and content.content.strip():
        body = content.content.strip()
    else:
        visible_lines = [line.rstrip() for line in pane_text.splitlines() if line.strip()]
        body = "\n".join(list(islice(visible_lines[-10:], 10))).strip()
    header = f"⚠️ {surface_name or 'Prompt'} detected"
    if read_only:
        return f"{header}\n\n{body}\n\n{READ_ONLY_PROMPT_NOTE}".strip()
    return f"{header}\n\n{body}".strip()


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.
    """
    ikey = _interactive_key(
        user_id,
        thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return False

    # Capture plain text (no ANSI colors)
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        logger.debug("No pane text captured for window_id %s", window_id)
        return False

    surface = classify_input_surface(pane_text)
    if surface.kind != "blocked_prompt":
        logger.debug(
            "No blocked prompt detected in window_id %s (surface=%s)",
            window_id,
            surface.kind,
        )
        return False

    keyboard = (
        _build_interactive_keyboard(window_id, ui_name=surface.prompt_name)
        if surface.allows_remote_actions
        else None
    )
    text = _build_prompt_message(
        pane_text,
        surface.prompt_name,
        read_only=not surface.allows_remote_actions,
    )

    # Build thread kwargs for send_message
    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    # Check if we have an existing interactive message to edit
    existing_msg_id = _interactive_msgs.get(ikey)
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            _interactive_mode[ikey] = window_id
            return True
        except Exception:
            # Edit failed (message deleted, etc.) - clear stale msg_id and send new
            logger.debug(
                "Edit failed for interactive msg %s, sending new", existing_msg_id
            )
            _interactive_msgs.pop(ikey, None)
            # Fall through to send new message

    # Send new message (plain text — terminal content is not markdown)
    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as e:
        logger.error("Failed to send interactive UI: %s", e)
        return False
    if sent:
        _interactive_msgs[ikey] = sent.message_id
        _interactive_mode[ikey] = window_id
        return True
    return False


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = _interactive_key(
        user_id,
        thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if bot and msg_id:
        if chat_id is None:
            chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # Message may already be deleted or too old
