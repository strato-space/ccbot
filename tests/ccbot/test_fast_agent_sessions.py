"""Tests for the fast-agent session catalog adapter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccbot.codex_threads import CodexThreadCatalog
from ccbot.fast_agent_sessions import FastAgentSessionCatalog
from ccbot.session import SessionManager


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
