"""Tests for the Codex thread catalog adapter."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from ccbot.codex_threads import CodexThreadCatalog
from ccbot.fast_agent_sessions import FastAgentSessionCatalog
from ccbot.session import SessionManager


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def test_codex_catalog_defaults_to_codex_home_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "runtime-codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    catalog = CodexThreadCatalog()

    assert catalog.codex_home == codex_home
    assert catalog.session_index_path == codex_home / "session_index.jsonl"
    assert catalog.sessions_root == codex_home / "sessions"


def test_codex_catalog_keeps_home_fallback_with_codex_home_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_home = tmp_path / "service-home"
    runtime_home = tmp_path / "runtime-codex-home"
    fallback_codex_home = service_home / ".codex"
    monkeypatch.setenv("HOME", str(service_home))
    monkeypatch.setenv("CODEX_HOME", str(runtime_home))
    _write_codex_thread(
        fallback_codex_home,
        thread_id="019d4e63-f279-79b1-8dfd-be785dc4a419",
        cwd="/workspace/app",
        thread_name="Fallback thread",
        updated_at="2026-04-02T14:30:00Z",
    )

    catalog = CodexThreadCatalog()

    assert catalog.codex_home == runtime_home
    assert catalog.get_candidate("019d4e63-f279-79b1-8dfd-be785dc4a419") is not None
    assert (
        catalog.get_candidate_fast("019d4e63-f279-79b1-8dfd-be785dc4a419")
        is not None
    )


def _build_fixture_codex_home(tmp_path: Path) -> Path:
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
    sessions_root.mkdir(parents=True)

    session_index_rows = json.loads(
        (FIXTURE_ROOT / "session_index_rows.json").read_text(encoding="utf-8")
    )["rows"]
    session_index_file = codex_home / "session_index.jsonl"
    with session_index_file.open("w", encoding="utf-8") as handle:
        for item in session_index_rows:
            handle.write(json.dumps(item["row"]) + "\n")

    for rollout in (FIXTURE_ROOT / "rollouts").glob("*.jsonl"):
        shutil.copy2(rollout, sessions_root / rollout.name)

    return codex_home


def _write_codex_thread(
    codex_home: Path,
    *,
    thread_id: str,
    cwd: str,
    thread_name: str,
    updated_at: str,
    originator: str = "codex-tui",
    source: object = "cli",
) -> None:
    sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
    sessions_root.mkdir(parents=True, exist_ok=True)
    session_index = codex_home / "session_index.jsonl"
    with session_index.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "id": thread_id,
                    "thread_name": thread_name,
                    "updated_at": updated_at,
                }
            )
            + "\n"
        )
    rollout = sessions_root / f"rollout-{thread_id}.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": updated_at,
                        "type": "session_meta",
                        "payload": {
                            "id": thread_id,
                            "cwd": cwd,
                            "originator": originator,
                            "source": source,
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": updated_at,
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "phase": "commentary",
                            "message": "hello",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _build_legacy_claude_project(tmp_path: Path, cwd: str, thread_id: str) -> Path:
    encoded_cwd = SessionManager._encode_cwd(cwd)
    project_dir = tmp_path / encoded_cwd
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript = project_dir / f"{thread_id}.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "summary", "summary": "Legacy Claude thread"}),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "text", "text": "legacy prompt"},
                            ]
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return transcript


@pytest.fixture
def fixture_catalog(tmp_path: Path) -> CodexThreadCatalog:
    return CodexThreadCatalog(codex_home=_build_fixture_codex_home(tmp_path))


@pytest.fixture
def session_manager(monkeypatch: pytest.MonkeyPatch, fixture_catalog: CodexThreadCatalog) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    monkeypatch.setattr(
        "ccbot.session.config.claude_projects_path",
        fixture_catalog.codex_home / "empty-claude-projects",
    )
    return SessionManager(
        codex_thread_catalog=fixture_catalog,
        fast_agent_session_catalog=FastAgentSessionCatalog(
            environment_root=fixture_catalog.codex_home / "empty-fast-agent"
        ),
    )


def test_catalog_enumerates_available_candidates_and_orphans(
    fixture_catalog: CodexThreadCatalog,
) -> None:
    candidates = fixture_catalog.candidates
    assert [candidate.thread_id for candidate in candidates] == [
        "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9",
        "019d4d8b-65d8-7c60-9317-459cdb087487",
        "019d3932-39e4-74e3-9e72-136b65c3841a",
    ]
    assert all(candidate.rollout_file.exists() for candidate in candidates)
    assert fixture_catalog.index_only_entries and {
        entry.thread_id for entry in fixture_catalog.index_only_entries
    } == {
        "019d4e63-f279-79b1-8dfd-be785dc4a419",
        "019dffff-0000-7000-8000-stale0000000",
    }


def test_catalog_hides_codex_exec_helper_sessions_from_resume_candidates(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    _write_codex_thread(
        codex_home,
        thread_id="019d5000-0000-7000-8000-000000000010",
        cwd="/workspace/app",
        thread_name="Visible interactive thread",
        updated_at="2026-04-02T14:00:00Z",
    )
    _write_codex_thread(
        codex_home,
        thread_id="019d5000-0000-7000-8000-000000000011",
        cwd="/workspace/app",
        thread_name="Hidden helper thread",
        updated_at="2026-04-02T14:01:00Z",
        originator="codex_exec",
        source="exec",
    )

    catalog = CodexThreadCatalog(codex_home=codex_home)

    candidates = catalog.list_candidates_for_cwd("/workspace/app")
    assert [candidate.thread_id for candidate in candidates] == [
        "019d5000-0000-7000-8000-000000000010",
    ]
    assert candidates[0].originator == "codex-tui"
    assert candidates[0].source == "cli"


def test_catalog_hides_codex_native_subagent_sessions_from_resume_candidates(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    parent_id = "019d5000-0000-7000-8000-000000000020"
    subagent_id = "019d5000-0000-7000-8000-000000000021"
    _write_codex_thread(
        codex_home,
        thread_id=parent_id,
        cwd="/workspace/app",
        thread_name="Parent interactive thread",
        updated_at="2026-04-02T14:00:00Z",
    )
    _write_codex_thread(
        codex_home,
        thread_id=subagent_id,
        cwd="/workspace/app",
        thread_name="Boole helper thread",
        updated_at="2026-04-02T14:01:00Z",
        source={
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent_id,
                    "agent_nickname": "Boole",
                    "agent_role": "default",
                }
            }
        },
    )

    catalog = CodexThreadCatalog(codex_home=codex_home)

    assert [candidate.thread_id for candidate in catalog.list_candidates_for_cwd("/workspace/app")] == [
        parent_id,
    ]
    assert catalog.is_helper_thread_fast(subagent_id) is True
    assert catalog.get_identity_fast(subagent_id).source == "subagent"  # type: ignore[union-attr]


def test_catalog_uses_first_human_codex_user_message_as_unnamed_preview(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
    sessions_root.mkdir(parents=True)
    thread_id = "019d5000-0000-7000-8000-000000000012"
    rollout = sessions_root / f"rollout-{thread_id}.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-04-02T14:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": thread_id,
                            "cwd": "/workspace/app",
                            "originator": "codex-tui",
                            "source": "cli",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-02T14:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "# AGENTS.md instructions\n<INSTRUCTIONS>\nAUTONOMY DIRECTIVE",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-04-02T14:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "ping"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    catalog = CodexThreadCatalog(codex_home=codex_home)

    candidates = catalog.list_candidates_for_cwd("/workspace/app")
    assert len(candidates) == 1
    assert candidates[0].summary == "ping"


def test_catalog_resolution_prefers_explicit_ids_then_launcher_then_cwd(
    fixture_catalog: CodexThreadCatalog,
) -> None:
    explicit = fixture_catalog.resolve(
        thread_id="019d4e4b-7fac-77f3-b559-cb8e9b4c39a9",
        registered_thread_id="019d4d8b-65d8-7c60-9317-459cdb087487",
        cwd="/home/strato-space",
    )
    assert explicit.status == "selected"
    assert explicit.selected is not None
    assert explicit.selected.thread_id == "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9"
    assert explicit.reason == "explicit_thread_id"

    launcher = fixture_catalog.resolve(
        registered_thread_id="019d4d8b-65d8-7c60-9317-459cdb087487",
        cwd="/home",
    )
    assert launcher.status == "selected"
    assert launcher.selected is not None
    assert launcher.selected.thread_id == "019d4d8b-65d8-7c60-9317-459cdb087487"
    assert launcher.reason == "explicit_launcher_registration"

    cwd_resolution = fixture_catalog.resolve(cwd="/home")
    assert cwd_resolution.status == "selected"
    assert cwd_resolution.selected is not None
    assert cwd_resolution.selected.thread_id == "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9"
    assert cwd_resolution.reason == "normalized_cwd"

    root_resolution = fixture_catalog.resolve(cwd="/root")
    assert root_resolution.status == "selected"
    assert root_resolution.selected is not None
    assert root_resolution.selected.thread_id == "019d3932-39e4-74e3-9e72-136b65c3841a"
    assert root_resolution.reason == "normalized_cwd"


def test_codex_resume_and_rename_resolve_exact_name_and_id(
    fixture_catalog: CodexThreadCatalog,
) -> None:
    by_name = fixture_catalog.resolve_resume_target("Investigate ccbot bug")
    assert by_name.status == "selected"
    assert by_name.selected is not None
    assert by_name.selected.thread_id == "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9"
    assert by_name.reason == "resume_explicit_thread_name"

    by_id = fixture_catalog.resolve_rename_target(
        "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9"
    )
    assert by_id.status == "selected"
    assert by_id.selected is not None
    assert by_id.selected.thread_name == "Investigate ccbot bug"
    assert by_id.reason == "rename_explicit_thread_id"


def test_codex_resume_and_rename_fail_closed_for_duplicate_names(tmp_path: Path) -> None:
    codex_home = _build_fixture_codex_home(tmp_path)
    _write_codex_thread(
        codex_home,
        thread_id="019d5000-0000-7000-8000-dupe-name00001",
        cwd="/home/alpha",
        thread_name="Shared Codex Name",
        updated_at="2026-04-02T23:59:59Z",
    )
    _write_codex_thread(
        codex_home,
        thread_id="019d5000-0000-7000-8000-dupe-name00002",
        cwd="/home/beta",
        thread_name="Shared Codex Name",
        updated_at="2026-04-03T00:00:59Z",
    )
    catalog = CodexThreadCatalog(codex_home=codex_home)

    resume = catalog.resolve_resume_target("Shared Codex Name")
    rename = catalog.resolve_rename_target("Shared Codex Name")

    assert resume.status == "ambiguous"
    assert resume.reason == "resume_explicit_thread_name_ambiguous"
    assert {candidate.thread_id for candidate in resume.candidates} == {
        "019d5000-0000-7000-8000-dupe-name00001",
        "019d5000-0000-7000-8000-dupe-name00002",
    }

    assert rename.status == "ambiguous"
    assert rename.reason == "rename_explicit_thread_name_ambiguous"
    assert {candidate.thread_id for candidate in rename.candidates} == {
        "019d5000-0000-7000-8000-dupe-name00001",
        "019d5000-0000-7000-8000-dupe-name00002",
    }


def test_codex_resume_and_rename_fail_closed_for_id_name_collision(tmp_path: Path) -> None:
    codex_home = _build_fixture_codex_home(tmp_path)
    thread_id = "019d5000-0000-7000-8000-collision00001"
    _write_codex_thread(
        codex_home,
        thread_id=thread_id,
        cwd="/home/alpha",
        thread_name="Collision primary thread",
        updated_at="2026-04-02T23:59:59Z",
    )
    _write_codex_thread(
        codex_home,
        thread_id="019d5000-0000-7000-8000-collision00002",
        cwd="/home/beta",
        thread_name=thread_id,
        updated_at="2026-04-03T00:00:59Z",
    )
    catalog = CodexThreadCatalog(codex_home=codex_home)

    resume = catalog.resolve_resume_target(thread_id)
    rename = catalog.resolve_rename_target(thread_id)

    assert resume.status == "ambiguous"
    assert resume.reason == "resume_token_id_name_collision"
    assert {candidate.thread_id for candidate in resume.candidates} == {
        thread_id,
        "019d5000-0000-7000-8000-collision00002",
    }

    assert rename.status == "ambiguous"
    assert rename.reason == "rename_token_id_name_collision"
    assert {candidate.thread_id for candidate in rename.candidates} == {
        thread_id,
        "019d5000-0000-7000-8000-collision00002",
    }


def test_catalog_fails_closed_on_missing_rollout_and_session_manager_uses_catalog(
    fixture_catalog: CodexThreadCatalog,
    session_manager: SessionManager,
) -> None:
    missing = fixture_catalog.resolve(
        thread_id="019d4e63-f279-79b1-8dfd-be785dc4a419",
    )
    assert missing.status == "not_found"
    assert missing.reason == "explicit_thread_id_not_found"


def test_catalog_resolves_recent_registration_without_full_catalog_scan(
    tmp_path: Path,
) -> None:
    codex_home = _build_fixture_codex_home(tmp_path)
    thread_id = "019d5000-0000-7000-8000-fastpath000001"
    cwd = "/tmp/ccbot-fast-path"
    updated_at = "2026-04-02T23:59:59Z"
    _write_codex_thread(
        codex_home,
        thread_id=thread_id,
        cwd=cwd,
        thread_name="Fresh fast-path thread",
        updated_at=updated_at,
    )
    rollout = next((codex_home / "sessions").rglob(f"*{thread_id}*.jsonl"))
    now = time.time()
    Path(rollout).touch()
    catalog = CodexThreadCatalog(codex_home=codex_home)

    resolution = catalog.resolve_recent_for_registration(
        cwd=cwd,
        registered_at=now - 1.0,
    )

    assert resolution.status == "selected"
    assert resolution.selected is not None
    assert resolution.selected.thread_id == thread_id
    assert resolution.reason == "explicit_launcher_registration_recent_rollout"


@pytest.mark.asyncio
async def test_session_manager_uses_fast_thread_lookup_before_full_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = _build_fixture_codex_home(tmp_path)
    thread_id = "019d5000-0000-7000-8000-fastpath000002"
    cwd = "/tmp/ccbot-fast-thread-id"
    _write_codex_thread(
        codex_home,
        thread_id=thread_id,
        cwd=cwd,
        thread_name="Explicit thread-id fast path",
        updated_at="2026-04-02T23:59:59Z",
    )
    catalog = CodexThreadCatalog(codex_home=codex_home)

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager(codex_thread_catalog=catalog)
    manager.register_live_process("@1", cwd, runtime_kind="codex", thread_id=thread_id)

    def _explode_refresh() -> None:
        raise AssertionError("full refresh should not run for explicit thread-id fast path")

    monkeypatch.setattr(catalog, "refresh", _explode_refresh)

    locator = await manager.resolve_thread_for_window("@1")

    assert locator is not None
    assert locator.thread_id == thread_id


@pytest.mark.asyncio
async def test_session_manager_lists_codex_candidates_for_directory(
    session_manager: SessionManager,
) -> None:
    sessions = await session_manager.list_threads_for_directory("/home")
    assert [session.thread_id for session in sessions[:1]] == [
        "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9",
    ]
    assert [session.summary for session in sessions[:1]] == [
        "Investigate ccbot bug",
    ]


@pytest.mark.asyncio
async def test_session_manager_preserves_legacy_claude_threads_in_mixed_directory(
    monkeypatch: pytest.MonkeyPatch,
    session_manager: SessionManager,
    tmp_path: Path,
) -> None:
    _build_legacy_claude_project(
        tmp_path,
        "/home",
        "legacy-claude-thread",
    )
    monkeypatch.setattr("ccbot.session.config.claude_projects_path", tmp_path)

    sessions = await session_manager.list_threads_for_directory("/home")

    assert sessions[0].thread_id == "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9"
    assert any(session.thread_id == "legacy-claude-thread" for session in sessions)
    legacy = next(
        session for session in sessions if session.thread_id == "legacy-claude-thread"
    )
    assert legacy.summary == "Legacy Claude thread"


@pytest.mark.asyncio
async def test_codex_registration_fails_closed_for_parallel_same_cwd_starts(
    session_manager: SessionManager,
) -> None:
    session_manager.register_live_process("@1", "/home", runtime_kind="codex")
    session_manager.register_live_process("@2", "/home", runtime_kind="codex")
    session_manager.get_window_state("@1").registered_at = 0.0
    session_manager.get_window_state("@2").registered_at = 0.0

    assert await session_manager.resolve_thread_for_window("@1") is None
    assert await session_manager.resolve_thread_for_window("@2") is None


@pytest.mark.asyncio
async def test_codex_registration_ignores_stale_history_candidates(
    session_manager: SessionManager,
) -> None:
    session_manager.register_live_process("@1", "/home", runtime_kind="codex")
    state = session_manager.get_window_state("@1")
    newest_home_candidate = max(
        candidate.ordering_timestamp
        for candidate in session_manager.codex_thread_catalog.candidates
        if candidate.normalized_cwd == "/home"
    )
    state.registered_at = newest_home_candidate + 1000

    assert await session_manager.resolve_thread_for_window("@1") is None


@pytest.mark.asyncio
async def test_codex_resume_registration_prefers_explicit_thread_id(
    session_manager: SessionManager,
) -> None:
    session_manager.register_live_process(
        "@1",
        "/home",
        runtime_kind="codex",
        thread_id="019d4e4b-7fac-77f3-b559-cb8e9b4c39a9",
    )

    resolved = await session_manager.resolve_thread_for_window("@1")

    assert resolved is not None
    assert resolved.thread_id == "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9"


@pytest.mark.asyncio
async def test_register_live_process_resets_stale_window_state(
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_manager.register_live_process(
        "@1",
        "/old",
        runtime_kind="codex",
        thread_id="stale-thread",
    )
    state = session_manager.get_window_state("@1")
    state.registered_at = 10.0

    monkeypatch.setattr("ccbot.session.time.time", lambda: 42.0)
    session_manager.register_live_process("@1", "/new", runtime_kind="codex")

    refreshed = session_manager.get_window_state("@1")
    assert refreshed.cwd == "/new"
    assert refreshed.thread_id == ""
    assert refreshed.registered_at == 42.0


@pytest.mark.asyncio
async def test_codex_registration_recovers_after_delayed_index_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    catalog = CodexThreadCatalog(codex_home=codex_home)
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager(codex_thread_catalog=catalog)
    manager.register_live_process("@1", "/workspace/app", runtime_kind="codex")

    assert await manager.resolve_thread_for_window("@1") is None

    _write_codex_thread(
        codex_home,
        thread_id="019d4e63-f279-79b1-8dfd-be785dc4a419",
        cwd="/workspace/app",
        thread_name="Delayed thread",
        updated_at="2026-04-02T14:30:00Z",
    )
    manager.get_window_state("@1").registered_at = 0.0

    resolved = await manager.resolve_thread_for_window("@1")

    assert resolved is not None
    assert resolved.thread_id == "019d4e63-f279-79b1-8dfd-be785dc4a419"
