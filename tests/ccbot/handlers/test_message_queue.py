"""Focused tests for message queue merge invariants and stale-delivery guards."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, RetryAfter

import ccbot.handlers.message_queue as mq
from ccbot import delivery_audit
from ccbot.handlers.message_queue import (
    MessageTask,
    _check_and_send_status,
    _can_merge_tasks,
    _flood_until,
    _is_task_binding_active,
    _is_stale_turn_generation,
    _image_preview_msg_info,
    _image_preview_delete_retry_tasks,
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
    clear_image_preview_message,
    current_turn_generation,
    enqueue_commentary_update,
    enqueue_content_message,
    enqueue_pending_input_update,
    enqueue_ingress_receipt,
    enqueue_plan_update,
    enqueue_status_update,
    flush_terminal_artifacts_before_new_turn,
    get_message_queue,
    get_telegram_delivery_backlog_metrics,
    shutdown_workers,
    _mark_commentary_closed,
    open_new_turn_generation,
    open_new_turn_generation_with_cleanup,
    _process_commentary_update_task,
    _process_content_task,
    _process_status_update_task,
)
from ccbot.runtime_types import (
    IMAGE_PREVIEW_SEMANTIC_KIND,
    OMX_WORKFLOW_PANEL_CONTENT_TYPE,
    OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
    WARNING_SEMANTIC_KIND,
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




def test_ingress_receipt_render_distinguishes_steer_and_queue_modes() -> None:
    steer_text = _render_ingress_receipt_text("hello", "pending")
    queue_text = _render_ingress_receipt_text("hello", "queued_runtime")

    assert steer_text == "↗ Steer\n\nhello"
    assert queue_text == "⏭ Queue\n\nhello"

def test_ingress_receipt_render_distinguishes_delivered_no_ack() -> None:
    text = _render_ingress_receipt_text("hello", "delayed_runtime")

    assert text.startswith("⏳ Delivered")
    assert "waiting for Codex replay ACK" in text
    assert "❌" not in text


def test_ingress_receipt_render_can_include_runtime_target_hint() -> None:
    text = _render_ingress_receipt_text(
        "ping",
        "delayed_runtime",
        target_hint="@9 · comfy-agent-ops · /home/tools/mediagen-comfy",
    )

    assert "→ @9" in text
    assert "comfy-agent-ops" in text
    assert "/home/tools/mediagen-comfy" in text
    assert text.endswith("ping")


@pytest.mark.asyncio
async def test_queue_worker_retries_content_after_retryafter_without_loss() -> None:
    bot = AsyncMock()
    sent = SimpleNamespace(message_id=7001)

    try:
        with (
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[RetryAfter(1), sent],
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._is_task_binding_active",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.message_queue.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch("ccbot.handlers.message_queue._audit_task_delivery") as mock_audit,
        ):
            await enqueue_content_message(
                bot,
                user_id=1,
                window_id="@7",
                parts=["final answer"],
                chat_id=100,
                thread_id=42,
            )
            queue = get_message_queue(1)
            assert queue is not None
            await asyncio.wait_for(queue.join(), timeout=1)

        assert mock_send.await_count == 2
        assert any(
            call.kwargs.get("action") == "retry_scheduled"
            for call in mock_audit.call_args_list
        )
    finally:
        await shutdown_workers()
        _flood_until.pop(1, None)


@pytest.mark.asyncio
async def test_queue_worker_retry_does_not_duplicate_delivered_content_parts() -> None:
    bot = AsyncMock()
    first_sent = SimpleNamespace(message_id=7001)
    second_sent = SimpleNamespace(message_id=7002)

    try:
        with (
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[first_sent, RetryAfter(1), second_sent],
            ) as mock_send,
            patch(
                "ccbot.handlers.message_queue._is_task_binding_active",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
            patch(
                "ccbot.handlers.message_queue.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await enqueue_content_message(
                bot,
                user_id=1,
                window_id="@7",
                parts=["part one", "part two"],
                chat_id=100,
                thread_id=42,
            )
            queue = get_message_queue(1)
            assert queue is not None
            await asyncio.wait_for(queue.join(), timeout=1)

        assert [call.args[2] for call in mock_send.await_args_list] == [
            "part one",
            "part two",
            "part two",
        ]
    finally:
        await shutdown_workers()
        _flood_until.pop(1, None)


@pytest.mark.asyncio
async def test_queue_worker_retries_status_after_retryafter_without_consuming_task() -> None:
    bot = AsyncMock()

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._process_status_update_task",
                new_callable=AsyncMock,
                side_effect=[RetryAfter(1), None],
            ) as mock_status,
            patch(
                "ccbot.handlers.message_queue.asyncio.sleep",
                new_callable=AsyncMock,
            ),
            patch("ccbot.handlers.message_queue._audit_task_delivery") as mock_audit,
        ):
            await enqueue_status_update(
                bot,
                user_id=1,
                window_id="@7",
                status_text="⌘ Command\n```sh\npytest\n```",
                chat_id=100,
                thread_id=42,
            )
            queue = get_message_queue(1)
            assert queue is not None
            await asyncio.wait_for(queue.join(), timeout=1)

        assert mock_status.await_count == 2
        assert any(
            call.kwargs.get("action") == "retry_scheduled"
            and "mutable" in (call.kwargs.get("reason") or "")
            for call in mock_audit.call_args_list
        )
    finally:
        await shutdown_workers()
        _flood_until.pop(1, None)


@pytest.mark.asyncio
async def test_retryafter_audit_records_transport_and_queue_context(
    monkeypatch,
    tmp_path,
) -> None:
    bot = AsyncMock()
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    user_id = 81

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._process_status_update_task",
                new_callable=AsyncMock,
                side_effect=[RetryAfter(3), None],
            ),
            patch(
                "ccbot.handlers.message_queue.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            await enqueue_status_update(
                bot,
                user_id=user_id,
                window_id="@7",
                status_text="⌘ Command\n```sh\npytest\n```",
                chat_id=100,
                thread_id=42,
            )
            queue = get_message_queue(user_id)
            assert queue is not None
            await asyncio.wait_for(queue.join(), timeout=1)

        rows = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        retry = next(row for row in rows if row["action"] == "retry_scheduled")
        assert retry["transport_error_type"] == "retry_after"
        assert retry["error_class"] == "RetryAfter"
        assert retry["retry_after"] == 3
        assert retry["task_class"] == "mutable"
        assert retry["depth_at_enqueue"] == 0
        assert retry["depth_at_send"] == 0
        assert retry["queue_age_ms"] >= 0
        assert retry["backpressure_reason"].startswith("retry_after_scheduled:mutable")
    finally:
        await shutdown_workers()
        _flood_until.pop(user_id, None)


@pytest.mark.asyncio
async def test_pending_input_coalesce_audit_records_queue_context(
    monkeypatch,
    tmp_path,
) -> None:
    bot = AsyncMock()
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    user_id = 82
    blocker_started = asyncio.Event()
    unblock = asyncio.Event()

    async def _block_content(*_args, **_kwargs):
        blocker_started.set()
        await unblock.wait()

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._process_content_task",
                new_callable=AsyncMock,
                side_effect=_block_content,
            ),
            patch(
                "ccbot.handlers.message_queue._process_pending_input_update_task",
                new_callable=AsyncMock,
            ) as mock_pending,
        ):
            await enqueue_content_message(
                bot,
                user_id=user_id,
                window_id="@7",
                parts=["blocking durable content"],
                chat_id=100,
                thread_id=42,
            )
            await asyncio.wait_for(blocker_started.wait(), timeout=1)

            await enqueue_pending_input_update(
                bot,
                user_id=user_id,
                window_id="@7",
                pending_input_text="pending one",
                chat_id=100,
                thread_id=42,
                turn_generation=1,
            )
            await enqueue_pending_input_update(
                bot,
                user_id=user_id,
                window_id="@7",
                pending_input_text="pending two",
                chat_id=100,
                thread_id=42,
                turn_generation=1,
            )
            queue = get_message_queue(user_id)
            assert queue is not None
            assert queue.qsize() == 1

            unblock.set()
            await asyncio.wait_for(queue.join(), timeout=1)

        mock_pending.assert_awaited_once()
        delivered_task = mock_pending.await_args.args[2]
        assert delivered_task.text == "pending two"
        rows = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        coalesce = next(row for row in rows if row["action"] == "coalesce")
        assert coalesce["reason"].startswith("mutable_queue_coalesced:")
        assert coalesce["task_class"] == "mutable"
        assert coalesce["depth_at_enqueue"] == 0
        assert coalesce["depth_at_send"] == 1
        assert coalesce["queue_age_ms"] >= 0
    finally:
        unblock.set()
        await shutdown_workers()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enqueue_kind", "processor_name"),
    [
        ("status", "_process_status_update_task"),
        ("commentary", "_process_commentary_update_task"),
        ("plan", "_process_plan_update_task"),
        ("pending", "_process_pending_input_update_task"),
    ],
)
async def test_mutable_updates_coalesce_to_latest_while_queue_is_backed_up(
    enqueue_kind: str,
    processor_name: str,
) -> None:
    bot = AsyncMock()
    blocker_started = asyncio.Event()
    unblock = asyncio.Event()

    async def _block_content(*_args, **_kwargs):
        blocker_started.set()
        await unblock.wait()

    async def _enqueue_mutable(index: int) -> None:
        text = f"{enqueue_kind} {index}"
        if enqueue_kind == "status":
            await enqueue_status_update(
                bot,
                user_id=1,
                window_id="@7",
                status_text=text,
                chat_id=100,
                thread_id=42,
                turn_generation=1,
            )
        elif enqueue_kind == "commentary":
            await enqueue_commentary_update(
                bot,
                user_id=1,
                window_id="@7",
                commentary_text=text,
                parts=[text],
                chat_id=100,
                thread_id=42,
                turn_generation=1,
            )
        elif enqueue_kind == "plan":
            await enqueue_plan_update(
                bot,
                user_id=1,
                window_id="@7",
                plan_text=text,
                chat_id=100,
                thread_id=42,
                turn_generation=1,
            )
        else:
            await enqueue_pending_input_update(
                bot,
                user_id=1,
                window_id="@7",
                pending_input_text=text,
                chat_id=100,
                thread_id=42,
                turn_generation=1,
            )

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._process_content_task",
                new_callable=AsyncMock,
                side_effect=_block_content,
            ),
            patch(
                f"ccbot.handlers.message_queue.{processor_name}",
                new_callable=AsyncMock,
            ) as mock_processor,
            patch("ccbot.handlers.message_queue._audit_task_delivery") as mock_audit,
        ):
            await enqueue_content_message(
                bot,
                user_id=1,
                window_id="@7",
                parts=["blocking durable content"],
                chat_id=100,
                thread_id=42,
            )
            await asyncio.wait_for(blocker_started.wait(), timeout=1)

            for index in range(100):
                await _enqueue_mutable(index)

            queue = get_message_queue(1)
            assert queue is not None
            assert queue.qsize() == 1

            unblock.set()
            await asyncio.wait_for(queue.join(), timeout=1)

        mock_processor.assert_awaited_once()
        delivered_task = mock_processor.await_args.args[2]
        assert delivered_task.text == f"{enqueue_kind} 99"
        assert sum(
            1
            for call in mock_audit.call_args_list
            if call.kwargs.get("action") == "coalesce"
        ) == 99
    finally:
        unblock.set()
        await shutdown_workers()


@pytest.mark.asyncio
async def test_telegram_backlog_metrics_distinguish_durable_and_mutable_queue() -> None:
    bot = AsyncMock()
    blocker_started = asyncio.Event()

    async def _block_content(*_args, **_kwargs):
        blocker_started.set()
        await asyncio.Event().wait()

    try:
        with patch(
            "ccbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
            side_effect=_block_content,
        ):
            await enqueue_content_message(
                bot,
                user_id=1,
                window_id="@7",
                parts=["blocking durable content"],
                chat_id=100,
                thread_id=42,
            )
            await asyncio.wait_for(blocker_started.wait(), timeout=1)
            await enqueue_status_update(
                bot,
                user_id=1,
                window_id="@7",
                status_text="status waiting",
                chat_id=100,
                thread_id=42,
                turn_generation=1,
            )

            [metrics] = get_telegram_delivery_backlog_metrics(1)

        assert metrics["queue_depth"] == 1
        assert metrics["in_flight"] is True
        assert metrics["in_flight_task_type"] == "content"
        assert metrics["in_flight_task_class"] == "durable"
        assert metrics["mutable_count"] == 1
        assert metrics["durable_count"] == 0
        assert metrics["oldest_queued_age_seconds"] >= 0
    finally:
        await shutdown_workers()


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

    async def _fake_clear(_bot, _user_id, _thread_id_or_0, **_kwargs):
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
@pytest.mark.parametrize(
    "task",
    [
        MessageTask(
            task_type="content",
            window_id="@7",
            thread_id=42,
            parts=["final answer"],
            content_type="text",
            chat_id=-100,
            surface_key="t:-100:42",
        ),
        MessageTask(
            task_type="status_update",
            window_id="@7",
            thread_id=42,
            text="Thinking…",
            chat_id=-100,
            surface_key="t:-100:42",
        ),
    ],
)
async def test_stale_task_clears_pending_input_only_on_canonical_surface(
    task: MessageTask,
) -> None:
    legacy_key = mq._topic_state_key(1, 42)
    surface_key = mq._topic_state_key(
        1,
        42,
        chat_id=-100,
        surface_key="t:-100:42",
    )
    mq._pending_input_msg_info.clear()
    mq._pending_input_msg_info[legacy_key] = (701, "@7", "legacy pending")
    mq._pending_input_msg_info[surface_key] = (702, "@7", "surface pending")

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@9"
        mock_sm.get_topic_binding_state.return_value = "bound"
        bot = AsyncMock()
        if task.task_type == "content":
            await _process_content_task(bot, 1, task)
        else:
            await _process_status_update_task(bot, 1, task)

    deleted_ids = {call.kwargs["message_id"] for call in bot.delete_message.await_args_list}
    assert 702 in deleted_ids
    assert 701 not in deleted_ids
    assert legacy_key in mq._pending_input_msg_info
    assert surface_key not in mq._pending_input_msg_info
    mq._pending_input_msg_info.clear()


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
async def test_command_execution_plain_fallback_status_stays_on_surface_key() -> None:
    first = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho hi\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_exec",
        chat_id=-100,
        surface_key="t:-100:42",
        turn_generation=1,
    )
    second = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho hi\n```\n```sh\nhi\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_exec",
        chat_id=-100,
        surface_key="t:-100:42",
        turn_generation=1,
    )

    bot = AsyncMock()
    sent_first = AsyncMock()
    sent_first.message_id = 301
    bot.edit_message_text.side_effect = [Exception("markdown failed"), None]

    mq._tool_msg_ids.clear()
    legacy_key = mq._topic_state_key(1, 42)
    surface_key = mq._topic_state_key(1, 42, chat_id=-100, surface_key="t:-100:42")
    mq._status_msg_info.clear()
    mq._status_msg_info[legacy_key] = (801, "@7", "legacy status")
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                side_effect=[sent_first],
            ),
            patch(
                "ccbot.handlers.message_queue._do_send_status_message",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_status_send,
            patch("ccbot.handlers.message_queue.current_turn_generation", return_value=1),
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=type("Window", (), {"window_id": "@7"})()
            )
            mock_tmux.capture_pane = AsyncMock(
                return_value="output\n✻ Doing work\n──────────────────────────────"
            )
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = -100

            await _process_content_task(bot, 1, first)
            mock_status_send.reset_mock()
            await _process_content_task(bot, 1, second)

        assert bot.edit_message_text.await_count == 2
        mock_status_send.assert_awaited_once()
        await_args = mock_status_send.await_args
        assert await_args is not None
        assert await_args.kwargs["chat_id"] == -100
        assert await_args.kwargs["surface_key"] == "t:-100:42"
        assert await_args.args[2] == 42
        assert await_args.args[4] == "Doing work"
        assert mq._status_msg_info[legacy_key] == (801, "@7", "legacy status")
        assert surface_key != legacy_key
    finally:
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()


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
        mock_status.assert_not_awaited()
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
async def test_generated_image_terminal_preview_photo_and_text_failure_records_final_failure() -> None:
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="",
        content_type="generated_image_preview",
        semantic_kind="assistant_final",
        image_data=[("image/png", b"png-bytes")],
        image_caption="🖼 Generated Image\nRequest: frame",
        turn_generation=1,
    )

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
        ),
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=None,
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

    mock_send.assert_awaited_once()
    mock_status.assert_not_awaited()
    assert (
        mq.pop_assistant_final_delivery_failure(
            1,
            thread_id=42,
            chat_id=None,
            surface_key=None,
            window_id="@7",
            turn_generation=1,
        )
        == "assistant_final_delivery_incomplete"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["viewed_image_preview", "generated_image_preview"],
)
async def test_image_preview_sends_single_photo_without_closing_final_lanes(
    content_type: str,
) -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ contact_sheet.png",
        content_type=content_type,
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
async def test_image_preview_edits_same_turn_media_in_place() -> None:
    first_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ first.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"first-png")],
        image_caption="🖼 Viewed Image\nFile: first.png",
        turn_generation=1,
    )
    second_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ second.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"second-png")],
        image_caption="🖼 Viewed Image\nFile: second.png",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 2101

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
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100

            bot = AsyncMock()
            await _process_content_task(bot, 1, first_task)
            await _process_content_task(bot, 1, second_task)

        mock_photo.assert_awaited_once()
        bot.edit_message_media.assert_awaited_once()
        assert bot.edit_message_media.await_args.kwargs["message_id"] == 2101
        assert _image_preview_msg_info[(1, 42)].message_id == 2101
        assert _image_preview_msg_info[(1, 42)].turn_generation == 1
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_edit_noop_is_audited_without_resend() -> None:
    first_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ same.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"same-png")],
        image_caption="🖼 Viewed Image\nFile: same.png",
        turn_generation=1,
    )
    second_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ same.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"same-png")],
        image_caption="🖼 Viewed Image\nFile: same.png",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 2102

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
                "ccbot.handlers.message_queue._audit_task_delivery",
            ) as mock_audit,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()
            bot.edit_message_media.side_effect = BadRequest("Message is not modified")

            await _process_content_task(bot, 1, first_task)
            await _process_content_task(bot, 1, second_task)

        mock_photo.assert_awaited_once()
        bot.delete_message.assert_not_awaited()
        assert any(
            call.kwargs.get("action") == "edit_noop"
            and call.kwargs.get("reason") == "message_not_modified"
            for call in mock_audit.call_args_list
        )
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_retryafter_propagates_from_edit() -> None:
    first_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ first.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"first-png")],
        image_caption="🖼 Viewed Image\nFile: first.png",
        turn_generation=1,
    )
    second_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ second.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"second-png")],
        image_caption="🖼 Viewed Image\nFile: second.png",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 2103

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
            ),
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()
            bot.edit_message_media.side_effect = RetryAfter(3)

            await _process_content_task(bot, 1, first_task)
            with pytest.raises(RetryAfter):
                await _process_content_task(bot, 1, second_task)
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_edit_failure_deletes_then_sends_replacement() -> None:
    first_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ first.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"first-png")],
        image_caption="🖼 Viewed Image\nFile: first.png",
        turn_generation=1,
    )
    second_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ replacement.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"replacement-png")],
        image_caption="🖼 Viewed Image\nFile: replacement.png",
        turn_generation=1,
    )
    first_sent = AsyncMock()
    first_sent.message_id = 2104
    replacement_sent = AsyncMock()
    replacement_sent.message_id = 2105

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
                side_effect=[first_sent, replacement_sent],
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()
            bot.edit_message_media.side_effect = RuntimeError("edit exploded")

            await _process_content_task(bot, 1, first_task)
            await _process_content_task(bot, 1, second_task)

        assert mock_photo.await_count == 2
        bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=2104)
        assert _image_preview_msg_info[(1, 42)].message_id == 2105
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_delete_failure_suppresses_replacement_without_stacking() -> None:
    first_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ first.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"first-png")],
        image_caption="🖼 Viewed Image\nFile: first.png",
        turn_generation=1,
    )
    second_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ suppressed.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"suppressed-png")],
        image_caption="🖼 Viewed Image\nFile: suppressed.png",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 2106

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
            ) as mock_text,
            patch(
                "ccbot.handlers.message_queue._audit_task_delivery",
            ) as mock_audit,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()
            bot.edit_message_media.side_effect = RuntimeError("edit exploded")
            bot.delete_message.side_effect = RuntimeError("delete exploded")

            await _process_content_task(bot, 1, first_task)
            await _process_content_task(bot, 1, second_task)

        mock_photo.assert_awaited_once()
        mock_text.assert_not_awaited()
        assert _image_preview_msg_info[(1, 42)].message_id == 2106
        assert any(
            call.kwargs.get("reason") == "image_preview_no_stack_after_delete_failed"
            for call in mock_audit.call_args_list
        )
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_multi_image_preview_truncates_to_single_mutable_photo() -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ contact_sheet.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"first-png"), ("image/png", b"second-png")],
        image_caption="🖼 Viewed Image\nFile: contact_sheet.png",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 2107

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
                "ccbot.handlers.message_queue._audit_task_delivery",
            ) as mock_audit,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()

            await _process_content_task(bot, 1, preview_task)

        mock_photo.assert_awaited_once()
        assert len(mock_photo.await_args.args[2]) == 1
        assert any(
            call.kwargs.get("reason") == "multi_image_preview_truncated"
            for call in mock_audit.call_args_list
        )
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_terminal_final_deletes_tracked_image_preview_and_prevents_cross_turn_edit() -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ progress.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"progress-png")],
        image_caption="🖼 Viewed Image\nFile: progress.png",
        turn_generation=1,
    )
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final answer"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=1,
    )
    next_turn_preview = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ next.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"next-png")],
        image_caption="🖼 Viewed Image\nFile: next.png",
        turn_generation=2,
    )
    first_photo = AsyncMock()
    first_photo.message_id = 2108
    next_photo = AsyncMock()
    next_photo.message_id = 2109
    final_text = AsyncMock()
    final_text.message_id = 2110

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                side_effect=[first_photo, next_photo],
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=final_text,
            ),
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()

            open_new_turn_generation(1, 42)
            await _process_content_task(bot, 1, preview_task)
            await _process_content_task(bot, 1, final_task)
            open_new_turn_generation(1, 42)
            await _process_content_task(bot, 1, next_turn_preview)

        assert mock_photo.await_count == 2
        bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=2108)
        bot.edit_message_media.assert_not_awaited()
        assert _image_preview_msg_info[(1, 42)].message_id == 2109
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_new_turn_cleanup_deletes_preview_without_final_before_next_preview() -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ progress.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"progress-png")],
        image_caption="🖼 Viewed Image\nFile: progress.png",
        turn_generation=1,
    )
    next_turn_preview = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ next.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"next-png")],
        image_caption="🖼 Viewed Image\nFile: next.png",
        turn_generation=2,
    )
    first_photo = AsyncMock()
    first_photo.message_id = 2111
    next_photo = AsyncMock()
    next_photo.message_id = 2112

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                side_effect=[first_photo, next_photo],
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()

            open_new_turn_generation(1, 42)
            await _process_content_task(bot, 1, preview_task)
            await open_new_turn_generation_with_cleanup(bot, 1, 42)
            await _process_content_task(bot, 1, next_turn_preview)

        assert mock_photo.await_count == 2
        bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=2111)
        bot.edit_message_media.assert_not_awaited()
        assert _image_preview_msg_info[(1, 42)].message_id == 2112
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_new_turn_cleanup_advances_generation_before_delete_await_to_drop_stale_preview() -> (
    None
):
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ progress.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"progress-png")],
        image_caption="🖼 Viewed Image\nFile: progress.png",
        turn_generation=1,
    )
    stale_preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ stale.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"stale-png")],
        image_caption="🖼 Viewed Image\nFile: stale.png",
        turn_generation=1,
    )
    first_photo = AsyncMock()
    first_photo.message_id = 2115
    delete_started = asyncio.Event()
    allow_delete = asyncio.Event()

    async def slow_delete(*, chat_id: int, message_id: int) -> None:
        assert chat_id == 100
        assert message_id == 2115
        delete_started.set()
        await allow_delete.wait()

    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_photo",
                new_callable=AsyncMock,
                return_value=first_photo,
            ) as mock_photo,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
            patch("ccbot.handlers.message_queue._audit_task_delivery") as mock_audit,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()

            open_new_turn_generation(1, 42)
            await _process_content_task(bot, 1, preview_task)
            bot.delete_message.side_effect = slow_delete
            cleanup_task = asyncio.create_task(
                open_new_turn_generation_with_cleanup(bot, 1, 42)
            )
            await asyncio.wait_for(delete_started.wait(), timeout=1)

            assert current_turn_generation(1, 42) == 2
            await _process_content_task(bot, 1, stale_preview_task)
            allow_delete.set()
            assert await cleanup_task == 2

        mock_photo.assert_awaited_once()
        bot.edit_message_media.assert_not_awaited()
        assert (1, 42) not in _image_preview_msg_info
        assert any(
            call.kwargs.get("reason") == "stale_turn_generation"
            for call in mock_audit.call_args_list
        )
    finally:
        allow_delete.set()
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_clear_delete_failure_retains_tracking_to_prevent_stack() -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ progress.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"progress-png")],
        image_caption="🖼 Viewed Image\nFile: progress.png",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 2113

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
            ),
            patch(
                "ccbot.handlers.message_queue.log_telegram_delivery",
            ) as mock_audit,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()

            await _process_content_task(bot, 1, preview_task)
            bot.delete_message.side_effect = RuntimeError("delete failed")
            await clear_image_preview_message(bot, 1, 42)

        assert _image_preview_msg_info[(1, 42)].message_id == 2113
        assert (1, 42, 2113) in _image_preview_delete_retry_tasks
        assert any(
            call.kwargs.get("reason") == "clear_image_preview_failed"
            and call.kwargs.get("success") is False
            for call in mock_audit.call_args_list
        )
    finally:
        clear_commentary_lane_state(1, 42)


@pytest.mark.asyncio
async def test_image_preview_clear_retryafter_retains_tracking_and_does_not_raise() -> None:
    preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=[],
        text="• Viewed Image:\n  └ progress.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"progress-png")],
        image_caption="🖼 Viewed Image\nFile: progress.png",
        turn_generation=1,
    )
    sent_photo = AsyncMock()
    sent_photo.message_id = 2114

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
            ),
            patch(
                "ccbot.handlers.message_queue.log_telegram_delivery",
            ) as mock_audit,
            patch(
                "ccbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=object())
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_topic_binding_state.return_value = "bound"
            mock_sm.resolve_chat_id.return_value = 100
            bot = AsyncMock()

            await _process_content_task(bot, 1, preview_task)
            bot.delete_message.side_effect = RetryAfter(3)
            await clear_image_preview_message(bot, 1, 42)

        assert _image_preview_msg_info[(1, 42)].message_id == 2114
        assert (1, 42, 2114) in _image_preview_delete_retry_tasks
        assert any(
            call.kwargs.get("reason") == "clear_image_preview_retry_after"
            and call.kwargs.get("success") is False
            for call in mock_audit.call_args_list
        )
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
        mock_status.assert_not_awaited()
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
    assert rows[-1]["success"] is True
    assert rows[-1]["render_mode"] == "markdown_v2"
    assert rows[-1]["transport_outcome"] == "edit_noop"

    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_edit_success_audits_markdown_render_mode(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (501, "@7", "old status")

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
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

    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["success"] is True
    assert rows[-1]["render_mode"] == "markdown_v2"
    assert rows[-1]["transport_outcome"] == "edited"
    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_edit_plain_fallback_noop_audits_success(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (501, "@7", "old status")

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [
        Exception("formatted failed"),
        BadRequest("Message is not modified"),
    ]

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

    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["success"] is True
    assert rows[-1]["render_mode"] == "plain_text"
    assert rows[-1]["transport_outcome"] == "fallback_edit_noop"
    assert rows[-1]["formatted_error_class"] == "Exception"
    assert rows[-1]["plain_error_class"] == "BadRequest"
    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_edit_unknown_failure_preserves_existing_status_without_replacement(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (501, "@7", "old status")
    mq._persist_status_msg_info(key, (501, "@7", "old status"))

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [
        Exception("formatted edit timed out"),
        Exception("plain edit timed out"),
    ]

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

    assert bot.edit_message_text.await_count == 2
    mock_send.assert_not_awaited()
    assert mq._status_msg_info[key] == (501, "@7", "old status")
    persisted = mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]
    assert persisted["message_id"] == 501
    assert persisted["window_id"] == "@7"
    assert persisted["last_text"] == "old status"
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["action"] == "edit_failed_preserved"
    assert rows[-1]["success"] is False
    assert rows[-1]["reason"] == "status_edit_failed_old_maybe_visible"
    assert rows[-1]["message_id"] == 501
    assert rows[-1]["error_class"] == "Exception"
    assert rows[-1]["transport_error_type"] == "exception"
    assert "render_mode" not in rows[-1]
    assert rows[-1]["transport_outcome"] == "failed"
    assert rows[-1]["formatted_error_class"] == "Exception"
    assert rows[-1]["plain_error_class"] == "Exception"

    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_status_edit_plain_fallback_success_updates_existing_status(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (501, "@7", "old status")
    mq._persist_status_msg_info(key, (501, "@7", "old status"))

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [Exception("formatted failed"), None]

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

    assert bot.edit_message_text.await_count == 2
    mock_send.assert_not_awaited()
    assert mq._status_msg_info[key] == (501, "@7", "new status")
    persisted = mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]
    assert persisted["message_id"] == 501
    assert persisted["window_id"] == "@7"
    assert persisted["last_text"] == "new status"
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["render_mode"] == "plain_text"
    assert rows[-1]["transport_outcome"] == "fallback_edited"
    assert rows[-1]["formatted_error_class"] == "Exception"

    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_status_edit_plain_fallback_known_gone_replaces_status(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (501, "@7", "old status")
    mq._persist_status_msg_info(key, (501, "@7", "old status"))
    sent = AsyncMock()
    sent.message_id = 777

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [
        Exception("formatted failed"),
        BadRequest("Message not found"),
    ]

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
            return_value=sent,
        ) as mock_send,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    assert bot.edit_message_text.await_count == 2
    mock_send.assert_awaited_once()
    assert mq._status_msg_info[key] == (777, "@7", "new status")

    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_content_send_audit_records_markdown_render_mode(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command output\n```text\n189 passed\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
    )
    bot = AsyncMock()
    sent = SimpleNamespace(message_id=700)
    bot.send_message.return_value = sent

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=type("Window", (), {"window_id": "@7"})()
        )
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_content_task(bot, 1, task)

    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    send_row = next(row for row in rows if row.get("action") == "send" and row.get("message_id") == 700)
    assert send_row["success"] is True
    assert send_row["render_mode"] == "markdown_v2"
    assert send_row["transport_outcome"] == "sent"


@pytest.mark.asyncio
async def test_content_send_audit_records_plain_fallback_render_mode(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command output\n```text\n189 passed\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
    )
    bot = AsyncMock()
    sent = SimpleNamespace(message_id=701)
    bot.send_message.side_effect = [Exception("bad markdown token=abc"), sent]

    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
    ):
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=type("Window", (), {"window_id": "@7"})()
        )
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_content_task(bot, 1, task)

    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    send_row = next(row for row in rows if row.get("action") == "send" and row.get("message_id") == 701)
    assert send_row["render_mode"] == "plain_text"
    assert send_row["transport_outcome"] == "fallback_sent"
    assert send_row["formatted_error_class"] == "Exception"
    assert "token=abc" not in json.dumps(send_row)


@pytest.mark.asyncio
async def test_command_edit_audit_records_plain_fallback_render_mode(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    first = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho hi\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_exec_audit",
    )
    second = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["⌘ Command\n```sh\necho hi\n```\n```text\nhi\n```"],
        content_type="command_execution",
        semantic_kind="command_execution",
        tool_use_id="call_exec_audit",
    )
    bot = AsyncMock()
    sent_first = SimpleNamespace(message_id=801)
    bot.send_message.return_value = sent_first
    bot.edit_message_text.side_effect = [Exception("bad markdown token=abc"), SimpleNamespace(message_id=801)]

    mq._tool_msg_ids.clear()
    try:
        with (
            patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
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

        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        edit_row = next(row for row in rows if row.get("action") == "edit" and row.get("message_id") == 801)
        assert edit_row["render_mode"] == "plain_text"
        assert edit_row["transport_outcome"] == "fallback_edited"
        assert edit_row["formatted_error_class"] == "Exception"
        assert "token=abc" not in json.dumps(edit_row)
    finally:
        mq._tool_msg_ids.clear()


@pytest.mark.asyncio
async def test_convert_status_to_content_audits_plain_fallback(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42, chat_id=100)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (901, "@7", "old status")
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [Exception("bad markdown token=abc"), SimpleNamespace(message_id=901)]

    converted = await mq._convert_status_to_content(bot, 1, 42, "@7", "new content", chat_id=100)

    assert converted == 901
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["success"] is True
    assert rows[-1]["render_mode"] == "plain_text"
    assert rows[-1]["transport_outcome"] == "fallback_edited"
    assert rows[-1]["formatted_error_class"] == "Exception"
    assert "token=abc" not in json.dumps(rows[-1])
    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_convert_status_to_content_audits_total_failure(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42, chat_id=100)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (902, "@7", "old status")
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [Exception("bad markdown"), RuntimeError("telegram down")]

    converted = await mq._convert_status_to_content(bot, 1, 42, "@7", "new content", chat_id=100)

    assert converted is None
    rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["success"] is False
    assert "render_mode" not in rows[-1]
    assert rows[-1]["transport_outcome"] == "failed"
    assert rows[-1]["formatted_error_class"] == "Exception"
    assert rows[-1]["plain_error_class"] == "RuntimeError"
    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_status_send_audit_records_plain_fallback_render_mode(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    sent = SimpleNamespace(message_id=601)
    bot = AsyncMock()
    bot.send_message.side_effect = [Exception("bad markdown token=abc"), sent]

    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        delivered = await mq._do_send_status_message(
            bot,
            1,
            42,
            "@7",
            "⌘ Command\n```sh\npytest\n```",
        )

    assert delivered is True
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["render_mode"] == "plain_text"
    assert rows[-1]["transport_outcome"] == "fallback_sent"
    assert rows[-1]["formatted_error_class"] == "Exception"
    assert "token=abc" not in json.dumps(rows[-1])
    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_send_audit_records_total_failure(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    bot = AsyncMock()
    bot.send_message.side_effect = [
        Exception("bad markdown"),
        RuntimeError("telegram down"),
    ]

    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        delivered = await mq._do_send_status_message(bot, 1, 42, "@7", "new status")

    assert delivered is False
    rows = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["success"] is False
    assert "render_mode" not in rows[-1]
    assert rows[-1]["transport_outcome"] == "failed"
    assert rows[-1]["formatted_error_class"] == "Exception"
    assert rows[-1]["plain_error_class"] == "RuntimeError"
    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_edit_retry_after_from_formatted_edit_is_re_raised(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (501, "@7", "old status")
    mq._persist_status_msg_info(key, (501, "@7", "old status"))

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = RetryAfter(1)

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
        with pytest.raises(RetryAfter):
            await _process_status_update_task(bot, 1, task)

    mock_send.assert_not_awaited()
    assert mq._status_msg_info[key] == (501, "@7", "old status")

    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_status_edit_retry_after_from_plain_fallback_is_re_raised(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info.clear()
    mq._status_msg_info[key] = (501, "@7", "old status")
    mq._persist_status_msg_info(key, (501, "@7", "old status"))

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [Exception("formatted failed"), RetryAfter(1)]

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
        with pytest.raises(RetryAfter):
            await _process_status_update_task(bot, 1, task)

    assert bot.edit_message_text.await_count == 2
    mock_send.assert_not_awaited()
    assert mq._status_msg_info[key] == (501, "@7", "old status")

    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_status_update_hydrates_persisted_status_message_after_restart(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._persist_status_msg_info((1, 42), (501, "@7", "old status"))

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
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
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
        ) as mock_send,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 501
    mock_send.assert_not_awaited()
    assert mq._status_msg_info[(1, 42)] == (501, "@7", "new status")

    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info((1, 42))


@pytest.mark.asyncio
async def test_status_update_drops_missing_persisted_status_and_sends_replacement(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._persist_status_msg_info((1, 42), (501, "@7", "old status"))

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
        turn_generation=0,
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = BadRequest("Message not found")
    sent = AsyncMock()
    sent.message_id = 777

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
            return_value=sent,
        ) as mock_send,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    bot.edit_message_text.assert_awaited_once()
    mock_send.assert_awaited_once()
    assert mq._status_msg_info[(1, 42)] == (777, "@7", "new status")

    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info((1, 42))

@pytest.mark.asyncio
async def test_status_hydration_is_scoped_by_surface_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    legacy_key = mq._topic_state_key(1, 42)
    surface_key = mq._topic_state_key(1, 42, chat_id=-100, surface_key="t:-100:42")
    mq._persist_status_msg_info(legacy_key, (501, "@7", "legacy status"))
    mq._persist_status_msg_info(surface_key, (777, "@7", "surface status"))

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        chat_id=-100,
        surface_key="t:-100:42",
        text="surface update",
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
        mock_sm.resolve_chat_id.return_value = -100
        await _process_status_update_task(bot, 1, task)

    assert bot.edit_message_text.await_args.kwargs["message_id"] == 777
    assert mq._status_msg_info[surface_key] == (777, "@7", "surface update")
    assert legacy_key not in mq._status_msg_info

    mq._clear_persisted_status_msg_info(legacy_key)
    mq._clear_persisted_status_msg_info(surface_key)
    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_hydration_ignores_cross_window_registry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._persist_status_msg_info(key, (501, "@8", "old status"))
    sent = AsyncMock()
    sent.message_id = 777

    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="new status",
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
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=sent,
        ) as mock_send,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await _process_status_update_task(bot, 1, task)

    bot.edit_message_text.assert_not_awaited()
    mock_send.assert_awaited_once()
    assert mq._status_msg_info[key] == (777, "@7", "new status")

    mq._clear_persisted_status_msg_info(key)
    mq._status_msg_info.clear()


def test_poll_only_and_empty_status_do_not_persist(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    key = mq._topic_state_key(1, 42)

    assert mq._is_poll_only_status_text("🛠 Tool\nwrite_stdin(session 1, poll)")
    assert mq._normalize_technical_status_text(
        "↳ Tool Output\n```text\nChunk ID: x\nWall time: 0\nProcess running with session ID 1\nOriginal token count: 0\nOutput:\n```\npreview 0/0 lines"
    ) == ""
    assert mq._load_status_artifacts() == {}
    assert key not in mq._status_msg_info

@pytest.mark.asyncio
async def test_clear_status_message_deletes_persisted_status_after_restart(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._persist_status_msg_info(key, (501, "@7", "old status"))

    bot = AsyncMock()
    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        await mq._do_clear_status_message(bot, 1, 42)

    bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=501)
    assert mq._load_status_artifacts() == {}
    assert key not in mq._status_msg_info

@pytest.mark.asyncio
async def test_clear_status_message_preserves_cross_window_persisted_status(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._persist_status_msg_info(key, (501, "@8", "old status"))

    bot = AsyncMock()
    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        await mq._do_clear_status_message(bot, 1, 42, expected_window_id="@7")

    bot.delete_message.assert_not_awaited()
    assert mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]["message_id"] == 501
    assert key not in mq._status_msg_info


@pytest.mark.asyncio
async def test_direct_send_status_deletes_persisted_status_after_restart(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._persist_status_msg_info(key, (501, "@7", "old status"))
    sent = AsyncMock()
    sent.message_id = 777
    bot = AsyncMock()

    with (
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=sent,
        ) as mock_send,
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await mq._do_send_status_message(bot, 1, 42, "@7", "new status")

    bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=501)
    mock_send.assert_awaited_once()
    assert mq._status_msg_info[key] == (777, "@7", "new status")
    assert mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]["message_id"] == 777

@pytest.mark.asyncio
async def test_queued_status_clear_preserves_cross_window_persisted_status(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    await mq.shutdown_workers()
    key = mq._topic_state_key(100, 42)
    mq._persist_status_msg_info(key, (501, "@8", "old status"))
    bot = AsyncMock()

    await mq.enqueue_status_update(bot, 100, "@7", None, thread_id=42)
    queue = mq.get_or_create_queue(bot, 100)
    await queue.join()

    bot.delete_message.assert_not_awaited()
    assert mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]["message_id"] == 501
    assert key not in mq._status_msg_info


@pytest.mark.asyncio
async def test_direct_send_status_preserves_cross_window_in_memory_status(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info[key] = (501, "@8", "old status")
    mq._persist_status_msg_info(key, (501, "@8", "old status"))
    sent = AsyncMock()
    sent.message_id = 777
    bot = AsyncMock()

    with (
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=sent,
        ),
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await mq._do_send_status_message(bot, 1, 42, "@7", "new status")

    bot.delete_message.assert_not_awaited()
    assert mq._status_msg_info[key] == (777, "@7", "new status")
    assert mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]["message_id"] == 777
    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


@pytest.mark.asyncio
async def test_convert_status_to_content_hydrates_persisted_status_after_restart(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(
        delivery_audit.config, "telegram_delivery_audit_file", audit_path
    )
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._persist_status_msg_info(key, (501, "@7", "old status"))

    bot = AsyncMock()
    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        result = await mq._convert_status_to_content(
            bot,
            1,
            42,
            "@7",
            "Final content",
        )

    assert result == 501
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 501
    assert mq._load_status_artifacts() == {}
    assert key not in mq._status_msg_info


@pytest.mark.asyncio
async def test_convert_status_to_content_ignores_cross_window_persisted_status(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._persist_status_msg_info(key, (501, "@8", "old status"))

    bot = AsyncMock()
    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        result = await mq._convert_status_to_content(
            bot,
            1,
            42,
            "@7",
            "Final content",
        )

    assert result is None
    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    assert mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]["message_id"] == 501
    mq._clear_persisted_status_msg_info(key)

@pytest.mark.asyncio
async def test_convert_status_to_content_preserves_cross_window_in_memory_status(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    key = mq._topic_state_key(1, 42)
    mq._status_msg_info[key] = (501, "@8", "old status")
    mq._persist_status_msg_info(key, (501, "@8", "old status"))

    bot = AsyncMock()
    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        result = await mq._convert_status_to_content(bot, 1, 42, "@7", "Final content")

    assert result is None
    bot.edit_message_text.assert_not_awaited()
    bot.delete_message.assert_not_awaited()
    assert mq._status_msg_info[key] == (501, "@8", "old status")
    assert mq._load_status_artifacts()[mq._status_key_to_storage_key(key)]["message_id"] == 501
    mq._status_msg_info.clear()
    mq._clear_persisted_status_msg_info(key)


def test_normalize_bare_command_status_wraps_shell_fence() -> None:
    rendered = mq._normalize_technical_status_text(
        "⌘ Command set -euo pipefail preview 1/11 lines"
    )

    assert rendered == "⌘ Command\n```sh\nset -euo pipefail\n```\npreview 1/11 lines"


def test_normalize_already_fenced_command_status_is_idempotent() -> None:
    text = "⌘ Command\n```sh\necho ok\n```\npreview 1/2 lines"

    assert mq._normalize_technical_status_text(text) == text


def test_normalize_command_output_status_keeps_output_category() -> None:
    text = "⌘ Command output\n```json\n{\"ok\": true}\n```"

    assert mq._normalize_technical_status_text(text) == text


def test_normalize_bare_tool_status_wraps_text_fence() -> None:
    rendered = mq._normalize_technical_status_text("🛠 Tool\nread_file")

    assert rendered == "🛠 Tool\n```text\nread_file\n```"


def test_normalize_raw_tool_status_wraps_text_fence() -> None:
    rendered = mq._normalize_technical_status_text("Tool read_file Reading chunk 1/2")

    assert rendered == "🛠 Tool\n```text\nread_file Reading chunk 1/2\n```"


@pytest.mark.asyncio
async def test_compact_status_renders_delivered_history_above_current_panel(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._technical_status_history.clear()
    bot = AsyncMock()
    sent = SimpleNamespace(message_id=501)

    first = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="⌘ Command pytest -q preview 1/2 lines",
        turn_generation=1,
    )
    second = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="⌘ Command ruff check preview 1/1 lines",
        turn_generation=1,
    )

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._is_task_binding_active",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("ccbot.handlers.message_queue.current_turn_generation", return_value=1),
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent,
            ) as mock_send,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            await _process_status_update_task(bot, 1, first)
            await _process_status_update_task(bot, 1, second)

        await_args = mock_send.await_args
        assert await_args is not None
        sent_text = await_args.args[2]
        assert sent_text.startswith('💻 terminal: "pytest -q"')
        assert "\n\n⌘ Command\n```sh\npytest -q\n```" in sent_text

        edit_args = bot.edit_message_text.await_args
        assert edit_args is not None
        edited_text = edit_args.kwargs["text"]
        assert '💻 terminal: "pytest \\-q"' in edited_text
        assert '💻 terminal: "ruff check"' in edited_text
        assert "\n\n⌘ Command\n```sh\nruff check\n```" in edited_text
        assert mq._status_msg_info[mq._topic_state_key(1, 42)][0] == 501
        assert mq._technical_status_history[(mq._topic_state_key(1, 42), "@7", 1)] == (
            '💻 terminal: "pytest -q"',
            '💻 terminal: "ruff check"',
        )
    finally:
        mq._status_msg_info.clear()
        mq._technical_status_history.clear()


@pytest.mark.asyncio
async def test_compact_status_does_not_commit_history_when_initial_send_fails(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._technical_status_history.clear()
    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="⌘ Command pytest -q preview 1/2 lines",
        turn_generation=1,
    )

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._is_task_binding_active",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("ccbot.handlers.message_queue.current_turn_generation", return_value=1),
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_sm.resolve_chat_id.return_value = 100
            await _process_status_update_task(AsyncMock(), 1, task)

        assert mq._status_msg_info == {}
        assert mq._technical_status_history == {}
    finally:
        mq._status_msg_info.clear()
        mq._technical_status_history.clear()


@pytest.mark.asyncio
async def test_compact_tool_status_renders_history_with_fenced_detail_panel(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._technical_status_history.clear()
    bot = AsyncMock()
    sent = SimpleNamespace(message_id=501)
    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="🛠 Tool\nread_file",
        turn_generation=1,
    )

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._is_task_binding_active",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("ccbot.handlers.message_queue.current_turn_generation", return_value=1),
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
            patch(
                "ccbot.handlers.message_queue.send_with_fallback",
                new_callable=AsyncMock,
                return_value=sent,
            ) as mock_send,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            await _process_status_update_task(bot, 1, task)

        await_args = mock_send.await_args
        assert await_args is not None
        sent_text = await_args.args[2]
        assert sent_text.startswith("🛠 read_file")
        assert "\n\n🛠 Tool\n```text\nread_file\n```" in sent_text
    finally:
        mq._status_msg_info.clear()
        mq._technical_status_history.clear()


@pytest.mark.asyncio
async def test_compact_status_commits_history_after_plain_fallback_edit(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._technical_status_history.clear()
    key = mq._topic_state_key(1, 42)
    old_text = '💻 terminal: "pytest -q"\n\n⌘ Command\n```sh\npytest -q\n```'
    mq._status_msg_info[key] = (501, "@7", old_text)
    mq._technical_status_history[(key, "@7", 1)] = ('💻 terminal: "pytest -q"',)
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [Exception("markdown failed"), None]
    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="⌘ Command ruff check preview 1/1 lines",
        turn_generation=1,
    )

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._is_task_binding_active",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("ccbot.handlers.message_queue.current_turn_generation", return_value=1),
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            await _process_status_update_task(bot, 1, task)

        assert bot.edit_message_text.await_count == 2
        assert mq._technical_status_history[(key, "@7", 1)] == (
            '💻 terminal: "pytest -q"',
            '💻 terminal: "ruff check"',
        )
        assert '💻 terminal: "ruff check"' in mq._status_msg_info[key][2]
    finally:
        mq._status_msg_info.clear()
        mq._technical_status_history.clear()


@pytest.mark.asyncio
async def test_compact_status_preserves_history_after_unknown_edit_failure(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._technical_status_history.clear()
    key = mq._topic_state_key(1, 42)
    old_text = '💻 terminal: "pytest -q"\n\n⌘ Command\n```sh\npytest -q\n```'
    mq._status_msg_info[key] = (501, "@7", old_text)
    mq._technical_status_history[(key, "@7", 1)] = ('💻 terminal: "pytest -q"',)
    bot = AsyncMock()
    bot.edit_message_text.side_effect = [Exception("markdown failed"), Exception("plain failed")]
    task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="⌘ Command ruff check preview 1/1 lines",
        turn_generation=1,
    )

    try:
        with (
            patch(
                "ccbot.handlers.message_queue._is_task_binding_active",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch("ccbot.handlers.message_queue.current_turn_generation", return_value=1),
            patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        ):
            mock_sm.resolve_chat_id.return_value = 100
            await _process_status_update_task(bot, 1, task)

        assert bot.edit_message_text.await_count == 2
        assert mq._technical_status_history[(key, "@7", 1)] == (
            '💻 terminal: "pytest -q"',
        )
        assert mq._status_msg_info[key] == (501, "@7", old_text)
    finally:
        mq._status_msg_info.clear()
        mq._technical_status_history.clear()


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
    assert sent_text.startswith('↳ output: "✓ 22')
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
    assert rows[-1]["preview"].startswith('↳ output: "✓ 22')

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


@pytest.mark.asyncio
async def test_status_send_typing_uses_throttled_surface_helper() -> None:
    bot = AsyncMock()

    with (
        patch("ccbot.handlers.message_queue.send_runtime_update_typing_once", new_callable=AsyncMock) as mock_typing,
        patch("ccbot.handlers.message_queue.send_with_fallback", new_callable=AsyncMock) as mock_send,
    ):
        mock_send.return_value = SimpleNamespace(message_id=77)
        await mq._do_send_status_message(
            bot,
            1,
            42,
            "@7",
            "Working (1m • esc to interrupt)",
            chat_id=-100200300,
            surface_key="t:-100200300:42",
        )

    mock_typing.assert_awaited_once_with(
        bot,
        1,
        chat_id=-100200300,
        thread_id=42,
        surface_key="t:-100200300:42",
        window_id="@7",
    )
    bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_control_status_bypasses_closed_technical_status(
    monkeypatch, tmp_path
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    mq._technical_status_closed.clear()
    mq._mark_technical_status_closed(100, 42)

    sent = AsyncMock()
    sent.message_id = 777
    bot = AsyncMock()
    await mq.shutdown_workers()

    with (
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=sent,
        ),
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100
        await mq.enqueue_status_update(
            bot,
            100,
            "@7",
            "🎯 Codex goal\nStatus: complete",
            thread_id=42,
            content_type="terminal_control_panel",
            semantic_kind="terminal_control",
        )
        queue = mq.get_or_create_queue(bot, 100)
        await queue.join()

    rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert rows[-1]["action"] == "send"
    assert rows[-1]["content_type"] == "terminal_control_panel"
    assert rows[-1]["semantic_kind"] == "terminal_control"
    assert mq._status_msg_info[mq._topic_state_key(100, 42)][0] == 777
    await mq.shutdown_workers()
    mq._technical_status_closed.clear()


@pytest.mark.asyncio
async def test_omx_workflow_status_does_not_bypass_closed_technical_status() -> None:
    mq._technical_status_closed.clear()
    mq._mark_technical_status_closed(100, 42)
    bot = AsyncMock()

    with patch("ccbot.handlers.message_queue.get_or_create_queue") as mock_queue_factory:
        await mq.enqueue_status_update(
            bot,
            100,
            "@7",
            "🧭 OMX ultragoal 1/1 · G001 · running",
            thread_id=42,
            content_type=OMX_WORKFLOW_PANEL_CONTENT_TYPE,
            semantic_kind=OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
        )

    mock_queue_factory.assert_not_called()
    mq._technical_status_closed.clear()


@pytest.mark.asyncio
async def test_omx_workflow_status_uses_separate_mutable_status_lane(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", tmp_path / "audit.jsonl")
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    await mq.shutdown_workers()

    sent_omx = SimpleNamespace(message_id=101)
    sent_technical = SimpleNamespace(message_id=202)
    bot = AsyncMock()

    with (
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch("ccbot.handlers.message_queue.send_with_fallback", new_callable=AsyncMock) as mock_send,
        patch("ccbot.handlers.message_queue.current_turn_generation", return_value=0),
        patch("ccbot.handlers.message_queue._is_task_binding_active", new_callable=AsyncMock, return_value=True),
    ):
        mock_sm.resolve_chat_id.return_value = 100
        mock_send.side_effect = [sent_omx, sent_technical]
        await enqueue_status_update(
            bot,
            1,
            "@7",
            "🧭 OMX ultragoal 4/8 · G005 · running",
            thread_id=42,
            content_type=OMX_WORKFLOW_PANEL_CONTENT_TYPE,
            semantic_kind=OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
        )
        await enqueue_status_update(
            bot,
            1,
            "@7",
            "⌘ Command\n```sh\npytest\n```",
            thread_id=42,
        )
        queue = mq.get_or_create_queue(bot, 1)
        await queue.join()

    base_key = mq._topic_state_key(1, 42)
    omx_key = mq._status_tracking_key(
        base_key,
        content_type=OMX_WORKFLOW_PANEL_CONTENT_TYPE,
        semantic_kind=OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
    )
    assert mq._status_msg_info[omx_key][0] == 101
    assert mq._status_msg_info[base_key][0] == 202
    bot.edit_message_text.assert_not_awaited()
    await mq.shutdown_workers()
    mq._status_msg_info.clear()


@pytest.mark.asyncio
async def test_status_clear_deletes_all_status_lanes_with_lane_metadata(monkeypatch, tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    monkeypatch.setattr(mq.config, "config_dir", tmp_path)
    mq._status_msg_info.clear()
    await mq.shutdown_workers()

    base_key = mq._topic_state_key(1, 42)
    omx_key = mq._status_tracking_key(
        base_key,
        content_type=OMX_WORKFLOW_PANEL_CONTENT_TYPE,
        semantic_kind=OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
    )
    mq._status_msg_info[base_key] = (202, "@7", "technical")
    mq._status_msg_info[omx_key] = (101, "@7", "omx")
    mq._persist_status_msg_info(base_key, (202, "@7", "technical"))
    mq._persist_status_msg_info(
        omx_key,
        (101, "@7", "omx"),
        content_type=OMX_WORKFLOW_PANEL_CONTENT_TYPE,
        semantic_kind=OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
    )
    bot = AsyncMock()

    with (
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch("ccbot.handlers.message_queue.current_turn_generation", return_value=0),
        patch("ccbot.handlers.message_queue._is_task_binding_active", new_callable=AsyncMock, return_value=True),
    ):
        mock_sm.resolve_chat_id.return_value = 100
        await enqueue_status_update(bot, 1, "@7", None, thread_id=42)
        queue = mq.get_or_create_queue(bot, 1)
        await queue.join()

    assert bot.delete_message.await_count == 2
    assert base_key not in mq._status_msg_info
    assert omx_key not in mq._status_msg_info
    rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
    deleted_semantics = {row["semantic_kind"] for row in rows if row["action"] == "delete"}
    assert {"technical_status", OMX_WORKFLOW_STATUS_SEMANTIC_KIND} <= deleted_semantics
    await mq.shutdown_workers()
    mq._status_msg_info.clear()

@pytest.mark.asyncio
async def test_final_barrier_drops_queued_mutable_progress_but_preserves_pending_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    audit_path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    queue: asyncio.Queue[MessageTask] = asyncio.Queue()
    lock = asyncio.Lock()
    user_id = 987654
    final_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=3,
    )
    status_task = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="⌘ Command running",
        content_type="status",
        semantic_kind="technical_status",
        turn_generation=3,
    )
    commentary_task = MessageTask(
        task_type="commentary_update",
        window_id="@7",
        thread_id=42,
        text="Working...",
        content_type="commentary",
        semantic_kind="commentary",
        turn_generation=3,
    )
    image_preview_task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        text="• Viewed Image\n  └ preview.png",
        content_type="viewed_image_preview",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        image_data=[("image/png", b"preview")],
        turn_generation=3,
    )
    plan_task = MessageTask(
        task_type="plan_update",
        window_id="@7",
        thread_id=42,
        text="Plan updated",
        content_type="plan_update",
        semantic_kind="plan_update",
        turn_generation=3,
    )
    pending_task = MessageTask(
        task_type="pending_input_update",
        window_id="@7",
        thread_id=42,
        text="queued future input",
        semantic_kind="pending_input",
        turn_generation=3,
    )
    status_clear_task = MessageTask(
        task_type="status_clear",
        window_id="@7",
        thread_id=42,
        text="clear status",
        content_type="status",
        semantic_kind="technical_status",
        turn_generation=3,
    )
    commentary_clear_task = MessageTask(
        task_type="commentary_clear",
        window_id="@7",
        thread_id=42,
        text="clear commentary",
        content_type="commentary",
        semantic_kind="commentary",
        turn_generation=3,
    )
    plan_clear_task = MessageTask(
        task_type="plan_clear",
        window_id="@7",
        thread_id=42,
        text="clear plan",
        content_type="plan_update",
        semantic_kind="plan_update",
        turn_generation=3,
    )
    other_turn_status = MessageTask(
        task_type="status_update",
        window_id="@7",
        thread_id=42,
        text="older turn",
        content_type="status",
        semantic_kind="technical_status",
        turn_generation=2,
    )
    for task in [
        status_task,
        commentary_task,
        image_preview_task,
        plan_task,
        pending_task,
        status_clear_task,
        commentary_clear_task,
        plan_clear_task,
        other_turn_status,
    ]:
        queue.put_nowait(task)

    with patch("ccbot.handlers.message_queue.session_manager") as mock_sm:
        mock_sm.resolve_chat_id.return_value = 100
        dropped = await mq._drop_queued_mutable_progress_before_final(
            queue,
            lock,
            user_id,
            final_task,
        )

    remaining = mq._inspect_queue(queue)
    assert dropped == 4
    assert pending_task in remaining
    assert status_clear_task in remaining
    assert commentary_clear_task in remaining
    assert plan_clear_task in remaining
    assert other_turn_status in remaining
    assert status_task not in remaining
    assert commentary_task not in remaining
    assert image_preview_task not in remaining
    assert plan_task not in remaining
    rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
    suppressed = [
        row
        for row in rows
        if row["reason"] == "final_barrier_dropped_queued_mutable_progress"
    ]
    assert len(suppressed) == 4
    assert {row["semantic_kind"] for row in suppressed} >= {
        "technical_status",
        "commentary",
        "plan_update",
        IMAGE_PREVIEW_SEMANTIC_KIND,
    }


@pytest.mark.asyncio
async def test_assistant_final_send_failure_records_terminal_delivery_failure() -> None:
    task = MessageTask(
        task_type="content",
        window_id="@7",
        thread_id=42,
        parts=["Final answer"],
        content_type="text",
        semantic_kind="assistant_final",
        turn_generation=5,
    )
    with (
        patch("ccbot.handlers.message_queue.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.message_queue.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.message_queue.current_turn_generation",
            return_value=5,
        ),
        patch(
            "ccbot.handlers.message_queue.send_with_fallback",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "ccbot.handlers.message_queue._check_and_send_status",
            new_callable=AsyncMock,
        ) as mock_status,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=object())
        mock_sm.get_window_for_thread.return_value = "@7"
        mock_sm.get_topic_binding_state.return_value = "bound"
        mock_sm.resolve_chat_id.return_value = 100

        await _process_content_task(AsyncMock(), 123, task)

    assert (
        mq.pop_assistant_final_delivery_failure(
            123,
            thread_id=42,
            chat_id=None,
            surface_key=None,
            window_id="@7",
            turn_generation=5,
        )
        == "assistant_final_delivery_incomplete"
    )
    mock_status.assert_not_awaited()
