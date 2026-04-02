"""Migration and cutover tests for versioned persisted state."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ccbot import hook as hook_module
from ccbot import monitor_state as monitor_state_module
from ccbot import session as session_module
from ccbot.monitor_state import MonitorState, TrackedSession
from ccbot.session import SessionManager
from ccbot.session_monitor import SessionMonitor
from ccbot.state_schema import legacy_backup_path, split_session_map_payload


@pytest.mark.asyncio
async def test_legacy_session_map_is_migrated_with_backup(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    session_map_file = tmp_path / "session_map.json"
    session_map_file.write_text(
        json.dumps(
            {
                "ccbot:proj": {
                    "session_id": "thread-1",
                    "cwd": "/tmp/project",
                    "window_name": "proj",
                }
            }
        )
    )

    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(session_module.config, "session_map_file", session_map_file)
    monkeypatch.setattr(session_module.SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    await manager.load_session_map()

    backup = legacy_backup_path(session_map_file)
    assert backup.exists()

    session_payload = json.loads(session_map_file.read_text())
    entries, metadata, versioned = split_session_map_payload(session_payload)
    assert versioned is True
    assert metadata["schema_version"] == session_module.config.state_schema_version
    assert metadata["runtime_kind"] == "claude"
    assert entries["ccbot:proj"]["session_id"] == "thread-1"
    assert entries["ccbot:proj"]["cwd"] == "/tmp/project"
    assert entries["ccbot:proj"]["runtime_kind"] == "claude"

    saved_state = json.loads(state_file.read_text())
    assert saved_state["schema_version"] == session_module.config.state_schema_version
    assert saved_state["runtime_kind"] == "claude"
    assert saved_state["window_states"]["proj"]["session_id"] == "thread-1"
    assert saved_state["window_states"]["proj"]["cwd"] == "/tmp/project"
    assert saved_state["window_states"]["proj"]["runtime_kind"] == "claude"


def test_legacy_state_json_is_migrated_with_backup(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "window_states": {
                    "@2": {
                        "session_id": "thread-2",
                        "cwd": "/tmp/project-2",
                        "window_name": "proj-2",
                    }
                },
                "user_window_offsets": {"100": {"@2": 99}},
                "thread_bindings": {"100": {"7": "@2"}},
                "window_display_names": {"@2": "proj-2"},
                "group_chat_ids": {"100:7": -1001234567890},
            }
        )
    )

    monkeypatch.setattr(session_module.config, "state_file", state_file)

    manager = SessionManager()

    backup = legacy_backup_path(state_file)
    assert backup.exists()

    saved = json.loads(state_file.read_text())
    assert saved["schema_version"] == session_module.config.state_schema_version
    assert saved["runtime_kind"] == "claude"
    assert saved["window_states"]["@2"]["session_id"] == "thread-2"
    assert saved["window_states"]["@2"]["cwd"] == "/tmp/project-2"
    assert saved["window_states"]["@2"]["runtime_kind"] == "claude"
    assert manager.get_window_state("@2").thread_id == "thread-2"
    assert manager.get_window_state("@2").cwd == "/tmp/project-2"


def test_monitor_state_legacy_load_rewrites_versioned_envelope(tmp_path):
    state_file = tmp_path / "monitor_state.json"
    state_file.write_text(
        json.dumps(
            {
                "tracked_sessions": {
                    "thread-3": {
                        "session_id": "thread-3",
                        "file_path": "/tmp/thread-3.jsonl",
                        "last_byte_offset": 123,
                    }
                }
            }
        )
    )

    state = MonitorState(state_file=state_file)
    state.load()

    backup = legacy_backup_path(state_file)
    assert backup.exists()

    saved = json.loads(state_file.read_text())
    assert saved["schema_version"] == monitor_state_module.SCHEMA_VERSION
    assert saved["runtime_kind"] == "claude"
    assert saved["tracked_sessions"]["thread-3"]["last_byte_offset"] == 123
    assert saved["tracked_sessions"]["thread-3"]["runtime_kind"] == "claude"
    assert state.get_session("thread-3") is not None
    assert state.get_session("thread-3").last_byte_offset == 123


def test_monitor_state_mixed_runtime_envelope_is_preserved(tmp_path):
    state_file = tmp_path / "monitor_state.json"
    state = MonitorState(state_file=state_file)
    state.update_session(
        TrackedSession(
            session_id="thread-claude",
            file_path="/tmp/claude.jsonl",
            last_byte_offset=10,
        )
    )
    state.update_session(
        TrackedSession(
            session_id="thread-codex",
            file_path="/tmp/codex.jsonl",
            last_byte_offset=20,
            runtime_kind="codex",
        )
    )
    state.save()

    saved = json.loads(state_file.read_text())
    assert saved["schema_version"] == monitor_state_module.SCHEMA_VERSION
    assert saved["runtime_kind"] == "mixed"
    assert saved["tracked_sessions"]["thread-claude"]["runtime_kind"] == "claude"
    assert saved["tracked_sessions"]["thread-codex"]["runtime_kind"] == "codex"


def test_hook_writes_versioned_session_map_and_backup(tmp_path, monkeypatch):
    legacy_map = {
        "ccbot:proj": {
            "session_id": "old-session",
            "cwd": "/tmp/project",
            "window_name": "proj",
        }
    }
    map_file = tmp_path / "session_map.json"
    map_file.write_text(json.dumps(legacy_map))

    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(hook_module.sys, "argv", ["ccbot", "hook"])
    monkeypatch.setattr(
        hook_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="ccbot:@7:proj\n"),
    )
    payload = {
        "session_id": "550e8400-e29b-41d4-a716-446655440000",
        "cwd": "/tmp/project",
        "hook_event_name": "SessionStart",
    }
    # Drive the hook through stdin by monkeypatching json.load input stream.
    monkeypatch.setattr(
        hook_module.json,
        "load",
        lambda stream: payload,
    )

    hook_module.hook_main()

    backup = legacy_backup_path(map_file)
    assert backup.exists()

    saved = json.loads(map_file.read_text())
    entries, metadata, versioned = split_session_map_payload(saved)
    assert versioned is True
    assert metadata["schema_version"] == session_module.config.state_schema_version
    assert entries["ccbot:@7"]["session_id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert entries["ccbot:@7"]["runtime_kind"] == "claude"
    assert "ccbot:proj" not in entries


@pytest.mark.asyncio
async def test_versioned_session_map_is_read_by_session_monitor(tmp_path, monkeypatch):
    session_map_file = tmp_path / "session_map.json"
    session_map_file.write_text(
        json.dumps(
            {
                "schema_version": session_module.config.state_schema_version,
                "runtime_kind": "claude",
                "entries": {
                    "ccbot:@9": {
                        "session_id": "thread-9",
                        "cwd": "/tmp/project-9",
                        "window_name": "proj-9",
                        "runtime_kind": "claude",
                    }
                },
            }
        )
    )

    monkeypatch.setattr(session_module.config, "session_map_file", session_map_file)
    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )

    current_map = await monitor._load_current_session_map()

    assert current_map == {"@9": "thread-9"}
