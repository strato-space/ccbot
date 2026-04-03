"""Integration coverage for the Claude runtime adapter on the new ontology."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.runtime_types import RolloutSource, runtime_capability_registry
from ccbot.session_monitor import SessionMonitor
from ccbot.transcript_parser import TranscriptParser


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "claude"


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_claude_adapter_profile_is_first_class() -> None:
    claude = runtime_capability_registry.get("claude")

    assert claude.display_name == "Claude Code"
    assert claude.launch_command_name == "claude"
    assert claude.resume_style == "flag"
    assert claude.live_stream_discovery == "transcript_tail"
    assert claude.replay_evidence_discovery == "transcript_jsonl"
    assert claude.tmux_stdio_cli_first is True
    assert claude.interactive_control_supported is True
    assert claude.blocked_input_policy == "fail_closed_on_visible_prompt"


def test_claude_launch_command_uses_resume_flag() -> None:
    claude = runtime_capability_registry.get("claude")

    assert claude.build_launch_command(resume_session_id="session-123") == (
        "claude --resume session-123"
    )


@pytest.mark.asyncio
async def test_claude_monitor_consumes_transcript_tail_as_live_semantics(
    tmp_path,
) -> None:
    projects_root = tmp_path / "projects"
    project_dir = projects_root / "tmp-project"
    project_dir.mkdir(parents=True)

    transcript = project_dir / "session-claude.jsonl"
    transcript.write_text(
        (FIXTURE_ROOT / "parity_transcript.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    monitor = SessionMonitor(
        projects_path=projects_root,
        state_file=tmp_path / "monitor_state.json",
    )
    
    async def _scan_rollout_sources() -> list[RolloutSource]:
        return [RolloutSource(thread_id="session-claude", file_path=transcript)]

    monitor.scan_rollout_sources = _scan_rollout_sources  # type: ignore[assignment]
    monitor.state.update_session(
        TrackedSession(
            session_id="session-claude",
            file_path=str(transcript),
            last_byte_offset=0,
            runtime_kind="claude",
        )
    )

    events = await monitor.check_for_updates({"session-claude"})

    assert events
    assert {event.runtime_kind for event in events} == {"claude"}
    assert any(event.semantic_kind == "reasoning" for event in events)
    assert any(event.semantic_kind == "tool_start" for event in events)
    assert any(event.semantic_kind == "tool_result" for event in events)
    assert any(event.include_in_history for event in events)
    assert any(event.dispatch_to_telegram for event in events)

    parsed_entries, _ = TranscriptParser.parse_entries(
        _load_jsonl(FIXTURE_ROOT / "parity_transcript.jsonl")
    )
    assert parsed_entries[0].runtime_kind == "claude"
