"""Directory browser and window picker UI for thread creation and resume.

Provides UIs in Telegram for:
  - Window picker: list unbound tmux windows for quick binding
  - Directory browser: navigate directory hierarchies to create new threads

Key components:
  - DIRS_PER_PAGE: Number of directories shown per page
  - User state keys for tracking browse/picker session
  - build_window_picker: Build unbound window picker UI
  - build_directory_browser: Build directory browser UI
  - clear_window_picker_state: Clear picker state from user_data
  - clear_browse_state: Clear browsing state from user_data
"""

import os
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..runtime_types import ThreadLocator

from ..config import config
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_THREAD_CANCEL,
    CB_THREAD_NEW,
    CB_THREAD_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
    append_bind_flow_token,
)

# Directories per page in directory browser
DIRS_PER_PAGE = 6

# User state keys
STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"  # Cache of subdirs for current path
BROWSE_SURFACE_SLOTS_KEY = "browse_surface_slots"
UNBOUND_WINDOWS_KEY = "unbound_windows"  # Cache of (name, cwd) tuples
STATE_SELECTING_THREAD = "selecting_thread"
THREADS_KEY = "cached_threads"  # Cache of ThreadLocator list

# Backward-compatible aliases while the rest of the bot is migrated.
STATE_SELECTING_SESSION = STATE_SELECTING_THREAD
SESSIONS_KEY = THREADS_KEY


def clear_browse_state(user_data: dict | None) -> None:
    """Clear directory browsing state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)


def default_browse_root() -> str:
    """Return a cwd-neutral default root for the bind directory browser."""
    for env_name in (
        "CCBOT_BIND_DEFAULT_ROOT",
        "CCBOT_WORKSPACE_ROOT",
    ):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.exists() and path.is_dir():
            return str(path.resolve())
    home_tools = Path("/home/tools")
    if home_tools.exists() and home_tools.is_dir():
        return str(home_tools)
    return str(Path.home())


def clear_window_picker_state(user_data: dict | None) -> None:
    """Clear window picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(UNBOUND_WINDOWS_KEY, None)


