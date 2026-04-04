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

import json
import os
import shlex
from dataclasses import dataclass
import re
from typing import Any, Iterable

from .runtime_types import NormalizedEvent

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


def _compact_inline(text: str, *, max_chars: int = 160) -> str:
    text = reflow_whitespace(text).strip()
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


def _command_code_block(command: str, *, max_lines: int = 4, max_chars: int = 140) -> str:
    payload = _extract_shell_payload(command)
    lines = _nonempty_lines(payload)
    if not lines:
        return ""

    clipped = [_compact_inline(line, max_chars=max_chars) for line in lines[:max_lines]]
    if len(lines) > max_lines:
        clipped.append(f"... (+{len(lines) - max_lines} more lines)")
    return "```sh\n" + "\n".join(clipped) + "\n```"


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


def _tool_text_code_block(
    text: str,
    *,
    language: str = "text",
    max_lines: int = 4,
    max_chars: int = 140,
) -> str:
    lines = _nonempty_lines(text)
    if not lines:
        return ""

    clipped = [_compact_inline(line, max_chars=max_chars) for line in lines[:max_lines]]
    if len(lines) > max_lines:
        clipped.append(f"... (+{len(lines) - max_lines} more lines)")
    return f"```{language}\n" + "\n".join(clipped) + "\n```"


