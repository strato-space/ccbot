"""Migration and cutover tests for versioned persisted state."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import hook as hook_module
from ccbot import monitor_state as monitor_state_module
from ccbot import session as session_module
from ccbot.codex_threads import CodexThreadCatalog
from ccbot.monitor_state import MonitorState, TrackedSession
from ccbot.session import SessionManager
from ccbot.session_monitor import SessionMonitor
from ccbot.state_schema import (
    BINDING_STATE_BIND_FLOW,
    BINDING_STATE_BOUND,
    BINDING_STATE_NONE,
    TOPIC_POLICY_IMPLICIT_BIND_ALLOWED,
    TOPIC_POLICY_MANUAL_BIND_REQUIRED,
    legacy_backup_path,
    split_session_map_payload,
)


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
    monkeypatch.setattr(session_module.config, "tmux_session_name", "ccbot")
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


def test_monitor_state_keeps_legacy_tracked_session_keys(tmp_path):
    state_file = tmp_path / "monitor_state.json"
    state = MonitorState(state_file=state_file)
    state.update_tracked_source(
        TrackedSession(
            session_id="thread-codex",
            file_path="/tmp/codex.jsonl",
            last_byte_offset=20,
            runtime_kind="codex",
        )
    )
    state.save()

    saved = json.loads(state_file.read_text())
    tracked = saved["tracked_sessions"]["thread-codex"]

    assert tracked["session_id"] == "thread-codex"
    assert tracked["file_path"] == "/tmp/codex.jsonl"
    assert "thread_id" not in tracked
    assert "replay_path" not in tracked


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
    project_root = tmp_path / "projects"
    project_dir = project_root / SessionManager._encode_cwd("/tmp/project-9")
    project_dir.mkdir(parents=True)
    (project_dir / "thread-9.jsonl").write_text(
        json.dumps({"type": "summary", "summary": "Recovered Claude thread"}) + "\n",
        encoding="utf-8",
    )
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
    monkeypatch.setattr(session_module.config, "tmux_session_name", "ccbot")
    monkeypatch.setattr(session_module.config, "claude_projects_path", project_root)
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    manager = SessionManager()
    await manager.load_session_map()
    manager.bind_thread(100, 7, "@9", window_name="proj-9")
    monkeypatch.setattr(session_module, "session_manager", manager)
    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    monkeypatch.setattr(
        "ccbot.session_monitor.tmux_manager.list_windows",
        AsyncMock(return_value=[SimpleNamespace(window_id="@9")]),
    )

    current_map = await monitor._load_current_session_map()

    assert current_map == {"@9": "thread-9"}


@pytest.mark.asyncio
async def test_monitor_recovers_codex_binding_from_persisted_registration(
    tmp_path,
    monkeypatch,
):
    state_file = tmp_path / "state.json"
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
    sessions_root.mkdir(parents=True)
    thread_id = "019d4e76-7fae-7a90-bc40-2290ee269660"
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": thread_id,
                "thread_name": "Recovered thread",
                "updated_at": "2026-04-02T14:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (sessions_root / f"rollout-{thread_id}.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-02T14:00:00Z",
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": "/tmp/project-9"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    manager = SessionManager(
        codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
    )
    manager.register_live_process(
        "@9",
        "/tmp/project-9",
        runtime_kind="codex",
        thread_id=thread_id,
    )
    manager.bind_thread(100, 7, "@9", window_name="proj-9")
    monkeypatch.setattr(session_module, "session_manager", manager)

    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    monkeypatch.setattr(
        "ccbot.session_monitor.tmux_manager.list_windows",
        AsyncMock(return_value=[SimpleNamespace(window_id="@9")]),
    )

    current_map = await monitor._load_current_session_map()

    assert current_map == {"@9": thread_id}
    assert thread_id in monitor._active_rollout_sources
    active_source = monitor._active_rollout_sources[thread_id]
    assert active_source.runtime_kind == "codex"
    assert active_source.cwd == "/tmp/project-9"
    assert active_source.file_path.name == f"rollout-{thread_id}.jsonl"


@pytest.mark.asyncio
async def test_monitor_includes_external_codex_binding_without_tmux_window(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
    sessions_root.mkdir(parents=True)
    thread_id = "019d4e76-7fae-7a90-bc40-2290ee269660"
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": thread_id,
                "thread_name": "External thread",
                "updated_at": "2026-04-02T14:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rollout = sessions_root / f"rollout-{thread_id}.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-02T14:00:00Z",
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": "/tmp/project-9"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(session_module.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    manager = SessionManager(
        codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
    )
    manager.bind_external_thread(
        100,
        7,
        runtime_kind="codex",
        source_thread_id=thread_id,
        summary="External thread",
        cwd="/tmp/project-9",
        file_path=str(rollout),
        read_only=True,
    )
    monkeypatch.setattr(session_module, "session_manager", manager)

    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    monkeypatch.setattr(
        "ccbot.session_monitor.tmux_manager.list_windows",
        AsyncMock(return_value=[]),
    )

    current_map = await monitor._load_current_session_map()

    assert current_map == {"external:codex:019d4e76-7fae-7a90-bc40-2290ee269660": thread_id}
    assert thread_id in monitor._active_rollout_sources
    source = monitor._active_rollout_sources[thread_id]
    assert source.runtime_kind == "codex"
    assert source.file_path == rollout


@pytest.mark.asyncio
async def test_monitor_keeps_tmux_binding_when_window_is_gone_but_replay_survives(
    tmp_path,
    monkeypatch,
):
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
    sessions_root.mkdir(parents=True)
    thread_id = "019d4e76-7fae-7a90-bc40-2290ee269661"
    (codex_home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": thread_id,
                "thread_name": "Lost window thread",
                "updated_at": "2026-04-02T14:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rollout = sessions_root / f"rollout-{thread_id}.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-02T14:00:00Z",
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": "/tmp/project-9"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(session_module.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    manager = SessionManager(
        codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
    )
    manager.register_live_process(
        "@9",
        "/tmp/project-9",
        runtime_kind="codex",
        thread_id=thread_id,
    )
    manager.bind_thread(100, 7, "@9", window_name="proj-9")
    monkeypatch.setattr(session_module, "session_manager", manager)

    monitor = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    monkeypatch.setattr(
        "ccbot.session_monitor.tmux_manager.list_windows",
        AsyncMock(return_value=[]),
    )

    current_map = await monitor._load_current_session_map()

    assert current_map == {"@9": thread_id}
    assert thread_id in monitor._active_rollout_sources
    assert monitor._active_rollout_sources[thread_id].file_path == rollout


def test_state_json_persists_topic_policy_and_binding_state(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(session_module.SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    manager.require_manual_bind(100, 42)
    manager.allow_implicit_bind(100, 43)
    manager.start_topic_bind_flow(100, 43)
    manager.bind_thread(100, 43, "@7", window_name="proj")

    saved = json.loads(state_file.read_text())
    assert saved["schema_version"] == session_module.config.state_schema_version
    assert saved["topic_policies"]["100"]["42"] == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    assert saved["topic_policies"]["100"]["43"] == TOPIC_POLICY_IMPLICIT_BIND_ALLOWED
    assert saved["topic_binding_states"]["100"]["42"] == BINDING_STATE_NONE
    assert saved["topic_binding_states"]["100"]["43"] == BINDING_STATE_BOUND
    assert saved["topic_bind_flow_versions"]["100"]["42"] >= 1
    assert saved["topic_bind_flow_versions"]["100"]["43"] >= 1
    assert saved["topic_bind_flow_nonces"]["100"]["42"]
    assert saved["topic_bind_flow_nonces"]["100"]["43"]
    assert saved["thread_bindings"]["100"]["43"] == "@7"


def test_legacy_bind_flow_state_migrates_nonce_and_version(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "thread_bindings": {},
                "window_to_session": {},
                "window_states": {},
                "user_window_offsets": {},
                "topic_policies": {"100": {"42": TOPIC_POLICY_MANUAL_BIND_REQUIRED}},
                "topic_binding_states": {"100": {"42": "bind_flow"}},
                "window_display_names": {},
                "group_chat_ids": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_module.config, "state_file", state_file)

    manager = SessionManager()

    assert manager.get_topic_binding_state(100, 42) == "bind_flow"
    assert manager.get_topic_bind_flow_version(100, 42) >= 1
    assert manager.get_topic_bind_flow_nonce(100, 42)

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["topic_bind_flow_versions"]["100"]["42"] >= 1
    assert saved["topic_bind_flow_nonces"]["100"]["42"]


def test_surface_state_is_persisted_as_canonical_maps(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(session_module.SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    manager.require_manual_bind(100, 42)
    manager.bind_thread(100, 43, "@7", window_name="proj")
    manager.bind_surface(100, "@8", chat_id=-100200300, window_name="main")
    manager.set_surface_pending_slot(100, "queued text", chat_id=-100200300)

    saved = json.loads(state_file.read_text())
    assert saved["surface_policies"]["100"]["t:42"] == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    assert saved["surface_bindings"]["100"]["t:43"] == "@7"
    assert saved["surface_bindings"]["100"]["c:-100200300"] == "@8"
    assert saved["surface_binding_states"]["100"]["t:43"] == BINDING_STATE_BOUND
    assert saved["surface_pending_slots"]["100"]["c:-100200300"]["text"] == "queued text"
    assert saved["thread_bindings"]["100"]["43"] == "@7"


def test_surface_titles_round_trip_in_state(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    original_load_state = SessionManager._load_state
    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(session_module.SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    changed = manager.set_surface_title(
        100,
        "comfy-agent-ops",
        thread_id=8227,
        chat_id=-100200,
    )

    assert changed is True
    saved = json.loads(state_file.read_text())
    assert saved["surface_titles"]["t:-100200:8227"] == "comfy-agent-ops"

    monkeypatch.setattr(session_module.SessionManager, "_load_state", original_load_state)
    reloaded = SessionManager()
    assert (
        reloaded.get_surface_title(100, thread_id=8227, chat_id=-100200)
        == "comfy-agent-ops"
    )


def test_surface_titles_are_chat_qualified_for_equal_topic_ids(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(session_module.SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    manager.set_surface_title(100, "comfy-agent", thread_id=42, chat_id=-1001)
    manager.set_surface_title(100, "comfy-agent-ops", thread_id=42, chat_id=-1002)

    assert manager.get_surface_title(100, thread_id=42, chat_id=-1001) == "comfy-agent"
    assert (
        manager.get_surface_title(100, thread_id=42, chat_id=-1002)
        == "comfy-agent-ops"
    )
    assert manager.get_surface_title(100, thread_id=42, chat_id=-1003) == ""

    saved = json.loads(state_file.read_text())
    assert saved["surface_titles"]["t:-1001:42"] == "comfy-agent"
    assert saved["surface_titles"]["t:-1002:42"] == "comfy-agent-ops"


def test_shared_surface_title_lookup_crosses_actor_scope_only_for_exact_surface(
    tmp_path, monkeypatch
):
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(session_module.SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    manager.set_surface_title(100, "comfy-agent-ops", thread_id=42, chat_id=-1001)
    manager.set_surface_title(200, "other-group", thread_id=42, chat_id=-1002)

    assert (
        manager.get_shared_surface_title(thread_id=42, chat_id=-1001)
        == "comfy-agent-ops"
    )
    assert manager.get_surface_title(300, thread_id=42, chat_id=-1001) == "comfy-agent-ops"
    assert (
        manager.get_shared_surface_title(thread_id=42, chat_id=-1002)
        == "other-group"
    )
    assert manager.get_shared_surface_title(thread_id=42, chat_id=-1003) == ""


def test_legacy_surface_title_promotes_when_group_chat_coordinate_is_unique(
    tmp_path, monkeypatch
):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "surface_titles": {"100": {"t:42": "comfy-agent-ops"}},
                "group_chat_ids": {"100:42": -100200300},
                "window_states": {},
                "user_window_offsets": {},
                "thread_bindings": {},
                "window_display_names": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_module.config, "state_file", state_file)

    manager = SessionManager()

    assert (
        manager.get_surface_title(100, thread_id=42, chat_id=-100200300)
        == "comfy-agent-ops"
    )
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["surface_titles"]["t:-100200300:42"] == "comfy-agent-ops"
    assert saved["surface_titles"]["t:42"] == "comfy-agent-ops"


def test_legacy_surface_title_does_not_promote_across_ambiguous_groups(
    tmp_path, monkeypatch
):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "surface_titles": {"100": {"t:42": "comfy-agent-ops"}},
                "group_chat_ids": {
                    "100:42": -100200300,
                    "200:42": -100200301,
                },
                "window_states": {},
                "user_window_offsets": {},
                "thread_bindings": {},
                "window_display_names": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_module.config, "state_file", state_file)

    manager = SessionManager()

    assert manager.get_surface_title(100, thread_id=42, chat_id=-100200300) == ""
    assert manager.get_surface_title(100, thread_id=42, chat_id=-100200301) == ""
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["surface_titles"] == {"t:42": "comfy-agent-ops"}


def test_surface_key_migration_from_legacy_topic_maps(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "thread_bindings": {"100": {"42": "@7"}},
                "external_topic_bindings": {
                    "100": {
                        "42": {
                            "runtime_kind": "codex",
                            "source_thread_id": "thread-42",
                            "summary": "Recovered",
                            "cwd": "/tmp/project",
                            "file_path": "/tmp/rollout.jsonl",
                            "read_only": True,
                        }
                    }
                },
                "topic_policies": {"100": {"42": TOPIC_POLICY_MANUAL_BIND_REQUIRED}},
                "topic_binding_states": {"100": {"42": BINDING_STATE_BOUND}},
                "topic_bind_flow_versions": {"100": {"42": 3}},
                "topic_bind_flow_nonces": {"100": {"42": "nonce-42"}},
                "window_states": {},
                "user_window_offsets": {},
                "window_display_names": {},
                "group_chat_ids": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_module.config, "state_file", state_file)

    manager = SessionManager()

    assert manager.surface_bindings[100]["t:42"] == "@7"
    assert manager.get_surface_policy(100, thread_id=42) == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    assert manager.get_surface_binding_state(100, thread_id=42) == BINDING_STATE_BOUND
    assert manager.get_surface_bind_flow_credentials(100, thread_id=42) == (3, "nonce-42")
    assert manager.get_external_surface_binding(100, thread_id=42)["source_thread_id"] == "thread-42"

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["surface_bindings"]["100"]["t:42"] == "@7"
    assert saved["surface_policies"]["100"]["t:42"] == TOPIC_POLICY_MANUAL_BIND_REQUIRED


def test_surface_key_migration_roundtrip_after_restart_preserves_behavior(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    original_load_state = SessionManager._load_state
    monkeypatch.setattr(session_module.config, "state_file", state_file)
    monkeypatch.setattr(session_module.SessionManager, "_load_state", lambda self: None)

    manager = SessionManager()
    manager.bind_thread(100, 42, "@7", window_name="proj")
    manager.require_manual_bind_for_surface(100, chat_id=-100200300)
    manager.start_surface_bind_flow(100, chat_id=-100200300)
    manager.set_surface_pending_slot(100, "queued text", chat_id=-100200300)

    monkeypatch.setattr(session_module.SessionManager, "_load_state", original_load_state)
    reloaded = SessionManager()

    assert reloaded.get_window_for_thread(100, 42) == "@7"
    assert reloaded.get_window_for_surface(100, chat_id=-100200300) is None
    assert reloaded.get_surface_binding_state(100, chat_id=-100200300) == BINDING_STATE_BIND_FLOW
    assert reloaded.get_surface_policy(100, chat_id=-100200300) == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    assert reloaded.peek_surface_pending_slot(100, chat_id=-100200300)["text"] == "queued text"


def test_surface_key_conflict_resolution_prefers_surface_maps_over_legacy(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "surface_bindings": {"100": {"t:42": "@surface"}},
                "surface_policies": {"100": {"t:42": TOPIC_POLICY_MANUAL_BIND_REQUIRED}},
                "surface_binding_states": {"100": {"t:42": BINDING_STATE_BIND_FLOW}},
                "surface_bind_flow_versions": {"100": {"t:42": 9}},
                "surface_bind_flow_nonces": {"100": {"t:42": "surface-nonce"}},
                "thread_bindings": {"100": {"42": "@legacy"}},
                "topic_policies": {"100": {"42": TOPIC_POLICY_IMPLICIT_BIND_ALLOWED}},
                "topic_binding_states": {"100": {"42": BINDING_STATE_BOUND}},
                "topic_bind_flow_versions": {"100": {"42": 1}},
                "topic_bind_flow_nonces": {"100": {"42": "legacy-nonce"}},
                "window_states": {},
                "user_window_offsets": {},
                "window_display_names": {},
                "group_chat_ids": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_module.config, "state_file", state_file)

    manager = SessionManager()

    assert manager.get_window_for_thread(100, 42) == "@surface"
    assert manager.get_topic_policy(100, 42) == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    assert manager.get_topic_binding_state(100, 42) == BINDING_STATE_BIND_FLOW
    assert manager.get_topic_bind_flow_credentials(100, 42) == (9, "surface-nonce")

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["thread_bindings"]["100"]["42"] == "@surface"
    assert saved["topic_policies"]["100"]["42"] == TOPIC_POLICY_MANUAL_BIND_REQUIRED
    assert saved["topic_binding_states"]["100"]["42"] == BINDING_STATE_BIND_FLOW
