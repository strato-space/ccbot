"""Tests for thread-oriented directory browser UI helpers."""

from types import SimpleNamespace

from ccbot.handlers.callback_data import CB_BIND_FLOW_SUFFIX
from ccbot.handlers.directory_browser import build_thread_picker, build_window_picker


def test_build_thread_picker_uses_thread_language():
    text, keyboard = build_thread_picker(
        [
            SimpleNamespace(
                thread_id="thread-1",
                summary="Existing Codex thread",
                message_count=12,
                file_path="/tmp/project/thread-1.jsonl",
            )
        ],
        bind_flow_version=3,
        bind_flow_nonce="nonce123",
    )

    assert "Resume Existing Thread" in text
    assert "Persisted threads were found" in text
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "➕ Fresh Thread" in labels
    assert any(label.startswith("↺ ") for label in labels)
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert all(CB_BIND_FLOW_SUFFIX in item for item in callback_data)


def test_build_window_picker_offers_new_thread():
    text, keyboard, window_ids = build_window_picker(
        [("@7", "project", "/tmp/project")],
        bind_flow_version=2,
        bind_flow_nonce="nonce456",
    )

    assert "Bind to Existing Window" in text
    assert window_ids == ["@7"]
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "➕ New Thread" in labels
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert all(CB_BIND_FLOW_SUFFIX in item for item in callback_data)
