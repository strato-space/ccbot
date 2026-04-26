"""Human-facing Telegram delivery policy.

The runtime layer emits normalized semantic events. This module decides how
much of that execution surface reaches Telegram in the default human-facing
chat mode.

`compact` is the production default:
- hide internal injected user payloads (`<skill>`, local command XML, etc.)
- keep only the latest human-facing narrative visible; `commentary` and
  orchestration milestones collapse into one mutable surface
- keep reasoning/tool/command/file-change updates in the mutable status
  artifact instead of as permanent content bubbles
- keep warning artifacts as Telegram-visible system notices
- suppress placeholder reasoning with no human-readable summary

`verbose` leaves the existing runtime-visible behavior intact for debugging.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .runtime_types import (
    COMMAND_EXECUTION_SEMANTIC_KIND,
    COMMENTARY_SEMANTIC_KIND,
    FILE_CHANGE_SEMANTIC_KIND,
    ORCHESTRATION_SEMANTIC_KIND,
    REASONING_SEMANTIC_KIND,
    TOOL_RESULT_SEMANTIC_KIND,
    TOOL_START_SEMANTIC_KIND,
    USER_ECHO_SEMANTIC_KIND,
    WARNING_SEMANTIC_KIND,
    NormalizedEvent,
)

_INTERNAL_USER_ECHO_RE = re.compile(
    r"^\s*<("
    r"skill|command-name|local-command-stdout|bash-input|bash-stdout|bash-stderr|"
    r"subagent_notification|"
    r"local-command-caveat|system-reminder"
    r")\b",
    re.IGNORECASE,
)
_PLACEHOLDER_REASONING = {"[reasoning]", "(thinking)"}
_STATUS_ONLY_SEMANTIC_KINDS = {
    REASONING_SEMANTIC_KIND,
    TOOL_START_SEMANTIC_KIND,
    TOOL_RESULT_SEMANTIC_KIND,
    COMMAND_EXECUTION_SEMANTIC_KIND,
    FILE_CHANGE_SEMANTIC_KIND,
}
_COMPACT_STATUS_LIMIT = 280


def is_internal_user_payload(text: str) -> bool:
    stripped = text.strip()
    if _INTERNAL_USER_ECHO_RE.match(stripped):
        return True
    if stripped.startswith("<turn_aborted>"):
        return True
    return False


def is_non_turn_user_notification(text: str) -> bool:
    """Return True for hidden user payloads that do not open a new turn."""
    stripped = text.strip()
    if stripped.startswith("<turn_aborted>"):
        return True
    return _INTERNAL_USER_ECHO_RE.match(stripped) is not None


def _clip_inline(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _compact_single_block(text: str, *, max_chars: int = _COMPACT_STATUS_LIMIT) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    if "```" in text:
        lines = text.splitlines()
        fence_start = next(
            (index for index, line in enumerate(lines) if line.startswith("```")),
            None,
        )
        if fence_start is not None:
            close_index = next(
                (
                    index
                    for index, line in enumerate(
                        lines[fence_start + 1 :],
                        start=fence_start + 1,
                    )
                    if line.startswith("```")
                ),
                None,
            )
            if close_index is None:
                body = lines[fence_start + 1 :]
                trailing: list[str] = []
            else:
                body = lines[fence_start + 1 : close_index]
                trailing = lines[close_index + 1 :]
            prefix_lines = [
                _clip_inline(line, max_chars=80)
                for line in lines[:fence_start]
                if line.strip()
            ][:2]
            clipped_body = [
                _clip_inline(line, max_chars=72) for line in body[:5]
            ]
            compact_lines = [*prefix_lines, lines[fence_start], *clipped_body, "```"]
            omitted = (
                max(
                    0,
                    len([line for line in lines[:fence_start] if line.strip()])
                    - len(prefix_lines),
                )
                + max(0, len(body) - len(clipped_body))
                + len([line for line in trailing if line.strip()])
            )
            if omitted > 0:
                compact_lines.extend(
                    ["", f"preview {len(clipped_body)}/{len(clipped_body) + omitted} lines"]
                )
            compact = "\n".join(compact_lines)
            if len(compact) <= max_chars:
                return compact
            trailing_nonempty = [line for line in trailing if line.strip()]
            fallback_lines = prefix_lines[:1]
            if body:
                fallback_lines.append(_clip_inline(body[0], max_chars=80))
            elif trailing_nonempty:
                fallback_lines.append(_clip_inline(trailing_nonempty[0], max_chars=80))
            omitted_plain = (
                max(
                    0,
                    len([line for line in lines[:fence_start] if line.strip()])
                    - len(prefix_lines[:1]),
                )
                + max(0, len(body) - (1 if body else 0))
                + max(0, len(trailing_nonempty) - (0 if body else 1 if trailing_nonempty else 0))
            )
            plain = "\n".join(line for line in fallback_lines if line)
            if omitted_plain > 0:
                shown_plain = 1 if (body or trailing_nonempty) else 0
                plain = "\n".join(
                    part
                    for part in (
                        plain,
                        f"preview {shown_plain}/{shown_plain + omitted_plain} lines",
                    )
                    if part
                )
            if len(plain) <= max_chars:
                return plain
            return _clip_inline(" ".join(plain.split()), max_chars=max_chars)
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

    if projected.role == "user" and is_internal_user_payload(text):
        return _suppress(projected)

    if projected.semantic_kind == USER_ECHO_SEMANTIC_KIND:
        # Ordinary user echo stays visible in compact mode even if upstream
        # normalizers set dispatch_to_telegram=False on user events.
        projected.include_in_history = True
        projected.dispatch_to_telegram = True
        projected.status_message_eligible = False
        projected.is_complete = True
        return projected

    if projected.semantic_kind == WARNING_SEMANTIC_KIND:
        projected.include_in_history = True
        projected.dispatch_to_telegram = True
        projected.status_message_eligible = False
        projected.is_complete = True
        return projected

    if projected.semantic_kind in {
        COMMENTARY_SEMANTIC_KIND,
        ORCHESTRATION_SEMANTIC_KIND,
    }:
        if projected.semantic_kind == ORCHESTRATION_SEMANTIC_KIND:
            projected.text = _compact_single_block(text)
        else:
            projected.text = text
        projected.include_in_history = False
        projected.dispatch_to_telegram = True
        projected.status_message_eligible = False
        projected.is_complete = True
        return projected

    if projected.semantic_kind == REASONING_SEMANTIC_KIND:
        if not text or text in _PLACEHOLDER_REASONING:
            return _suppress(projected)
        projected.text = _compact_single_block(text)
        projected.status_message_eligible = True
        projected.is_complete = False
        projected.include_in_history = False
        return projected

    if projected.content_type == "tool_use" and (projected.tool_name or "").lower() == "skill":
        return _suppress(projected)

    if projected.content_type == "tool_result" and (projected.tool_name or "").lower() == "skill":
        return _suppress(projected)

    if projected.semantic_kind in _STATUS_ONLY_SEMANTIC_KINDS:
        projected.text = _compact_single_block(text)
        projected.status_message_eligible = True
        projected.is_complete = False
        projected.include_in_history = False
        return projected

    return projected