def _tool_json_code_block(
    value: Any,
    *,
    max_lines: int = 10,
    max_chars: int = 120,
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
    if len(lines) > max_lines:
        clipped.append(f"... (+{len(lines) - max_lines} more lines)")
    return "```json\n" + "\n".join(clipped) + "\n```"


def _tool_call_summary(name: str, arguments: Any) -> str:
    lowered = name.lower()

    if isinstance(arguments, str):
        text = arguments.strip()
        if lowered == "apply_patch":
            line_count = len(text.splitlines())
            return f"{name}(patch {line_count} lines)"
        json_block = _tool_json_code_block(text)
        if json_block:
            return "\n".join([name, json_block])
        summary = _compact_multiline(text)
        return f"{name}({summary})" if summary else name

    if isinstance(arguments, dict):
        if lowered == "exec_command":
            cmd = _as_text(arguments.get("cmd")).strip()
            workdir = _as_text(arguments.get("workdir")).strip()
            parts = ["exec_command"]
            if cmd:
                command_block = _command_code_block(cmd)
                parts.append(command_block or _compact_multiline(cmd))
            if workdir:
                parts.append(_compact_inline(workdir, max_chars=120))
            return "\n".join(part for part in parts if part)
        if lowered == "write_stdin":
            session_id = _as_text(arguments.get("session_id")).strip()
            chars = _as_text(arguments.get("chars"))
            if chars.strip():
                parts = [f"write_stdin(session {session_id or '?'})"]
                parts.append(
                    _tool_text_code_block(chars, language="text", max_lines=3)
                    or _compact_multiline(chars)
                )
                return "\n".join(part for part in parts if part)
            if session_id:
                return f"write_stdin(session {session_id}, poll)"
            return "write_stdin(poll)"
        if lowered == "apply_patch":
            patch_text = _as_text(arguments.get("patch") or arguments.get("input")).strip()
            if patch_text:
                return f"{name}(patch {len(patch_text.splitlines())} lines)"
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

    lines = _nonempty_lines(text)
    if not lines:
        return "[tool_output]"

    head = lines[0]
    if head.lower().startswith("success. updated the following files:"):
        file_count = max(0, len(lines) - 1)
        return f"Updated {file_count} file(s)"

    lowered = (tool_name or "").lower()
    if lowered == "exec_command":
        return f"completed · output {len(lines)} line(s)"
    if lowered == "write_stdin":
        return f"output {len(lines)} line(s)"

    json_block = _tool_json_code_block(text)
    if json_block:
        return json_block

    if len(lines) == 1 and len(lines[0]) <= 200:
        return lines[0]

    label = tool_name or "Tool output"
    return f"{label}: {len(lines)} line(s)"


def _truncate_preview_lines(
    text: str,
    *,
    max_lines: int = 4,
    max_chars: int = 220,
) -> list[str]:
    lines = _nonempty_lines(text)
    if not lines:
        return []

    clipped = [_compact_inline(line, max_chars=max_chars) for line in lines[:max_lines]]
    if len(lines) > max_lines:
        clipped.append(f"... (+{len(lines) - max_lines} more lines)")
    return clipped


def _prefixed_detail_block(text: str) -> str:
    lines = _truncate_preview_lines(text)
    if not lines:
        return ""
    first, *rest = lines
    result = [f"  └ {first}"]
    result.extend(f"    {line}" for line in rest)
    return "\n".join(result)


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
    if status:
        parts.append(status)
    elif output:
        parts.append("completed")
    lines = _nonempty_lines(output)
    if lines:
        if parts and parts[-1] in {"completed", "failed", "declined", "in_progress"}:
            parts[-1] = f"{parts[-1]} · output {len(lines)} line(s)"
        else:
            parts.append(f"output {len(lines)} line(s)")
    elif cwd and len(parts) < 3:
        parts.append(_compact_inline(cwd, max_chars=120))
    return "\n".join(part for part in parts if part) or "[command_execution]"


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


def _response_message_signature(payload: Any) -> tuple[str, str | None, str] | None:
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


def _event_msg_message_signature(payload: Any) -> tuple[str, str | None, str] | None:
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
    signature = _event_msg_message_signature(entry.get("payload"))
    if signature is None:
        return False
    for candidate in all_entries:
        if _as_text(candidate.get("type")).strip() != "response_item":
            continue
        if _response_message_signature(candidate.get("payload")) == signature:
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
    ) -> list[NormalizedEvent]:
        entry_list = list(entries)
        current_thread_id = thread_id or ""
        result: list[NormalizedEvent] = []
        pending_spawns: dict[str, _SpawnCall] = {}
        pending_waits: dict[str, list[str]] = {}
        active_waits: set[tuple[str, ...]] = set()
        agents_by_id: dict[str, _AgentDescriptor] = {}
        seen_statuses: set[tuple[str, str, str]] = set()
        for data in entry_list:
            if not isinstance(data, dict):
                continue
            payload = data.get("payload")
            if not current_thread_id:
                current_thread_id = _thread_id_from_payload(payload) or current_thread_id
            if data.get("type") == "session_meta":
                current_thread_id = _thread_id_from_payload(payload) or current_thread_id
            events = cls.normalize_record(
                data,
                thread_id=current_thread_id,
            )
            timestamp = _timestamp(data)
            if isinstance(payload, dict) and _as_text(data.get("type")).strip() == "response_item":
                response_type = _as_text(payload.get("type")).strip()
                if response_type == "function_call":
                    tool_name = _as_text(payload.get("name")).strip()
                    call_id = _as_text(payload.get("call_id")).strip()
                    arguments = _structured_json(payload.get("arguments") or payload.get("input"))
                    if tool_name == "spawn_agent":
                        if call_id and isinstance(arguments, dict):
                            pending_spawns[call_id] = _SpawnCall(
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
                            pending_waits[call_id] = targets
                            wait_key = tuple(sorted(targets))
                            if wait_key and wait_key not in active_waits:
                                active_waits.add(wait_key)
                                events = [_suppress_history_delivery(event) for event in events]
                                events.append(
                                    _orchestration_event(
                                        thread_id=current_thread_id,
                                        text=_waiting_for_targets_text(targets, agents_by_id),
                                        timestamp=timestamp,
                                    )
                                )
                            else:
                                events = [_suppress_history_delivery(event) for event in events]
                elif response_type == "function_call_output":
                    call_id = _as_text(payload.get("call_id")).strip()
                    raw_output = payload.get("output")
                    parsed_output = _structured_json(raw_output)
                    spawn_request = pending_spawns.pop(call_id, None)
                    targets = pending_waits.pop(call_id, [])
                    if isinstance(parsed_output, dict) and spawn_request and "agent_id" in parsed_output:
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
                            agents_by_id[agent_id] = descriptor
                            events = [_suppress_history_delivery(event) for event in events]
                            events.append(
                                _orchestration_event(
                                    thread_id=current_thread_id,
                                    text=_spawned_agent_text(descriptor),
                                    timestamp=timestamp,
                                )
                            )
                    elif call_id in pending_spawns or spawn_request is not None:
                        events = [_suppress_history_delivery(event) for event in events]

                    if targets:
                        wait_key = tuple(sorted(targets))
                        if wait_key and isinstance(parsed_output, dict):
                            statuses = parsed_output.get("status")
                            timed_out = bool(parsed_output.get("timed_out"))
                            if isinstance(statuses, dict) and statuses:
                                active_waits.discard(wait_key)
                                events = [_suppress_history_delivery(event) for event in events]
                                for agent_id, status in statuses.items():
                                    signature = _status_signature(agent_id, status)
                                    if signature in seen_statuses:
                                        continue
                                    seen_statuses.add(signature)
                                    events.append(
                                        _orchestration_event(
                                            thread_id=current_thread_id,
                                            text=_agent_status_text(
                                                agent_id=agent_id,
                                                status=status,
                                                agents_by_id=agents_by_id,
                                            ),
                                            timestamp=timestamp,
                                        )
                                    )
                            elif timed_out:
                                events = [_suppress_history_delivery(event) for event in events]
                            elif wait_key:
                                active_waits.discard(wait_key)
                                events = [_suppress_history_delivery(event) for event in events]
                elif response_type == "message" and _as_text(payload.get("role")).strip() == "user":
                    text = _text_from_content(payload.get("content")).strip()
                    notification = _parse_subagent_notification(text)
                    if notification is not None:
                        events = [_suppress_history_delivery(event) for event in events]
                        agent_id = _as_text(notification.get("agent_path")).strip()
                        status = notification.get("status")
                        if agent_id:
                            active_waits = {
                                wait_key
                                for wait_key in active_waits
                                if agent_id not in wait_key
                            }
                            signature = _status_signature(agent_id, status)
                            if signature not in seen_statuses:
                                seen_statuses.add(signature)
                                events.append(
                                    _orchestration_event(
                                        thread_id=current_thread_id,
                                        text=_agent_status_text(
                                            agent_id=agent_id,
                                            status=status,
                                            agents_by_id=agents_by_id,
                                        ),
                                        timestamp=timestamp,
                                    )
                                )
            if _should_suppress_event_msg_duplicate(data, entry_list):
                events = [_suppress_history_delivery(event) for event in events]
            result.extend(events)
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
