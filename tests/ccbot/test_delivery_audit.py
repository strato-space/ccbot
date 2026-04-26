"""Tests for Telegram delivery audit logging."""

import json

from ccbot import delivery_audit


def test_delivery_audit_writes_compact_jsonl(monkeypatch, tmp_path) -> None:
    path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", path)

    delivery_audit.log_telegram_delivery(
        action="send",
        user_id=1,
        chat_id=2,
        thread_id=3,
        message_id=4,
        window_id="@7",
        task_type="content",
        content_type="tool_result",
        semantic_kind="tool_output",
        text="line 1\nline 2",
    )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["action"] == "send"
    assert row["success"] is True
    assert row["content_type"] == "tool_result"
    assert row["text_len"] == len("line 1\nline 2")
    assert row["preview"] == "line 1 line 2"
    assert len(row["text_sha16"]) == 16


def test_delivery_audit_records_schema_and_negative_lifecycle(
    monkeypatch, tmp_path
) -> None:
    path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", path)

    delivery_audit.log_telegram_delivery(
        action="suppress",
        user_id=1,
        chat_id=2,
        thread_id=3,
        message_id=None,
        window_id="@7",
        task_type="status_update",
        content_type="status",
        semantic_kind="technical_status",
        text="🛠 Tool\nwrite_stdin(session 82998, poll)",
        reason="poll_without_existing_status",
        turn_generation=9,
        tool_use_id="call_123",
    )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["schema_version"] == 1
    assert row["action"] == "suppress"
    assert row["reason"] == "poll_without_existing_status"
    assert row["turn_generation"] == 9
    assert row["tool_use_id"] == "call_123"
