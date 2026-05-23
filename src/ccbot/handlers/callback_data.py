"""Callback data constants for Telegram inline keyboards.

Defines all CB_* prefixes used for routing callback queries in the bot.
Each prefix identifies a specific action or navigation target.

Constants:
  - CB_HISTORY_*: History pagination
  - CB_DIR_*: Directory browser navigation
  - CB_WIN_*: Window picker (bind existing unbound window)
  - CB_SCREENSHOT_*: Screenshot refresh
  - CB_ASK_*: Interactive UI navigation (arrows, enter, esc)
  - CB_KEYS_PREFIX: Screenshot control keys (kb:<key_id>:<window>)
"""

# History pagination
CB_HISTORY_PREV = "hp:"  # history page older
CB_HISTORY_NEXT = "hn:"  # history page newer

# Directory browser
CB_DIR_SELECT = "db:sel:"
CB_DIR_UP = "db:up"
CB_DIR_CONFIRM = "db:confirm"
CB_DIR_CANCEL = "db:cancel"
CB_DIR_PAGE = "db:page:"

# Window picker (bind existing unbound window)
CB_WIN_BIND = "wb:sel:"  # wb:sel:<index>
CB_WIN_NEW = "wb:new"  # proceed to directory browser
CB_WIN_CANCEL = "wb:cancel"

# Screenshot
CB_SCREENSHOT_REFRESH = "ss:ref:"

# Interactive UI (aq: prefix kept for backward compatibility)
CB_ASK_UP = "aq:up:"  # aq:up:<window>
CB_ASK_DOWN = "aq:down:"  # aq:down:<window>
CB_ASK_LEFT = "aq:left:"  # aq:left:<window>
CB_ASK_RIGHT = "aq:right:"  # aq:right:<window>
CB_ASK_ESC = "aq:esc:"  # aq:esc:<window>
CB_ASK_ENTER = "aq:enter:"  # aq:enter:<window>
CB_ASK_SPACE = "aq:spc:"  # aq:spc:<window>
CB_ASK_TAB = "aq:tab:"  # aq:tab:<window>
CB_ASK_REFRESH = "aq:ref:"  # aq:ref:<window>

# OMX durable question records
CB_OMX_QUESTION_SELECT = "oq:sel"  # oq:sel:<index>:<question-suffix>:<window>
CB_OMX_QUESTION_TOGGLE = "oq:tog"  # oq:tog:<index>:<question-suffix>:<window>
CB_OMX_QUESTION_SUBMIT = "oq:sub"  # oq:sub:<question-suffix>:<window>
CB_OMX_QUESTION_REFRESH = "oq:ref"  # oq:ref:<question-suffix>:<window>

# Thread picker (resume existing persisted thread)
CB_THREAD_SELECT = "rs:sel:"  # rs:sel:<index>
CB_THREAD_NEW = "rs:new"  # start a fresh thread
CB_THREAD_CANCEL = "rs:cancel"  # cancel

# Backward-compatible aliases for older call sites and callback payloads.
CB_SESSION_SELECT = CB_THREAD_SELECT
CB_SESSION_NEW = CB_THREAD_NEW
CB_SESSION_CANCEL = CB_THREAD_CANCEL

# Bind-flow token suffix for restart-safe picker invalidation.
CB_BIND_FLOW_SUFFIX = ":bf:"

# Screenshot control keys
CB_KEYS_PREFIX = "kb:"  # kb:<key_id>:<window>


def append_bind_flow_token(callback_data: str, *, version: int, nonce: str) -> str:
    """Attach bind-flow credentials to a callback payload.

    The credentials let the bot invalidate stale picker callbacks after
    restart, explicit /unbind, or a superseding bind flow.
    """
    if version <= 0 or not nonce:
        return callback_data
    payload = f"{callback_data}{CB_BIND_FLOW_SUFFIX}{version}:{nonce}"
    if len(payload) > 64:
        raise ValueError(f"Callback payload exceeds Telegram limit: {payload!r}")
    return payload


def split_bind_flow_token(callback_data: str) -> tuple[str, int, str]:
    """Extract bind-flow credentials from a callback payload.

    Returns ``(base_callback, version, nonce)``. Legacy payloads without a
    token are treated as stale and return ``version=0`` / ``nonce=''``.
    """
    base, marker, tail = callback_data.rpartition(CB_BIND_FLOW_SUFFIX)
    if not marker:
        return callback_data, 0, ""
    version_str, sep, nonce = tail.partition(":")
    if not sep or not nonce:
        return callback_data, 0, ""
    try:
        version = int(version_str)
    except ValueError:
        return callback_data, 0, ""
    return base, version, nonce
