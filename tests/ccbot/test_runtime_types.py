"""Unit tests for runtime-neutral core dataclasses."""

from pathlib import Path

from ccbot.runtime_types import (
    InputAction,
    OMX_WORKFLOW_PANEL_CONTENT_TYPE,
    OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
    TERMINAL_CONTROL_PANEL_CONTENT_TYPE,
    TERMINAL_CONTROL_SEMANTIC_KIND,
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
            "runtime_kind": "claude",
        }
        restored = LiveProcessDescriptor.from_dict(payload)
        assert restored.thread_id == "thread-1"
        assert restored.session_id == "thread-1"

    def test_round_trip_preserves_non_default_runtime(self) -> None:
        descriptor = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            window_name="proj",
            runtime_kind="codex",
        )

        payload = descriptor.to_dict()

        assert payload["runtime_kind"] == "codex"
        restored = LiveProcessDescriptor.from_dict(payload)
        assert restored.runtime_kind == "codex"


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
        assert locator.replay_path == "/tmp/thread.jsonl"


class TestRolloutSource:
    def test_file_path_coerces_to_path(self) -> None:
        source = RolloutSource(thread_id="thread-1", file_path="/tmp/thread.jsonl")

        assert source.file_path == Path("/tmp/thread.jsonl")
        assert source.session_id == "thread-1"
        assert source.replay_path == Path("/tmp/thread.jsonl")


class TestNormalizedEvent:
    def test_session_id_property_updates_thread_id(self) -> None:
        event = NormalizedEvent(text="hello")

        event.session_id = "thread-1"

        assert event.thread_id == "thread-1"
        assert event.session_id == "thread-1"

    def test_lifecycle_events_are_not_dispatched_or_persisted(self) -> None:
        event = NormalizedEvent(content_type="lifecycle", event_kind="lifecycle")

        assert event.semantic_kind == "lifecycle"
        assert event.delivery_class == "lifecycle"
        assert event.include_in_history is False
        assert event.dispatch_to_telegram is False

    def test_reasoning_events_are_progress_eligible(self) -> None:
        event = NormalizedEvent(content_type="thinking", event_kind="reasoning")

        assert event.semantic_kind == "reasoning"
        assert event.delivery_class == "progress"
        assert event.status_message_eligible is True
        assert event.include_in_history is True

    def test_local_command_maps_to_command_execution_contract(self) -> None:
        event = NormalizedEvent(content_type="local_command", role="assistant")

        assert event.semantic_kind == "command_execution"
        assert event.delivery_class == "history"


    def test_terminal_control_content_type_maps_to_terminal_control(self) -> None:
        event = NormalizedEvent(content_type=TERMINAL_CONTROL_PANEL_CONTENT_TYPE)

        assert event.semantic_kind == TERMINAL_CONTROL_SEMANTIC_KIND

    def test_omx_workflow_panel_maps_to_distinct_semantic_kind(self) -> None:
        event = NormalizedEvent(content_type=OMX_WORKFLOW_PANEL_CONTENT_TYPE)

        assert event.semantic_kind == OMX_WORKFLOW_STATUS_SEMANTIC_KIND
        assert event.semantic_kind != TERMINAL_CONTROL_SEMANTIC_KIND

    def test_tool_progress_is_not_included_in_history(self) -> None:
        event = NormalizedEvent(content_type="tool_progress", event_kind="tool_progress")

        assert event.semantic_kind == "tool_progress"
        assert event.status_message_eligible is True
        assert event.include_in_history is False


class TestInputAction:
    def test_input_action_defaults(self) -> None:
        action = InputAction(action_type="submit_text", payload="hello")

        assert action.submit is True
        assert action.runtime_kind == "claude"
        assert action.metadata == {}
