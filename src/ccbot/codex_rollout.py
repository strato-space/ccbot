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
    content = payload.get("content")
    encrypted_content = payload.get("encrypted_content")
    text = _text_from_content(summary) or _text_from_content(content)
    if not text and encrypted_content:
        text = "[reasoning]"
    if not text:
        text = "[reasoning]"
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="reasoning",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="reasoning",
    )


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
    parts = [part for part in (command, cwd, status, output) if part]
    text = "\n".join(parts) if parts else "[command_execution]"
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
    arg_text = _json_fragment(arguments).strip()
    text = f"{name}({arg_text})" if arg_text else name
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
    content = (
        payload.get("content")
        or payload.get("output")
        or payload.get("result")
        or payload.get("data")
    )
    text = _text_from_content(content)
    if not text:
        text = "[tool_output]"
    return NormalizedEvent(
        thread_id=thread_id,
        text=text,
        is_complete=True,
        content_type="tool_result",
        role="assistant",
        timestamp=timestamp,
        runtime_kind=runtime_kind,
        event_kind="tool_output",
        tool_name=_as_text(payload.get("name") or payload.get("tool")).strip() or None,
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
        if phase == "final_answer":
            return [_suppress_history_delivery(event)]
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
        return [_suppress_history_delivery(event)]

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
        current_thread_id = thread_id or ""
        result: list[NormalizedEvent] = []
        for data in entries:
            if not isinstance(data, dict):
                continue
            payload = data.get("payload")
            if not current_thread_id:
                current_thread_id = _thread_id_from_payload(payload) or current_thread_id
            if data.get("type") == "session_meta":
                current_thread_id = _thread_id_from_payload(payload) or current_thread_id
            result.extend(
                cls.normalize_record(
                    data,
                    thread_id=current_thread_id,
                )
            )
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
