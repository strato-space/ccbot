"""Focused tests for message queue merge invariants and stale-delivery guards."""

from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.message_queue import (
    MessageTask,
    _check_and_send_status,
    _can_merge_tasks,
    _is_stale_turn_generation,
    clear_commentary_lane_state,
    current_turn_generation,
    _mark_commentary_closed,
    open_new_turn_generation,
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

    _mark_commentary_closed(1, 42)
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


@pytest.mark.asyncio
async def test_process_content_task_drops_late_pre_final_visible_artifact() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["• Waiting for Mill [explorer]"],
        content_type="orchestration",
        semantic_kind="orchestration",
    )

    _mark_commentary_closed(1, 42)
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

            await _process_content_task(AsyncMock(), 1, task)

        mock_send.assert_not_awaited()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_process_status_task_drops_updates_after_final_answer() -> None:
    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="Working (2m 15s • esc to interrupt)",
    )

    _mark_commentary_closed(1, 42)
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"

            bot = AsyncMock()
            await _process_status_update_task(bot, 1, task)

        bot.edit_message_text.assert_not_awaited()
        bot.send_message.assert_not_awaited()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_check_and_send_status_skips_closed_terminal_surface() -> None:
    _mark_commentary_closed(1, 42)
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_tmux.capture_pane = AsyncMock(
                return_value="Working (2m 15s • esc to interrupt)"
            )
            mock_sm.resolve_chat_id.return_value = 100

            bot = AsyncMock()
            await _check_and_send_status(bot, 1, "@7", 42)

        bot.send_message.assert_not_awaited()
    finally:
        clear_commentary_lane_state(1, 42)


def test_open_new_turn_generation_reopens_surface_and_advances_generation() -> None:
    _mark_commentary_closed(1, 42)
    try:
        first = open_new_turn_generation(1, 42)
        second = open_new_turn_generation(1, 42)

        assert first == 1
        assert second == 2
        assert current_turn_generation(1, 42) == 2
    finally:
        clear_commentary_lane_state(1, 42)


def test_turn_generation_zero_is_still_stale_after_new_turn() -> None:
    assert _is_stale_turn_generation(0, 1) is True
    assert _is_stale_turn_generation(0, 0) is False


@pytest.mark.asyncio
async def test_stale_turn_commentary_is_dropped_after_new_generation() -> None:
    task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Old turn commentary",
        turn_generation=1,
    )

    open_new_turn_generation(1, 42)
    open_new_turn_generation(1, 42)
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


@pytest.mark.asyncio
async def test_stale_turn_final_is_dropped_after_new_generation() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final from old turn"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=1,
    )

    open_new_turn_generation(1, 42)
    open_new_turn_generation(1, 42)
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

            await _process_content_task(AsyncMock(), 1, task)

        mock_send.assert_not_awaited()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_in_flight_multpart_content_aborts_when_turn_generation_advances() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["First part", "Second part"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=1,
    )

    sent_message = AsyncMock()
    sent_message.message_id = 501

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.current_turn_generation",
            side_effect=[1, 1, 2, 2],
        ),
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=sent_message,
        ) as mock_send,
        patch(
            "ccbot.handlers.message_queue._check_and_send_status",
            new_callable=AsyncMock,
        ) as mock_status,
        patch(
            "ccbot.handlers.message_queue._send_task_images",
            new_callable=AsyncMock,
        ) as mock_images,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_content_task(AsyncMock(), 1, task)

    assert mock_send.await_count == 1
    mock_status.assert_not_awaited()
    mock_images.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_send_failure_does_not_close_pre_final_visible_lane() -> None:
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final answer"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=1,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Queued commentary before terminal success",
        turn_generation=1,
    )

    commentary_message = AsyncMock()
    commentary_message.message_id = 601

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[None, commentary_message],
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ) as mock_status,
            patch(
                "ccbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ) as mock_images,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(AsyncMock(), 1, final_task)
            await _process_commentary_update_task(AsyncMock(), 1, commentary_task)

        assert mock_send.await_count == 2
        mock_status.assert_awaited_once()
        mock_images.assert_awaited_once()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_successful_final_closes_pre_final_visible_lane() -> None:
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final answer"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=1,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Late commentary after successful final",
        turn_generation=1,
    )

    sent_final = AsyncMock()
    sent_final.message_id = 701

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent_final,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ) as mock_status,
            patch(
                "ccbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ) as mock_images,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            bot = AsyncMock()
            await _process_content_task(bot, 1, final_task)
            await _process_commentary_update_task(bot, 1, commentary_task)

        assert mock_send.await_count == 1
        mock_status.assert_not_awaited()
        mock_images.assert_awaited_once()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_partial_multipart_final_does_not_close_pre_final_visible_lane() -> None:
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final part 1", "Final part 2"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=1,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Commentary after partial final failure",
        turn_generation=1,
    )

    sent_final = AsyncMock()
    sent_final.message_id = 711
    commentary_message = AsyncMock()
    commentary_message.message_id = 712

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[sent_final, None, commentary_message],
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ) as mock_status,
            patch(
                "ccbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ) as mock_images,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(AsyncMock(), 1, final_task)
            await _process_commentary_update_task(AsyncMock(), 1, commentary_task)

        assert mock_send.await_count == 3
        mock_status.assert_awaited_once()
        mock_images.assert_awaited_once()
    finally:
        clear_commentary_lane_state(1, 42)
