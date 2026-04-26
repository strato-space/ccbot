"""Focused tests for topic cleanup coverage."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.cleanup import clear_topic_state


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
