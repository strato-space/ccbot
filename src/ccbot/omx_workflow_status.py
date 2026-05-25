"""Optional OMX workflow-status extraction and rendering.

This module is deliberately read-only and suppressive.  OMX workflow state is an
optional enrichment for ccbot compact delivery; pure Codex runtimes must keep
working when no recognized, fresh OMX state exists.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_RECOGNIZED_WORKFLOWS = {
    "autopilot",
    "ralph",
    "ralplan",
    "swarm",
    "team",
    "ultragoal",
    "ultraqa",
}
_OMX_STATUSLINE_RE = re.compile(
    r"^(?P<workflow>ultragoal|ralph|ralplan|autopilot|ultraqa|team|swarm)"
    r"(?:\s+(?P<current>\d+)\s*/\s*(?P<total>\d+))?"
    r"\s*(?P<marker>[▶►])?\s*(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_LAST_AGE_RE = re.compile(r"\blast\s*:\s*(?P<age>[^|·\n]+)", re.IGNORECASE)
_UNIT_ID_RE = re.compile(r"\b(?P<unit>[A-Z]{1,4}\d{2,4})(?:[-\s:—]|$)")
_UNSAFE_RE = re.compile(
    r"("  # Raw paths, JSON-looking blobs, stack traces, and debug ledgers.
    r"(?:^|\s)/(?:data|home|tmp)/\S+|"
    r"(?:^|\s)\.omx/\S+|"
    r"\{[^\n]{8,}\}|"
    r"Traceback \(most recent call last\)|"
    r"ledger\.jsonl|goals\.json|ralplan-state\.json|"
    r"stack trace|debug payload"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OmxWorkflowStatus:
    """Normalized, Telegram-safe OMX workflow progress state."""

    workflow: str
    state: str
    progress_current: int | None = None
    progress_total: int | None = None
    unit_id: str | None = None
    current_unit_summary: str | None = None
    last_age: str | None = None
    updated_at: datetime | None = None
    source_path: Path | None = None
    source: str = "state"


def render_omx_workflow_status(status: OmxWorkflowStatus) -> str:
    """Render a compact Telegram status text for a normalized OMX workflow."""

    parts = ["🧭 OMX", status.workflow]
    if status.progress_current is not None and status.progress_total is not None:
        parts.append(f"{status.progress_current}/{status.progress_total}")

    first_line = " ".join(parts)
    suffixes: list[str] = []
    if status.unit_id:
        suffixes.append(status.unit_id)
    if status.state:
        suffixes.append(status.state)
    if status.last_age:
        suffixes.append(f"last {status.last_age}")
    if suffixes:
        first_line = f"{first_line} · {' · '.join(suffixes)}"

    summary = _safe_clip(status.current_unit_summary, limit=80)
    if summary:
        return f"{first_line}\n↳ {summary}"
    return first_line


def read_omx_workflow_status(
    cwd: Path | str,
    *,
    pane_text: str | None = None,
    now: datetime | None = None,
    max_age_seconds: int = 24 * 60 * 60,
) -> OmxWorkflowStatus | None:
    """Return the freshest recognized status under ``cwd/.omx`` or pane fallback."""

    root = Path(cwd)
    for path in _candidate_state_paths(root):
        status = load_omx_workflow_status_path(
            path,
            cwd=root,
            now=now,
            max_age_seconds=max_age_seconds,
        )
        if status is not None:
            return status
    if pane_text:
        return parse_omx_statusline(pane_text)
    return None


def load_omx_workflow_status_path(
    path: Path | str,
    *,
    cwd: Path | str | None = None,
    now: datetime | None = None,
    max_age_seconds: int = 24 * 60 * 60,
) -> OmxWorkflowStatus | None:
    """Load a recognized fresh OMX workflow status JSON file.

    If ``cwd`` is provided, the file must live under ``cwd/.omx``. This prevents
    unrelated nearby state from being treated as the bound runtime state.
    """

    state_path = Path(path)
    if cwd is not None and not _is_relative_to(state_path, Path(cwd) / ".omx"):
        return None
    if not state_path.exists() or not state_path.is_file():
        return None
    if _is_stale(state_path, now=now, max_age_seconds=max_age_seconds):
        return None
    try:
        payload = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    if state_path.name == "goals.json" or "goals" in payload:
        return _status_from_ultragoal_plan(payload, state_path)
    return _status_from_workflow_state(payload, state_path)


def parse_omx_statusline(pane_text: str) -> OmxWorkflowStatus | None:
    """Parse a strict OMX statusline/footer from visible pane text.

    This intentionally ignores ordinary prose. The line must begin with a
    recognized workflow name and include an OMX-ish progress marker/body.
    """

    lines = [line.strip() for line in pane_text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        match = _OMX_STATUSLINE_RE.match(line)
        if not match:
            continue
        if not (match.group("marker") or match.group("current")):
            continue
        workflow = match.group("workflow").lower()
        body = match.group("body") or ""
        current = _to_int(match.group("current"))
        total = _to_int(match.group("total"))
        last_age = _extract_last_age("\n".join(lines[index : index + 3]))
        unit_id, summary = _split_unit_summary(body)
        return OmxWorkflowStatus(
            workflow=workflow,
            state="running",
            progress_current=current,
            progress_total=total,
            unit_id=unit_id,
            current_unit_summary=summary,
            last_age=last_age,
            source="pane",
        )
    return None


def _candidate_state_paths(cwd: Path) -> list[Path]:
    omx_root = cwd / ".omx"
    paths: list[Path] = []
    paths.extend(omx_root.glob("ultragoal/goals.json"))
    paths.extend(omx_root.glob("state/*-state.json"))
    paths.extend(omx_root.glob("state/sessions/*/*-state.json"))
    return sorted(
        {p for p in paths if p.is_file()},
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _status_from_ultragoal_plan(
    payload: dict[str, Any], source_path: Path
) -> OmxWorkflowStatus | None:
    goals = payload.get("goals")
    if not isinstance(goals, list) or not goals:
        return None

    selected_index = None
    selected_goal: dict[str, Any] | None = None
    for wanted in ("running", "pending", "blocked"):
        for index, candidate in enumerate(goals):
            if not isinstance(candidate, dict):
                continue
            if _normalize_state(candidate.get("status")) == wanted:
                selected_index = index
                selected_goal = candidate
                break
        if selected_goal is not None:
            break
    if selected_goal is None:
        complete_goals = [
            (index, candidate)
            for index, candidate in enumerate(goals)
            if isinstance(candidate, dict)
            and _normalize_state(candidate.get("status")) == "complete"
        ]
        if len(complete_goals) == len(goals):
            selected_index, selected_goal = complete_goals[-1]
    if selected_goal is None or selected_index is None:
        return None

    state = _normalize_state(selected_goal.get("status"))
    if state == "pending":
        state = "waiting"
    unit_id = _extract_unit_id(str(selected_goal.get("id") or ""))
    summary = str(
        selected_goal.get("title")
        or selected_goal.get("objective")
        or selected_goal.get("id")
        or ""
    )
    updated_at = _parse_datetime(
        selected_goal.get("updatedAt") or payload.get("updatedAt") or payload.get("createdAt")
    )
    return OmxWorkflowStatus(
        workflow="ultragoal",
        state=state,
        progress_current=selected_index + 1,
        progress_total=len(goals),
        unit_id=unit_id,
        current_unit_summary=_safe_clip(summary, limit=160),
        last_age=_format_age(updated_at),
        updated_at=updated_at,
        source_path=source_path,
    )


def _status_from_workflow_state(
    payload: dict[str, Any], source_path: Path
) -> OmxWorkflowStatus | None:
    workflow = str(
        payload.get("workflow")
        or payload.get("mode")
        or source_path.name.removesuffix("-state.json")
    ).lower()
    if workflow not in _RECOGNIZED_WORKFLOWS:
        return None

    raw_state = payload.get("status") or payload.get("current_phase") or payload.get("phase")
    if raw_state is None and payload.get("active") is True:
        raw_state = "running"
    state = _normalize_state(raw_state)
    if state == "unknown":
        return None
    if state == "pending":
        state = "waiting"

    current, total = _extract_progress(payload)
    unit_id = _extract_unit_id(
        str(payload.get("goal_id") or payload.get("current_goal_id") or payload.get("unit_id") or "")
    )
    summary = str(
        payload.get("current_unit_summary")
        or payload.get("objective")
        or payload.get("title")
        or payload.get("task")
        or ""
    )
    updated_at = _parse_datetime(payload.get("updated_at") or payload.get("updatedAt"))
    return OmxWorkflowStatus(
        workflow=workflow,
        state=state,
        progress_current=current,
        progress_total=total,
        unit_id=unit_id,
        current_unit_summary=_safe_clip(summary, limit=160),
        last_age=_format_age(updated_at),
        updated_at=updated_at,
        source_path=source_path,
    )


def _extract_progress(payload: dict[str, Any]) -> tuple[int | None, int | None]:
    current = payload.get("current") or payload.get("current_index") or payload.get("index")
    total = payload.get("total") or payload.get("goal_count") or payload.get("count")
    return _to_int(current), _to_int(total)


def _split_unit_summary(body: str) -> tuple[str | None, str | None]:
    text = body.strip()
    text = re.sub(r"\s+·\s*objective\s*:.+$", "", text, flags=re.IGNORECASE)
    if ":" in text:
        left, right = text.split(":", 1)
        unit = _extract_unit_id(left)
        return unit, _safe_clip(right.strip(), limit=160)
    unit = _extract_unit_id(text)
    return unit, _safe_clip(text, limit=160)


def _extract_last_age(text: str) -> str | None:
    match = _LAST_AGE_RE.search(text)
    if not match:
        return None
    return _safe_clip(match.group("age").strip(), limit=24)


def _extract_unit_id(text: str) -> str | None:
    match = _UNIT_ID_RE.search(text)
    return match.group("unit") if match else None


def _normalize_state(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"active", "in_progress", "planning", "executing", "running", "started"}:
        return "running"
    if text in {"waiting", "pending", "idle", "awaiting_input"}:
        return "pending"
    if text in {"blocked", "failed", "error", "needs_user_decision"}:
        return "blocked"
    if text in {"complete", "completed", "done", "finished"}:
        return "complete"
    return "unknown"


def _safe_clip(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text or _UNSAFE_RE.search(text):
        return None
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _is_stale(path: Path, *, now: datetime | None, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0:
        return False
    reference = now or datetime.now(UTC)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return (reference - mtime).total_seconds() > max_age_seconds


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_age(updated_at: datetime | None) -> str | None:
    if updated_at is None:
        return None
    seconds = max(0, int((datetime.now(UTC) - updated_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _to_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
