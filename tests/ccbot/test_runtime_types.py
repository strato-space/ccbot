"""Unit tests for runtime-neutral core dataclasses."""

from pathlib import Path

from ccbot.runtime_types import (
    InputAction,
    LiveProcessDescriptor,
    NormalizedEvent,
    RolloutSource,
    ThreadLocator,
)


class TestLiveProcessDescriptor:
    def test_round_trip_preserves_legacy_shape(self) -> None:
        descriptor = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            window_name="proj",
        )

        payload = descriptor.to_dict()

        assert payload == {
            "session_id": "thread-1",
            "cwd": "/tmp/project",
            "window_name": "proj",
        }
        restored = LiveProcessDescriptor.from_dict(payload)
        assert restored.thread_id == "thread-1"
        assert restored.session_id == "thread-1"


class TestThreadLocator:
    def test_session_id_is_thread_alias(self) -> None:
        locator = ThreadLocator(
            thread_id="thread-1",
            summary="hello",
            message_count=3,
            file_path="/tmp/thread.jsonl",
        )

        assert locator.session_id == "thread-1"
        assert locator.file_path == "/tmp/thread.jsonl"


class TestRolloutSource:
    def test_file_path_coerces_to_path(self) -> None:
        source = RolloutSource(thread_id="thread-1", file_path="/tmp/thread.jsonl")

        assert source.file_path == Path("/tmp/thread.jsonl")
        assert source.session_id == "thread-1"


class TestNormalizedEvent:
    def test_session_id_property_updates_thread_id(self) -> None:
        event = NormalizedEvent(text="hello")

        event.session_id = "thread-1"

        assert event.thread_id == "thread-1"
        assert event.session_id == "thread-1"


class TestInputAction:
    def test_input_action_defaults(self) -> None:
        action = InputAction(action_type="submit_text", payload="hello")

        assert action.submit is True
        assert action.runtime_kind == "claude"
        assert action.metadata == {}
