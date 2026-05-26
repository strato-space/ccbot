"""Tests for Telegram delivery audit logging."""

import json

from telegram.error import RetryAfter, TimedOut

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


def test_delivery_audit_records_video_media_metadata(monkeypatch, tmp_path) -> None:
    path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", path)

    media = {
        "request": {
            "type": "video",
            "method": "send_video",
            "width": 720,
            "height": 1280,
            "duration": 55,
            "supports_streaming": True,
            "thumbnail": {
                "provided": True,
                "filename": "thumb.jpg",
                "path": "/tmp/ccbot/thumb.jpg",
            },
        },
        "telegram": {
            "video": {
                "width": 720,
                "height": 1280,
                "duration": 55,
                "mime_type": "video/mp4",
            },
            "thumbnail": {"width": 180, "height": 320},
        },
        "evidence_status": "complete",
    }

    delivery_audit.log_telegram_delivery(
        action="send_bot_message",
        user_id=1,
        chat_id=-100200300,
        thread_id=42,
        message_id=8463,
        task_type="cli",
        content_type="video",
        semantic_kind="external_cli_result",
        text="Namazu final preview",
        media=media,
    )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["media"] == media
    assert "token" not in json.dumps(row).lower()


def test_delivery_audit_records_transport_error_context(monkeypatch, tmp_path) -> None:
    path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", path)

    delivery_audit.log_telegram_delivery(
        action="retry_scheduled",
        user_id=1,
        chat_id=2,
        thread_id=3,
        task_type="content",
        content_type="text",
        semantic_kind="assistant_final",
        text="secret payload should only be previewed",
        success=False,
        error=RetryAfter(5),
        queue_age_ms=123,
        depth_at_enqueue=7,
        depth_at_send=3,
        task_class="durable",
        backpressure_reason="telegram_backpressure:retry_after:5",
    )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["transport_error_type"] == "retry_after"
    assert row["error_class"] == "RetryAfter"
    assert row["retry_after"] == 5
    assert row["queue_age_ms"] == 123
    assert row["depth_at_enqueue"] == 7
    assert row["depth_at_send"] == 3
    assert row["task_class"] == "durable"
    assert row["backpressure_reason"] == "telegram_backpressure:retry_after:5"
    assert "token" not in json.dumps(row).lower()


def test_delivery_audit_classifies_timeout_errors(monkeypatch, tmp_path) -> None:
    path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", path)

    delivery_audit.log_telegram_delivery(
        action="runtime_update_typing_suppressed",
        user_id=1,
        chat_id=2,
        text="typing",
        success=False,
        error=TimedOut("timed out"),
    )

    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["transport_error_type"] == "timeout"
    assert row["error_class"] == "TimedOut"


def test_delivery_audit_redacts_sensitive_error_fragments(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", path)

    delivery_audit.log_telegram_delivery(
        action="send",
        user_id=1,
        chat_id=2,
        text="safe text",
        success=False,
        error=RuntimeError(
            "POST https://api.telegram.org/bot123:SECRET/sendMessage "
            "token=abc123 proxy://user:pass@example.test raw_payload={secret}"
        ),
    )

    serialized = path.read_text(encoding="utf-8")
    assert "SECRET" not in serialized
    assert "token=abc123" not in serialized
    assert "user:pass@" not in serialized
    assert "raw_payload={secret}" not in serialized
    row = json.loads(serialized)
    assert row["transport_error_type"] == "exception"
    assert row["error_class"] == "RuntimeError"


def test_delivery_audit_records_render_outcome_and_dual_errors(
    monkeypatch,
    tmp_path,
) -> None:
    path = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", path)

    delivery_audit.log_telegram_delivery(
        action="send",
        user_id=1,
        chat_id=2,
        text="safe text",
        success=True,
        render_mode="plain_text",
        transport_outcome="fallback_sent",
        formatted_error=RuntimeError(
            "POST https://api.telegram.org/bot123:SECRET/sendMessage token=abc123"
        ),
        plain_error=TimedOut("timed out"),
    )

    serialized = path.read_text(encoding="utf-8")
    assert "SECRET" not in serialized
    assert "token=abc123" not in serialized
    row = json.loads(serialized)
    assert row["render_mode"] == "plain_text"
    assert row["transport_outcome"] == "fallback_sent"
    assert row["formatted_error_class"] == "RuntimeError"
    assert row["formatted_transport_error_type"] == "exception"
    assert row["plain_error_class"] == "TimedOut"
    assert row["plain_transport_error_type"] == "timeout"
    assert "formatted_error" in row
    assert "plain_error" in row
