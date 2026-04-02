"""Focused tests for message queue merge invariants."""

from ccbot.handlers.message_queue import MessageTask, _can_merge_tasks


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
