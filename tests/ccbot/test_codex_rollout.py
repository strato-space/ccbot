"""Tests for Codex rollout normalization."""

from __future__ import annotations

import json
from pathlib import Path

from ccbot.codex_rollout import CodexRolloutNormalizer
from ccbot.transcript_parser import TranscriptParser


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "rollouts"


def _load_jsonl(name: str) -> list[dict]:
    path = FIXTURE_ROOT / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_many(*names: str) -> list[dict]:
    records: list[dict] = []
    for name in names:
        records.extend(_load_jsonl(name))
    return records


def test_codex_rollout_preserves_event_taxonomy() -> None:
    entries = _load_many(
        "fresh_home_thread.jsonl",
        "nonroot_reasoning_turn.jsonl",
        "root_tool_call_and_output.jsonl",
        "interrupted_turn_nonroot.jsonl",
    )
    entries.append(
        {
            "timestamp": "2026-04-02T14:05:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "response",
                "content": [{"type": "output_text", "text": "assistant reply"}],
            },
        }
    )

    events = TranscriptParser.parse_codex_rollout_entries(entries)
    kinds = {event.event_kind for event in events}

    assert {
        "user_message",
        "assistant_message",
        "commentary",
        "reasoning",
        "tool_call",
        "tool_output",
        "lifecycle",
    }.issubset(kinds)

    user_event = next(event for event in events if event.event_kind == "user_message")
    assert user_event.role == "user"
    assert user_event.content_type == "text"

    commentary_event = next(event for event in events if event.event_kind == "commentary")
    assert commentary_event.role == "assistant"
    assert commentary_event.content_type == "commentary"

    reasoning_event = next(event for event in events if event.event_kind == "reasoning")
    assert reasoning_event.content_type == "reasoning"

    tool_call = next(event for event in events if event.event_kind == "tool_call")
    assert tool_call.content_type == "tool_use"

    tool_output = next(event for event in events if event.event_kind == "tool_output")
    assert tool_output.content_type == "tool_result"


def test_codex_rollout_handles_command_and_file_change_turns() -> None:
    records = [
        {
            "timestamp": "2026-04-02T14:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "command_execution",
                "command": "codex run --help",
                "cwd": "/home",
                "status": "completed",
                "output": "ok",
            },
        },
        {
            "timestamp": "2026-04-02T14:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "file_change",
                "status": "applied",
                "changes": [
                    {"kind": "modified", "path": "src/ccbot/session_monitor.py"}
                ],
            },
        },
        {
            "timestamp": "2026-04-02T14:00:02.000Z",
            "type": "event_msg",
            "payload": {"type": "turn_completed", "turn_id": "thread-1"},
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert [event.event_kind for event in events] == [
        "command_execution",
        "file_change",
        "lifecycle",
    ]
    assert events[0].content_type == "command_execution"
    assert "codex run --help" in events[0].text
    assert "output 1 line(s)" in events[0].text
    assert "ok" not in events[0].text
    assert events[1].content_type == "file_change"
    assert "src/ccbot/session_monitor.py" in events[1].text
    assert events[2].tool_name == "turn_completed"


def test_codex_rollout_suppresses_duplicate_event_msg_history_delivery() -> None:
    records = [
        {
            "timestamp": "2026-04-04T06:56:12.157Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ping 4"}],
            },
        },
        {
            "timestamp": "2026-04-04T06:56:12.157Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "ping 4",
            },
        },
        {
            "timestamp": "2026-04-04T06:56:15.705Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "На месте.",
                "phase": "final_answer",
            },
        },
        {
            "timestamp": "2026-04-04T06:56:15.706Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "На месте."}],
            },
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    dispatchable = [event for event in events if event.dispatch_to_telegram]
    assert [(event.role, event.text) for event in dispatchable] == [
        ("user", "ping 4"),
        ("assistant", "На месте."),
    ]

    suppressed = [
        event
        for event in events
        if not event.dispatch_to_telegram and event.event_kind in {"user_message", "assistant_message"}
    ]
    assert [(event.role, event.text) for event in suppressed] == [
        ("user", "ping 4"),
        ("assistant", "На месте."),
    ]


def test_codex_rollout_suppresses_empty_reasoning_summary() -> None:
    records = [
        {
            "timestamp": "2026-04-04T07:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [],
                "content": [{"type": "output_text", "text": "raw private reasoning"}],
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].text == "[reasoning]"
    assert events[0].dispatch_to_telegram is False
    assert events[0].include_in_history is False


def test_codex_rollout_compacts_large_tool_call_and_file_change_payloads() -> None:
    patch_text = "*** Begin Patch\n" + "\n".join(f"+line {i}" for i in range(40))
    records = [
        {
            "timestamp": "2026-04-04T07:01:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "apply_patch",
                "arguments": patch_text,
            },
        },
        {
            "timestamp": "2026-04-04T07:01:01.000Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "status": "completed",
                "changes": {
                    "/tmp/a.txt": {"type": "add", "content": "alpha"},
                    "/tmp/b.txt": {"type": "modify", "content": "beta"},
                },
            },
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert events[0].content_type == "tool_use"
    assert events[0].text == "apply_patch(patch 41 lines)"
    assert events[1].content_type == "file_change"
    assert "add /tmp/a.txt" in events[1].text
    assert "modify /tmp/b.txt" in events[1].text
    assert "alpha" not in events[1].text
    assert "beta" not in events[1].text