def clear_thread_picker_state(user_data: dict | None) -> None:
    """Clear thread picker state keys from user_data."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(THREADS_KEY, None)


def clear_session_picker_state(user_data: dict | None) -> None:
    """Backward-compatible alias for clear_thread_picker_state()."""
    clear_thread_picker_state(user_data)


def build_window_picker(
    windows: list[tuple[str, str, str]],
    *,
    bind_flow_version: int = 0,
    bind_flow_nonce: str = "",
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build window picker UI for unbound tmux windows.

    Args:
        windows: List of (window_id, window_name, cwd) tuples.
        bind_flow_version: Bind-flow credential version embedded in callbacks
            so stale picker actions cannot mutate a newer bind flow.
        bind_flow_nonce: Bind-flow credential nonce embedded in callbacks so
            stale picker actions cannot mutate a newer bind flow.

    Returns: (text, keyboard, window_ids) where window_ids is the ordered list for caching.
    """
    window_ids = [wid for wid, _, _ in windows]

    lines = [
        "*Bind to Existing Window*\n",
        "These windows are running but not bound to any topic.",
        "Pick one to attach it here, or start a fresh thread.\n",
    ]
    for _wid, name, cwd in windows:
        display_cwd = cwd.replace(str(Path.home()), "~")
        lines.append(f"• `{name}` — {display_cwd}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(windows), 2):
        row = []
        for j in range(min(2, len(windows) - i)):
            name = windows[i + j][1]
            display = name[:12] + "…" if len(name) > 13 else name
            row.append(
                InlineKeyboardButton(
                    f"🖥 {display}",
                    callback_data=append_bind_flow_token(
                        f"{CB_WIN_BIND}{i + j}",
                        version=bind_flow_version,
                        nonce=bind_flow_nonce,
                    ),
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(
                "➕ New Thread",
                callback_data=append_bind_flow_token(
                    CB_WIN_NEW,
                    version=bind_flow_version,
                    nonce=bind_flow_nonce,
                ),
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=append_bind_flow_token(
                    CB_WIN_CANCEL,
                    version=bind_flow_version,
                    nonce=bind_flow_nonce,
                ),
            ),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons), window_ids


def build_directory_browser(
    current_path: str,
    page: int = 0,
    *,
    bind_flow_version: int = 0,
    bind_flow_nonce: str = "",
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build directory browser UI.

    Returns: (text, keyboard, subdirs) where subdirs is the full list for caching.
    """
    path = Path(current_path).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        path = Path(default_browse_root())

    try:
        subdirs = sorted(
            [
                d.name
                for d in path.iterdir()
                if d.is_dir()
                and (config.show_hidden_dirs or not d.name.startswith("."))
            ]
        )
    except (PermissionError, OSError):
        subdirs = []

    total_pages = max(1, (len(subdirs) + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * DIRS_PER_PAGE
    page_dirs = subdirs[start : start + DIRS_PER_PAGE]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_dirs), 2):
        row = []
        for j, name in enumerate(page_dirs[i : i + 2]):
            display = name[:12] + "…" if len(name) > 13 else name
            # Use global index (start + i + j) to avoid long dir names in callback_data
            idx = start + i + j
            row.append(
                InlineKeyboardButton(
                    f"📁 {display}",
                    callback_data=append_bind_flow_token(
                        f"{CB_DIR_SELECT}{idx}",
                        version=bind_flow_version,
                        nonce=bind_flow_nonce,
                    ),
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀",
                    callback_data=append_bind_flow_token(
                        f"{CB_DIR_PAGE}{page - 1}",
                        version=bind_flow_version,
                        nonce=bind_flow_nonce,
                    ),
                )
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    "▶",
                    callback_data=append_bind_flow_token(
                        f"{CB_DIR_PAGE}{page + 1}",
                        version=bind_flow_version,
                        nonce=bind_flow_nonce,
                    ),
                )
            )
        buttons.append(nav)

    action_row: list[InlineKeyboardButton] = []
    # Allow going up unless at filesystem root
    if path != path.parent:
        action_row.append(
            InlineKeyboardButton(
                "..",
                callback_data=append_bind_flow_token(
                    CB_DIR_UP,
                    version=bind_flow_version,
                    nonce=bind_flow_nonce,
                ),
            )
        )
    action_row.append(
        InlineKeyboardButton(
            "Select",
            callback_data=append_bind_flow_token(
                CB_DIR_CONFIRM,
                version=bind_flow_version,
                nonce=bind_flow_nonce,
            ),
        )
    )
    action_row.append(
        InlineKeyboardButton(
            "Cancel",
            callback_data=append_bind_flow_token(
                CB_DIR_CANCEL,
                version=bind_flow_version,
                nonce=bind_flow_nonce,
            ),
        )
    )
    buttons.append(action_row)

    display_path = str(path).replace(str(Path.home()), "~")
    if not subdirs:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\n_(No subdirectories)_"
    else:
        text = f"*Select Working Directory*\n\nCurrent: `{display_path}`\n\nTap a folder to enter, or select current directory"

    return text, InlineKeyboardMarkup(buttons), subdirs


def _relative_time(file_path: str, activity_timestamp: float = 0.0) -> str:
    """Format activity time as a human-readable relative time string."""
    if activity_timestamp:
        mtime = activity_timestamp
    else:
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return ""
    delta = int(time.time() - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    return f"{d}d ago"


def build_thread_picker(
    threads: list[ThreadLocator],
    *,
    bind_flow_version: int = 0,
    bind_flow_nonce: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    """Build thread picker UI for resuming an existing persisted thread.

    Args:
        threads: List of ThreadLocator objects (sorted by recency).
        bind_flow_version: Bind-flow credential version embedded in callbacks
            so stale picker actions cannot mutate a newer bind flow.
        bind_flow_nonce: Bind-flow credential nonce embedded in callbacks so
            stale picker actions cannot mutate a newer bind flow.

    Returns: (text, keyboard).
    """
    lines = [
        "*Resume Existing Thread?*\n",
        "Persisted threads were found in this directory.\n",
    ]
    for i, thread in enumerate(threads):
        summary = (
            thread.summary[:40] + "…" if len(thread.summary) > 40 else thread.summary
        )
        rel = _relative_time(thread.file_path, getattr(thread, "activity_timestamp", 0.0))
        time_str = f" ({rel})" if rel else ""
        lines.append(f"{i + 1}. {summary} — {thread.message_count} messages{time_str}")

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(threads), 2):
        row = []
        for j in range(min(2, len(threads) - i)):
            thread = threads[i + j]
            label = (
                thread.summary[:14] + "…"
                if len(thread.summary) > 14
                else thread.summary
            )
            row.append(
                InlineKeyboardButton(
                    f"↺ {label}",
                    callback_data=append_bind_flow_token(
                        f"{CB_THREAD_SELECT}{i + j}",
                        version=bind_flow_version,
                        nonce=bind_flow_nonce,
                    ),
                )
            )
        buttons.append(row)

    buttons.append(
        [
            InlineKeyboardButton(
                "➕ Fresh Thread",
                callback_data=append_bind_flow_token(
                    CB_THREAD_NEW,
                    version=bind_flow_version,
                    nonce=bind_flow_nonce,
                ),
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=append_bind_flow_token(
                    CB_THREAD_CANCEL,
                    version=bind_flow_version,
                    nonce=bind_flow_nonce,
                ),
            ),
        ]
    )

    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons)


def build_session_picker(
    sessions: list[ThreadLocator],
    *,
    bind_flow_version: int = 0,
    bind_flow_nonce: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    """Backward-compatible alias for build_thread_picker()."""
    return build_thread_picker(
        sessions,
        bind_flow_version=bind_flow_version,
        bind_flow_nonce=bind_flow_nonce,
    )
