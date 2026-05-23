"""Unit tests for SessionMonitor JSONL reading and offset handling."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time

import pytest
from unittest.mock import patch

from ccbot.runtime_types import RolloutSource
from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import SessionMonitor
from ccbot.transcript_parser import TranscriptParser


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_partial_trailing_line_is_retained_for_retry(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        first_line = json.dumps(entry1)
        partial_second = json.dumps(entry2)[: len(json.dumps(entry2)) // 2]
        jsonl_file.write_text(
            first_line + "\n" + partial_second,
            encoding="utf-8",
        )

        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 1
        assert session.last_byte_offset == len(first_line.encode("utf-8")) + 1

    @pytest.mark.asyncio
    async def test_corrupted_complete_line_is_skipped(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry3 = make_jsonl_entry(msg_type="assistant", content="third")
        jsonl_file.write_text(
            json.dumps(entry1)
            + "\n"
            + '{"type":"response_item","payload":{"type":"message","role":"assistant",'
            + '"content": [broken]}}\n'
            + json.dumps(entry3)
            + "\n",
            encoding="utf-8",
        )

        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_truncation_detection_resets_codex_rollout_state(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,
        )
        monitor._codex_rollout_states["test-session"] = object()  # type: ignore[assignment]

        await monitor._read_new_lines(session, jsonl_file)

        assert "test-session" not in monitor._codex_rollout_states


class TestCheckForUpdatesCodexRollout:
    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_repeated_reads_do_not_duplicate_codex_events(
        self, monitor, tmp_path
    ):
        rollout = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "codex"
            / "rollouts"
            / "root_tool_call_and_output.jsonl"
        )
        copied = tmp_path / "thread.jsonl"
        copied.write_text(rollout.read_text(encoding="utf-8"), encoding="utf-8")

        thread_id = "thread-1"
        tracked = TrackedSession(
            session_id=thread_id,
            file_path=str(copied),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)

        async def _scan():
            return [RolloutSource(thread_id=thread_id, file_path=copied)]

        monitor.scan_rollout_sources = _scan  # type: ignore[assignment]

        first = await monitor.check_for_updates({thread_id})
        second = await monitor.check_for_updates({thread_id})

        assert first
        assert {event.event_kind for event in first} >= {
            "command_execution",
            "lifecycle",
        }
        assert second == []

    @pytest.mark.asyncio
    async def test_check_for_updates_uses_runtime_resolved_active_rollout_sources(
        self, monitor, tmp_path
    ):
        rollout = (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "codex"
            / "rollouts"
            / "root_tool_call_and_output.jsonl"
        )
        copied = tmp_path / "runtime-resolved-thread.jsonl"
        copied.write_text(rollout.read_text(encoding="utf-8"), encoding="utf-8")

        thread_id = "thread-runtime-resolved"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id,
                file_path=copied,
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        }
        tracked = TrackedSession(
            session_id=thread_id,
            file_path=str(copied),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)

        async def _scan():
            raise AssertionError(
                "legacy project scan should not run for active runtime sources"
            )

        monitor.scan_rollout_sources = _scan  # type: ignore[assignment]

        events = await monitor.check_for_updates({thread_id})

        assert events
        assert {event.event_kind for event in events} >= {
            "command_execution",
            "lifecycle",
        }

    @staticmethod
    def _iso_from_epoch(seconds: float) -> str:
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    @pytest.mark.asyncio
    async def test_new_live_codex_source_replays_recent_turn_opener(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "late-live-user.jsonl"
        live_since = time.time() - 2.0
        user_ts = self._iso_from_epoch(live_since - 3.0)
        event_ts = self._iso_from_epoch(live_since - 2.999)
        command_ts = self._iso_from_epoch(live_since + 1.0)
        records = [
            {
                "timestamp": user_ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "собери только бриф на тему - анекдот по процессу Citematic Shorts",
                        }
                    ],
                },
            },
            {
                "timestamp": event_ts,
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "собери только бриф на тему - анекдот по процессу Citematic Shorts",
                },
            },
            {
                "timestamp": command_ts,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_cmd",
                    "arguments": json.dumps({"cmd": "pwd"}),
                },
            },
        ]
        copied.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        thread_id = "thread-late-live-user"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id,
                file_path=copied,
                runtime_kind="codex",
                live_since=live_since,
            )
        }

        events = await monitor.check_for_updates({thread_id})

        user_events = [event for event in events if event.role == "user"]
        assert len(user_events) == 1
        assert user_events[0].semantic_kind == "user_echo"
        # The duplicate event_msg copy can carry dispatch_to_telegram=False
        # until compact policy promotes ordinary user_echo in bot delivery.
        assert "Citematic Shorts" in user_events[0].text
        assert any(event.event_kind == "command_execution" for event in events)
        tracked = monitor.state.get_tracked_source(thread_id)
        assert tracked is not None
        assert tracked.last_byte_offset == copied.stat().st_size

    @pytest.mark.asyncio
    async def test_new_live_codex_source_does_not_replay_old_history(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "old-history.jsonl"
        live_since = time.time() - 2.0
        old_ts = self._iso_from_epoch(live_since - 1800.0)
        old_answer_ts = self._iso_from_epoch(live_since - 1798.0)
        records = [
            {
                "timestamp": old_ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "old prompt"}],
                },
            },
            {
                "timestamp": old_answer_ts,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "old answer"}],
                },
            },
        ]
        copied.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        thread_id = "thread-old-history"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id,
                file_path=copied,
                runtime_kind="codex",
                live_since=live_since,
            )
        }

        events = await monitor.check_for_updates({thread_id})

        assert events == []
        tracked = monitor.state.get_tracked_source(thread_id)
        assert tracked is not None
        assert tracked.last_byte_offset == copied.stat().st_size

    @pytest.mark.asyncio
    async def test_new_live_codex_source_anchors_before_later_internal_user_payload(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "later-internal-user.jsonl"
        live_since = time.time() - 2.0
        records = [
            {
                "timestamp": self._iso_from_epoch(live_since - 3.0),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ordinary prompt"}],
                },
            },
            {
                "timestamp": self._iso_from_epoch(live_since - 1.0),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<subagent_notification>hidden</subagent_notification>",
                        }
                    ],
                },
            },
            {
                "timestamp": self._iso_from_epoch(live_since + 1.0),
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call_cmd",
                    "arguments": json.dumps({"cmd": "pwd"}),
                },
            },
        ]
        copied.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        thread_id = "thread-later-internal-user"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id,
                file_path=copied,
                runtime_kind="codex",
                live_since=live_since,
            )
        }

        events = await monitor.check_for_updates({thread_id})

        user_events = [event for event in events if event.role == "user"]
        assert user_events[0].text == "ordinary prompt"
        assert user_events[0].dispatch_to_telegram is True
        assert (
            user_events[1].text
            == "<subagent_notification>hidden</subagent_notification>"
        )
        assert any(event.event_kind == "command_execution" for event in events)

    @pytest.mark.asyncio
    async def test_new_live_codex_source_ignores_stale_live_since(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "stale-live-since.jsonl"
        live_since = time.time() - 3600.0
        records = [
            {
                "timestamp": self._iso_from_epoch(live_since + 10.0),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "stale prompt"}],
                },
            },
            {
                "timestamp": self._iso_from_epoch(live_since + 12.0),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "stale answer"}],
                },
            },
        ]
        copied.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

        thread_id = "thread-stale-live-since"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id,
                file_path=copied,
                runtime_kind="codex",
                live_since=live_since,
            )
        }

        events = await monitor.check_for_updates({thread_id})

        assert events == []
        tracked = monitor.state.get_tracked_source(thread_id)
        assert tracked is not None
        assert tracked.last_byte_offset == copied.stat().st_size

    @pytest.mark.asyncio
    async def test_hydrate_codex_rollout_state_preserves_active_turn_user_duplicate_buffer(
        self, monitor, tmp_path
    ):
        rollout = tmp_path / "thread.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-04-04T12:08:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "$parallel flux2-plan.md",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        tracked = TrackedSession(
            session_id="thread-1",
            file_path=str(rollout),
            last_byte_offset=rollout.stat().st_size,
        )

        hydrated = await monitor._hydrate_codex_rollout_state(
            tracked,
            rollout,
            up_to_offset=rollout.stat().st_size,
        )

        assert hydrated.pending_event_messages == []
        assert len(hydrated.recent_user_event_messages) == 1
        assert (
            next(iter(hydrated.recent_user_event_messages.keys()))[0] == "surrogate:1"
        )

        canonical = TranscriptParser.parse_codex_rollout_entries(
            [
                {
                    "timestamp": "2026-04-04T12:08:01.000Z",
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
            state=hydrated,
        )

        assert canonical == []

    @pytest.mark.asyncio
    async def test_check_for_updates_preserves_hidden_user_echo_turn_boundaries(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "user-echo.jsonl"
        copied.write_text(
            json.dumps(
                {
                    "timestamp": "2026-04-04T12:03:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "ping"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        thread_id = "thread-user-hidden"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id, file_path=copied, runtime_kind="codex"
            )
        }
        tracked = TrackedSession(
            session_id=thread_id,
            file_path=str(copied),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)

        with patch("ccbot.session_monitor.config.show_user_messages", False):
            events = await monitor.check_for_updates({thread_id})

        assert len(events) == 1
        assert events[0].role == "user"
        assert events[0].dispatch_to_telegram is False

    @pytest.mark.asyncio
    async def test_check_for_updates_carries_codex_spawn_state_across_poll_slices(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "spawn-stateful.jsonl"
        thread_id = "thread-spawn-stateful"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id, file_path=copied, runtime_kind="codex"
            )
        }
        tracked = TrackedSession(
            session_id=thread_id,
            file_path=str(copied),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)

        spawn_call = {
            "timestamp": "2026-04-04T12:04:00.000Z",
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
        spawn_output = {
            "timestamp": "2026-04-04T12:04:00.100Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_spawn",
                "output": json.dumps({"agent_id": "agent-1", "nickname": "Mill"}),
            },
        }

        copied.write_text(json.dumps(spawn_call) + "\n", encoding="utf-8")
        first = await monitor.check_for_updates({thread_id})
        assert all(not event.dispatch_to_telegram for event in first)

        copied.write_text(
            json.dumps(spawn_call) + "\n" + json.dumps(spawn_output) + "\n",
            encoding="utf-8",
        )
        second = await monitor.check_for_updates({thread_id})

        dispatchable = [event for event in second if event.dispatch_to_telegram]
        assert len(dispatchable) == 1
        assert dispatchable[0].text.startswith(
            "• Spawned Mill [explorer] (gpt-5.4 medium)"
        )

    @pytest.mark.asyncio
    async def test_check_for_updates_flushes_pending_codex_event_msg_without_file_change(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "pending-commentary.jsonl"
        thread_id = "thread-pending-commentary"
        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id,
                file_path=copied,
                runtime_kind="codex",
            )
        }
        tracked = TrackedSession(
            session_id=thread_id,
            file_path=str(copied),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)

        copied.write_text(
            json.dumps(
                {
                    "timestamp": "2026-04-04T12:05:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "commentary",
                        "message": "Wave B2 уже идёт.",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with patch(
            "ccbot.codex_rollout._now_seconds",
            side_effect=[500.0, 501.0],
        ):
            first = await monitor.check_for_updates({thread_id})
            second = await monitor.check_for_updates({thread_id})

        assert first == []
        assert [event.text for event in second if event.dispatch_to_telegram] == [
            "Wave B2 уже идёт."
        ]

    @pytest.mark.asyncio
    async def test_check_for_updates_does_not_replay_hydrated_historical_event_msg(
        self, monitor, tmp_path
    ):
        copied = tmp_path / "historical-commentary.jsonl"
        thread_id = "thread-historical-commentary"
        old_commentary = {
            "timestamp": "2026-04-04T12:05:00.000Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "commentary",
                "message": "Old commentary already consumed.",
            },
        }
        new_lifecycle = {
            "timestamp": "2026-04-04T12:06:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "local_shell_call",
                "action": "started",
            },
        }

        copied.write_text(json.dumps(old_commentary) + "\n", encoding="utf-8")
        consumed_size = copied.stat().st_size
        copied.write_text(
            json.dumps(old_commentary) + "\n" + json.dumps(new_lifecycle) + "\n",
            encoding="utf-8",
        )

        monitor._active_rollout_sources = {
            thread_id: RolloutSource(
                thread_id=thread_id,
                file_path=copied,
                runtime_kind="codex",
            )
        }
        tracked = TrackedSession(
            session_id=thread_id,
            file_path=str(copied),
            last_byte_offset=consumed_size,
        )
        monitor.state.update_session(tracked)
        monitor._file_mtimes[thread_id] = copied.stat().st_mtime - 1

        first = await monitor.check_for_updates({thread_id})
        second = await monitor.check_for_updates({thread_id})

        assert [event.text for event in first if event.dispatch_to_telegram] == []
        assert second == []
