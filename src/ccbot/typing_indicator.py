"""Rate-limited Telegram typing indicators for runtime update delivery."""

from __future__ import annotations

import logging
import time

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TimedOut

from .delivery_audit import log_telegram_delivery

logger = logging.getLogger(__name__)

RUNTIME_UPDATE_TYPING_THROTTLE_SECONDS = 3.0
RUNTIME_UPDATE_TYPING_DEGRADED_COOLDOWN_SECONDS = 15.0
_runtime_update_typing_last_sent: dict[tuple[int, int | None], float] = {}
_runtime_update_typing_chat_last_sent: dict[int, float] = {}
_runtime_update_typing_backpressure_until: dict[int, tuple[float, str]] = {}


def clear_runtime_update_typing_state() -> None:
    _runtime_update_typing_last_sent.clear()
    _runtime_update_typing_chat_last_sent.clear()
    _runtime_update_typing_backpressure_until.clear()


def _runtime_update_typing_key(
    *,
    chat_id: int,
    thread_id: int | None,
) -> tuple[int, int | None]:
    return (chat_id, thread_id)


def _retry_after_seconds(exc: RetryAfter) -> int:
    retry_after = exc.retry_after
    seconds = (
        retry_after
        if isinstance(retry_after, int | float)
        else int(retry_after.total_seconds())
    )
    return max(1, int(seconds))


def record_runtime_update_backpressure(
    chat_id: int,
    *,
    seconds: float,
    reason: str,
) -> None:
    """Record chat-level Telegram transport backpressure for typing/status probes."""
    until = time.monotonic() + max(1.0, seconds)
    _runtime_update_typing_backpressure_until[chat_id] = (until, reason)


def runtime_update_backpressure_reason(chat_id: int) -> str | None:
    """Return active backpressure reason for a Telegram chat, if any."""
    state = _runtime_update_typing_backpressure_until.get(chat_id)
    if state is None:
        return None
    until, reason = state
    if until <= time.monotonic():
        _runtime_update_typing_backpressure_until.pop(chat_id, None)
        return None
    return reason


def _audit_transport_suppressed(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str | None,
    reason: str,
    error: Exception | None = None,
    retry_after: float | None = None,
    action: str = "runtime_update_typing_suppressed",
    content_type: str = "chat_action",
    semantic_kind: str = "typing_suppressed",
    text: str = "typing",
) -> None:
    log_telegram_delivery(
        action=action,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        task_type="telegram_transport",
        content_type=content_type,
        semantic_kind=semantic_kind,
        text=text,
        reason=reason,
        error=error,
        retry_after=retry_after,
        backpressure_reason=reason,
    )

def reserve_runtime_update_transport_budget(
    *,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    window_id: str | None = None,
    action: str,
    content_type: str = "chat_action",
    semantic_kind: str,
    text: str,
) -> bool:
    """Reserve chat-level budget for nonessential runtime Telegram traffic.

    This gate is shared by typing indicators, mutable status updates, and
    status/topic probes so parallel topics in one chat cannot multiply optional
    Bot API calls while durable content delivery remains queue-owned.
    """
    now = time.monotonic()
    backpressure_reason = runtime_update_backpressure_reason(chat_id)
    if backpressure_reason is not None:
        _audit_transport_suppressed(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            reason=f"telegram_backpressure:{backpressure_reason}",
            action=action,
            content_type=content_type,
            semantic_kind=semantic_kind,
            text=text,
        )
        return False
    chat_last_sent = _runtime_update_typing_chat_last_sent.get(chat_id)
    if (
        chat_last_sent is not None
        and now - chat_last_sent < RUNTIME_UPDATE_TYPING_THROTTLE_SECONDS
    ):
        _audit_transport_suppressed(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            reason="telegram_chat_budget",
            action=action,
            content_type=content_type,
            semantic_kind=semantic_kind,
            text=text,
        )
        return False
    _runtime_update_typing_chat_last_sent[chat_id] = now
    return True



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
    if not reserve_runtime_update_transport_budget(
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        action="runtime_update_typing_suppressed",
        semantic_kind="typing_suppressed",
        text="typing",
    ):
        return False
    # Reserve the throttle slot before the network call. This keeps repeated
    # transport failures/timeouts from producing one Bot API attempt per update.
    _runtime_update_typing_last_sent[key] = now
    try:
        if thread_id is not None:
            await bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
                message_thread_id=thread_id,
            )
        else:
            await bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
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
    except RetryAfter as exc:
        retry_after = _retry_after_seconds(exc)
        record_runtime_update_backpressure(
            chat_id,
            seconds=retry_after,
            reason=f"retry_after:{retry_after}",
        )
        _audit_transport_suppressed(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            reason=f"telegram_backpressure:retry_after:{retry_after}",
            error=exc,
            retry_after=retry_after,
        )
        logger.debug("Runtime update typing was rate-limited by Telegram", exc_info=True)
        return False
    except (TimedOut, NetworkError) as exc:
        record_runtime_update_backpressure(
            chat_id,
            seconds=RUNTIME_UPDATE_TYPING_DEGRADED_COOLDOWN_SECONDS,
            reason="transport_timeout",
        )
        _audit_transport_suppressed(
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            window_id=window_id,
            reason="telegram_backpressure:transport_timeout",
            error=exc,
        )
        logger.debug("Failed to send runtime update typing indicator", exc_info=True)
        return False
    except Exception:
        logger.debug("Failed to send runtime update typing indicator", exc_info=True)
        return False
