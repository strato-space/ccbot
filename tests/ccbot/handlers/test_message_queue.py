"""Focused tests for message queue merge invariants and stale-delivery guards."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.message_queue import (
    MessageTask,
    _check_and_send_status,
    _can_merge_tasks,
    _flood_until,
    _is_task_binding_active,
    _is_stale_turn_generation,
    _pending_input_enqueued,
    _pending_input_msg_info,
    _process_pending_input_clear_task,
    _warning_msg_info,
    clear_commentary_lane_state,
    current_turn_generation,
    enqueue_pending_input_update,
    shutdown_workers,
    _mark_commentary_closed,
    open_new_turn_generation,
    _process_commentary_update_task,
    _process_content_task,
    _process_status_update_task,
)
from ccbot.runtime_types import WARNING_SEMANTIC_KIND


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
async def test_is_task_binding_active_accepts_external_binding_without_tmux_probe() -> None:
    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_sm.is_external_binding_window_id.return_value = True
        mock_sm.get_window_for_thread.return_value = "external:codex:thread-1"
        mock_sm.get_topic_binding_state.return_value = "bound"

        active = await _is_task_binding_active(1, "external:codex:thread-1", 42)

    assert active is True
    mock_tmux.find_window_by_id.assert_not_called()


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

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            side_effect=[sent_first],
        ) as mock_send,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_commentary_update_task(bot, 1, first)
        await _process_commentary_update_task(bot, 1, second)

    assert mock_send.await_count == 1
    bot.delete_message.assert_not_awaited()
    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 100
    assert kwargs["message_id"] == 101
    assert "Wave A2 started" in kwargs["text"]


@pytest.mark.asyncio
async def test_process_commentary_task_sends_multi_part_commentary_losslessly() -> None:
    task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="ℹ Commentary\n" + ("x" * 5000),
        parts=["ℹ Commentary\n" + ("a" * 3900), ("b" * 1500) + "\n\n[2/2]"],
    )

    bot = AsyncMock()
    first = AsyncMock()
    first.message_id = 211
    second = AsyncMock()
    second.message_id = 212

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            side_effect=[first, second],
        ) as mock_send,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_commentary_update_task(bot, 1, task)

    assert mock_send.await_count == 2
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_warning_task_deduplicates_and_shows_counter_after_third_repeat() -> None:
    first = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: quota is low"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: quota is low",
    )
    second = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: quota is low"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: quota is low",
    )
    third = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: quota is low"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: quota is low",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 501

    _warning_msg_info.clear()
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[sent_first],
            ) as mock_send,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(bot, 1, first)
            await _process_content_task(bot, 1, second)
            await _process_content_task(bot, 1, third)

        assert mock_send.await_count == 1
        bot.edit_message_text.assert_awaited_once()
        kwargs = bot.edit_message_text.await_args.kwargs
        assert kwargs["message_id"] == 501
        assert "×3" in kwargs["text"]
    finally:
        _warning_msg_info.clear()


@pytest.mark.asyncio
async def test_process_warning_task_new_text_opens_new_warning_bubble() -> None:
    first = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: quota is low"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: quota is low",
    )
    second = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: model catalog changed"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: model catalog changed",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 601
    sent_second = AsyncMock()
    sent_second.message_id = 602

    _warning_msg_info.clear()
    try:
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

            await _process_content_task(bot, 1, first)
            await _process_content_task(bot, 1, second)

        assert mock_send.await_count == 2
        bot.edit_message_text.assert_not_awaited()
    finally:
        _warning_msg_info.clear()


@pytest.mark.asyncio
async def test_process_warning_task_distinct_warning_keys_open_distinct_bubbles() -> None:
    first = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: runtime stopped"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: runtime stopped",
        warning_key="runtime-discontinuity:1",
    )
    second = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: runtime stopped"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: runtime stopped",
        warning_key="runtime-discontinuity:2",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 611
    sent_second = AsyncMock()
    sent_second.message_id = 612

    _warning_msg_info.clear()
    try:
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

            await _process_content_task(bot, 1, first)
            await _process_content_task(bot, 1, second)

        assert mock_send.await_count == 2
        bot.edit_message_text.assert_not_awaited()
    finally:
        _warning_msg_info.clear()


@pytest.mark.asyncio
async def test_process_warning_task_sends_images_before_text() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["```text\ncodex resume thread-1\n```"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="```text\ncodex resume thread-1\n```",
        image_data=[("image/png", b"png-bytes")],
        warning_key="runtime-discontinuity:1",
    )
    bot = AsyncMock()
    sent = AsyncMock()
    sent.message_id = 701
    call_order: list[str] = []

    async def _send_text(*args, **kwargs):
        call_order.append("text")
        return sent

    async def _send_images(*args, **kwargs):
        call_order.append("image")

    _warning_msg_info.clear()
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=_send_text,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                side_effect=_send_images,
            ) as mock_photo,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(bot, 1, task)

        assert mock_photo.await_count == 1
        assert mock_send.await_count == 1
        assert call_order == ["image", "text"]
    finally:
        _warning_msg_info.clear()


@pytest.mark.asyncio
async def test_process_warning_task_fallbacks_to_plain_edit_after_markdown_failure() -> None:
    first = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: quota is low"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: quota is low",
    )
    second = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: quota is low"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: quota is low",
    )
    third = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Heads up: quota is low"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="Heads up: quota is low",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 701

    _warning_msg_info.clear()
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[sent_first],
            ) as mock_send,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            bot.edit_message_text.side_effect = [
                Exception("markdown parse failed"),
                None,
            ]

            await _process_content_task(bot, 1, first)
            await _process_content_task(bot, 1, second)
            await _process_content_task(bot, 1, third)

        assert mock_send.await_count == 1
        assert bot.edit_message_text.await_count == 2
        first_call = bot.edit_message_text.await_args_list[0].kwargs
        second_call = bot.edit_message_text.await_args_list[1].kwargs
        assert first_call["parse_mode"] == "MarkdownV2"
        assert "parse_mode" not in second_call
    finally:
        _warning_msg_info.clear()


@pytest.mark.asyncio
async def test_stale_binding_content_task_clears_warning_tracking() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["final answer"],
        content_type="text",
    )
    _warning_msg_info[(1, 42, "latest-warning")] = (
        999,
        "@7",
        "Heads up: quota is low",
        4,
    )

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
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.get_topic_binding_state.return_value = "none"

            await _process_content_task(AsyncMock(), 1, task)

        mock_send.assert_not_awaited()
        assert (1, 42, "latest-warning") not in _warning_msg_info
    finally:
        _warning_msg_info.clear()


@pytest.mark.asyncio
async def test_process_pending_input_task_reuses_visible_preview_in_place() -> None:
    first = MessageTask(
        task_type="pending_input_update",
        window_id="@7",
        thread_id=42,
        text="⏭ Queued follow-up messages\n↳ update docs",
    )
    second = MessageTask(
        task_type="pending_input_update",
        window_id="@7",
        thread_id=42,
        text="⏭ Queued follow-up messages\n↳ update docs\n↳ continue infra",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 201

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            side_effect=[sent_first],
        ) as mock_send,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        from ccbot.handlers.message_queue import _process_pending_input_update_task

        await _process_pending_input_update_task(bot, 1, first)
        await _process_pending_input_update_task(bot, 1, second)

    assert mock_send.await_count == 1
    bot.delete_message.assert_not_awaited()
    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 100
    assert kwargs["message_id"] == 201
    assert "continue infra" in kwargs["text"]


@pytest.mark.asyncio
async def test_process_pending_input_task_fallbacks_to_plain_edit_after_markdown_failure() -> None:
    first = MessageTask(
        task_type="pending_input_update",
        window_id="@7",
        thread_id=42,
        text="⏭ Queued follow-up messages\n↳ update docs",
    )
    second = MessageTask(
        task_type="pending_input_update",
        window_id="@7",
        thread_id=42,
        text="⏭ Queued follow-up messages\n↳ update docs\n↳ continue infra",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 401

    _pending_input_msg_info.clear()
    _pending_input_enqueued.clear()
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[sent_first],
            ) as mock_send,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot.edit_message_text.side_effect = [Exception("markdown failed"), None]

            from ccbot.handlers.message_queue import _process_pending_input_update_task

            await _process_pending_input_update_task(bot, 1, first)
            await _process_pending_input_update_task(bot, 1, second)

        assert mock_send.await_count == 1
        assert bot.edit_message_text.await_count == 2
        first_call = bot.edit_message_text.await_args_list[0].kwargs
        second_call = bot.edit_message_text.await_args_list[1].kwargs
        assert first_call["parse_mode"] == "MarkdownV2"
        assert "parse_mode" not in second_call
    finally:
        _pending_input_msg_info.clear()
        _pending_input_enqueued.clear()


@pytest.mark.asyncio
async def test_pending_input_send_failure_clears_dedupe_pin_for_retry() -> None:
    _pending_input_msg_info.clear()
    _pending_input_enqueued.clear()
    try:
        with patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from ccbot.handlers.message_queue import _do_send_pending_input_message

            await _do_send_pending_input_message(
                AsyncMock(),
                user_id=1,
                thread_id_or_0=42,
                window_id="@7",
                text="⏭ Pending input\n↳ continue infra",
            )

        assert (1, 42) not in _pending_input_enqueued
    finally:
        _pending_input_msg_info.clear()
        _pending_input_enqueued.clear()


@pytest.mark.asyncio
async def test_pending_input_clear_is_not_generation_scoped() -> None:
    task = MessageTask(
        task_type="pending_input_clear",
        window_id="@7",
        thread_id=42,
        turn_generation=1,
    )

    with (
        patch(
            "ccbot.handlers.message_queue._is_task_binding_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "ccbot.handlers.message_queue._do_clear_pending_input_message",
            new_callable=AsyncMock,
        ) as mock_clear,
    ):
        await _process_pending_input_clear_task(AsyncMock(), 1, task)

    mock_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_flood_drop_of_pending_input_clears_dedupe_state() -> None:
    _flood_until[1] = time.monotonic() + 5.0
    try:
        await enqueue_pending_input_update(
            AsyncMock(),
            user_id=1,
            window_id="@7",
            pending_input_text="⏭ Pending input\nQueued follow-up messages\n↳ continue infra",
            thread_id=42,
        )
        assert (1, 42) not in _pending_input_enqueued
    finally:
        _flood_until.pop(1, None)
        await shutdown_workers()


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
async def test_commentary_after_orchestration_re_emits_at_tail_instead_of_editing_old_bubble() -> None:
    commentary = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Wave A1 started",
        turn_generation=1,
    )
    orchestration = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["• Waiting for Mill [explorer]"],
        content_type="orchestration",
        semantic_kind="orchestration",
        turn_generation=1,
    )
    commentary_update = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Wave A1 updated after waiting",
        turn_generation=1,
    )

    commentary_msg = AsyncMock()
    commentary_msg.message_id = 301
    orchestration_msg = AsyncMock()
    orchestration_msg.message_id = 302
    commentary_tail_msg = AsyncMock()
    commentary_tail_msg.message_id = 303

    bot = AsyncMock()

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
                side_effect=[commentary_msg, orchestration_msg, commentary_tail_msg],
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_commentary_update_task(bot, 1, commentary)
            await _process_content_task(bot, 1, orchestration)
            await _process_commentary_update_task(bot, 1, commentary_update)

        assert mock_send.await_count == 3
        bot.edit_message_text.assert_not_awaited()
        assert bot.delete_message.await_count >= 1
        deleted_ids = {
            call.kwargs["message_id"] for call in bot.delete_message.await_args_list
        }
        assert 301 in deleted_ids
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
async def test_successful_final_keeps_existing_commentary_visible_above_final() -> None:
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
        text="ℹ Commentary\nReading the repo first.",
        parts=["ℹ Commentary\nReading the repo first."],
        turn_generation=1,
    )

    commentary_message = AsyncMock()
    commentary_message.message_id = 801
    final_message = AsyncMock()
    final_message.message_id = 802

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
                side_effect=[commentary_message, final_message],
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
            await _process_commentary_update_task(bot, 1, commentary_task)
            await _process_content_task(bot, 1, final_task)

        assert mock_send.await_count == 2
        from ccbot.handlers.message_queue import _commentary_msg_info

        assert _commentary_msg_info[(1, 42)][0] == 801
        mock_status.assert_not_awaited()
        mock_images.assert_awaited_once()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_final_answer_is_sent_as_new_message_even_when_status_exists() -> None:
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final answer"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=1,
    )
    sent_final = AsyncMock()
    sent_final.message_id = 731

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue._convert_status_to_content",
                new_callable=AsyncMock,
                return_value=999,
            ) as mock_convert,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent_final,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(AsyncMock(), 1, final_task)

        mock_convert.assert_not_awaited()
        mock_send.assert_awaited_once()
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
