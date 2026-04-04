"""Focused tests for message queue merge invariants and stale-delivery guards."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.message_queue import (
    MessageTask,
    _can_merge_tasks,
    clear_commentary_lane_state,
    mark_commentary_closed,
    _process_commentary_update_task,
    _process_content_task,
    _process_status_update_task,
)


def test_can_merge_tasks_rejects_mixed_content_types():
    base = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["hello"],
        content_type="commentary",
    )
    candidate = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["world"],
        content_type="text",
    )

    assert _can_merge_tasks(base, candidate) is False


def test_can_merge_tasks_rejects_different_topics_for_same_window():
    base = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["hello"],
        content_type="text",
    )
    candidate = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=43,
        parts=["world"],
        content_type="text",
    )

    assert _can_merge_tasks(base, candidate) is False


@pytest.mark.asyncio
async def test_process_content_task_drops_stale_binding() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["final answer"],
        content_type="text",
    )

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch("ccbot.handlers.message_queue.send_with_fallback", new_callable=AsyncMock) as mock_send,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = None
        mock_sm.get_topic_binding_state.return_value = "none"

        await _process_content_task(AsyncMock(), 1, task)

    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_status_task_drops_stale_binding() -> None:
    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="Thinking…",
    )

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@9"
        mock_sm.get_topic_binding_state.return_value = "bound"

        bot = AsyncMock()
        await _process_status_update_task(bot, 1, task)

    bot.edit_message_text.assert_not_awaited()
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_commentary_task_replaces_previous_visible_commentary() -> None:
    first = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Wave A1 started",
    )
    second = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Wave A2 started",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 101
    sent_second = AsyncMock()
    sent_second.message_id = 102

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            side_effect=[sent_first, sent_second],
        ) as mock_send,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_commentary_update_task(bot, 1, first)
        await _process_commentary_update_task(bot, 1, second)

    assert mock_send.await_count == 2
    bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=101)


@pytest.mark.asyncio
async def test_process_commentary_task_drops_updates_after_final_answer() -> None:
    task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Late commentary after final answer",
    )

    mark_commentary_closed(1, 42)
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"

            await _process_commentary_update_task(AsyncMock(), 1, task)

        mock_send.assert_not_awaited()
    finally:
        clear_commentary_lane_state(1, 42)
