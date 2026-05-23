"""Tests for the fast-agent session catalog adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ccbot.codex_threads import CodexThreadCatalog
from ccbot.fast_agent_sessions import FastAgentSessionCatalog
from ccbot.session import SessionManager


def _write_test_codex_thread(
    codex_home: Path,
    *,
    thread_id: str,
    cwd: str,
    thread_name: str,
    updated_at: str,
) -> None:
    sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
    sessions_root.mkdir(parents=True, exist_ok=True)
    with (codex_home / "session_index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": thread_id, "thread_name": thread_name, "updated_at": updated_at}) + "\n")
    rollout = sessions_root / f"rollout-{thread_id}.jsonl"
    rollout.write_text(
        json.dumps({"timestamp": updated_at, "type": "session_meta", "payload": {"id": thread_id, "cwd": cwd, "originator": "codex-tui", "source": "cli"}}) + "\n",
        encoding="utf-8",
    )


def _build_fast_agent_environment(tmp_path: Path) -> Path:
    environment_root = tmp_path / ".fast-agent"
    session_dir = environment_root / "sessions" / "fa-session-20260403-01"
    session_dir.mkdir(parents=True)

    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "name": "fa-session-20260403-01",
                "history_files": ["history_agent.json"],
                "metadata": {
                    "agent_name": "planner",
                    "title": "Daily planner",
                    "session_id": "fa-session-20260403-01",
                    "updated_at": "2026-04-03T08:10:00Z",
                },
            }
        ),
        encoding="utf-8",
    )
    (session_dir / "history_agent.json").write_text(
        json.dumps({"messages": [{"role": "assistant", "tool_calls": []}]}),
        encoding="utf-8",
    )
    (session_dir / "acp_log.jsonl").write_text(
        json.dumps({"sessionUpdate": "agent_message_chunk", "text": "Working"}) + "\n",
        encoding="utf-8",
    )
    return environment_root


def test_fast_agent_catalog_discovers_sessions_and_prefers_acp_log(tmp_path: Path) -> None:
    environment_root = _build_fast_agent_environment(tmp_path)
    catalog = FastAgentSessionCatalog(environment_root=environment_root)

    candidates = catalog.list_candidates_for_directory(str(tmp_path))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.session_id == "fa-session-20260403-01"
    assert candidate.session_title == "Daily planner"
    assert candidate.replay_file.name == "acp_log.jsonl"
    assert candidate.message_count >= 1


def test_fast_agent_catalog_resolves_resume_and_title_rename(tmp_path: Path) -> None:
    environment_root = _build_fast_agent_environment(tmp_path)
    catalog = FastAgentSessionCatalog(environment_root=environment_root)
    cwd = str(tmp_path)

    resume = catalog.resolve_resume_target("Daily planner", cwd=cwd)
    assert resume.status == "selected"
    assert resume.selected is not None
    assert resume.selected.session_id == "fa-session-20260403-01"
    assert resume.reason == "resume_explicit_session_title"

    title_rename = catalog.resolve_title_rename_target("Daily planner", cwd=cwd)
    assert title_rename.status == "selected"
    assert title_rename.reason == "title_rename_supported"

    session_id_rename = catalog.resolve_session_id_rename_target(
        "fa-session-20260403-01",
        cwd=cwd,
    )
    assert session_id_rename.status == "unsupported"
    assert session_id_rename.reason == "session_id_rename_unsupported"

    rename = catalog.rename_title(
        session_id="fa-session-20260403-01",
        cwd=cwd,
        title="Daily planner v2",
    )
    assert rename.status == "selected"

    refreshed = catalog.get_candidate("fa-session-20260403-01", cwd=cwd)
    assert refreshed is not None
    assert refreshed.session_title == "Daily planner v2"
    assert refreshed.session_id == "fa-session-20260403-01"


@pytest.mark.asyncio
async def test_session_manager_lists_and_resolves_fast_agent_sessions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    environment_root = _build_fast_agent_environment(tmp_path)

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager(
        codex_thread_catalog=CodexThreadCatalog(codex_home=tmp_path / ".codex-empty"),
        fast_agent_session_catalog=FastAgentSessionCatalog(
            environment_root=environment_root
        )
    )
    manager.register_live_process(
        "@9",
        str(tmp_path),
        runtime_kind="fast-agent",
        thread_id="fa-session-20260403-01",
    )

    sessions = await manager.list_threads_for_directory(str(tmp_path))
    assert [session.thread_id for session in sessions] == ["fa-session-20260403-01"]
    assert sessions[0].summary == "Daily planner"

    resolved = await manager.resolve_thread_for_window("@9")
    assert resolved is not None
    assert resolved.thread_id == "fa-session-20260403-01"
    assert resolved.file_path.endswith("acp_log.jsonl")


@pytest.mark.asyncio
async def test_session_manager_sorts_mixed_runtime_threads_by_activity_and_runtime_scoped_ids(
    monkeypatch,
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / ".codex"
    shared_id = "shared-thread-id"
    _write_test_codex_thread(
        codex_home,
        thread_id=shared_id,
        cwd=str(tmp_path),
        thread_name="Codex older",
        updated_at="2026-04-01T10:00:00Z",
    )
    codex_rollout = next(codex_home.glob(f"sessions/**/rollout-{shared_id}.jsonl"))
    os.utime(codex_rollout, (1_000_000_000, 1_000_000_000))

    environment_root = _build_fast_agent_environment(tmp_path)
    session_json = environment_root / "sessions" / "fa-session-20260403-01" / "session.json"
    payload = json.loads(session_json.read_text(encoding="utf-8"))
    payload["name"] = shared_id
    payload["metadata"]["session_id"] = shared_id
    payload["metadata"]["title"] = "Fast newer"
    session_json.write_text(json.dumps(payload), encoding="utf-8")
    for path in (environment_root / "sessions" / "fa-session-20260403-01").glob("*"):
        os.utime(path, (2_000_000_000, 2_000_000_000))

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    manager = SessionManager(
        codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home),
        fast_agent_session_catalog=FastAgentSessionCatalog(environment_root=environment_root),
    )

    sessions = await manager.list_threads_for_directory(str(tmp_path))

    assert [(session.runtime_kind, session.thread_id) for session in sessions[:2]] == [
        ("fast-agent", shared_id),
        ("codex", shared_id),
    ]
