"""Focused tests for message queue merge invariants and stale-delivery guards."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

import ccbot.handlers.message_queue as mq
from ccbot import delivery_audit
from ccbot.handlers.message_queue import (
    MessageTask,
    _check_and_send_status,
    _can_merge_tasks,
    _flood_until,
    _is_task_binding_active,
    _is_stale_turn_generation,
    _pending_input_enqueued,
    _pending_input_msg_info,
    _plan_update_msg_info,
    _process_pending_input_clear_task,
    _process_plan_update_task,
    _render_ingress_receipt_text,
    _warning_msg_info,
    _ingress_receipt_msg_info,
    _ingress_receipt_superseded,
    clear_commentary_lane_state,
    current_turn_generation,
    enqueue_pending_input_update,
    enqueue_ingress_receipt,
    enqueue_plan_update,
    flush_terminal_artifacts_before_new_turn,
    get_message_queue,
    shutdown_workers,
    _mark_commentary_closed,
    open_new_turn_generation,
    _process_commentary_update_task,
    _process_content_task,
    _process_status_update_task,
)
from ccbot.runtime_types import IMAGE_PREVIEW_SEMANTIC_KIND, WARNING_SEMANTIC_KIND


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


def test_ingress_receipt_render_distinguishes_delivered_no_ack() -> None:
    text = _render_ingress_receipt_text("hello", "delayed_runtime")

    assert text.startswith("⏳ Delivered to tmux")
    assert "waiting for Codex replay ACK" in text
    assert "❌" not in text


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


def test_can_merge_tasks_rejects_command_execution_identity_collapse():
    base = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho one\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_one",
    )
    candidate = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho two\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_two",
    )

    assert _can_merge_tasks(base, candidate) is False


@pytest.mark.asyncio
async def test_ingress_receipt_priority_passes_status_but_not_final() -> None:
    sent: list[tuple[str, str]] = []
    bot = AsyncMock()

    async def _fake_process_content(_bot, _user_id, task):
        sent.append(("content", task.semantic_kind))

    async def _fake_process_status(_bot, _user_id, task):
        sent.append(("status", task.text or ""))

    async def _fake_process_receipt(_bot, _user_id, task):
        sent.append(("receipt", task.receipt_status))

    await mq.shutdown_workers()
    with (
        patch("ccbot.handlers.message_queue._process_content_task", side_effect=_fake_process_content),
        patch("ccbot.handlers.message_queue._process_status_update_task", side_effect=_fake_process_status),
        patch("ccbot.handlers.message_queue._process_ingress_receipt_task", side_effect=_fake_process_receipt),
    ):
        await mq.enqueue_content_message(
            bot,
            100,
            "@1",
            ["final"],
            semantic_kind="assistant_final",
            thread_id=42,
        )
        await mq.enqueue_status_update(bot, 100, "@1", "Working", thread_id=42)
        await enqueue_ingress_receipt(
            bot,
            100,
            "@1",
            "hello",
            proof_id="proof-1",
            thread_id=42,
        )
        queue = mq.get_message_queue(100)
        assert queue is not None
        await queue.join()

    assert sent == [
        ("content", "assistant_final"),
        ("receipt", "pending"),
        ("status", "Working"),
    ]
    await mq.shutdown_workers()


@pytest.mark.asyncio
async def test_ingress_receipt_priority_passes_status_clear() -> None:
    sent: list[tuple[str, str]] = []
    bot = AsyncMock()

    async def _fake_clear(_bot, _user_id, _thread_id_or_0):
        sent.append(("status_clear", ""))

    async def _fake_process_receipt(_bot, _user_id, task):
        sent.append(("receipt", task.receipt_status))

    await mq.shutdown_workers()
    with (
        patch("ccbot.handlers.message_queue._do_clear_status_message", side_effect=_fake_clear),
        patch("ccbot.handlers.message_queue._process_ingress_receipt_task", side_effect=_fake_process_receipt),
    ):
        queue = mq.get_or_create_queue(bot, 100)
        queue.put_nowait(MessageTask(task_type="status_clear", thread_id=42))
        await enqueue_ingress_receipt(
            bot,
            100,
            "@1",
            "hello",
            proof_id="proof-1",
            thread_id=42,
        )
        await queue.join()

    assert sent == [("receipt", "pending"), ("status_clear", "")]
    await mq.shutdown_workers()


@pytest.mark.asyncio
async def test_ingress_receipt_does_not_cross_later_final_barrier() -> None:
    sent: list[tuple[str, str]] = []
    bot = AsyncMock()

    async def _fake_process_content(_bot, _user_id, task):
        sent.append(("content", task.semantic_kind))

    async def _fake_process_status(_bot, _user_id, task):
        sent.append(("status", task.text or ""))

    async def _fake_process_receipt(_bot, _user_id, task):
        sent.append(("receipt", task.receipt_status))

    await mq.shutdown_workers()
    with (
        patch("ccbot.handlers.message_queue._process_content_task", side_effect=_fake_process_content),
        patch("ccbot.handlers.message_queue._process_status_update_task", side_effect=_fake_process_status),
        patch("ccbot.handlers.message_queue._process_ingress_receipt_task", side_effect=_fake_process_receipt),
    ):
        await mq.enqueue_status_update(bot, 100, "@1", "Working", thread_id=42)
        await mq.enqueue_content_message(
            bot,
            100,
            "@1",
            ["final"],
            semantic_kind="assistant_final",
            thread_id=42,
        )
        await enqueue_ingress_receipt(
            bot,
            100,
            "@1",
            "hello",
            proof_id="proof-1",
            thread_id=42,
        )
        queue = mq.get_message_queue(100)
        assert queue is not None
        await queue.join()

    assert sent == [
        ("status", "Working"),
        ("content", "assistant_final"),
        ("receipt", "pending"),
    ]
    await mq.shutdown_workers()


@pytest.mark.asyncio
async def test_ingress_receipt_superseded_deletes_existing_and_blocks_late_pending() -> None:
    bot = AsyncMock()
    _ingress_receipt_msg_info[(100, 42, "proof-race")] = (77, "@1", "pending")
    _ingress_receipt_superseded.discard((100, 42, "proof-race"))

    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.is_external_binding_window_id.return_value = False
        mock_sm.get_window_for_thread.return_value = "@1"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = -100
        with patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux:
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            await mq._process_ingress_receipt_task(
                bot,
                100,
                MessageTask(
                    task_type="ingress_receipt",
                    text="ping",
                    window_id="@1",
                    thread_id=42,
                    proof_id="proof-race",
                    receipt_status="superseded",
                ),
            )
            await mq._process_ingress_receipt_task(
                bot,
                100,
                MessageTask(
                    task_type="ingress_receipt",
                    text="ping",
                    window_id="@1",
                    thread_id=42,
                    proof_id="proof-race",
                    receipt_status="pending",
                ),
            )

    bot.delete_message.assert_awaited_once_with(chat_id=-100, message_id=77)
    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_task_binding_active_accepts_external_binding_without_tmux_probe() -> (
    None
):
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
        patch(
            "ccbot.handlers.message_queue.send_with_fallback", new_callable=AsyncMock
        ) as mock_send,
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
async def test_process_commentary_task_reemits_reviewer_wait_at_tail() -> None:
    clear_commentary_lane_state(1, 42)
    first = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="ℹ Commentary\nПроверяю план перед запуском reviewers",
    )
    second = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text=(
            "ℹ Commentary\n"
            "Запущены оба reviewer lanes. Жду до 15 минут, не обрываю рано."
        ),
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 101
    sent_second = AsyncMock()
    sent_second.message_id = 102

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

            await _process_commentary_update_task(bot, 1, first)
            await _process_commentary_update_task(bot, 1, second)

        assert mock_send.await_count == 2
        bot.edit_message_text.assert_not_awaited()
        deleted_ids = {
            call.kwargs["message_id"] for call in bot.delete_message.await_args_list
        }
        assert 101 in deleted_ids
        assert "reviewer lanes" in mock_send.await_args_list[1].args[2]
    finally:
        clear_commentary_lane_state(1, 42)


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
async def test_process_content_task_edits_command_execution_by_tool_use_id() -> None:
    first = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho hi\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_exec",
    )
    second = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho hi\n```\n```sh\nhi\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_exec",
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 301

    mq._tool_msg_ids.clear()
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
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=type("Window", (), {"window_id": "@7"})()
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(bot, 1, first)
            await _process_content_task(bot, 1, second)

        mock_send.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()
        kwargs = bot.edit_message_text.await_args.kwargs
        assert kwargs["message_id"] == 301
        assert "hi" in kwargs["text"]
    finally:
        mq._tool_msg_ids.clear()


@pytest.mark.asyncio
async def test_process_warning_task_deduplicates_and_shows_counter_after_third_repeat() -> (
    None
):
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
async def test_process_warning_task_distinct_warning_keys_open_distinct_bubbles() -> (
    None
):
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
async def test_process_content_task_sends_documents_after_text() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Created archive"],
        content_type="tool_result",
        semantic_kind="tool_result",
        text="Created archive",
        document_data=[("archive.tar.gz", "application/gzip", b"tgz-bytes")],
    )
    bot = AsyncMock()
    sent = AsyncMock()
    sent.message_id = 701
    call_order: list[str] = []

    async def _send_text(*args, **kwargs):
        call_order.append("text")
        return sent

    async def _send_documents(*args, **kwargs):
        call_order.append("document")

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            side_effect=_send_text,
        ) as mock_send,
        patch(
            "ccbot.handlers.message_queue.send_document",
            new_callable=AsyncMock,
            side_effect=_send_documents,
        ) as mock_document,
        patch(
            "ccbot.handlers.message_queue._check_and_send_status",
            new_callable=AsyncMock,
        ) as mock_status,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_content_task(bot, 1, task)

    mock_send.assert_awaited_once()
    mock_document.assert_awaited_once()
    assert mock_document.await_args.args[2] == [
        ("archive.tar.gz", "application/gzip", b"tgz-bytes")
    ]
    mock_status.assert_awaited_once()
    assert call_order == ["text", "document"]


@pytest.mark.asyncio
async def test_process_warning_task_sends_documents_before_text() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["archive evidence"],
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text="archive evidence",
        document_data=[("archive.tar.gz", "application/gzip", b"tgz-bytes")],
        warning_key="runtime-discontinuity:archive",
    )
    bot = AsyncMock()
    sent = AsyncMock()
    sent.message_id = 702
    call_order: list[str] = []

    async def _send_text(*args, **kwargs):
        call_order.append("text")
        return sent

    async def _send_documents(*args, **kwargs):
        call_order.append("document")

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
                "ccbot.handlers.message_queue.send_document",
                new_callable=AsyncMock,
                side_effect=_send_documents,
            ) as mock_document,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(bot, 1, task)

        assert mock_document.await_count == 1
        assert mock_send.await_count == 1
        assert call_order == ["document", "text"]
    finally:
        _warning_msg_info.clear()


@pytest.mark.asyncio
async def test_process_warning_task_fallbacks_to_plain_edit_after_markdown_failure() -> (
    None
):
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
async def test_process_plan_update_task_edits_existing_plan_artifact() -> None:
    first = MessageTask(
        task_type="plan_update",
        window_id="@7",
        thread_id=42,
        text="• Updated Plan\n  ☐ First",
        turn_generation=1,
    )
    second = MessageTask(
        task_type="plan_update",
        window_id="@7",
        thread_id=42,
        text="• Updated Plan\n  ☑ First\n  ▶ Second",
        turn_generation=1,
    )
    sent = AsyncMock()
    sent.message_id = 901
    bot = AsyncMock()

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation", return_value=1
            ),
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent,
            ) as mock_send,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_plan_update_task(bot, 1, first)
            await _process_plan_update_task(bot, 1, second)

        mock_send.assert_awaited_once()
        bot.edit_message_text.assert_awaited_once()
        assert bot.edit_message_text.await_args.kwargs["message_id"] == 901
        assert _plan_update_msg_info[(1, 42)][2] == second.text
    finally:
        _plan_update_msg_info.pop((1, 42), None)


@pytest.mark.asyncio
async def test_enqueue_plan_update_suppresses_after_final_answer() -> None:
    _mark_commentary_closed(1, 42)
    try:
        await enqueue_plan_update(
            AsyncMock(),
            user_id=1,
            window_id="@7",
            plan_text="• Updated Plan",
            thread_id=42,
        )
        assert get_message_queue(1) is None
    finally:
        clear_commentary_lane_state(1, 42)
        await shutdown_workers()


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
async def test_process_pending_input_task_fallbacks_to_plain_edit_after_markdown_failure() -> (
    None
):
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
async def test_commentary_after_orchestration_re_emits_at_tail_instead_of_editing_old_bubble() -> (
    None
):
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
async def test_commentary_after_plan_update_re_emits_at_tail_instead_of_editing_old_bubble() -> (
    None
):
    commentary = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Starting implementation",
        turn_generation=1,
    )
    plan_update = MessageTask(
        task_type="plan_update",
        window_id="@7",
        thread_id=42,
        text="• Updated Plan\n  ▶ Implement delivery",
        turn_generation=1,
    )
    commentary_update = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Implementation still running",
        turn_generation=1,
    )

    commentary_msg = AsyncMock()
    commentary_msg.message_id = 401
    plan_msg = AsyncMock()
    plan_msg.message_id = 402
    commentary_tail_msg = AsyncMock()
    commentary_tail_msg.message_id = 403

    bot = AsyncMock()

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation", return_value=1
            ),
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[commentary_msg, plan_msg, commentary_tail_msg],
            ) as mock_send,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_commentary_update_task(bot, 1, commentary)
            await _process_plan_update_task(bot, 1, plan_update)
            await _process_commentary_update_task(bot, 1, commentary_update)

        assert mock_send.await_count == 3
        bot.edit_message_text.assert_not_awaited()
        assert bot.delete_message.await_count >= 1
        deleted_ids = {
            call.kwargs["message_id"] for call in bot.delete_message.await_args_list
        }
        assert 401 in deleted_ids
    finally:
        clear_commentary_lane_state(1, 42)
        _plan_update_msg_info.pop((1, 42), None)


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
async def test_flush_terminal_artifacts_delivers_final_before_generation_advances() -> (
    None
):
    mq._message_queues[1] = asyncio.Queue()
    mq._queue_locks[1] = asyncio.Lock()
    queue = mq._message_queues[1]
    await queue.put(
        MessageTask(
            task_type="status_update",
            window_id="@7",
            thread_id=42,
            text="Working",
            turn_generation=1,
        )
    )
    await queue.put(
        MessageTask(
            task_type="content",
            window_id="@7",
            thread_id=42,
            parts=["Готово.\n\nFinal answer"],
            content_type="text",
            semantic_kind="assistant_final",
            turn_generation=1,
        )
    )

    sent_final = AsyncMock()
    sent_final.message_id = 901

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

            flushed = await flush_terminal_artifacts_before_new_turn(
                AsyncMock(),
                1,
                42,
            )

        assert flushed == 1
        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[2].startswith("Готово.")
        mock_status.assert_not_awaited()
        mock_images.assert_awaited_once()
        assert queue.qsize() == 1
        remaining = queue.get_nowait()
        assert remaining.task_type == "status_update"
        queue.task_done()
    finally:
        clear_commentary_lane_state(1, 42)
        mq._message_queues.pop(1, None)
        mq._queue_locks.pop(1, None)


@pytest.mark.asyncio
async def test_in_flight_multpart_content_aborts_when_turn_generation_advances() -> (
    None
):
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
async def test_generated_image_terminal_preview_sends_single_photo_and_closes_lane() -> None:
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="Generated Image:\n  └ Saved to: file:///tmp/ig.png",
        content_type="generated_image_preview",
        semantic_kind="assistant_final",
        image_data=[("image/png", b"png-bytes")],
        image_caption="🖼 Generated Image\nRequest: frame",
        turn_generation=1,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Late commentary after terminal media",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 901

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                return_value=sent_photo,
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            bot = AsyncMock()
            await _process_content_task(bot, 1, final_task)
            await _process_commentary_update_task(bot, 1, commentary_task)

        mock_photo.assert_awaited_once()
        assert mock_photo.await_args.kwargs["caption"] == final_task.image_caption
        mock_send.assert_not_awaited()
        mock_status.assert_not_awaited()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_generated_image_terminal_preview_photo_failure_falls_back_to_text() -> None:
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="Generated Image:\n  └ Saved to: file:///tmp/ig.png",
        content_type="generated_image_preview",
        semantic_kind="assistant_final",
        image_data=[("image/png", b"png-bytes")],
        image_caption="🖼 Generated Image\nRequest: frame",
        turn_generation=1,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Late commentary after fallback terminal text",
        turn_generation=1,
    )
    sent_text = AsyncMock()
    sent_text.message_id = 902

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent_text,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            bot = AsyncMock()
            await _process_content_task(bot, 1, final_task)
            await _process_commentary_update_task(bot, 1, commentary_task)

        mock_photo.assert_awaited_once()
        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[2] == final_task.text
        mock_status.assert_not_awaited()
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_sends_single_photo_without_closing_final_lanes() -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ contact_sheet.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"png-bytes")],
        image_caption="🖼 Viewed Image\nFile: contact_sheet.png",
        turn_generation=1,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Commentary remains pre-final after image preview",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 1901
    sent_commentary = AsyncMock()
    sent_commentary.message_id = 1902

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                return_value=sent_photo,
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent_commentary,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            bot = AsyncMock()
            await _process_content_task(bot, 1, preview_task)
            await _process_commentary_update_task(bot, 1, commentary_task)

        mock_photo.assert_awaited_once()
        assert mock_photo.await_args.kwargs["caption"] == preview_task.image_caption
        mock_status.assert_awaited_once()
        mock_send.assert_awaited_once()
        assert "Commentary remains pre-final" in mock_send.await_args.args[2]
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_photo_failure_falls_back_to_text_without_closing_lanes() -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ contact_sheet.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"png-bytes")],
        image_caption="🖼 Viewed Image\nFile: contact_sheet.png",
        turn_generation=1,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Commentary after preview fallback",
        turn_generation=1,
    )
    sent_text = AsyncMock()
    sent_text.message_id = 1903

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation",
                return_value=1,
            ),
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent_text,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            bot = AsyncMock()
            await _process_content_task(bot, 1, preview_task)
            await _process_commentary_update_task(bot, 1, commentary_task)

        mock_photo.assert_awaited_once()
        assert mock_send.await_count == 2
        assert mock_send.await_args_list[0].args[2] == "• Viewed Image:\n  └ contact_sheet.png"
        assert "Commentary after preview fallback" in mock_send.await_args_list[1].args[2]
        mock_status.assert_awaited_once()
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


@pytest.mark.asyncio
async def test_poll_only_write_stdin_does_not_create_status_bubble(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    mq._status_msg_info.clear()

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="🛠 Tool\nwrite_stdin(session 82998, poll)",
        turn_generation=0,
    )
    bot = AsyncMock()

    with (
        patch(
            "ccbot.handlers.message_queue._is_task_binding_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ccbot.handlers.message_queue.current_turn_generation", return_value=0),
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["action"] == "suppress"
    assert rows[-1]["reason"] == "poll_without_existing_status"
    assert rows[-1]["content_type"] == "status"
    assert rows[-1]["semantic_kind"] == "technical_status"


@pytest.mark.asyncio
async def test_status_edit_not_modified_does_not_create_duplicate_bubble(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    mq._status_msg_info.clear()
    mq._status_msg_info[(1, 42)] = (
        501,
        "@7",
        "Working (1m 00s) ",
    )

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="Working (1m 00s)",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = Exception(
        "Message is not modified: specified new message content and reply markup "
        "are exactly the same as a current content and reply markup of the message"
    )

    with (
        patch(
            "ccbot.handlers.message_queue._is_task_binding_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ccbot.handlers.message_queue.current_turn_generation", return_value=0),
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
        ) as mock_send,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    mock_send.assert_not_awaited()
    assert mq._status_msg_info[(1, 42)] == (501, "@7", task.text)
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["action"] == "edit_noop"
    assert rows[-1]["reason"] == "message_not_modified"

    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_poll_only_write_stdin_does_not_replace_existing_status_bubble(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    mq._status_msg_info.clear()
    mq._status_msg_info[(1, 42)] = (501, "@7", "🛠 Tool\nwrite_stdin(session 1, poll)")

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="🛠 Tool\nwrite_stdin(session 82998, poll)",
        turn_generation=0,
    )
    bot = AsyncMock()

    with (
        patch(
            "ccbot.handlers.message_queue._is_task_binding_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ccbot.handlers.message_queue.current_turn_generation", return_value=0),
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["action"] == "suppress"
    assert rows[-1]["reason"] == "poll_does_not_replace_existing_status"
    assert rows[-1]["content_type"] == "status"
    assert rows[-1]["semantic_kind"] == "technical_status"
    assert mq._status_msg_info[(1, 42)] == (
        501,
        "@7",
        "🛠 Tool\nwrite_stdin(session 1, poll)",
    )

    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_tool_output_wrapper_is_cleaned_for_humans(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    mq._status_msg_info.clear()
    mq._status_msg_info[(1, 42)] = (501, "@7", "Working (1m 00s)")

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text=(
            "↳ Tool Output\n"
            "```text\n"
            "Chunk ID: e5c144\n"
            "Wall time: 7.9130 seconds\n"
            "Process exited with code 0\n"
            "Original token count: 180\n"
            "Output:\n"
            "✓ 22 [desktop-chromium] › tests/platform-routes.spec.ts:146:1\n"
            "✓ 23 [iphone-xr] › tests/platform-routes.spec.ts:116:1\n"
            "```\n"
            "preview 10/11 lines"
        ),
        turn_generation=0,
    )
    bot = AsyncMock()

    with (
        patch(
            "ccbot.handlers.message_queue._is_task_binding_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ccbot.handlers.message_queue.current_turn_generation", return_value=0),
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    sent_text = bot.edit_message_text.await_args.kwargs["text"]
    assert sent_text.startswith("⌘ Command output")
    assert "```text" in sent_text
    assert "desktop" in sent_text
    assert "preview 10/11 lines" in sent_text
    assert "Chunk ID" not in sent_text
    assert "Wall time" not in sent_text
    assert "Process exited" not in sent_text
    assert "Original token count" not in sent_text
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["action"] == "edit"
    assert rows[-1]["preview"].startswith("⌘ Command output")

    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_running_tool_output_wrapper_strips_process_metadata(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    mq._status_msg_info.clear()
    mq._status_msg_info[(1, 42)] = (501, "@7", "Working (1m 00s)")

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text=(
            "↳ Tool Output\n"
            "```text\n"
            "Chunk ID: e5c144\n"
            "Wall time: 0.0000 seconds\n"
            "Process running with session ID 67516\n"
            "Original token count: 0\n"
            "Output:\n"
            "```\n"
            "preview 0/0 lines"
        ),
        turn_generation=0,
    )
    bot = AsyncMock()

    with (
        patch(
            "ccbot.handlers.message_queue._is_task_binding_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ccbot.handlers.message_queue.current_turn_generation", return_value=0),
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    bot.edit_message_text.assert_not_awaited()
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["action"] == "suppress"
    assert rows[-1]["reason"] == "empty_after_status_normalization"

    mq._status_msg_info.clear()


def test_open_new_turn_generation_does_not_reuse_previous_plan_artifact() -> None:
    _plan_update_msg_info[(1, 42)] = (901, "@7", "• Updated Plan\n  ☑ Old")
    try:
        generation = open_new_turn_generation(1, 42)
        assert generation >= 1
        assert (1, 42) not in _plan_update_msg_info
    finally:
        clear_commentary_lane_state(1, 42)
        _plan_update_msg_info.pop((1, 42), None)


@pytest.mark.asyncio
async def test_user_echo_does_not_convert_existing_status_to_content(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    mq._status_msg_info.clear()
    mq._status_msg_info[(1, 42)] = (501, "@7", "🛠 Tool\nexec_command(...)")

    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["👤 queued follow-up"],
        content_type="text",
        semantic_kind="user_echo",
        text="queued follow-up",
        turn_generation=1,
    )
    sent = AsyncMock()
    sent.message_id = 777

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.current_turn_generation", return_value=1
            ),
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent,
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            await _process_content_task(AsyncMock(), 1, task)

        mock_send.assert_awaited_once()
        assert mock_send.await_args.args[2] == "👤 queued follow-up"
        assert (1, 42) in mq._status_msg_info
        rows = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        assert rows[-1]["action"] == "send"
        assert rows[-1]["semantic_kind"] == "user_echo"
    finally:
        mq._status_msg_info.clear()
        clear_commentary_lane_state(1, 42)
