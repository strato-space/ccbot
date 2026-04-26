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
