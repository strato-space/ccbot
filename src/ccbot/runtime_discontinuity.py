"""Helpers for Codex runtime-discontinuity detection and delivery.

These helpers keep runtime-termination and live-surface-loss delivery out of
assistant-final semantics while still preferring replay-native evidence over
pane-text recovery when both are available.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .codex_rollout import CodexRolloutNormalizer
from .screenshot import text_to_image

_TOKEN_USAGE_RE = re.compile(r"(?m)^Token usage:\s*total=")
_CONTINUE_SESSION_RE = re.compile(
    r"(?m)^To continue this session, run (?P<command>codex resume .+)$"
)
_EXIT_CODE_LINE_RE = re.compile(r"(?i)\b(?:exit code|return code)\D*(-?\d+)\b")
_SHELL_PROMPT_RE = re.compile(r"^[^\n]*[#$>]\s*$")
_MAX_ROLLOUT_TAIL_LINES = 400
_MAX_PANE_TAIL_LINES = 16
_EXIT_CODE_KEYS = (
    "exit_code",
    "exitcode",
    "return_code",
    "returncode",
)


def is_codex_termination_summary_text(text: str) -> bool:
    """Return True when text matches the Codex session-end summary block."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    return bool(
        _TOKEN_USAGE_RE.search(stripped)
        or _CONTINUE_SESSION_RE.search(stripped)
    )


def format_codex_termination_summary_for_telegram(text: str) -> str:
    """Preserve the raw summary text while monospace-formatting the resume command."""

    def _replace(match: re.Match[str]) -> str:
        command = match.group("command").strip()
        if command.startswith("`") and command.endswith("`"):
            return match.group(0)
        return f"To continue this session, run `{command}`"

    return _CONTINUE_SESSION_RE.sub(_replace, (text or "").strip())


def read_recent_rollout_records(
    file_path: str | Path,
    *,
    max_lines: int = _MAX_ROLLOUT_TAIL_LINES,
) -> list[dict[str, Any]]:
    """Load the tail of a rollout JSONL file, ignoring malformed lines."""
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return []

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []

    records: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def extract_codex_termination_summary_from_rollout(
    file_path: str | Path,
    *,
    thread_id: str,
) -> str | None:
    """Extract the raw Codex termination summary from replay evidence when present."""
    records = read_recent_rollout_records(file_path)
    if not records:
        return None

    events = CodexRolloutNormalizer.normalize_records(records, thread_id=thread_id)
    for event in reversed(events):
        text = (event.text or "").strip()
        if is_codex_termination_summary_text(text):
            return format_codex_termination_summary_for_telegram(text)
    return None


def _trim_prompt_line(lines: list[str]) -> list[str]:
    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    if trimmed and _SHELL_PROMPT_RE.match(trimmed[-1].rstrip()):
        trimmed.pop()
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


def extract_terminal_tail_block(text: str | None) -> str | None:
    """Best-effort extraction of the last visible terminal block from a pane."""
    stripped = (text or "").strip("\n")
    if not stripped:
        return None

    lines = [line.rstrip() for line in stripped.splitlines()]
    lines = _trim_prompt_line(lines)
    if not lines:
        return None

    for idx, line in enumerate(lines):
        if _TOKEN_USAGE_RE.search(line) or _CONTINUE_SESSION_RE.search(line):
            tail = [part for part in lines[idx:] if part.strip()]
            return format_codex_termination_summary_for_telegram("\n".join(tail).strip())

    block: list[str] = []
    for line in reversed(lines):
        if not line.strip() and block:
            break
        if not line.strip():
            continue
        block.append(line)
        if len(block) >= _MAX_PANE_TAIL_LINES:
            break
    if not block:
        return None
    block.reverse()
    return "\n".join(block).strip()


def _extract_nonzero_exit_code(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in _EXIT_CODE_KEYS:
            if key in value:
                candidate = value[key]
                if isinstance(candidate, int) and candidate != 0:
                    return candidate
                if isinstance(candidate, str):
                    try:
                        parsed = int(candidate.strip())
                    except ValueError:
                        parsed = 0
                    if parsed != 0:
                        return parsed
        for candidate in reversed(list(value.values())):
            parsed = _extract_nonzero_exit_code(candidate)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, list):
        for candidate in reversed(value):
            parsed = _extract_nonzero_exit_code(candidate)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, str):
        match = _EXIT_CODE_LINE_RE.search(value)
        if not match:
            return None
        try:
            parsed = int(match.group(1))
        except ValueError:
            return None
        return parsed or None
    return None


def extract_nonzero_exit_code_from_rollout(file_path: str | Path) -> int | None:
    """Return the most recent non-zero exit code present in rollout tail records."""
    records = read_recent_rollout_records(file_path)
    for record in reversed(records):
        parsed = _extract_nonzero_exit_code(record)
        if parsed is not None:
            return parsed
    return None


async def build_discontinuity_image_data(
    pane_text: str | None,
) -> list[tuple[str, bytes]] | None:
    """Render a pane snapshot to PNG image_data for Telegram delivery."""
    if not pane_text:
        return None
    png_bytes = await text_to_image(pane_text, with_ansi=False)
    return [("discontinuity-screenshot.png", png_bytes)]


def build_runtime_termination_fallback_summary(
    *,
    exit_code: int | None = None,
) -> str:
    lines = [
        "⚠️ Codex runtime ended before a raw termination summary could be recovered.",
    ]
    if exit_code is not None:
        lines.append(f"Exit code: {exit_code}")
    return "\n".join(lines)


def build_live_surface_loss_fallback_summary(
    *,
    window_name: str,
    replay_readable: bool,
    exit_code: int | None = None,
) -> str:
    lines = [
        f"⚠️ Live tmux window '{window_name}' disappeared before a raw termination summary could be recovered.",
    ]
    if replay_readable:
        lines.append("Continuing from persisted replay evidence in external read-only mode.")
    else:
        lines.append(
            "Persisted replay evidence is currently unavailable; the stored Codex session identity can still be resumed from this control surface later."
        )
    if exit_code is not None:
        lines.append(f"Exit code: {exit_code}")
    return "\n".join(lines)


def build_live_surface_loss_notice(*, window_name: str, replay_readable: bool) -> str:
    if replay_readable:
        return (
            f"⚠️ Live tmux window '{window_name}' disappeared. "
            "This control surface switched to external read-only replay mode."
        )
    return (
        f"⚠️ Live tmux window '{window_name}' disappeared. "
        "Replay evidence is currently unavailable, but the persisted Codex session identity remains resumable from this control surface."
    )
