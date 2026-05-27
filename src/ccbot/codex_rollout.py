"""Normalization helpers for Codex rollout JSONL records.

Codex rollout files are append-only JSONL streams with a small set of record
types:

- ``session_meta``: thread/session identity and launch metadata
- ``turn_context``: prompt-time execution context
- ``response_item``: persisted model/tool content
- ``event_msg``: lightweight lifecycle/status stream

This module turns those raw records into :class:`NormalizedEvent` values while
preserving the semantic distinctions needed by the bot layer.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shlex
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .config import config
from .runtime_types import (
    GENERATED_IMAGE_PREVIEW_CONTENT_TYPE,
    IMAGE_PREVIEW_SEMANTIC_KIND,
    NormalizedEvent,
    VIEWED_IMAGE_PREVIEW_CONTENT_TYPE,
)

CODEX_ROLLOUT_TYPES = {
    "session_meta",
    "turn_context",
    "response_item",
    "event_msg",
}

_COMMENTARY_PHASES = {"commentary"}
_LIFECYCLE_ITEM_TYPES = {
    "enteredReviewMode",
    "exitedReviewMode",
    "contextCompaction",
}
_TOOL_SUMMARY_KEYS = (
    "file_path",
    "path",
    "url",
    "query",
    "description",
    "command",
    "cmd",
    "shell_command",
    "pattern",
    "title",
    "session_id",
    "ticket_id",
    "issue",
    "project",
)
_FENCED_BLOCK_RE = re.compile(r"```[A-Za-z0-9_-]*\n[\s\S]*?\n```")
_PREVIEW_FOOTER_RE = re.compile(r"^preview\s+\d+/\d+\s+lines$", re.IGNORECASE)
_REDUNDANT_OUTPUT_FOOTER_RE = re.compile(
    r"^(?:[a-z ]*·\s*)?output\s+\d+\s+line\(s\)$",
    re.IGNORECASE,
)
_GENERATED_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_IMAGE_GENERATION_FIELD_RE = re.compile(r"^\s*([^:\n]+):\s*(.*)$")
_GENERATED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_VIEWED_IMAGE_DATA_URL_RE = re.compile(
    r"^data:(?P<media>image/(?:png|jpeg|webp));base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return "" if value is False else str(value)
    if isinstance(value, list):
        parts = [_as_text(item).strip() for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in (
            "text",
            "message",
            "output_text",
            "input_text",
            "reasoning_text",
            "content",
            "summary",
            "output",
            "result",
            "command",
            "cwd",
            "path",
        ):
            if key in value:
                text = _as_text(value[key]).strip()
                if text:
                    return text
        if "type" in value and isinstance(value["type"], str):
            return value["type"]
        return ""
    return str(value).strip()


def _json_fragment(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _tool_name_is(name: str | None, expected: str) -> bool:
    lowered = (name or "").strip().lower()
    expected_lower = expected.lower()
    return lowered == expected_lower or lowered.endswith(f".{expected_lower}")


def _text_from_content(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                block_type = item.get("type")
                if block_type in {"text", "input_text", "output_text"}:
                    text = _as_text(item.get("text") or item.get("value")).strip()
                    if text:
                        parts.append(text)
                    continue
                if block_type in {"reasoning", "thinking"}:
                    text = _as_text(
                        item.get("text")
                        or item.get("thinking")
                        or item.get("summary")
                    ).strip()
                    if text:
                        parts.append(text)
                    continue
                text = _as_text(item).strip()
                if text:
                    parts.append(text)
            else:
                text = _as_text(item).strip()
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return _as_text(content).strip()


def _nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _split_shell_chain_line(line: str) -> list[str]:
    """Split a one-line shell chain into previewable command segments."""
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(line):
        char = line[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            index += 1
            continue
        if line.startswith("&&", index) or char == ";":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 2 if line.startswith("&&", index) else 1
            while index < len(line) and line[index].isspace():
                index += 1
            continue
        current.append(char)
        index += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments or [line.strip()]


def _shell_preview_lines(text: str) -> list[str]:
    """Return physical or shell-chain command lines for human previews."""
    lines = _nonempty_lines(text)
    if not lines:
        return []
    expanded: list[str] = []
    for line in lines:
        expanded.extend(_split_shell_chain_line(line))
    return expanded


def _compact_inline(text: str, *, max_chars: int = 160) -> str:
    text = reflow_whitespace(text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _clip_text(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def reflow_whitespace(text: str) -> str:
    return " ".join(text.split())


def _compact_multiline(text: str, *, max_chars: int = 160) -> str:
    lines = _nonempty_lines(text)
    if not lines:
        return ""
    head = _compact_inline(lines[0], max_chars=max_chars)
    if len(lines) == 1:
        return head
    return f"{head} (+{len(lines) - 1} more lines)"


_SHELL_NAMES = {"sh", "bash", "zsh", "fish"}
_SUBAGENT_NOTIFICATION_RE = re.compile(
    r"^\s*<subagent_notification>\s*(\{.*\})\s*</subagent_notification>\s*$",
    re.DOTALL,
)
_HEADS_UP_WARNING_RE = re.compile(r"^\s*(?:⚠️?\s*)?heads up\b", re.IGNORECASE)
_USAGE_LIMIT_WARNING_RE = re.compile(
    r"(usage limit|purchase more credits|try again at|usage_limit_exceeded)",
    re.IGNORECASE,
)
_HOOK_PROMPT_RE = re.compile(
    r'^\s*<hook_prompt\b[^>]*>(?P<body>[\s\S]*?)</hook_prompt>\s*$',
    re.IGNORECASE,
)
_PENDING_EVENT_FLUSH_WINDOW_SECONDS = 0.5
_USER_MESSAGE_DUPLICATE_WINDOW_SECONDS = 0.5


def _pending_event_flush_window_seconds() -> float:
    """Delay fallback event_msg flushes only long enough to prefer canonicals."""
    return _PENDING_EVENT_FLUSH_WINDOW_SECONDS


def _user_duplicate_window_seconds() -> float:
    """Use a duplicate window that is large enough to survive cross-poll delivery.

    The incremental monitor polls every ``monitor_poll_interval`` seconds, so a
    duplicate window shorter than one poll cycle cannot safely suppress later
    canonical copies that arrive on the next poll. We keep at least one full
    quiet poll between the lightweight copy and any fallback flush.
    """
    return max(
        _USER_MESSAGE_DUPLICATE_WINDOW_SECONDS,
        config.monitor_poll_interval * 2.0,
    )


@dataclass
class _SpawnCall:
    role: str
    model: str
    reasoning_effort: str
    prompt: str


@dataclass
class _AgentDescriptor:
    agent_id: str
    nickname: str
    role: str
    model: str
    reasoning_effort: str
    prompt: str


@dataclass
class _PendingMessageEvent:
    signature: tuple[str, str, str | None, str]
    timestamp_seconds: float
    events: list[NormalizedEvent]


@dataclass
class _PendingWait:
    target_ids: list[str]
    generations: dict[str, int]


@dataclass(frozen=True)
class _ViewImageCall:
    path: str = ""
    detail: str = ""


@dataclass
class CodexRolloutState:
    """Per-thread incremental normalization state for poll-sliced rollout tails."""

    pending_spawns: dict[str, _SpawnCall] = field(default_factory=dict)
    pending_waits: dict[str, _PendingWait] = field(default_factory=dict)
    active_waits: set[str] = field(default_factory=set)
    agents_by_id: dict[str, _AgentDescriptor] = field(default_factory=dict)
    wait_generations: dict[str, int] = field(default_factory=dict)
    seen_statuses_by_generation: dict[tuple[str, int], set[tuple[str, str, str]]] = (
        field(default_factory=dict)
    )
    pending_event_messages: list[_PendingMessageEvent] = field(default_factory=list)
    canonical_message_signatures: set[tuple[str, str, str | None, str]] = field(
        default_factory=set
    )
    recent_user_event_messages: dict[
        tuple[str, str, str | None, str], list[float]
    ] = field(default_factory=dict)
    tool_names_by_call_id: dict[str, str] = field(default_factory=dict)
    exec_commands_by_call_id: dict[str, tuple[str, str]] = field(default_factory=dict)
    view_images_by_call_id: dict[str, _ViewImageCall] = field(default_factory=dict)
    turn_generation: int = 0
    current_turn_key: str = ""
    active_turn_user_opened: bool = False


def _extract_shell_payload(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""

    lines = _nonempty_lines(stripped)
    if len(lines) >= 3:
        shell_name = os.path.basename(lines[0])
        if shell_name in _SHELL_NAMES and lines[1] == "-lc":
            return "\n".join(lines[2:]).strip()

    try:
        parts = shlex.split(stripped)
    except ValueError:
        return stripped

    if len(parts) >= 3:
        shell_name = os.path.basename(parts[0])
        if shell_name in _SHELL_NAMES and parts[1] == "-lc":
            return parts[2].strip()

    return stripped


def _preview_footer(total_lines: int, shown_lines: int) -> str:
    """Human-facing preview metadata kept outside the code block body."""
    remaining = max(0, total_lines - shown_lines)
    if remaining <= 0:
        return ""
    return f"preview {shown_lines}/{total_lines} lines"


def _preserve_existing_fenced_preview(text: str) -> str:
    stripped = text.strip()
    if not stripped or not _FENCED_BLOCK_RE.search(stripped):
        return ""

    lines = stripped.splitlines()
    if any(_PREVIEW_FOOTER_RE.match(line.strip()) for line in lines):
        while lines and not lines[-1].strip():
            lines.pop()
        while lines and _REDUNDANT_OUTPUT_FOOTER_RE.match(lines[-1].strip()):
            lines.pop()
            while lines and not lines[-1].strip():
                lines.pop()
        return "\n".join(lines).strip()
    return stripped


def _command_code_block(
    command: str,
    *,
    max_lines: int = 10,
    max_chars: int = 180,
) -> str:
    payload = _extract_shell_payload(command)
    lines = _shell_preview_lines(payload)
    if not lines:
        return ""

    clipped = [_compact_inline(line, max_chars=max_chars) for line in lines[:max_lines]]
    block = "```sh\n" + "\n".join(clipped) + "\n```"
    footer = _preview_footer(len(lines), len(clipped))
    return "\n\n".join(part for part in (block, footer) if part)


def _structured_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
    return value


def _exec_invocation(arguments: Any) -> tuple[str, str]:
    parsed = _structured_json(arguments)
    if not isinstance(parsed, dict):
        return "", ""
    cmd = _as_text(parsed.get("cmd") or parsed.get("command")).strip()
    workdir = _as_text(parsed.get("workdir") or parsed.get("cwd")).strip()
    return cmd, workdir


_EXEC_OUTPUT_WRAPPER_PREFIXES = (
    "Command:",
    "Chunk ID:",
    "Wall time:",
    "Process exited with code",
    "Process running with session ID",
    "Original token count:",
)
_EXEC_EXIT_STATUS_RE = re.compile(r"^Process exited with code\s+(-?\d+)\b")
_EXEC_RUNNING_STATUS_RE = re.compile(r"^Process running with session ID\s+(.+)$")


def _exec_output_wrapper_seen(text: str) -> bool:
    lines = text.strip().splitlines()
    return any(
        line.strip().startswith(_EXEC_OUTPUT_WRAPPER_PREFIXES)
        for line in lines[:10]
    )


def _exec_command_from_wrapper(text: str) -> str:
    for line in text.strip().splitlines()[:10]:
        clean = line.strip()
        if clean.startswith("Command:"):
            return clean.removeprefix("Command:").strip()
    return ""


def _exec_output_from_wrapper(text: str) -> tuple[str, str]:
    """Return human output plus status extracted from exec transport metadata."""
    stripped = text.strip()
    if not stripped:
        return "", ""

    lines = stripped.splitlines()
    wrapper_seen = _exec_output_wrapper_seen(text)
    status = ""
    for line in lines[:14]:
        clean = line.strip()
        exit_match = _EXEC_EXIT_STATUS_RE.match(clean)
        if exit_match:
            exit_code = exit_match.group(1)
            status = "completed" if exit_code == "0" else f"failed · exit {exit_code}"
            continue
        if _EXEC_RUNNING_STATUS_RE.match(clean):
            status = "running"
    output_index = next(
        (
            index
            for index, line in enumerate(lines[:14])
            if line.strip() == "Output:"
        ),
        None,
    )
    if wrapper_seen and output_index is not None:
        return "\n".join(lines[output_index + 1 :]).strip(), status
    if not wrapper_seen:
        return stripped, ""
    filtered = [
        line
        for line in lines
        if not line.strip().startswith(_EXEC_OUTPUT_WRAPPER_PREFIXES)
        and line.strip() != "Output:"
    ]
    return "\n".join(filtered).strip(), status


def _strip_exec_output_wrapper(text: str) -> str:
    """Remove Codex/developer-tool transport metadata from exec output text."""
    output, _status = _exec_output_from_wrapper(text)
    return output


def _tool_output_indicates_failure(payload: dict[str, Any]) -> bool:
    """Detect tool outputs that should stay visible even for normally quiet tools."""
    status = _as_text(
        payload.get("status")
        or payload.get("outcome")
        or payload.get("state")
        or payload.get("result_status")
    ).strip().lower()
    if status in {"failed", "failure", "error", "errored", "cancelled", "timeout"}:
        return True
    if payload.get("error") not in (None, "", {}, []):
        return True

    raw = (
        payload.get("output")
        if payload.get("output") is not None
        else payload.get("content")
        if payload.get("content") is not None
        else payload.get("result")
    )
    parsed = _structured_json(raw)
    if isinstance(parsed, dict):
        if parsed.get("ok") is False or parsed.get("success") is False:
            return True
        if parsed.get("error") not in (None, "", {}, []):
            return True
        nested_status = _as_text(
            parsed.get("status")
            or parsed.get("outcome")
            or parsed.get("result_status")
        ).strip().lower()
        if nested_status in {"failed", "failure", "error", "errored", "cancelled", "timeout"}:
            return True

    text = _text_from_content(raw).strip().lower()
    return text.startswith(("error:", "failed:", "traceback "))


def _tool_text_code_block(
    text: str,
    *,
    language: str = "text",
    max_lines: int = 20,
    max_chars: int = 180,
) -> str:
    preserved = _preserve_existing_fenced_preview(text)
    if preserved:
        return preserved

    lines = _nonempty_lines(text)
    if not lines:
        return ""

    clipped = [_compact_inline(line, max_chars=max_chars) for line in lines[:max_lines]]
    block = f"```{language}\n" + "\n".join(clipped) + "\n```"
    footer = _preview_footer(len(lines), len(clipped))
    return "\n\n".join(part for part in (block, footer) if part)


def _tool_json_code_block(
    value: Any,
    *,
    max_lines: int = 20,
    max_chars: int = 160,
) -> str:
    try:
        if isinstance(value, str):
            parsed = json.loads(value)
        else:
            parsed = value
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""

    pretty = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    lines = pretty.splitlines()
    clipped = [_compact_inline(line, max_chars=max_chars) for line in lines[:max_lines]]
    block = "```json\n" + "\n".join(clipped) + "\n```"
    footer = _preview_footer(len(lines), len(clipped))
    return "\n\n".join(part for part in (block, footer) if part)


def _tool_call_summary(name: str, arguments: Any) -> str:
    lowered = name.lower()

    if isinstance(arguments, str):
        text = arguments.strip()
        if _tool_name_is(lowered, "apply_patch"):
            line_count = len(text.splitlines())
            return f"{name}(patch {line_count} lines)"
        parsed_arguments = _structured_json(text)
        if isinstance(parsed_arguments, dict) and any(
            _tool_name_is(lowered, expected)
            for expected in ("exec_command", "write_stdin", "apply_patch", "state_write")
        ):
            return _tool_call_summary(name, parsed_arguments)
        json_block = _tool_json_code_block(text)
        if json_block:
            return "\n".join([name, json_block])
        summary = _compact_multiline(text)
        return f"{name}({summary})" if summary else name

    if isinstance(arguments, dict):
        if _tool_name_is(lowered, "exec_command"):
            cmd = _as_text(arguments.get("cmd")).strip()
            workdir = _as_text(arguments.get("workdir")).strip()
            parts = ["exec_command"]
            if cmd:
                command_block = _command_code_block(cmd)
                parts.append(command_block or _compact_multiline(cmd))
            if workdir:
                parts.append(_compact_inline(workdir, max_chars=120))
            return "\n".join(part for part in parts if part)
        if _tool_name_is(lowered, "write_stdin"):
            session_id = _as_text(arguments.get("session_id")).strip()
            chars = _as_text(arguments.get("chars"))
            if chars.strip():
                parts = [f"write_stdin(session {session_id or '?'})"]
                parts.append(
                    _tool_text_code_block(chars, language="text", max_lines=6)
                    or _compact_multiline(chars)
                )
                return "\n".join(part for part in parts if part)
            if session_id:
                return f"write_stdin(session {session_id}, poll)"
            return "write_stdin(poll)"
        if _tool_name_is(lowered, "apply_patch"):
            patch_text = _as_text(arguments.get("patch") or arguments.get("input")).strip()
            if patch_text:
                return f"{name}(patch {len(patch_text.splitlines())} lines)"
        if _tool_name_is(lowered, "state_write"):
            mode = _as_text(arguments.get("mode")).strip() or "state"
            phase = _as_text(arguments.get("current_phase")).strip()
            active = arguments.get("active")
            iteration = _as_text(arguments.get("iteration")).strip()
            task = _as_text(arguments.get("task_description")).strip()
            state_payload = arguments.get("state") if isinstance(arguments.get("state"), dict) else {}
            snapshot = _as_text(state_payload.get("context_snapshot_path") if isinstance(state_payload, dict) else "").strip()
            lines = [f"state_write: {mode}"]
            details = []
            if phase:
                details.append(f"phase={phase}")
            if active is not None:
                details.append(f"active={str(active).lower()}")
            if iteration:
                details.append(f"iteration={iteration}")
            if details:
                lines.append("  └ " + ", ".join(details))
            if task:
                lines.append("    " + _compact_inline(task, max_chars=180))
            if snapshot:
                lines.append("    snapshot: " + _compact_inline(snapshot, max_chars=160))
            return "\n".join(lines)
        json_block = _tool_json_code_block(arguments)
        if json_block:
            return "\n".join([name, json_block])
        for key in _TOOL_SUMMARY_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return f"{name}({_compact_multiline(value)})"
        for key in ("paths", "files", "todos", "questions"):
            value = arguments.get(key)
            if isinstance(value, list) and value:
                return f"{name}({len(value)} item(s))"
        summary = _compact_inline(_json_fragment(arguments), max_chars=120)
        return f"{name}({summary})" if summary else name

    summary = _compact_inline(_json_fragment(arguments), max_chars=120)
    return f"{name}({summary})" if summary else name


def _tool_output_summary(tool_name: str | None, text: str) -> str:
    text = text.strip()
    if not text:
        return "[tool_output]"

    preserved = _preserve_existing_fenced_preview(text)
    if preserved:
        return preserved

    lines = _nonempty_lines(text)
    if not lines:
        return "[tool_output]"

    head = lines[0]
    if head.lower().startswith("success. updated the following files:"):
        file_count = max(0, len(lines) - 1)
        return f"Updated {file_count} file(s)"

    lowered = (tool_name or "").lower()
    if _tool_name_is(lowered, "exec_command"):
        preview = _tool_json_code_block(text, max_lines=20) or _tool_text_code_block(
            text,
            language="sh",
            max_lines=20,
        )
        if preview:
            return preview
        return f"output {len(lines)} line(s)"
    if _tool_name_is(lowered, "write_stdin"):
        preview = _tool_json_code_block(text, max_lines=20) or _tool_text_code_block(
            text,
            language="text",
            max_lines=20,
        )
        if preview:
            return preview
        return f"output {len(lines)} line(s)"

    json_block = _tool_json_code_block(text)
    if json_block:
        return json_block

    preview = _tool_text_code_block(text, language="text", max_lines=10)
    if preview:
        return preview

    label = tool_name or "Tool output"
    return f"{label}: {len(lines)} line(s)"


def _truncate_preview_lines(
    text: str,
    *,
    max_lines: int = 4,
    max_chars: int = 220,
) -> tuple[list[str], int]:
    lines = _nonempty_lines(text)
    if not lines:
        return [], 0

    clipped = [_compact_inline(line, max_chars=max_chars) for line in lines[:max_lines]]
    return clipped, len(lines)


def _prefixed_detail_block(text: str) -> str:
    lines, total_lines = _truncate_preview_lines(text)
    if not lines:
        return ""
    first, *rest = lines
    result = [f"  └ {first}"]
    result.extend(f"    {line}" for line in rest)
    footer = _preview_footer(total_lines, len(lines))
    if footer:
        result.append(f"    {footer}")
    return "\n".join(result)


def _timestamp_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _now_seconds() -> float:
    return time.monotonic()


def _parse_subagent_notification(text: str) -> dict[str, Any] | None:
    match = _SUBAGENT_NOTIFICATION_RE.match(text.strip())
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _spawn_prompt(arguments: dict[str, Any]) -> str:
    message = _as_text(arguments.get("message")).strip()
    if message:
        return message
    items = arguments.get("items")
    if not isinstance(items, list):
        return ""
    texts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if _as_text(item.get("type")).strip() != "text":
            continue
        text = _as_text(item.get("text")).strip()
        if text:
            texts.append(text)
    return "\n".join(texts).strip()


def _agent_label(agent: _AgentDescriptor | None, fallback_id: str) -> str:
    nickname = (agent.nickname if agent else "").strip()
    role = (agent.role if agent else "").strip()
    base = nickname or fallback_id
    if role:
        return f"{base} [{role}]"
    return base


def _spawn_request_suffix(agent: _AgentDescriptor | None) -> str:
    if agent is None:
        return ""
    model = agent.model.strip()
    effort = agent.reasoning_effort.strip()
    if model and effort:
        return f" ({model} {effort})"
    if model:
        return f" ({model})"
    if effort:
        return f" ({effort})"
    return ""



def _plan_status_symbol(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "completed":
        return "☑"
    if normalized == "in_progress":
        return "▶"
    return "☐"


def _plan_update_text(arguments: Any) -> str:
    parsed = _structured_json(arguments)
    if not isinstance(parsed, dict):
        return "• Updated Plan"

    lines = ["• Updated Plan"]
    explanation = _as_text(parsed.get("explanation")).strip()
    if explanation:
        lines.append(f"  └ {explanation}")

    plan = parsed.get("plan")
    if isinstance(plan, list):
        for item in plan:
            if not isinstance(item, dict):
                step = _as_text(item).strip()
                status = ""
            else:
                step = _as_text(item.get("step") or item.get("description")).strip()
                status = _as_text(item.get("status")).strip()
            if not step:
                continue
            symbol = _plan_status_symbol(status)
            lines.append(f"  {symbol} {step}")
    return "\n".join(lines)


def _plan_update_event(
    *,
    thread_id: str,
    arguments: Any,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    return NormalizedEvent(
        thread_id=thread_id,
        text=_plan_update_text(arguments),
        is_complete=True,
        content_type="plan_update",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="plan_update",
        tool_name="update_plan",
    )

def _orchestration_event(
    *,
    thread_id: str,
    text: str,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="orchestration",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="orchestration",
    )


def _spawned_agent_text(agent: _AgentDescriptor) -> str:
    title = f"• Spawned {_agent_label(agent, agent.agent_id)}{_spawn_request_suffix(agent)}"
    detail = _prefixed_detail_block(agent.prompt)
    return "\n".join(part for part in (title, detail) if part)


def _spawn_failed_text(spawn_request: _SpawnCall, output: Any) -> str:
    role = spawn_request.role.strip() or "agent"
    title = f"• Failed to spawn {role}"
    detail = _prefixed_detail_block(_json_fragment(output))
    return "\n".join(part for part in (title, detail) if part)


def _waiting_for_targets_text(
    target_ids: list[str],
    agents_by_id: dict[str, _AgentDescriptor],
) -> str:
    if not target_ids:
        return "• Waiting for agents"
    if len(target_ids) == 1:
        target_id = target_ids[0]
        return f"• Waiting for {_agent_label(agents_by_id.get(target_id), target_id)}"

    details = [
        _agent_label(agents_by_id.get(target_id), target_id) for target_id in target_ids
    ]
    first, *rest = details
    lines = [f"• Waiting for {len(target_ids)} agents", f"  └ {first}"]
    lines.extend(f"    {line}" for line in rest)
    return "\n".join(lines)


def _wait_timeout_text(
    target_ids: list[str],
    agents_by_id: dict[str, _AgentDescriptor],
) -> str:
    if not target_ids:
        return "• Timed out waiting for agents"
    if len(target_ids) == 1:
        target_id = target_ids[0]
        return (
            "• Timed out waiting for "
            f"{_agent_label(agents_by_id.get(target_id), target_id)}"
        )
    details = [
        _agent_label(agents_by_id.get(target_id), target_id) for target_id in target_ids
    ]
    first, *rest = details
    lines = [f"• Timed out waiting for {len(target_ids)} agents", f"  └ {first}"]
    lines.extend(f"    {line}" for line in rest)
    return "\n".join(lines)


def _finished_waiting_text(
    target_ids: list[str],
    agents_by_id: dict[str, _AgentDescriptor],
) -> str:
    if not target_ids:
        return "• Finished waiting"
    if len(target_ids) == 1:
        target_id = target_ids[0]
        return (
            "• Finished waiting for "
            f"{_agent_label(agents_by_id.get(target_id), target_id)}"
        )
    details = [
        _agent_label(agents_by_id.get(target_id), target_id) for target_id in target_ids
    ]
    first, *rest = details
    lines = [f"• Finished waiting for {len(target_ids)} agents", f"  └ {first}"]
    lines.extend(f"    {line}" for line in rest)
    return "\n".join(lines)


def _agent_status_preview(status: Any) -> tuple[str, str]:
    if isinstance(status, str):
        return (status.replace("_", " "), "")
    if not isinstance(status, dict):
        return ("status updated", _compact_inline(_json_fragment(status), max_chars=200))

    for key in ("completed", "failed", "error", "cancelled", "timed_out", "shutdown"):
        if key not in status:
            continue
        raw = _as_text(status.get(key)).strip()
        verb = key.replace("_", " ")
        if raw and raw.lower() != verb:
            return (verb, raw)
        return (verb, "")

    return ("status updated", _compact_inline(_json_fragment(status), max_chars=200))


def _agent_status_text(
    *,
    agent_id: str,
    status: Any,
    agents_by_id: dict[str, _AgentDescriptor],
) -> str:
    label = _agent_label(agents_by_id.get(agent_id), agent_id)
    verb, preview = _agent_status_preview(status)
    title = f"• {label} {verb}"
    detail = _prefixed_detail_block(preview)
    return "\n".join(part for part in (title, detail) if part)


def _status_signature(agent_id: str, status: Any) -> tuple[str, str, str]:
    verb, preview = _agent_status_preview(status)
    return (agent_id, verb, _compact_inline(reflow_whitespace(preview), max_chars=200))


def _next_wait_generation(state: CodexRolloutState, agent_id: str) -> int:
    generation = state.wait_generations.get(agent_id, 0) + 1
    state.wait_generations[agent_id] = generation
    state.seen_statuses_by_generation = {
        key: value
        for key, value in state.seen_statuses_by_generation.items()
        if key[0] != agent_id
    }
    return generation


def _status_seen_in_generation(
    state: CodexRolloutState,
    *,
    agent_id: str,
    status: Any,
    generation: int | None = None,
) -> bool:
    if generation is None:
        generation = state.wait_generations.get(agent_id, 0)
    if generation <= 0:
        return False
    signature = _status_signature(agent_id, status)
    key = (agent_id, generation)
    seen = state.seen_statuses_by_generation.setdefault(key, set())
    if signature in seen:
        return True
    seen.add(signature)
    return False

def _flush_pending_event_messages(
    state: CodexRolloutState,
    *,
    current_time: float | None,
) -> list[NormalizedEvent]:
    if current_time is None or not state.pending_event_messages:
        return []
    ready_before = current_time - _pending_event_flush_window_seconds()
    ready = [
        pending
        for pending in state.pending_event_messages
        if pending.timestamp_seconds <= ready_before
    ]
    state.pending_event_messages = [
        pending
        for pending in state.pending_event_messages
        if pending.timestamp_seconds > ready_before
    ]
    ready.sort(key=lambda item: item.timestamp_seconds)
    flushed: list[NormalizedEvent] = []
    for pending in ready:
        flushed.extend(pending.events)
    return flushed


def _flush_all_pending_event_messages(state: CodexRolloutState) -> list[NormalizedEvent]:
    """Flush all buffered fallback message events in FIFO order.

    A newly opened user turn is a semantic boundary. Pending assistant-side
    fallback copies from the previous turn must flush before that boundary so
    they cannot leak below the next turn opener or collide with same-text
    canonicals from the next turn.
    """
    if not state.pending_event_messages:
        return []
    pending = sorted(
        state.pending_event_messages,
        key=lambda item: item.timestamp_seconds,
    )
    state.pending_event_messages = []
    flushed: list[NormalizedEvent] = []
    for item in pending:
        flushed.extend(item.events)
    return flushed


def _open_next_turn_generation(state: CodexRolloutState) -> int:
    """Advance the surrogate turn generation for duplicate suppression."""
    state.turn_generation += 1
    return state.turn_generation


def _active_turn_key(state: CodexRolloutState) -> str:
    if state.current_turn_key:
        return state.current_turn_key
    return f"surrogate:{state.turn_generation}"


def _is_surrogate_turn_key(turn_key: str) -> bool:
    return turn_key.startswith("surrogate:")


def _message_turn_signature(
    turn_key: str,
    signature: tuple[str, str | None, str],
) -> tuple[str, str, str | None, str]:
    return (turn_key, signature[0], signature[1], signature[2])


def _drain_pending_event_messages(
    state: CodexRolloutState,
    signature: tuple[str, str, str | None, str],
) -> list[NormalizedEvent]:
    kept: list[_PendingMessageEvent] = []
    drained: list[NormalizedEvent] = []
    for pending in state.pending_event_messages:
        if pending.signature == signature:
            drained.extend(_suppress_history_delivery(event) for event in pending.events)
            continue
        kept.append(pending)
    state.pending_event_messages = kept
    return drained


def _consume_recent_user_event_duplicate(
    state: CodexRolloutState,
    signature: tuple[str, str, str | None, str],
) -> bool:
    """Consume a matching live user-message duplicate for the active turn."""
    emitted_at = state.recent_user_event_messages.get(signature)
    if not emitted_at:
        return False
    emitted_at.pop(0)
    if not emitted_at:
        state.recent_user_event_messages.pop(signature, None)
    return True


def _drop_recent_user_event_messages_for_other_turns(
    state: CodexRolloutState,
    *,
    keep_turn_key: str,
) -> None:
    """Keep only duplicate-suppression entries for the active turn."""
    for signature in list(state.recent_user_event_messages.keys()):
        if signature[0] != keep_turn_key:
            state.recent_user_event_messages.pop(signature, None)
    state.canonical_message_signatures = {
        signature
        for signature in state.canonical_message_signatures
        if signature[0] == keep_turn_key
    }


def _rewrite_turn_key(
    state: CodexRolloutState,
    *,
    old_turn_key: str,
    new_turn_key: str,
) -> None:
    """Rewrite buffered signatures from a surrogate turn key to the real key."""
    if old_turn_key == new_turn_key:
        return

    rewritten_pending: list[_PendingMessageEvent] = []
    for pending in state.pending_event_messages:
        if pending.signature[0] == old_turn_key:
            rewritten_pending.append(
                _PendingMessageEvent(
                    signature=(new_turn_key, *pending.signature[1:]),
                    timestamp_seconds=pending.timestamp_seconds,
                    events=pending.events,
                )
            )
        else:
            rewritten_pending.append(pending)
    state.pending_event_messages = rewritten_pending

    rewritten_recent: dict[tuple[str, str, str | None, str], list[float]] = {}
    for signature, emitted_at in state.recent_user_event_messages.items():
        rewritten_signature = signature
        if signature[0] == old_turn_key:
            rewritten_signature = (new_turn_key, *signature[1:])
        rewritten_recent.setdefault(rewritten_signature, []).extend(emitted_at)
    state.recent_user_event_messages = rewritten_recent
    rewritten_canonicals: set[tuple[str, str, str | None, str]] = set()
    for signature in state.canonical_message_signatures:
        if signature[0] == old_turn_key:
            rewritten_canonicals.add((new_turn_key, *signature[1:]))
        else:
            rewritten_canonicals.add(signature)
    state.canonical_message_signatures = rewritten_canonicals


def _activate_real_turn_key(
    state: CodexRolloutState,
    *,
    turn_key: str,
) -> list[NormalizedEvent]:
    """Switch to a real turn id, preserving surrogate buffers for the same turn."""
    if not turn_key:
        return []
    if not state.current_turn_key:
        state.current_turn_key = turn_key
        state.active_turn_user_opened = False
        return []
    if state.current_turn_key == turn_key:
        return []
    if _is_surrogate_turn_key(state.current_turn_key):
        _rewrite_turn_key(
            state,
            old_turn_key=state.current_turn_key,
            new_turn_key=turn_key,
        )
        state.current_turn_key = turn_key
        _drop_recent_user_event_messages_for_other_turns(
            state,
            keep_turn_key=turn_key,
        )
        return []

    flushed = _flush_all_pending_event_messages(state)
    state.current_turn_key = turn_key
    state.active_turn_user_opened = False
    _drop_recent_user_event_messages_for_other_turns(
        state,
        keep_turn_key=turn_key,
    )
    return flushed


def _open_surrogate_turn(
    state: CodexRolloutState,
) -> tuple[str, list[NormalizedEvent]]:
    """Open a new turn before a concrete turn id is known."""
    flushed = _flush_all_pending_event_messages(state)
    turn_key = f"surrogate:{_open_next_turn_generation(state)}"
    state.current_turn_key = turn_key
    state.active_turn_user_opened = False
    _drop_recent_user_event_messages_for_other_turns(
        state,
        keep_turn_key=turn_key,
    )
    return turn_key, flushed


def _command_execution_summary(
    *,
    command: str,
    cwd: str,
    status: str,
    output: str,
) -> str:
    parts: list[str] = []
    if command:
        command_block = _command_code_block(command)
        if command_block:
            parts.append(command_block)
        else:
            parts.append(_compact_multiline(command))
    lines = _nonempty_lines(output)
    if lines:
        preview = _tool_json_code_block(output, max_lines=20) or _tool_text_code_block(
            output,
            language="sh",
            max_lines=20,
        )
        if preview:
            parts.append(preview)
            normalized_status = status.lower()
            if status and normalized_status not in {"completed", "success", "succeeded"}:
                parts.append(status)
        else:
            if status:
                parts.append(f"{status} · output {len(lines)} line(s)")
            else:
                parts.append(f"output {len(lines)} line(s)")
    elif status:
        parts.append("completed · no output" if status.lower() == "completed" else status)
    elif cwd and len(parts) < 3:
        parts.append(_compact_inline(cwd, max_chars=120))
    return "\n".join(part for part in parts if part) or "[command_execution]"


def _parsed_exploration_text(parsed_cmd: Any) -> str:
    if not isinstance(parsed_cmd, list) or not parsed_cmd:
        return ""

    lines: list[str] = []
    read_names: list[str] = []
    seen_reads: set[str] = set()
    for item in parsed_cmd:
        if not isinstance(item, dict):
            return ""
        item_type = _as_text(item.get("type")).strip().lower()
        if item_type == "read":
            name = _as_text(item.get("name") or item.get("path") or item.get("cmd")).strip()
            if name and name not in seen_reads:
                read_names.append(name)
                seen_reads.add(name)
            continue
        if read_names:
            lines.append("Read " + ", ".join(read_names))
            read_names = []
        if item_type in {"list_files", "list"}:
            target = _as_text(item.get("path") or item.get("cmd")).strip()
            if not target:
                return ""
            lines.append("List " + target)
            continue
        if item_type == "search":
            query = _as_text(item.get("query")).strip()
            path = _as_text(item.get("path")).strip()
            if query and path:
                lines.append(f"Search {query} in {path}")
            elif query:
                lines.append(f"Search {query}")
            else:
                cmd = _as_text(item.get("cmd")).strip()
                if not cmd:
                    return ""
                lines.append(f"Search {cmd}")
            continue
        return ""
    if read_names:
        lines.append("Read " + ", ".join(read_names))
    if not lines:
        return ""
    first, *rest = lines[:8]
    rendered = ["• Explored", f"  └ {first}"]
    rendered.extend(f"    {line}" for line in rest)
    footer = _preview_footer(len(lines), min(len(lines), 8))
    if footer:
        rendered.append(f"    {footer}")
    return "\n".join(rendered)


def _exploration_event(
    *,
    thread_id: str,
    text: str,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="orchestration",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="orchestration",
        tool_name="explored",
    )


def _is_warning_text(text: str, *, phase: str) -> bool:
    """Detect operator-facing warnings without stealing assistant-final semantics.

    Codex can emit "Heads up..." as commentary/status guidance. We treat it as
    a warning only inside commentary phase; assistant message/final phases must
    keep normal turn semantics.
    """
    if phase not in _COMMENTARY_PHASES:
        return False
    return bool(_HEADS_UP_WARNING_RE.match(text.strip()))


def _extract_hook_prompt_text(text: str) -> str | None:
    match = _HOOK_PROMPT_RE.match(text.strip())
    if not match:
        return None
    body = reflow_whitespace(match.group("body"))
    return body or "Codex operator hook prompt"


def _operator_prompt_event(
    *,
    thread_id: str,
    text: str,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    body = _extract_hook_prompt_text(text) or text.strip() or "Codex operator hook prompt"
    return NormalizedEvent(
        thread_id=thread_id,
        text=f"⚠ Operator prompt\n{body}",
        is_complete=True,
        content_type="warning",
        role="system",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="operator_prompt",
    )


def _warning_event(
    *,
    thread_id: str,
    text: str,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    warning_text = text.strip() or "[warning]"
    return NormalizedEvent(
        thread_id=thread_id,
        text=warning_text,
        is_complete=True,
        content_type="warning",
        role="system",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="warning",
    )


def _is_usage_limit_warning_payload(payload: dict[str, Any]) -> bool:
    """Return True when an event_msg payload represents a quota/usage warning."""
    codex_error_info = _as_text(payload.get("codex_error_info")).strip()
    if codex_error_info and _USAGE_LIMIT_WARNING_RE.search(codex_error_info):
        return True
    warning_text = _as_text(
        payload.get("message") or payload.get("text") or payload.get("warning")
    ).strip()
    if not warning_text:
        return False
    return bool(_USAGE_LIMIT_WARNING_RE.search(warning_text))


def _thread_id_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "thread_id", "threadId", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for key in ("id", "thread_id", "threadId"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _turn_key_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("turn_id", "turnId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for key in ("turn_id", "turnId"):
            value = meta.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _timestamp(data: dict[str, Any]) -> str | None:
    timestamp = data.get("timestamp")
    return timestamp if isinstance(timestamp, str) and timestamp else None


def _lifecycle_event(
    *,
    thread_id: str,
    event_name: str,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    return NormalizedEvent(
        thread_id=thread_id,
        text="",
        is_complete=True,
        content_type="lifecycle",
        role="system",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="lifecycle",
        tool_name=event_name,
    )


def _message_event(
    *,
    thread_id: str,
    role: str,
    phase: str | None,
    text: str,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    if role == "user":
        if _extract_hook_prompt_text(text) is not None:
            return _operator_prompt_event(
                thread_id=thread_id,
                text=text,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        return NormalizedEvent(
            thread_id=thread_id,
            text=text,
            is_complete=True,
            content_type="text",
            role="user",
            timestamp=timestamp,
            runtime_kind=runtime_kind,
            event_kind="user_message",
        )

    if role != "assistant":
        return _lifecycle_event(
            thread_id=thread_id,
            event_name="message",
            timestamp=timestamp,
            runtime_kind=runtime_kind,
        )
    if _is_warning_text(text, phase=phase):
        return _warning_event(
            thread_id=thread_id,
            text=text,
            timestamp=timestamp,
            runtime_kind=runtime_kind,
        )

    event_kind = "commentary" if phase in _COMMENTARY_PHASES else "assistant_message"
    content_type = "commentary" if event_kind == "commentary" else "text"
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type=content_type,
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind=event_kind,
    )


def _suppress_history_delivery(event: NormalizedEvent) -> NormalizedEvent:
    """Keep the event in taxonomy, but suppress Telegram/history delivery.

    Codex rollout emits some message content twice:
      - lightweight ``event_msg`` records for live UI/status surfaces
      - canonical ``response_item.message`` records for persisted turn history

    We preserve the normalized event for semantic inspection, but only the
    canonical ``response_item`` version should reach Telegram/history.
    """
    event.dispatch_to_telegram = False
    event.include_in_history = False
    event.status_message_eligible = False
    return event


def _reasoning_event(
    *,
    thread_id: str,
    payload: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    summary = payload.get("summary")
    text = _text_from_content(summary) or _as_text(payload.get("text")).strip()
    event = NormalizedEvent(
        thread_id=thread_id,
        text=text or "[reasoning]",
        is_complete=True,
        content_type="reasoning",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="reasoning",
    )
    if not text:
        return _suppress_history_delivery(event)
    return event


def _response_message_base_signature(payload: Any) -> tuple[str, str | None, str] | None:
    if not isinstance(payload, dict):
        return None
    if _as_text(payload.get("type")).strip() != "message":
        return None
    role = _as_text(payload.get("role")).strip() or "assistant"
    phase = _as_text(payload.get("phase")).strip() or None
    text = _text_from_content(payload.get("content")).strip()
    if not text:
        return None
    return (role, phase, text)


def _event_msg_message_base_signature(payload: Any) -> tuple[str, str | None, str] | None:
    if not isinstance(payload, dict):
        return None
    payload_type = _as_text(payload.get("type")).strip()
    if payload_type == "user_message":
        text = _as_text(payload.get("message") or payload.get("text")).strip()
        if text:
            return ("user", None, text)
        return None
    if payload_type == "agent_message":
        text = _as_text(payload.get("message") or payload.get("text")).strip()
        phase = _as_text(payload.get("phase")).strip() or None
        if text and phase in {"final_answer", "commentary"}:
            return ("assistant", phase, text)
    return None


def _should_suppress_event_msg_duplicate(
    entry: dict[str, Any],
    all_entries: list[dict[str, Any]],
) -> bool:
    if _as_text(entry.get("type")).strip() != "event_msg":
        return False
    signature = _event_msg_message_base_signature(entry.get("payload"))
    if signature is None:
        return False
    for candidate in all_entries:
        if _as_text(candidate.get("type")).strip() != "response_item":
            continue
        if _response_message_base_signature(candidate.get("payload")) == signature:
            return True
    return False


def _command_execution_event(
    *,
    thread_id: str,
    payload: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    command = _as_text(
        payload.get("command")
        or payload.get("cmd")
        or payload.get("raw_command")
        or payload.get("shell_command")
    ).strip()
    cwd = _as_text(payload.get("cwd")).strip()
    output = _text_from_content(
        payload.get("aggregated_output")
        or payload.get("output")
        or payload.get("stdout")
        or payload.get("stderr")
        or payload.get("output_text")
        or payload.get("output_preview")
        or payload.get("preview")
        or payload.get("command_output")
        or payload.get("result")
        or payload.get("response")
    )
    status = _as_text(payload.get("status")).strip()
    text = _command_execution_summary(
        command=command,
        cwd=cwd,
        status=status,
        output=output,
    )
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="command_execution",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="command_execution",
        tool_name="command_execution",
        tool_use_id=_as_text(payload.get("call_id") or payload.get("id")) or None,
    )


def _file_change_text(changes: Any) -> str:
    if isinstance(changes, dict):
        lines: list[str] = []
        entries = list(changes.items())
        for path, change in entries[:5]:
            kind = ""
            if isinstance(change, dict):
                kind = _as_text(change.get("kind") or change.get("type") or change.get("change_kind")).strip()
            if kind and path:
                lines.append(f"{kind} {path}")
            elif path:
                lines.append(path)
        if len(entries) > 5:
            lines.append(f"+{len(entries) - 5} more files")
        return "\n".join(lines).strip()
    if not isinstance(changes, list):
        return _text_from_content(changes)
    lines: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            text = _as_text(change).strip()
            if text:
                lines.append(text)
            continue
        kind = _as_text(change.get("kind") or change.get("change_kind")).strip()
        path = _as_text(change.get("path") or change.get("file_path")).strip()
        if kind and path:
            lines.append(f"{kind} {path}")
        elif path:
            lines.append(path)
        elif kind:
            lines.append(kind)
    return "\n".join(lines).strip()


def _file_change_event(
    *,
    thread_id: str,
    payload: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    changes = payload.get("changes")
    text = _file_change_text(changes)
    status = _as_text(payload.get("status")).strip()
    if status and text:
        text = f"{status}\n{text}"
    elif status:
        text = status
    if not text:
        text = "[file_change]"
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="file_change",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="file_change",
        tool_name="file_change",
        tool_use_id=_as_text(payload.get("call_id") or payload.get("id")) or None,
    )


def _tool_call_event(
    *,
    thread_id: str,
    payload: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    name = _as_text(payload.get("name") or payload.get("tool") or "tool").strip()
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input")
    if _tool_name_is(name, "exec_command"):
        command, cwd = _exec_invocation(arguments)
        return NormalizedEvent(
            thread_id=thread_id,
            text=_command_execution_summary(
                command=command,
                cwd=cwd,
                status="",
                output="",
            ),
            is_complete=True,
            content_type="command_execution",
            role="assistant",
            timestamp=timestamp,
            runtime_kind=runtime_kind,
            event_kind="command_execution",
            tool_name=name,
            tool_use_id=_as_text(payload.get("call_id") or payload.get("id")) or None,
        )
    text = _tool_call_summary(name, arguments)
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="tool_use",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="tool_call",
        tool_name=name,
        tool_use_id=_as_text(payload.get("call_id") or payload.get("id")) or None,
    )


def _tool_output_event(
    *,
    thread_id: str,
    payload: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
    command: str = "",
    cwd: str = "",
) -> NormalizedEvent:
    call_id = _as_text(payload.get("call_id") or payload.get("id") or "").strip()
    tool_name = _as_text(payload.get("name") or payload.get("tool")).strip() or None
    content = (
        payload.get("content")
        or payload.get("output")
        or payload.get("result")
        or payload.get("data")
    )
    raw_text = _text_from_content(content)
    wrapper_command = _exec_command_from_wrapper(raw_text)
    is_command_like_output = _tool_name_is(
        tool_name,
        "exec_command",
    ) or (tool_name is None and _exec_output_wrapper_seen(raw_text))
    if is_command_like_output:
        output, wrapper_status = _exec_output_from_wrapper(raw_text)
        status = _as_text(payload.get("status")).strip() or wrapper_status
        return NormalizedEvent(
            thread_id=thread_id,
            text=_command_execution_summary(
                command=command or wrapper_command,
                cwd=cwd,
                status=status,
                output=output,
            ),
            is_complete=True,
            content_type="command_execution",
            role="assistant",
            timestamp=timestamp,
            runtime_kind=runtime_kind,
            event_kind="command_execution",
            tool_name=tool_name or "exec_command",
            tool_use_id=call_id or None,
        )
    text = _tool_output_summary(tool_name, raw_text)
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="tool_result",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="tool_output",
        tool_name=tool_name,
        tool_use_id=call_id or None,
    )


def _generated_image_media_type(raw_bytes: bytes) -> str | None:
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if (
        len(raw_bytes) >= 12
        and raw_bytes.startswith(b"RIFF")
        and raw_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"
    return None


def _media_type_matches_signature(media_type: str, raw_bytes: bytes) -> bool:
    detected = _generated_image_media_type(raw_bytes)
    return detected == media_type


def _decode_base64_image_bytes(
    *,
    media_type: str,
    encoded: str,
) -> tuple[str, bytes] | None:
    normalized_media_type = media_type.strip().lower()
    if normalized_media_type not in {"image/png", "image/jpeg", "image/webp"}:
        return None
    payload = "".join(encoded.split())
    if not payload:
        return None
    max_encoded = ((_GENERATED_IMAGE_MAX_BYTES + 2) // 3) * 4 + 16
    if len(payload) > max_encoded:
        return None
    try:
        raw_bytes = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw_bytes or len(raw_bytes) > _GENERATED_IMAGE_MAX_BYTES:
        return None
    if not _media_type_matches_signature(normalized_media_type, raw_bytes):
        return None
    return normalized_media_type, raw_bytes


def _decode_data_url_image(data_url: str) -> tuple[str, bytes] | None:
    match = _VIEWED_IMAGE_DATA_URL_RE.match(data_url.strip())
    if not match:
        return None
    return _decode_base64_image_bytes(
        media_type=match.group("media"),
        encoded=match.group("data"),
    )


def _decode_viewed_image_payload(content: Any) -> tuple[str, bytes] | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        block_type = _as_text(item.get("type")).strip()
        if block_type == "input_image":
            image_url = _as_text(item.get("image_url")).strip()
            decoded = _decode_data_url_image(image_url)
            if decoded is not None:
                return decoded
        if block_type == "image":
            source = item.get("source")
            if not isinstance(source, dict):
                continue
            if _as_text(source.get("type")).strip() != "base64":
                continue
            decoded = _decode_base64_image_bytes(
                media_type=_as_text(source.get("media_type")).strip() or "image/png",
                encoded=_as_text(source.get("data")).strip(),
            )
            if decoded is not None:
                return decoded
    return None


def _decode_generated_image_payload(payload: dict[str, Any]) -> tuple[str, bytes] | None:
    encoded = _as_text(payload.get("result")).strip()
    if not encoded:
        return None
    max_encoded = ((_GENERATED_IMAGE_MAX_BYTES + 2) // 3) * 4 + 16
    if len(encoded) > max_encoded:
        return None
    try:
        raw_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw_bytes or len(raw_bytes) > _GENERATED_IMAGE_MAX_BYTES:
        return None
    media_type = _generated_image_media_type(raw_bytes)
    if media_type is None:
        return None
    return media_type, raw_bytes


def _saved_image_file_url_from_path(
    raw_path: str,
    *,
    thread_id: str,
    call_id: str,
) -> str:
    raw_path = raw_path.strip()
    if not raw_path or "\x00" in raw_path:
        return ""
    if raw_path.startswith("file://"):
        parsed = urlparse(raw_path)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return ""
        raw_path = unquote(parsed.path)
    path = Path(raw_path)
    if not path.is_absolute():
        return ""
    suffix = path.suffix.lower()
    if suffix not in _GENERATED_IMAGE_SUFFIXES:
        return ""
    if call_id and not path.name.startswith(call_id):
        return ""
    parts = set(path.parts)
    if "generated_images" not in parts:
        return ""
    if thread_id and thread_id not in parts:
        return ""
    try:
        return path.resolve(strict=False).as_uri()
    except (OSError, ValueError):
        return ""


def _replay_saved_image_url(
    payload: dict[str, Any],
    *,
    thread_id: str,
    call_id: str,
) -> str:
    for key in ("saved_path", "path", "output_path"):
        saved_url = _saved_image_file_url_from_path(
            _as_text(payload.get(key)),
            thread_id=thread_id,
            call_id=call_id,
        )
        if saved_url:
            return saved_url
    return ""


def _codex_generated_image_url(thread_id: str, call_id: str) -> str:
    if not thread_id or not call_id:
        return ""
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    image_path = codex_home / "generated_images" / thread_id / f"{call_id}.png"
    if not image_path.is_file():
        return ""
    try:
        with image_path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return ""
    if _generated_image_media_type(header) is None:
        return ""
    try:
        return image_path.as_uri()
    except ValueError:
        return ""


def _generated_image_fields(revised_prompt: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_key = ""
    for raw_line in revised_prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _IMAGE_GENERATION_FIELD_RE.match(line)
        if match:
            current_key = match.group(1).strip().lower()
            value = match.group(2).strip()
            if current_key and value:
                fields[current_key] = value
            continue
        if current_key and line:
            fields[current_key] = " ".join(
                part for part in (fields.get(current_key, ""), line) if part
            )
    return fields


def _generated_image_rendered_text(
    *,
    revised_prompt: str,
    saved_url: str,
) -> str:
    lines = ["• Generated Image:"]
    for raw_line in revised_prompt.splitlines():
        line = raw_line.strip()
        if line:
            lines.append(f"  └ {line}")
    if saved_url:
        lines.append(f"  └ Saved to: {saved_url}")
    return "\n".join(lines)


def _generated_image_caption(
    *,
    revised_prompt: str,
    saved_url: str,
) -> str:
    fields = _generated_image_fields(revised_prompt)
    lines = ["🖼 Generated Image"]
    for label, key, limit in (
        ("Use case", "use case", 120),
        ("Asset", "asset type", 160),
        ("Request", "primary request", 360),
    ):
        value = fields.get(key, "").strip()
        if value:
            lines.append(f"{label}: {_compact_inline(value, max_chars=limit)}")
    if saved_url:
        basename = Path(saved_url).name
        if basename:
            lines.append(f"File: {_compact_inline(basename, max_chars=120)}")
    return _clip_text("\n".join(lines), max_chars=900)


def _sanitized_viewed_image_display_path(path_text: str) -> str:
    """Return non-sensitive viewed-image provenance for Telegram display.

    The viewed-image MVP deliberately does not read local paths for media bytes.
    The path is only provenance, so keep the default display to a basename and
    avoid leaking raw absolute workspace paths such as ``/home/...``.
    """
    raw = path_text.strip().strip("`'\"")
    if not raw:
        return ""
    parsed = urlparse(raw)
    candidate = unquote(parsed.path) if parsed.scheme == "file" else raw
    name = Path(candidate).name
    return _compact_inline(name or "viewed image", max_chars=120)


def _viewed_image_rendered_text(view: _ViewImageCall | None) -> str:
    lines = ["• Viewed Image:"]
    display_path = _sanitized_viewed_image_display_path(view.path if view else "")
    if display_path:
        lines.append(f"  └ {display_path}")
    return "\n".join(lines)


def _viewed_image_caption(view: _ViewImageCall | None) -> str:
    lines = ["🖼 Viewed Image"]
    display_path = _sanitized_viewed_image_display_path(view.path if view else "")
    if display_path:
        lines.append(f"File: {display_path}")
    if view and view.detail:
        lines.append(f"Detail: {_compact_inline(view.detail, max_chars=80)}")
    return _clip_text("\n".join(lines), max_chars=900)


def _viewed_image_output_event(
    *,
    thread_id: str,
    payload: dict[str, Any],
    view: _ViewImageCall | None,
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent:
    call_id = _as_text(payload.get("call_id") or payload.get("id") or "").strip()
    content = (
        payload.get("content")
        or payload.get("output")
        or payload.get("result")
        or payload.get("data")
    )
    image_payload = _decode_viewed_image_payload(content)
    if image_payload is None:
        return NormalizedEvent(
            thread_id=thread_id,
            text=_viewed_image_rendered_text(view),
            is_complete=True,
            content_type="tool_result",
            role="assistant",
            timestamp=timestamp,
            runtime_kind=runtime_kind,
            event_kind="tool_output",
            tool_name="view_image",
            tool_use_id=call_id or None,
        )
    media_type, raw_bytes = image_payload
    return NormalizedEvent(
        thread_id=thread_id,
        text=_viewed_image_rendered_text(view),
        is_complete=True,
        content_type=VIEWED_IMAGE_PREVIEW_CONTENT_TYPE,
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="tool_output",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        include_in_history=True,
        dispatch_to_telegram=True,
        status_message_eligible=False,
        image_data=[(media_type, raw_bytes)],
        image_caption=_viewed_image_caption(view),
        tool_name="view_image",
        tool_use_id=call_id or None,
    )


def _generated_image_event(
    *,
    thread_id: str,
    payload: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> NormalizedEvent | None:
    call_id = _as_text(payload.get("call_id") or payload.get("id")).strip()
    revised_prompt = _as_text(
        payload.get("revised_prompt")
        or payload.get("prompt")
        or payload.get("message")
        or payload.get("text")
    ).strip()
    saved_url = _replay_saved_image_url(
        payload,
        thread_id=thread_id,
        call_id=call_id,
    )
    text = _generated_image_rendered_text(
        revised_prompt=revised_prompt,
        saved_url=saved_url,
    )
    image_payload = _decode_generated_image_payload(payload)
    if image_payload is None:
        if not revised_prompt and not saved_url:
            return None
        return NormalizedEvent(
            thread_id=thread_id,
            text=text,
            is_complete=True,
            content_type="tool_result",
            role="assistant",
            timestamp=timestamp,
            runtime_kind=runtime_kind,
            event_kind="tool_output",
            tool_name="image_gen.imagegen",
            tool_use_id=call_id or None,
        )
    if not saved_url:
        saved_url = _codex_generated_image_url(thread_id, call_id)
        text = _generated_image_rendered_text(
            revised_prompt=revised_prompt,
            saved_url=saved_url,
        )
    media_type, raw_bytes = image_payload
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type=GENERATED_IMAGE_PREVIEW_CONTENT_TYPE,
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="tool_output",
        semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
        include_in_history=True,
        dispatch_to_telegram=True,
        status_message_eligible=False,
        image_data=[(media_type, raw_bytes)],
        image_caption=_generated_image_caption(
            revised_prompt=revised_prompt,
            saved_url=saved_url,
        ),
        tool_name="image_gen.imagegen",
        tool_use_id=call_id or None,
    )


def _normalize_thread_item(
    *,
    thread_id: str,
    item: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> list[NormalizedEvent]:
    item_type = _as_text(item.get("type")).strip()
    if not item_type:
        return []

    normalized_type = item_type.lower()
    if normalized_type in {"usermessage", "user_message"}:
        content = _text_from_content(item.get("content"))
        return [
            NormalizedEvent(
                thread_id=thread_id,
                text=content,
                is_complete=True,
                content_type="text",
                role="user",
                timestamp=timestamp,
                runtime_kind=runtime_kind,
                event_kind="user_message",
            )
        ]

    if normalized_type in {"agentmessage", "agent_message"}:
        phase = _as_text(item.get("phase")).strip()
        text = _as_text(item.get("text")).strip()
        return [
            _message_event(
                thread_id=thread_id,
                role="assistant",
                phase=phase or None,
                text=text,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if normalized_type == "reasoning":
        text = _text_from_content(item.get("summary")) or _text_from_content(
            item.get("content")
        )
        if not text:
            text = _as_text(item.get("text")).strip()
        if not text:
            text = "[reasoning]"
        return [
            NormalizedEvent(
                thread_id=thread_id,
                text=text,
                is_complete=True,
                content_type="reasoning",
                role="assistant",
                timestamp=timestamp,
                runtime_kind=runtime_kind,
                event_kind="reasoning",
            )
        ]

    if normalized_type == "commandexecution":
        return [
            _command_execution_event(
                thread_id=thread_id,
                payload=item,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if normalized_type == "filechange":
        return [
            _file_change_event(
                thread_id=thread_id,
                payload=item,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if normalized_type == "mcptoolcall":
        status = _as_text(item.get("status")).strip().lower()
        if status in {"completed", "failed"} or item.get("result") is not None:
            return [
                _tool_output_event(
                    thread_id=thread_id,
                    payload={
                        **item,
                        "call_id": item.get("id"),
                        "name": item.get("tool"),
                        "content": item.get("result") or item.get("output"),
                    },
                    timestamp=timestamp,
                    runtime_kind=runtime_kind,
                )
            ]
        return [
            _tool_call_event(
                thread_id=thread_id,
                payload={
                    **item,
                    "call_id": item.get("id"),
                    "name": item.get("tool"),
                    "arguments": item.get("arguments"),
                },
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if normalized_type in {"enteredreviewmode", "exitedreviewmode", "contextcompaction"}:
        return [
            _lifecycle_event(
                thread_id=thread_id,
                event_name=item_type,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    return []


def _normalize_event_msg_payload(
    *,
    thread_id: str,
    payload: dict[str, Any],
    timestamp: str | None,
    runtime_kind: str = "codex",
) -> list[NormalizedEvent]:
    payload_type = _as_text(payload.get("type")).strip()
    if not payload_type:
        return []

    if payload_type == "agent_message":
        phase = _as_text(payload.get("phase")).strip() or "commentary"
        event = _message_event(
            thread_id=thread_id,
            role="assistant",
            phase=phase,
            text=_as_text(payload.get("message") or payload.get("text")).strip(),
            timestamp=timestamp,
            runtime_kind=runtime_kind,
        )
        return [event]

    if payload_type == "user_message":
        event = _message_event(
            thread_id=thread_id,
            role="user",
            phase=None,
            text=_as_text(payload.get("message") or payload.get("text")).strip(),
            timestamp=timestamp,
            runtime_kind=runtime_kind,
        )
        return [event]

    if payload_type in {"warning", "heads_up"}:
        warning_text = _as_text(
            payload.get("message") or payload.get("text") or payload.get("warning")
        ).strip()
        return [
            _warning_event(
                thread_id=thread_id,
                text=warning_text,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type == "error" and _is_usage_limit_warning_payload(payload):
        warning_text = _as_text(
            payload.get("message") or payload.get("text") or payload.get("warning")
        ).strip()
        return [
            _warning_event(
                thread_id=thread_id,
                text=warning_text,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type in {"turn_started", "turn_completed", "turn_aborted"}:
        return [
            _lifecycle_event(
                thread_id=thread_id,
                event_name=payload_type,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type in {"agent_reasoning", "agent_reasoning_raw_content"}:
        return [
            _reasoning_event(
                thread_id=thread_id,
                payload=payload,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type == "image_generation_end":
        event = _generated_image_event(
            thread_id=thread_id,
            payload=payload,
            timestamp=timestamp,
            runtime_kind=runtime_kind,
        )
        if event is not None:
            return [event]

    if payload_type in {"patch_apply_begin", "patch_apply_end"}:
        if payload_type.endswith("_begin"):
            return [
                _lifecycle_event(
                    thread_id=thread_id,
                    event_name=payload_type,
                    timestamp=timestamp,
                    runtime_kind=runtime_kind,
                )
            ]
        return [
            _file_change_event(
                thread_id=thread_id,
                payload=payload,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type in {
        "exec_command_begin",
        "exec_command_output_delta",
        "exec_command_end",
    }:
        if payload_type != "exec_command_end":
            return [
                _lifecycle_event(
                    thread_id=thread_id,
                    event_name=payload_type,
                    timestamp=timestamp,
                    runtime_kind=runtime_kind,
                )
            ]
        explored_text = _parsed_exploration_text(payload.get("parsed_cmd"))
        if explored_text:
            return [
                _exploration_event(
                    thread_id=thread_id,
                    text=explored_text,
                    timestamp=timestamp,
                    runtime_kind=runtime_kind,
                )
            ]
        return [
            _command_execution_event(
                thread_id=thread_id,
                payload=payload,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type in {"mcp_tool_call_begin", "mcp_tool_call_end"}:
        if payload_type != "mcp_tool_call_end":
            return [
                _lifecycle_event(
                    thread_id=thread_id,
                    event_name=payload_type,
                    timestamp=timestamp,
                    runtime_kind=runtime_kind,
                )
            ]
        status = _as_text(payload.get("status")).strip().lower()
        if status in {"failed", "error"}:
            content = payload.get("error") or payload.get("result")
        else:
            content = payload.get("result")
        if content is not None:
            return [
                _tool_output_event(
                    thread_id=thread_id,
                    payload={
                        "call_id": payload.get("call_id"),
                        "name": _as_text(
                            payload.get("invocation", {}).get("tool")
                            if isinstance(payload.get("invocation"), dict)
                            else payload.get("tool")
                        ).strip(),
                        "content": content if status not in {"failed", "error"} else _text_from_content(content),
                    },
                    timestamp=timestamp,
                    runtime_kind=runtime_kind,
                )
            ]
        return [
            _tool_call_event(
                thread_id=thread_id,
                payload=payload,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type == "item_started":
        item = payload.get("item")
        if isinstance(item, dict):
            return _normalize_thread_item(
                thread_id=thread_id,
                item=item,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        return [
            _lifecycle_event(
                thread_id=thread_id,
                event_name=payload_type,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    if payload_type in {"item_completed", "item_updated"}:
        item = payload.get("item")
        if isinstance(item, dict):
            return _normalize_thread_item(
                thread_id=thread_id,
                item=item,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        return [
            _lifecycle_event(
                thread_id=thread_id,
                event_name=payload_type,
                timestamp=timestamp,
                runtime_kind=runtime_kind,
            )
        ]

    return [
        _lifecycle_event(
            thread_id=thread_id,
            event_name=payload_type,
            timestamp=timestamp,
            runtime_kind=runtime_kind,
        )
    ]


class CodexRolloutNormalizer:
    """Normalize Codex rollout JSONL records into :class:`NormalizedEvent` values."""

    @staticmethod
    def is_codex_rollout_record(data: dict[str, Any]) -> bool:
        return _as_text(data.get("type")).strip() in CODEX_ROLLOUT_TYPES

    @classmethod
    def normalize_records(
        cls,
        entries: Iterable[dict[str, Any]],
        *,
        thread_id: str | None = None,
        state: CodexRolloutState | None = None,
    ) -> list[NormalizedEvent]:
        entry_list = list(entries)
        current_thread_id = thread_id or ""
        result: list[NormalizedEvent] = []
        incremental_mode = state is not None
        state = state or CodexRolloutState()
        current_time = _now_seconds()
        for data in entry_list:
            if not isinstance(data, dict):
                continue
            payload = data.get("payload")
            timestamp = _timestamp(data)
            record_type = _as_text(data.get("type")).strip()
            if not current_thread_id:
                current_thread_id = _thread_id_from_payload(payload) or current_thread_id
            if record_type == "session_meta":
                current_thread_id = _thread_id_from_payload(payload) or current_thread_id
            if record_type == "turn_context":
                flushed_before_turn = _activate_real_turn_key(
                    state,
                    turn_key=_turn_key_from_payload(payload),
                )
                if flushed_before_turn:
                    result.extend(flushed_before_turn)
            events = cls.normalize_record(
                data,
                thread_id=current_thread_id,
            )
            if isinstance(payload, dict) and record_type == "response_item":
                response_type = _as_text(payload.get("type")).strip()
                if response_type == "function_call":
                    tool_name = _as_text(payload.get("name")).strip()
                    call_id = _as_text(payload.get("call_id")).strip()
                    arguments = _structured_json(payload.get("arguments") or payload.get("input"))
                    if call_id and tool_name:
                        state.tool_names_by_call_id[call_id] = tool_name
                    if call_id and _tool_name_is(tool_name, "exec_command"):
                        command, cwd = _exec_invocation(arguments)
                        state.exec_commands_by_call_id[call_id] = (command, cwd)
                    if (
                        call_id
                        and _tool_name_is(tool_name, "view_image")
                        and isinstance(arguments, dict)
                    ):
                        state.view_images_by_call_id[call_id] = _ViewImageCall(
                            path=_as_text(arguments.get("path")).strip(),
                            detail=_as_text(arguments.get("detail")).strip(),
                        )
                    if tool_name == "update_plan":
                        events = [
                            _plan_update_event(
                                thread_id=current_thread_id,
                                arguments=payload.get("arguments"),
                                timestamp=timestamp,
                            )
                        ]
                    elif tool_name == "spawn_agent":
                        if call_id and isinstance(arguments, dict):
                            state.pending_spawns[call_id] = _SpawnCall(
                                role=_as_text(arguments.get("agent_type")).strip(),
                                model=_as_text(arguments.get("model")).strip(),
                                reasoning_effort=_as_text(arguments.get("reasoning_effort")).strip(),
                                prompt=_spawn_prompt(arguments),
                            )
                        events = [_suppress_history_delivery(event) for event in events]
                    elif tool_name == "wait_agent":
                        if call_id and isinstance(arguments, dict):
                            targets = [
                                _as_text(target).strip()
                                for target in (arguments.get("targets") or [])
                                if _as_text(target).strip()
                            ]
                            generations: dict[str, int] = {}
                            for target in targets:
                                generations[target] = _next_wait_generation(state, target)
                            state.pending_waits[call_id] = _PendingWait(
                                target_ids=targets,
                                generations=generations,
                            )
                            if targets and call_id not in state.active_waits:
                                state.active_waits.add(call_id)
                                events = [_suppress_history_delivery(event) for event in events]
                                events.append(
                                    _orchestration_event(
                                        thread_id=current_thread_id,
                                        text=_waiting_for_targets_text(
                                            targets, state.agents_by_id
                                        ),
                                        timestamp=timestamp,
                                    )
                                )
                            else:
                                events = [_suppress_history_delivery(event) for event in events]
                elif response_type == "function_call_output":
                    call_id = _as_text(payload.get("call_id")).strip()
                    remembered_tool_name = state.tool_names_by_call_id.pop(call_id, "")
                    command, cwd = state.exec_commands_by_call_id.pop(call_id, ("", ""))
                    view_image_call = state.view_images_by_call_id.pop(call_id, None)
                    if _tool_name_is(
                        remembered_tool_name, "update_plan"
                    ) and not _tool_output_indicates_failure(payload):
                        events = [
                            _suppress_history_delivery(
                                _tool_output_event(
                                    thread_id=current_thread_id,
                                    payload={**payload, "name": remembered_tool_name},
                                    timestamp=timestamp,
                                )
                            )
                        ]
                    elif _tool_name_is(remembered_tool_name, "view_image"):
                        events = [
                            _viewed_image_output_event(
                                thread_id=current_thread_id,
                                payload={**payload, "name": remembered_tool_name},
                                view=view_image_call,
                                timestamp=timestamp,
                            )
                        ]
                    elif remembered_tool_name:
                        events = [
                            _tool_output_event(
                                thread_id=current_thread_id,
                                payload={**payload, "name": remembered_tool_name},
                                timestamp=timestamp,
                                command=command,
                                cwd=cwd,
                            )
                        ]
                    raw_output = payload.get("output")
                    parsed_output = _structured_json(raw_output)
                    spawn_request = state.pending_spawns.pop(call_id, None)
                    pending_wait = state.pending_waits.pop(call_id, None)
                    if (
                        isinstance(parsed_output, dict)
                        and spawn_request
                        and "agent_id" in parsed_output
                    ):
                        agent_id = _as_text(parsed_output.get("agent_id")).strip()
                        if agent_id:
                            descriptor = _AgentDescriptor(
                                agent_id=agent_id,
                                nickname=_as_text(parsed_output.get("nickname")).strip(),
                                role=spawn_request.role,
                                model=spawn_request.model,
                                reasoning_effort=spawn_request.reasoning_effort,
                                prompt=spawn_request.prompt,
                            )
                            state.agents_by_id[agent_id] = descriptor
                            events = [_suppress_history_delivery(event) for event in events]
                            events.append(
                                _orchestration_event(
                                    thread_id=current_thread_id,
                                    text=_spawned_agent_text(descriptor),
                                    timestamp=timestamp,
                                )
                            )
                    elif spawn_request is not None:
                        events = [_suppress_history_delivery(event) for event in events]
                        events.append(
                            _orchestration_event(
                                thread_id=current_thread_id,
                                text=_spawn_failed_text(spawn_request, parsed_output),
                                timestamp=timestamp,
                            )
                        )

                    if pending_wait:
                        targets = pending_wait.target_ids
                        if isinstance(parsed_output, dict):
                            statuses = parsed_output.get("status")
                            timed_out = bool(parsed_output.get("timed_out"))
                            emitted_finished = False
                            if call_id in state.active_waits:
                                state.active_waits.discard(call_id)
                                events = [_suppress_history_delivery(event) for event in events]
                                events.append(
                                    _orchestration_event(
                                        thread_id=current_thread_id,
                                        text=_finished_waiting_text(
                                            targets, state.agents_by_id
                                        ),
                                        timestamp=timestamp,
                                    )
                                )
                                emitted_finished = True
                            elif statuses or timed_out:
                                events = [_suppress_history_delivery(event) for event in events]
                            if isinstance(statuses, dict) and statuses:
                                for agent_id, status in statuses.items():
                                    if _status_seen_in_generation(
                                        state,
                                        agent_id=agent_id,
                                        status=status,
                                        generation=pending_wait.generations.get(agent_id),
                                    ):
                                        continue
                                    events.append(
                                        _orchestration_event(
                                            thread_id=current_thread_id,
                                            text=_agent_status_text(
                                                agent_id=agent_id,
                                                status=status,
                                                agents_by_id=state.agents_by_id,
                                            ),
                                            timestamp=timestamp,
                                        )
                                    )
                                if timed_out:
                                    events.append(
                                        _orchestration_event(
                                            thread_id=current_thread_id,
                                            text=_wait_timeout_text(
                                                targets, state.agents_by_id
                                            ),
                                            timestamp=timestamp,
                                        )
                                    )
                            elif timed_out:
                                events.append(
                                    _orchestration_event(
                                        thread_id=current_thread_id,
                                        text=_wait_timeout_text(
                                            targets, state.agents_by_id
                                            ),
                                            timestamp=timestamp,
                                        )
                                    )
                            elif not emitted_finished:
                                state.active_waits.discard(call_id)
                                events = [_suppress_history_delivery(event) for event in events]
                elif response_type == "message" and _as_text(payload.get("role")).strip() == "user":
                    text = _text_from_content(payload.get("content")).strip()
                    notification = _parse_subagent_notification(text)
                    if notification is not None:
                        events = [_suppress_history_delivery(event) for event in events]
                        agent_id = _as_text(notification.get("agent_path")).strip()
                        status = notification.get("status")
                        if agent_id:
                            if not _status_seen_in_generation(
                                state,
                                agent_id=agent_id,
                                status=status,
                            ):
                                events.append(
                                    _orchestration_event(
                                        thread_id=current_thread_id,
                                        text=_agent_status_text(
                                            agent_id=agent_id,
                                            status=status,
                                            agents_by_id=state.agents_by_id,
                                        ),
                                        timestamp=timestamp,
                                    )
                                )
                        result.extend(events)
                        continue
            base_message_signature: tuple[str, str | None, str] | None = None
            if record_type == "response_item":
                base_message_signature = _response_message_base_signature(payload)
            elif record_type == "event_msg":
                base_message_signature = _event_msg_message_base_signature(payload)

            if (
                base_message_signature is not None
                and base_message_signature[0] == "user"
            ):
                turn_key = _active_turn_key(state)
                flushed_before_turn: list[NormalizedEvent] = []
                if record_type == "event_msg":
                    existing_signature = _message_turn_signature(
                        turn_key,
                        base_message_signature,
                    )
                    if existing_signature in state.canonical_message_signatures:
                        result.extend(
                            _suppress_history_delivery(event) for event in events
                        )
                        continue
                    if not state.current_turn_key or state.active_turn_user_opened:
                        turn_key, flushed_before_turn = _open_surrogate_turn(state)
                    user_signature = _message_turn_signature(
                        turn_key,
                        base_message_signature,
                    )
                    state.recent_user_event_messages.setdefault(
                        user_signature,
                        [],
                    ).append(current_time)
                    state.active_turn_user_opened = True
                    if flushed_before_turn:
                        result.extend(flushed_before_turn)
                    result.extend(events)
                    continue

                if not state.current_turn_key:
                    turn_key, flushed_before_turn = _open_surrogate_turn(state)
                user_signature = _message_turn_signature(
                    turn_key,
                    base_message_signature,
                )
                duplicate = _consume_recent_user_event_duplicate(
                    state,
                    user_signature,
                )
                if state.active_turn_user_opened and not duplicate:
                    turn_key, flushed_before_turn = _open_surrogate_turn(state)
                    user_signature = _message_turn_signature(
                        turn_key,
                        base_message_signature,
                    )
                elif duplicate:
                    state.canonical_message_signatures.add(user_signature)
                    state.active_turn_user_opened = True
                    if flushed_before_turn:
                        result.extend(flushed_before_turn)
                    continue
                state.canonical_message_signatures.add(user_signature)
                state.active_turn_user_opened = True
                if flushed_before_turn:
                    result.extend(flushed_before_turn)
                result.extend(events)
                continue

            message_signature: tuple[str, str, str | None, str] | None = None
            if base_message_signature is not None:
                message_signature = _message_turn_signature(
                    _active_turn_key(state),
                    base_message_signature,
                )

            if message_signature is not None and record_type == "response_item":
                suppressed = _drain_pending_event_messages(state, message_signature)
                if suppressed:
                    result.extend(suppressed)
                state.canonical_message_signatures.add(message_signature)
            elif message_signature is not None and record_type == "event_msg":
                if message_signature in state.canonical_message_signatures:
                    result.extend(
                        _suppress_history_delivery(event) for event in events
                    )
                    continue
                state.pending_event_messages.append(
                    _PendingMessageEvent(
                        signature=message_signature,
                        timestamp_seconds=current_time,
                        events=events,
                    )
                )
                continue
            if not incremental_mode and state.pending_event_messages:
                result.extend(_flush_all_pending_event_messages(state))
            result.extend(events)
        # In incremental monitor mode, keep lightweight event_msg duplicates
        # buffered until an idle poll. Flushing them on an unrelated non-idle
        # poll can still let a later canonical response_item.message duplicate
        # land within the same turn, so only idle polls or explicit turn
        # boundaries may flush the active-turn fallback queue.
        if not incremental_mode:
            flushed = _flush_all_pending_event_messages(state)
        elif not entry_list:
            flushed = _flush_pending_event_messages(
                state,
                current_time=current_time,
            )
        else:
            flushed = []
        if flushed:
            return result + flushed
        return result

    @classmethod
    def normalize_record(
        cls,
        data: dict[str, Any],
        *,
        thread_id: str | None = None,
    ) -> list[NormalizedEvent]:
        record_type = _as_text(data.get("type")).strip()
        if record_type not in CODEX_ROLLOUT_TYPES:
            return []

        payload = data.get("payload")
        timestamp = _timestamp(data)
        active_thread_id = thread_id or _thread_id_from_payload(payload)
        if record_type == "session_meta":
            return [
                _lifecycle_event(
                    thread_id=active_thread_id,
                    event_name="session_meta",
                    timestamp=timestamp,
                )
            ]
        if record_type == "turn_context":
            return [
                _lifecycle_event(
                    thread_id=active_thread_id,
                    event_name="turn_context",
                    timestamp=timestamp,
                )
            ]
        if not isinstance(payload, dict):
            return [
                _lifecycle_event(
                    thread_id=active_thread_id,
                    event_name=record_type,
                    timestamp=timestamp,
                )
            ]

        if record_type == "response_item":
            response_type = _as_text(payload.get("type")).strip()
            if response_type == "message":
                return [
                    _message_event(
                        thread_id=active_thread_id,
                        role=_as_text(payload.get("role")).strip() or "assistant",
                        phase=_as_text(payload.get("phase")).strip() or None,
                        text=_text_from_content(payload.get("content")),
                        timestamp=timestamp,
                    )
                ]
            if response_type == "reasoning":
                return [
                    _reasoning_event(
                        thread_id=active_thread_id,
                        payload=payload,
                        timestamp=timestamp,
                    )
                ]
            if response_type == "function_call":
                if _as_text(payload.get("name")).strip() == "update_plan":
                    return [
                        _plan_update_event(
                            thread_id=active_thread_id,
                            arguments=payload.get("arguments"),
                            timestamp=timestamp,
                        )
                    ]
                return [
                    _tool_call_event(
                        thread_id=active_thread_id,
                        payload=payload,
                        timestamp=timestamp,
                    )
                ]
            if response_type == "function_call_output":
                return [
                    _tool_output_event(
                        thread_id=active_thread_id,
                        payload=payload,
                        timestamp=timestamp,
                    )
                ]
            if response_type == "file_change":
                return [
                    _file_change_event(
                        thread_id=active_thread_id,
                        payload=payload,
                        timestamp=timestamp,
                    )
                ]
            if response_type in {"command_execution", "commandExecution"}:
                return [
                    _command_execution_event(
                        thread_id=active_thread_id,
                        payload=payload,
                        timestamp=timestamp,
                    )
                ]
            return [
                _lifecycle_event(
                    thread_id=active_thread_id,
                    event_name=response_type or "response_item",
                    timestamp=timestamp,
                )
            ]

        if record_type == "event_msg":
            return _normalize_event_msg_payload(
                thread_id=active_thread_id,
                payload=payload,
                timestamp=timestamp,
            )

        return [
            _lifecycle_event(
                thread_id=active_thread_id,
                event_name=record_type,
                timestamp=timestamp,
            )
        ]
