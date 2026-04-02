"""Tests for the Codex thread catalog adapter."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ccbot.codex_threads import CodexThreadCatalog
from ccbot.session import SessionManager


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


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


@pytest.fixture
def fixture_catalog(tmp_path: Path) -> CodexThreadCatalog:
    return CodexThreadCatalog(codex_home=_build_fixture_codex_home(tmp_path))


@pytest.fixture
def session_manager(monkeypatch: pytest.MonkeyPatch, fixture_catalog: CodexThreadCatalog) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager(codex_thread_catalog=fixture_catalog)


def test_catalog_enumerates_available_candidates_and_orphans(
    fixture_catalog: CodexThreadCatalog,
) -> None:
    candidates = fixture_catalog.candidates
    assert [candidate.thread_id for candidate in candidates] == [
        "019d4e76-7fae-7a90-bc40-2290ee269660",
        "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9",
        "019d4d8b-65d8-7c60-9317-459cdb087487",
        "019cd6ee-1188-7640-acbd-2c628477bde5",
        "019d3932-39e4-74e3-9e72-136b65c3841a",
    ]
    assert all(candidate.rollout_file.exists() for candidate in candidates)
    assert fixture_catalog.index_only_entries and {
        entry.thread_id for entry in fixture_catalog.index_only_entries
    } == {
        "019d4e63-f279-79b1-8dfd-be785dc4a419",
        "019dffff-0000-7000-8000-stale0000000",
    }


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
    assert cwd_resolution.status == "ambiguous"
    assert [candidate.thread_id for candidate in cwd_resolution.candidates] == [
        "019d4e76-7fae-7a90-bc40-2290ee269660",
        "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9",
    ]
    assert cwd_resolution.reason == "normalized_cwd_ambiguous"

    root_resolution = fixture_catalog.resolve(cwd="/root")
    assert root_resolution.status == "selected"
    assert root_resolution.selected is not None
    assert root_resolution.selected.thread_id == "019d3932-39e4-74e3-9e72-136b65c3841a"
    assert root_resolution.reason == "normalized_cwd"


def test_catalog_fails_closed_on_missing_rollout_and_session_manager_uses_catalog(
    fixture_catalog: CodexThreadCatalog,
    session_manager: SessionManager,
) -> None:
    missing = fixture_catalog.resolve(
        thread_id="019d4e63-f279-79b1-8dfd-be785dc4a419",
    )
    assert missing.status == "not_found"
    assert missing.reason == "explicit_thread_id_not_found"


@pytest.mark.asyncio
async def test_session_manager_lists_codex_candidates_for_directory(
    session_manager: SessionManager,
) -> None:
    sessions = await session_manager.list_threads_for_directory("/home")
    assert [session.thread_id for session in sessions] == [
        "019d4e76-7fae-7a90-bc40-2290ee269660",
        "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9",
    ]
    assert [session.summary for session in sessions] == [
        "Capture Codex evidence fixtures",
        "Investigate ccbot bug",
    ]
