"""Tests for Codex rollout normalization."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from ccbot.codex_rollout import CodexRolloutNormalizer, CodexRolloutState
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
    assert "```sh" in events[0].text
    assert "codex run --help" in events[0].text
    assert "ok" in events[0].text
    assert events[1].content_type == "file_change"
    assert "src/ccbot/session_monitor.py" in events[1].text
    assert events[2].tool_name == "turn_completed"


def test_codex_rollout_maps_heads_up_messages_to_warning_events() -> None:
    records = [
        {
            "timestamp": "2026-04-04T14:47:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "commentary",
                "message": "⚠️Heads up, you have less than 25% of your weekly limit left.",
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].event_kind == "warning"
    assert events[0].content_type == "warning"
    assert events[0].role == "system"


def test_codex_rollout_keeps_assistant_heads_up_message_as_assistant_text() -> None:
    records = [
        {
            "timestamp": "2026-04-04T14:48:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "assistant_message",
                "message": "Heads up: final report is ready.",
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].event_kind == "assistant_message"
    assert events[0].content_type == "text"
    assert events[0].role == "assistant"


def test_codex_rollout_maps_usage_limit_error_events_to_warning_events() -> None:
    records = [
        {
            "timestamp": "2026-04-07T17:16:07.446Z",
            "type": "event_msg",
            "payload": {
                "type": "error",
                "message": (
                    "You've hit your usage limit. Upgrade to Pro, "
                    "visit https://chatgpt.com/codex/settings/usage to purchase more credits "
                    "or try again at Apr 11th, 2026 10:11 PM."
                ),
                "codex_error_info": "usage_limit_exceeded",
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].event_kind == "warning"
    assert events[0].content_type == "warning"
    assert events[0].role == "system"
    assert "usage limit" in events[0].text.lower()


def test_codex_rollout_command_execution_extracts_bash_lc_script_into_code_block() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "command_execution",
                "command": "/bin/bash\n-lc\njq '.history[] | keys' /tmp/hard_b.json | sed -n '1,200p'",
                "cwd": "/home/tools/server/comfy",
                "status": "completed",
                "aggregated_output": "line1\nline2\n",
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "command_execution"
    assert "```sh" in events[0].text
    assert "/bin/bash" not in events[0].text
    assert "jq '.history[] | keys' /tmp/hard_b.json | sed -n '1,200p'" in events[0].text
    assert "line1" in events[0].text
    assert "line2" in events[0].text


def test_codex_rollout_tool_use_exec_command_extracts_shell_payload_into_code_block() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:05:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": {
                    "cmd": "/bin/bash\n-lc\njq '.history.prompt[0:3]' /tmp/hard_b.json | sed -n '1,220p'",
                    "workdir": "/home/tools/server/comfy",
                },
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "tool_use"
    assert events[0].text.startswith("exec_command\n```sh\n")
    assert "/bin/bash" not in events[0].text
    assert "jq '.history.prompt[0:3]' /tmp/hard_b.json | sed -n '1,220p'" in events[0].text
    assert "/home/tools/server/comfy" in events[0].text


def test_codex_rollout_tool_use_write_stdin_summarizes_chars_without_raw_json() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:06:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "write_stdin",
                "arguments": {
                    "session_id": "2041",
                    "chars": "ok go\n",
                },
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "tool_use"
    assert events[0].text.startswith("write_stdin(session 2041)\n```text\n")
    assert '"session_id"' not in events[0].text
    assert "ok go" in events[0].text


def test_codex_rollout_tool_use_generic_json_arguments_render_as_json_block() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:06:30.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "browser_click",
                "arguments": {
                    "ref": "node-1",
                    "element": "Submit",
                    "doubleClick": False,
                },
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "tool_use"
    assert events[0].text.startswith("browser_click\n```json\n")
    assert '"ref": "node-1"' in events[0].text


def test_codex_rollout_tool_output_generic_json_renders_as_json_block() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:06:45.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "name": "browser_snapshot",
                "call_id": "toolu_1",
                "output": '{"status":"ok","depth":2}',
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "tool_result"
    assert events[0].text.startswith("```json\n")
    assert '"depth": 2' in events[0].text


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


def test_codex_rollout_suppresses_duplicate_event_msg_commentary_delivery() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:20:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "commentary",
                "message": "Wave A1 уже идёт.",
            },
        },
        {
            "timestamp": "2026-04-04T10:20:00.001Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Wave A1 уже идёт."}],
            },
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    dispatchable = [event for event in events if event.dispatch_to_telegram]
    assert [(event.event_kind, event.text) for event in dispatchable] == [
        ("commentary", "Wave A1 уже идёт."),
    ]

    suppressed = [event for event in events if not event.dispatch_to_telegram]
    assert [(event.event_kind, event.text) for event in suppressed] == [
        ("commentary", "Wave A1 уже идёт."),
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


def test_codex_rollout_tool_output_exec_command_uses_preview_block() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:07:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "name": "exec_command",
                "output": {
                    "type": "output_text",
                    "text": "line1\nline2\nline3\n",
                },
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "tool_result"
    assert events[0].text.startswith("```sh\n")
    assert "line1" in events[0].text
    assert "line3" in events[0].text


def test_codex_rollout_tool_output_preserves_existing_fenced_preview_without_double_wrap() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:07:30.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "name": "exec_command",
                "output": "```sh\nline1\nline2\n```\n\npreview 2/4 lines",
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "tool_result"
    assert events[0].text.count("```sh") == 1
    assert events[0].text.endswith("preview 2/4 lines")


def test_codex_rollout_tool_output_strips_redundant_output_footer_when_preview_exists() -> None:
    records = [
        {
            "timestamp": "2026-04-04T10:07:31.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "name": "exec_command",
                "output": "```sh\nline1\nline2\n```\n\npreview 2/4 lines\noutput 4 line(s)",
            },
        }
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    assert len(events) == 1
    assert events[0].content_type == "tool_result"
    assert "preview 2/4 lines" in events[0].text
    assert "output 4 line(s)" not in events[0].text


def test_codex_rollout_synthesizes_spawn_and_wait_orchestration_events() -> None:
    records = [
        {
            "timestamp": "2026-04-04T12:00:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "call_spawn",
                "arguments": json.dumps(
                    {
                        "agent_type": "explorer",
                        "model": "gpt-5.4",
                        "reasoning_effort": "medium",
                        "message": "Review this implementation plan for:\n1. Missing dependencies\n2. Ordering issues",
                    }
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:00:00.100Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_spawn",
                "output": json.dumps(
                    {"agent_id": "agent-1", "nickname": "Mill"}
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "call_wait",
                "arguments": json.dumps(
                    {"targets": ["agent-1"], "timeout_ms": 30000}
                ),
            },
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    dispatchable = [event for event in events if event.dispatch_to_telegram]
    assert [(event.content_type, event.event_kind) for event in dispatchable] == [
        ("orchestration", "orchestration"),
        ("orchestration", "orchestration"),
    ]
    assert dispatchable[0].text.startswith("• Spawned Mill [explorer] (gpt-5.4 medium)")
    assert "Review this implementation plan for:" in dispatchable[0].text
    assert dispatchable[1].text == "• Waiting for Mill [explorer]"

    suppressed = [event for event in events if not event.dispatch_to_telegram]
    assert [event.content_type for event in suppressed] == [
        "tool_use",
        "tool_result",
        "tool_use",
    ]


def test_codex_rollout_stateful_spawn_across_poll_slices() -> None:
    state = CodexRolloutState()

    first = CodexRolloutNormalizer.normalize_records(
        [
            {
                "timestamp": "2026-04-04T12:00:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "call_spawn",
                    "arguments": json.dumps(
                        {
                            "agent_type": "explorer",
                            "model": "gpt-5.4",
                            "reasoning_effort": "medium",
                            "message": "Review the implementation plan.",
                        }
                    ),
                },
            }
        ],
        thread_id="thread-1",
        state=state,
    )
    second = CodexRolloutNormalizer.normalize_records(
        [
            {
                "timestamp": "2026-04-04T12:00:00.100Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_spawn",
                    "output": json.dumps({"agent_id": "agent-1", "nickname": "Mill"}),
                },
            }
        ],
        thread_id="thread-1",
        state=state,
    )

    assert all(not event.dispatch_to_telegram for event in first)
    dispatchable = [event for event in second if event.dispatch_to_telegram]
    assert len(dispatchable) == 1
    assert dispatchable[0].text.startswith("• Spawned Mill [explorer] (gpt-5.4 medium)")


def test_codex_rollout_stateful_wait_timeout_across_poll_slices() -> None:
    state = CodexRolloutState()

    first = CodexRolloutNormalizer.normalize_records(
        [
            {
                "timestamp": "2026-04-04T12:01:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "wait_agent",
                    "call_id": "call_wait",
                    "arguments": json.dumps({"targets": ["agent-1"], "timeout_ms": 30000}),
                },
            }
        ],
        thread_id="thread-1",
        state=state,
    )
    second = CodexRolloutNormalizer.normalize_records(
        [
            {
                "timestamp": "2026-04-04T12:01:30.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_wait",
                    "output": json.dumps({"timed_out": True, "status": {}}),
                },
            }
        ],
        thread_id="thread-1",
        state=state,
    )

    assert [event.text for event in first if event.dispatch_to_telegram] == [
        "• Waiting for agent-1"
    ]
    assert [event.text for event in second if event.dispatch_to_telegram] == [
        "• Finished waiting for agent-1",
        "• Timed out waiting for agent-1"
    ]


def test_codex_rollout_wait_timeout_with_partial_status_emits_status_and_timeout() -> None:
    state = CodexRolloutState()

    first = CodexRolloutNormalizer.normalize_records(
        [
            {
                "timestamp": "2026-04-04T12:01:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "wait_agent",
                    "call_id": "call_wait",
                    "arguments": json.dumps({"targets": ["agent-1"], "timeout_ms": 30000}),
                },
            }
        ],
        thread_id="thread-1",
        state=state,
    )
    second = CodexRolloutNormalizer.normalize_records(
        [
            {
                "timestamp": "2026-04-04T12:01:30.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_wait",
                    "output": json.dumps(
                        {
                            "timed_out": True,
                            "status": {"agent-1": {"completed": "done"}},
                        }
                    ),
                },
            }
        ],
        thread_id="thread-1",
        state=state,
    )

    assert [event.text for event in first if event.dispatch_to_telegram] == [
        "• Waiting for agent-1"
    ]
    assert [event.text for event in second if event.dispatch_to_telegram] == [
        "• Finished waiting for agent-1",
        "• agent-1 completed\n  └ done",
        "• Timed out waiting for agent-1",
    ]


def test_codex_rollout_stateful_wait_dedupe_is_scoped_to_wait_cycle() -> None:
    state = CodexRolloutState()

    first_wait = [
        {
            "timestamp": "2026-04-04T12:01:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "call_wait_1",
                "arguments": json.dumps({"targets": ["agent-1"], "timeout_ms": 30000}),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_wait_1",
                "output": json.dumps({"status": {"agent-1": {"completed": "done"}}}),
            },
        },
    ]
    second_wait = [
        {
            "timestamp": "2026-04-04T12:02:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "call_wait_2",
                "arguments": json.dumps({"targets": ["agent-1"], "timeout_ms": 30000}),
            },
        },
        {
            "timestamp": "2026-04-04T12:02:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_wait_2",
                "output": json.dumps({"status": {"agent-1": {"completed": "done"}}}),
            },
        },
    ]

    first = CodexRolloutNormalizer.normalize_records(first_wait, thread_id="thread-1", state=state)
    second = CodexRolloutNormalizer.normalize_records(second_wait, thread_id="thread-1", state=state)

    assert [event.text for event in first if event.dispatch_to_telegram] == [
        "• Waiting for agent-1",
        "• Finished waiting for agent-1",
        "• agent-1 completed\n  └ done",
    ]
    assert [event.text for event in second if event.dispatch_to_telegram] == [
        "• Waiting for agent-1",
        "• Finished waiting for agent-1",
        "• agent-1 completed\n  └ done",
    ]


def test_codex_rollout_overlapping_wait_calls_keep_distinct_wait_lifecycles() -> None:
    state = CodexRolloutState()
    records = [
        {
            "timestamp": "2026-04-04T12:01:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "call_wait_1",
                "arguments": json.dumps({"targets": ["agent-1"], "timeout_ms": 30000}),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:00.100Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "call_wait_2",
                "arguments": json.dumps({"targets": ["agent-1"], "timeout_ms": 30000}),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_wait_1",
                "output": json.dumps({"status": {"agent-1": {"completed": "done"}}}),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_wait_2",
                "output": json.dumps({"status": {"agent-1": {"completed": "done"}}}),
            },
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1", state=state)

    assert [event.text for event in events if event.dispatch_to_telegram] == [
        "• Waiting for agent-1",
        "• Waiting for agent-1",
        "• Finished waiting for agent-1",
        "• agent-1 completed\n  └ done",
        "• Finished waiting for agent-1",
        "• agent-1 completed\n  └ done",
    ]


def test_codex_rollout_stateful_cross_poll_message_dedupe() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[100.0, 100.0, 102.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:02:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Wave A1 уже идёт.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:02:00.100Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Wave A1 уже идёт."}],
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )

    assert first == []
    assert [event.text for event in second if event.dispatch_to_telegram] == [
        "Wave A1 уже идёт."
    ]


def test_codex_rollout_stateful_event_msg_flushes_without_canonical_followup() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[200.0, 201.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:03:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Wave B1 уже идёт.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [],
            thread_id="thread-1",
            state=state,
        )

    assert first == []
    assert [event.text for event in second if event.dispatch_to_telegram] == [
        "Wave B1 уже идёт."
    ]


def test_codex_rollout_stateful_user_duplicate_window_survives_next_poll() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[500.0, 502.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:08:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "$parallel flux2-plan.md",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:08:02.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "$parallel flux2-plan.md",
                            }
                        ],
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )

    assert [event.text for event in first if event.dispatch_to_telegram] == [
        "$parallel flux2-plan.md"
    ]
    assert second == []


def test_codex_rollout_stateful_preserves_same_text_commentary_across_turns() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[600.0, 600.1, 601.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:09:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Один и тот же комментарий.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:09:00.100Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Один и тот же комментарий.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        third = CodexRolloutNormalizer.normalize_records(
            [],
            thread_id="thread-1",
            state=state,
        )

    assert first == []
    assert second == []
    assert [event.text for event in third if event.dispatch_to_telegram] == [
        "Один и тот же комментарий.",
        "Один и тот же комментарий.",
    ]


def test_codex_rollout_stateful_keeps_duplicate_buffer_through_unrelated_non_idle_poll() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[400.0, 402.0, 403.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:06:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Wave C1 уже идёт.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:06:01.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_completed",
                        "turn_id": "thread-1",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        third = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:06:02.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Wave C1 уже идёт."}],
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )

    assert first == []
    assert [event.event_kind for event in second if event.dispatch_to_telegram] == []
    assert [event.text for event in third if event.dispatch_to_telegram] == [
        "Wave C1 уже идёт."
    ]


def test_codex_rollout_stateless_returns_unmatched_event_msg_immediately() -> None:
    events = CodexRolloutNormalizer.normalize_records(
        [
            {
                "timestamp": "2026-04-04T12:07:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "Wave D1 уже идёт.",
                },
            }
        ],
        thread_id="thread-1",
    )

    assert [event.text for event in events if event.dispatch_to_telegram] == [
        "Wave D1 уже идёт."
    ]


def test_codex_rollout_stateful_user_event_msg_opens_turn_immediately_and_suppresses_later_canonical_copy() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[500.0, 500.1, 500.2, 500.3],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:08:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "$parallel flux2-plan.md",
                    },
                },
                {
                    "timestamp": "2026-04-04T12:08:00.010Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Запускаю первую волну.",
                    },
                },
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:08:00.050Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "$parallel flux2-plan.md"}],
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )

    assert [event.text for event in first if event.dispatch_to_telegram] == [
        "$parallel flux2-plan.md"
    ]
    assert [
        event.text
        for event in first
        if event.semantic_kind == "commentary" and event.dispatch_to_telegram
    ] == []
    assert second == []


def test_codex_rollout_stateful_new_user_turn_flushes_old_pending_commentary_before_boundary() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[700.0, 700.5, 701.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:10:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Один и тот же комментарий.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:10:01.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "next turn"}],
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        third = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:10:02.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Один и тот же комментарий.",
                            }
                        ],
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )

    assert first == []
    assert [event.text for event in second if event.dispatch_to_telegram] == [
        "Один и тот же комментарий.",
        "next turn",
    ]
    assert [event.text for event in third if event.dispatch_to_telegram] == [
        "Один и тот же комментарий."
    ]


def test_codex_rollout_stateful_event_msg_flushes_duplicate_text_as_new_turn() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[300.0, 301.0, 302.0, 303.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:04:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Повторяемый текст.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        flushed_first = CodexRolloutNormalizer.normalize_records(
            [],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:05:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Повторяемый текст.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        flushed_second = CodexRolloutNormalizer.normalize_records(
            [],
            thread_id="thread-1",
            state=state,
        )

    assert first == []
    assert [event.text for event in flushed_first if event.dispatch_to_telegram] == [
        "Повторяемый текст."
    ]
    assert second == []
    assert [event.text for event in flushed_second if event.dispatch_to_telegram] == [
        "Повторяемый текст."
    ]

def test_codex_rollout_deduplicates_wait_completion_against_subagent_notification() -> None:
    records = [
        {
            "timestamp": "2026-04-04T12:01:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "call_spawn",
                "arguments": json.dumps(
                    {
                        "agent_type": "explorer",
                        "model": "gpt-5.4",
                        "reasoning_effort": "medium",
                        "message": "Review the implementation plan.",
                    }
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:00.050Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_spawn",
                "output": json.dumps(
                    {"agent_id": "agent-1", "nickname": "Mill"}
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "call_wait",
                "arguments": json.dumps(
                    {"targets": ["agent-1"], "timeout_ms": 30000}
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:05.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_wait",
                "output": json.dumps(
                    {
                        "status": {
                            "agent-1": {
                                "completed": "Findings\n1. Missing dependency\n2. Missing rollback"
                            }
                        },
                        "timed_out": False,
                    }
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:01:05.100Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "<subagent_notification>\n"
                            "{\"agent_path\":\"agent-1\",\"status\":{\"completed\":\"Findings\\n1. Missing dependency\\n2. Missing rollback\"}}\n"
                            "</subagent_notification>"
                        ),
                    }
                ],
            },
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")

    dispatchable = [event for event in events if event.dispatch_to_telegram]
    assert [event.content_type for event in dispatchable] == [
        "orchestration",
        "orchestration",
        "orchestration",
        "orchestration",
    ]
    assert dispatchable[2].text == "• Finished waiting for Mill [explorer]"
    assert dispatchable[3].text.startswith("• Mill [explorer] completed")
    assert dispatchable[3].text.count("Findings") == 1

    suppressed_user = [
        event
        for event in events
        if not event.dispatch_to_telegram and event.role == "user"
    ]
    assert len(suppressed_user) == 1
    assert "<subagent_notification>" in suppressed_user[0].text


def test_codex_rollout_multi_agent_wait_keeps_finished_waiting_after_early_notification() -> None:
    records = [
        {
            "timestamp": "2026-04-04T12:11:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "call_spawn_1",
                "arguments": json.dumps(
                    {
                        "agent_type": "explorer",
                        "model": "gpt-5.4",
                        "reasoning_effort": "medium",
                        "message": "Review plan A.",
                    }
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:11:00.010Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_spawn_1",
                "output": json.dumps({"agent_id": "agent-1", "nickname": "Mill"}),
            },
        },
        {
            "timestamp": "2026-04-04T12:11:00.020Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "spawn_agent",
                "call_id": "call_spawn_2",
                "arguments": json.dumps(
                    {
                        "agent_type": "explorer",
                        "model": "gpt-5.4",
                        "reasoning_effort": "medium",
                        "message": "Review plan B.",
                    }
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:11:00.030Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_spawn_2",
                "output": json.dumps({"agent_id": "agent-2", "nickname": "Ada"}),
            },
        },
        {
            "timestamp": "2026-04-04T12:11:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "wait_agent",
                "call_id": "call_wait",
                "arguments": json.dumps(
                    {"targets": ["agent-1", "agent-2"], "timeout_ms": 30000}
                ),
            },
        },
        {
            "timestamp": "2026-04-04T12:11:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "<subagent_notification>\n"
                            "{\"agent_path\":\"agent-1\",\"status\":{\"completed\":\"Findings\\n1. First review\"}}\n"
                            "</subagent_notification>"
                        ),
                    }
                ],
            },
        },
        {
            "timestamp": "2026-04-04T12:11:05.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_wait",
                "output": json.dumps(
                    {
                        "status": {
                            "agent-1": {"completed": "Findings\n1. First review"},
                            "agent-2": {"completed": "Findings\n1. Second review"},
                        },
                        "timed_out": False,
                    }
                ),
            },
        },
    ]

    events = CodexRolloutNormalizer.normalize_records(records, thread_id="thread-1")
    dispatchable = [event for event in events if event.dispatch_to_telegram]

    assert [event.content_type for event in dispatchable] == [
        "orchestration",
        "orchestration",
        "orchestration",
        "orchestration",
        "orchestration",
        "orchestration",
    ]
    assert dispatchable[3].text.startswith("• Mill [explorer] completed")
    assert dispatchable[4].text == "• Finished waiting for 2 agents\n  └ Mill [explorer]\n    Ada [explorer]"
    assert dispatchable[5].text.startswith("• Ada [explorer] completed")


def test_codex_rollout_subagent_notification_does_not_flush_buffered_assistant_event_msg() -> None:
    state = CodexRolloutState()

    with patch(
        "ccbot.codex_rollout._now_seconds",
        side_effect=[700.0, 700.1, 701.0],
    ):
        first = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:12:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Буферизованный комментарий.",
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        second = CodexRolloutNormalizer.normalize_records(
            [
                {
                    "timestamp": "2026-04-04T12:12:00.100Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "<subagent_notification>\n"
                                    "{\"agent_path\":\"agent-1\",\"status\":{\"completed\":\"Findings\\n1. done\"}}\n"
                                    "</subagent_notification>"
                                ),
                            }
                        ],
                    },
                }
            ],
            thread_id="thread-1",
            state=state,
        )
        third = CodexRolloutNormalizer.normalize_records(
            [],
            thread_id="thread-1",
            state=state,
        )

    assert first == []
    assert [event.text for event in second if event.dispatch_to_telegram] == [
        "• agent-1 completed\n  └ Findings\n    1. done"
    ]
    assert [event.text for event in third if event.dispatch_to_telegram] == [
        "Буферизованный комментарий."
    ]
