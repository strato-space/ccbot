"""Fresh input-safety snapshots for low-latency Telegram runtime sends.

Fast input may only bypass the conservative synchronous path when another
observer has recently proven that the target window is not showing a blocked
prompt.  This module intentionally stores only the safety facts needed for
that decision; it is not runtime proof and it is not a Telegram artifact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

InputSurfaceKind = Literal["input_ready", "busy", "blocked_prompt", "unknown"]
QuestionState = Literal["none", "active", "recoverable", "unknown"]
SnapshotSource = Literal["status_poll", "targeted_capture", "question_record_scan"]

WINDOW_INPUT_SAFETY_MAX_AGE_SECONDS = 1.5


@dataclass(frozen=True)
class WindowInputSafetySnapshot:
    window_id: str
    captured_at_monotonic: float
    input_surface_kind: InputSurfaceKind
    active_question_state: QuestionState = "unknown"
    allow_other: bool | None = None
    source: SnapshotSource = "targeted_capture"

    def age(self, *, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.captured_at_monotonic

    def is_fresh(self, *, now: float | None = None, max_age: float | None = None) -> bool:
        limit = (
            WINDOW_INPUT_SAFETY_MAX_AGE_SECONDS if max_age is None else float(max_age)
        )
        return self.age(now=now) <= limit

    def permits_fast_input(self, *, now: float | None = None) -> bool:
        if not self.is_fresh(now=now):
            return False
        if self.input_surface_kind in {"blocked_prompt", "unknown"}:
            return False
        if self.active_question_state != "none":
            return False
        return True


_snapshots: dict[str, WindowInputSafetySnapshot] = {}


def update_window_input_safety_snapshot(
    *,
    window_id: str,
    input_surface_kind: str,
    active_question_state: str = "unknown",
    allow_other: bool | None = None,
    source: SnapshotSource = "targeted_capture",
    captured_at_monotonic: float | None = None,
) -> WindowInputSafetySnapshot:
    """Record the latest safety snapshot for a tmux window."""
    surface_kind: InputSurfaceKind = (
        input_surface_kind
        if input_surface_kind in {"input_ready", "busy", "blocked_prompt"}
        else "unknown"
    )  # type: ignore[assignment]
    question_state: QuestionState = (
        active_question_state
        if active_question_state in {"none", "active", "recoverable"}
        else "unknown"
    )  # type: ignore[assignment]
    snapshot = WindowInputSafetySnapshot(
        window_id=window_id,
        captured_at_monotonic=(
            time.monotonic()
            if captured_at_monotonic is None
            else captured_at_monotonic
        ),
        input_surface_kind=surface_kind,
        active_question_state=question_state,
        allow_other=allow_other,
        source=source,
    )
    _snapshots[window_id] = snapshot
    return snapshot


def get_window_input_safety_snapshot(
    window_id: str,
    *,
    now: float | None = None,
    max_age: float | None = None,
) -> WindowInputSafetySnapshot | None:
    """Return a fresh snapshot, or ``None`` when absent/stale."""
    snapshot = _snapshots.get(window_id)
    if snapshot is None:
        return None
    if not snapshot.is_fresh(now=now, max_age=max_age):
        return None
    return snapshot


def clear_window_input_safety_snapshot(window_id: str | None = None) -> None:
    if window_id is None:
        _snapshots.clear()
    else:
        _snapshots.pop(window_id, None)
