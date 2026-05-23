"""Rate-limited Telegram typing indicators for runtime update delivery."""

from __future__ import annotations

import logging
import time

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from .delivery_audit import log_telegram_delivery

logger = logging.getLogger(__name__)

RUNTIME_UPDATE_TYPING_THROTTLE_SECONDS = 3.0
_runtime_update_typing_last_sent: dict[tuple[int, int | None], float] = {}


def clear_runtime_update_typing_state() -> None:
    _runtime_update_typing_last_sent.clear()


def _runtime_update_typing_key(
    *,
    chat_id: int,
    thread_id: int | None,
) -> tuple[int, int | None]:
    return (chat_id, thread_id)


async def send_runtime_update_typing_once(
    bot: Bot,
    user_id: int,
    *,
    chat_id: int,
    thread_id: int | None,
    surface_key: str | None = None,
    window_id: str | None = None,
) -> bool:
    """Send a throttled Telegram typing action for runtime-originated updates.

    Throttling is scoped to the effective Telegram delivery surface (chat/topic),
    not the owner user id, so shared group surfaces cannot multiply request rate.
    """
    now = time.monotonic()
    key = _runtime_update_typing_key(
        chat_id=chat_id,
        thread_id=thread_id,
    )
    last_sent = _runtime_update_typing_last_sent.get(key)
    if (
        last_sent is not None
        and now - last_sent < RUNTIME_UPDATE_TYPING_THROTTLE_SECONDS
    ):
        return False
    # Reserve the throttle slot before the network call. This keeps repeated
    # transport failures/timeouts from producing one Bot API attempt per update.
    _runtime_update_typing_last_sent[key] = now
    try:
        kwargs: dict[str, object] = {}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        await bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.TYPING,
            **kwargs,
        )
        log_telegram_delivery(
            action="runtime_update_typing",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            task_type="telegram_transport",
            content_type="chat_action",
            semantic_kind="typing_sent",
            text="typing",
        )
        return True
    except RetryAfter:
        logger.debug("Runtime update typing was rate-limited by Telegram", exc_info=True)
        return False
    except Exception:
        logger.debug("Failed to send runtime update typing indicator", exc_info=True)
        return False
