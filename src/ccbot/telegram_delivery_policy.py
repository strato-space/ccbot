"""Human-facing Telegram delivery policy.

The runtime layer emits normalized semantic events. This module decides how
much of that execution surface reaches Telegram in the default human-facing
chat mode.

`compact` is the production default:
- hide internal injected user payloads (`<skill>`, local command XML, etc.)
- keep commentary/reasoning/command/file-change updates in the mutable status
  artifact instead of as permanent content bubbles
- suppress placeholder reasoning with no human-readable summary

`verbose` leaves the existing runtime-visible behavior intact for debugging.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .runtime_types import NormalizedEvent

_INTERNAL_USER_ECHO_RE = re.compile(
    r"^\s*<("
    r"skill|command-name|local-command-stdout|bash-input|bash-stdout|bash-stderr|"
    r"local-command-caveat|system-reminder"
    r")\b",
    re.IGNORECASE,
)
_PLACEHOLDER_REASONING = {"[reasoning]", "(thinking)"}
_STATUS_ONLY_CONTENT_TYPES = {"commentary", "reasoning", "command_execution", "file_change"}
_COMPACT_STATUS_LIMIT = 280


def _compact_single_block(text: str, *, max_chars: int = _COMPACT_STATUS_LIMIT) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _suppress(event: NormalizedEvent) -> NormalizedEvent:
    event.dispatch_to_telegram = False
    event.include_in_history = False
    event.status_message_eligible = False
    return event


def apply_telegram_delivery_policy(
    event: NormalizedEvent,
    *,
    mode: str = "compact",
) -> NormalizedEvent:
    """Project a normalized runtime event into Telegram-facing delivery behavior."""
    if mode != "compact":
        return event

    projected = replace(event)
    text = projected.text.strip()

    if projected.role == "user" and _INTERNAL_USER_ECHO_RE.match(text):
        return _suppress(projected)

    if projected.content_type == "reasoning":
        if not text or text in _PLACEHOLDER_REASONING:
            return _suppress(projected)
        projected.text = _compact_single_block(text)
        projected.status_message_eligible = True
        projected.is_complete = False
        return projected

    if projected.content_type == "tool_use" and (projected.tool_name or "").lower() == "skill":
        return _suppress(projected)

    if projected.content_type == "tool_result" and (projected.tool_name or "").lower() == "skill":
        return _suppress(projected)

    if projected.content_type in _STATUS_ONLY_CONTENT_TYPES:
        projected.text = _compact_single_block(text)
        projected.status_message_eligible = True
        projected.is_complete = False
        return projected

    return projected
