"""Monitor state persistence for tracked replay evidence sources.

Persists TrackedSession records (persisted conversation identity, replay path,
last_byte_offset) to ~/.ccbot/monitor_state.json so the session monitor can
resume incremental reading after restarts without re-sending old messages.

Key classes: MonitorState, TrackedSession.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .state_schema import (
    DEFAULT_RUNTIME_KIND,
    SCHEMA_VERSION,
    ensure_legacy_backup,
    infer_runtime_kind,
    normalize_runtime_kind,
)

logger = logging.getLogger(__name__)


@dataclass
class TrackedSession:
    """Tracked replay-evidence cursor keyed by persisted conversation identity.

    The wider codebase still calls this a "session" for compatibility, but in
    Codex-oriented paths it tracks one replayable JSONL source for one persisted
    thread identity.
    """

    session_id: str
    file_path: str  # Path to replayable JSONL evidence
    last_byte_offset: int = 0  # Byte offset for incremental reading
    runtime_kind: str = DEFAULT_RUNTIME_KIND

    @property
    def thread_id(self) -> str:
        """Backward-compatible alias for persisted thread-oriented code paths."""
        return self.session_id

    @thread_id.setter
    def thread_id(self, value: str) -> None:
        self.session_id = value

    @property
    def replay_path(self) -> str:
        """Runtime-neutral alias for the persisted replay evidence path."""
        return self.file_path

    @replay_path.setter
    def replay_path(self, value: str) -> None:
        self.file_path = value

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrackedSession":
        """Create from dict."""
        return cls(
            session_id=data.get("session_id", ""),
            file_path=data.get("file_path", ""),
            last_byte_offset=data.get("last_byte_offset", 0),
            runtime_kind=normalize_runtime_kind(data.get("runtime_kind")),
        )


@dataclass
class MonitorState:
    """Persistent state for tracked replay-evidence cursors.

    Stores tracking information for all monitored replay sources to prevent
    duplicate notifications after restarts.
    """

    state_file: Path
    tracked_sessions: dict[str, TrackedSession] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    runtime_kind: str = DEFAULT_RUNTIME_KIND
    _dirty: bool = field(default=False, repr=False)

    def load(self) -> None:
        """Load state from file."""
        if not self.state_file.exists():
            logger.debug(f"State file does not exist: {self.state_file}")
            return

        try:
            data = json.loads(self.state_file.read_text())
            migrated_legacy = "schema_version" not in data
            if migrated_legacy:
                ensure_legacy_backup(self.state_file)
            self.schema_version = int(data.get("schema_version", SCHEMA_VERSION))
            self.runtime_kind = normalize_runtime_kind(
                data.get("runtime_kind", self.runtime_kind)
            )
            sessions = data.get("tracked_sessions", {})
            self.tracked_sessions = {
                k: TrackedSession.from_dict(v) for k, v in sessions.items()
            }
            self.runtime_kind = infer_runtime_kind(
                session.runtime_kind for session in self.tracked_sessions.values()
            )
            if migrated_legacy:
                self.save()
            logger.info(
                f"Loaded {len(self.tracked_sessions)} tracked sessions from state"
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(f"Failed to load state file: {e}")
            self.tracked_sessions = {}

    def save(self) -> None:
        """Save state to file atomically."""
        from .utils import atomic_write_json

        runtime_kind = infer_runtime_kind(
            session.runtime_kind for session in self.tracked_sessions.values()
        )
        self.runtime_kind = runtime_kind
        data = {
            "schema_version": self.schema_version,
            "runtime_kind": runtime_kind,
            "tracked_sessions": {
                k: v.to_dict() for k, v in self.tracked_sessions.items()
            },
        }

        try:
            atomic_write_json(self.state_file, data)
            self._dirty = False
            logger.debug(
                "Saved %d tracked sessions to state", len(self.tracked_sessions)
            )
        except OSError as e:
            logger.error("Failed to save state file: %s", e)

    def get_session(self, session_id: str) -> TrackedSession | None:
        """Get tracked replay source by persisted identity.

        Legacy name retained for the Claude-shaped call sites.
        """
        return self.tracked_sessions.get(session_id)

    def get_tracked_source(self, thread_id: str) -> TrackedSession | None:
        """Runtime-neutral alias for thread/replay-oriented monitor code."""
        return self.get_session(thread_id)

    def update_session(self, session: TrackedSession) -> None:
        """Update or add a tracked replay source.

        Legacy name retained for the Claude-shaped call sites.
        """
        self.tracked_sessions[session.session_id] = session
        self._dirty = True

    def update_tracked_source(self, tracked_source: TrackedSession) -> None:
        """Runtime-neutral alias for thread/replay-oriented monitor code."""
        self.update_session(tracked_source)

    def remove_session(self, session_id: str) -> None:
        """Remove a tracked replay source.

        Legacy name retained for the Claude-shaped call sites.
        """
        if session_id in self.tracked_sessions:
            del self.tracked_sessions[session_id]
            self._dirty = True

    def remove_tracked_source(self, thread_id: str) -> None:
        """Runtime-neutral alias for thread/replay-oriented monitor code."""
        self.remove_session(thread_id)

    def save_if_dirty(self) -> None:
        """Save state only if it has been modified."""
        if self._dirty:
            self.save()
