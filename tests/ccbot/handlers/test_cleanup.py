"""Focused tests for topic cleanup coverage."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.cleanup import clear_topic_state
from ccbot.handlers.message_queue import (
    ImagePreviewMessageInfo,
    _image_preview_delete_retry_tasks,
    _image_preview_msg_info,
    clear_commentary_lane_state,
)


@pytest.mark.asyncio
async def test_clear_topic_state_deletes_plan_update_artifact() -> None:
    bot = AsyncMock()

    with (
        patch("ccbot.handlers.cleanup.clear_status_message", new_callable=AsyncMock),
        patch("ccbot.handlers.cleanup.clear_commentary_message", new_callable=AsyncMock),
        patch("ccbot.handlers.cleanup.clear_pending_input_message", new_callable=AsyncMock),
        patch("ccbot.handlers.cleanup.clear_plan_update_message", new_callable=AsyncMock) as mock_clear_plan,
        patch("ccbot.handlers.cleanup.clear_interactive_msg", new_callable=AsyncMock),
    ):
        await clear_topic_state(1, 42, bot=bot)

    mock_clear_plan.assert_awaited_once_with(bot, 1, 42)


@pytest.mark.asyncio
async def test_clear_topic_state_preserves_preview_tracking_when_delete_fails() -> None:
    bot = AsyncMock()
    bot.delete_message.side_effect = RuntimeError("delete failed")
    _image_preview_msg_info[(1, 42)] = ImagePreviewMessageInfo(
        message_id=501,
        window_id="@7",
        turn_generation=3,
        media_signature="media",
        caption_signature="caption",
    )

    try:
        with (
            patch("ccbot.handlers.cleanup.clear_status_message", new_callable=AsyncMock),
            patch(
                "ccbot.handlers.cleanup.clear_commentary_message",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.cleanup.clear_pending_input_message",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.cleanup.clear_plan_update_message",
                new_callable=AsyncMock,
            ),
            patch("ccbot.handlers.cleanup.clear_interactive_msg", new_callable=AsyncMock),
            patch(
                "ccbot.handlers.message_queue.session_manager.resolve_chat_id",
                return_value=100,
            ),
        ):
            await clear_topic_state(1, 42, bot=bot)

        assert _image_preview_msg_info[(1, 42)].message_id == 501
        assert (1, 42, 501) in _image_preview_delete_retry_tasks
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_clear_topic_state_preserves_preview_tracking_after_retryafter() -> None:
    from telegram.error import RetryAfter

    bot = AsyncMock()
    bot.delete_message.side_effect = RetryAfter(3)
    _image_preview_msg_info[(1, 42)] = ImagePreviewMessageInfo(
        message_id=502,
        window_id="@7",
        turn_generation=3,
        media_signature="media",
        caption_signature="caption",
    )

    try:
        with (
            patch("ccbot.handlers.cleanup.clear_status_message", new_callable=AsyncMock),
            patch(
                "ccbot.handlers.cleanup.clear_commentary_message",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.cleanup.clear_pending_input_message",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.cleanup.clear_plan_update_message",
                new_callable=AsyncMock,
            ),
            patch("ccbot.handlers.cleanup.clear_interactive_msg", new_callable=AsyncMock),
            patch(
                "ccbot.handlers.message_queue.session_manager.resolve_chat_id",
                return_value=100,
            ),
        ):
            await clear_topic_state(1, 42, bot=bot)

        assert _image_preview_msg_info[(1, 42)].message_id == 502
        assert (1, 42, 502) in _image_preview_delete_retry_tasks
    finally:
        clear_commentary_lane_state(1, 42)
