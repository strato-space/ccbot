"""Tests for thread-oriented directory browser UI helpers."""

from types import SimpleNamespace

import pytest

from ccbot.handlers.callback_data import CB_BIND_FLOW_SUFFIX
from ccbot.handlers.directory_browser import (
    build_session_picker,
    build_thread_picker,
    build_window_picker,
    default_browse_root,
)


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


def test_build_session_picker_forwards_bind_flow_credentials():
    _text, keyboard = build_session_picker(
        [
            SimpleNamespace(
                thread_id="thread-1",
                summary="Existing Codex thread",
                message_count=12,
                file_path="/tmp/project/thread-1.jsonl",
            )
        ],
        bind_flow_version=9,
        bind_flow_nonce="session-nonce",
    )

    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert callback_data
    assert all(CB_BIND_FLOW_SUFFIX in item for item in callback_data)
    assert all(item.endswith(":9:session-nonce") for item in callback_data)


def test_default_browse_root_prefers_configured_root_over_process_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    service_cwd = tmp_path / "ccbot"
    workspace = tmp_path / "mediagen-comfy"
    service_cwd.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(service_cwd)
    monkeypatch.setenv("CCBOT_BIND_DEFAULT_ROOT", str(workspace))

    assert default_browse_root() == str(workspace.resolve())


def test_default_browse_root_ignores_restore_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    service_cwd = tmp_path / "ccbot"
    restore_cwd = tmp_path / "stale-restore"
    service_cwd.mkdir()
    restore_cwd.mkdir()
    monkeypatch.chdir(service_cwd)
    monkeypatch.delenv("CCBOT_BIND_DEFAULT_ROOT", raising=False)
    monkeypatch.delenv("CCBOT_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("CCBOT_RESTORE_CWD", str(restore_cwd))

    assert default_browse_root() != str(restore_cwd.resolve())
    assert default_browse_root() != str(service_cwd.resolve())


def test_build_thread_picker_uses_activity_timestamp_for_display(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr("ccbot.handlers.directory_browser.time.time", lambda: now)
    text, _keyboard = build_thread_picker(
        [
            SimpleNamespace(
                thread_id="thread-1",
                summary="Activity based thread",
                message_count=12,
                file_path="/missing/thread-1.jsonl",
                activity_timestamp=now - 120,
            )
        ],
        bind_flow_version=3,
        bind_flow_nonce="nonce123",
    )

    assert "Activity based thread — 12 messages (2m ago)" in text
