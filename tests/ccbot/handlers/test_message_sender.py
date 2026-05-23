"""Tests for Telegram message_sender fallback contracts."""

from unittest.mock import AsyncMock

import pytest

from ccbot.handlers.message_sender import safe_edit, safe_send


@pytest.mark.asyncio
async def test_safe_edit_returns_plain_fallback_message() -> None:
    target = AsyncMock()
    fallback_message = object()
    target.edit_message_text = AsyncMock(
        side_effect=[ValueError("bad markdown"), fallback_message]
    )

    result = await safe_edit(target, "**bad**")

    assert result is fallback_message
    assert target.edit_message_text.await_count == 2
    first_call, second_call = target.edit_message_text.await_args_list
    assert first_call.kwargs["parse_mode"] == "MarkdownV2"
    assert "parse_mode" not in second_call.kwargs


@pytest.mark.asyncio
async def test_safe_edit_supports_message_edit_text_method() -> None:
    target = AsyncMock()
    target.edit_message_text = None
    edited_message = object()
    target.edit_text = AsyncMock(return_value=edited_message)

    result = await safe_edit(target, "progress update")

    assert result is edited_message
    target.edit_text.assert_awaited_once()
    kwargs = target.edit_text.await_args.kwargs
    assert kwargs["parse_mode"] == "MarkdownV2"
    assert "link_preview_options" in kwargs


@pytest.mark.asyncio
async def test_safe_edit_returns_none_after_final_failure() -> None:
    target = AsyncMock()
    target.edit_message_text = AsyncMock(
        side_effect=[ValueError("bad markdown"), RuntimeError("telegram down")]
    )

    result = await safe_edit(target, "**bad**")

    assert result is None
    assert target.edit_message_text.await_count == 2


@pytest.mark.asyncio
async def test_safe_send_returns_plain_fallback_message() -> None:
    bot = AsyncMock()
    fallback_message = object()
    bot.send_message = AsyncMock(
        side_effect=[ValueError("bad markdown"), fallback_message]
    )

    result = await safe_send(bot, 123, "**bad**", message_thread_id=456)

    assert result is fallback_message
    assert bot.send_message.await_count == 2
    first_call, second_call = bot.send_message.await_args_list
    assert first_call.kwargs["parse_mode"] == "MarkdownV2"
    assert first_call.kwargs["message_thread_id"] == 456
    assert "parse_mode" not in second_call.kwargs
    assert second_call.kwargs["message_thread_id"] == 456


@pytest.mark.asyncio
async def test_safe_send_returns_none_after_final_failure() -> None:
    bot = AsyncMock()
    bot.send_message = AsyncMock(
        side_effect=[ValueError("bad markdown"), RuntimeError("telegram down")]
    )

    result = await safe_send(bot, 123, "**bad**")

    assert result is None
    assert bot.send_message.await_count == 2
