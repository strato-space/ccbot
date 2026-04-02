"""Runtime-neutral core types for tmux-driven agent control.

These dataclasses implement the ontology in ``doc/runtime-ontology.md``.
They deliberately keep binding, live process, thread, rollout source, and
normalized event concerns separate, while exposing compatibility properties
for the current Claude-oriented code paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TopicBinding:
    """Persisted association from a Telegram topic to a tmux window."""

    user_id: int
    thread_id: int
    window_id: str
    window_name: str = ""
    runtime_kind: str = "claude"


@dataclass
class LiveProcessDescriptor:
    """State describing the live process hosted by a tmux window."""

    thread_id: str = ""
    cwd: str = ""
    window_name: str = ""
    runtime_kind: str = "claude"

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for the persisted thread identifier."""
        return self.thread_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.thread_id = value

    def to_dict(self) -> dict[str, Any]:
        """Serialize in the current on-disk shape until T4 migration lands."""
        data: dict[str, Any] = {
            "session_id": self.thread_id,
            "cwd": self.cwd,
        }
        if self.runtime_kind != "claude":
            data["runtime_kind"] = self.runtime_kind
        if self.window_name:
            data["window_name"] = self.window_name
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveProcessDescriptor":
        return cls(
            thread_id=data.get("thread_id") or data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            runtime_kind=data.get("runtime_kind", "claude"),
        )


@dataclass
class ThreadLocator:
    """Resolved persisted conversation identity plus its rollout path."""

    thread_id: str
    summary: str
    message_count: int
    file_path: str
    runtime_kind: str = "claude"
    cwd: str = ""

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for historical Claude call sites."""
        return self.thread_id


@dataclass
class RolloutSource:
    """Readable event source associated with a persisted thread."""

    thread_id: str
    file_path: Path
    runtime_kind: str = "claude"
    source_kind: str = "jsonl"
    cwd: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.file_path, Path):
            self.file_path = Path(self.file_path)

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for monitor code that still says session."""
        return self.thread_id


@dataclass
class NormalizedEvent:
    """Runtime-neutral event emitted from a rollout source."""

    thread_id: str = ""
    text: str = ""
    is_complete: bool = True
    content_type: str = "text"
    tool_use_id: str | None = None
    role: str = "assistant"
    tool_name: str | None = None
    image_data: list[tuple[str, bytes]] | None = None
    timestamp: str | None = None
    runtime_kind: str = "claude"
    event_kind: str = "message"

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for notification code."""
        return self.thread_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.thread_id = value


@dataclass(frozen=True)
class InputAction:
    """Runtime-neutral intent for sending input to a live process."""

    action_type: str
    payload: str = ""
    submit: bool = True
    runtime_kind: str = "claude"
    metadata: dict[str, Any] = field(default_factory=dict)
