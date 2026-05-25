"""Telegram delivery audit log for human-surface self-improvement.

The audit records what ccbot attempted to deliver to Telegram after semantic
normalization. It is intentionally compact: raw payloads are not stored; only a
short preview and metadata needed to compare Telegram delivery against the
Codex/tmux human surface are persisted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import config

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _preview(text: str, *, max_chars: int = 240) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


_SENSITIVE_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot[redacted]"),
    (
        re.compile(
            r"(?i)\b(token|access_token|api_key|authorization|password|passwd|secret)="
            r"([^\s&]+)"
        ),
        r"\1=[redacted]",
    ),
    (
        re.compile(r"(?i)\b(raw_?payload|payload|request_body|body)=([^\s]+)"),
        r"\1=[redacted]",
    ),
    (re.compile(r"://([^:\s/@]+):([^@\s/]+)@"), r"://\1:[redacted]@"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+=*"), "Bearer [redacted]"),
)


def _sanitize_error_text(error: object) -> str:
    """Return a compact diagnostic string without credential/payload-shaped data."""
    text = str(error)
    for pattern, replacement in _SENSITIVE_ERROR_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _retry_after_value(error: object) -> float | None:
    retry_after = getattr(error, "retry_after", None)
    if retry_after is None:
        return None
    if isinstance(retry_after, int | float):
        return float(retry_after)
    total_seconds = getattr(retry_after, "total_seconds", None)
    if callable(total_seconds):
        return float(total_seconds())
    return None


def _transport_error_type(error: object, explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if error is None:
        return None
    name = error.__class__.__name__.lower()
    if "retryafter" in name or _retry_after_value(error) is not None:
        return "retry_after"
    if "timedout" in name or "timeout" in name:
        return "timeout"
    if "network" in name:
        return "network_error"
    if "badrequest" in name:
        return "bad_request"
    if "telegram" in name:
        return "telegram_error"
    return "exception"


def log_telegram_delivery(
    *,
    action: str,
    user_id: int | None = None,
    chat_id: int | None = None,
    thread_id: int | None = None,
    message_id: int | None = None,
    window_id: str | None = None,
    task_type: str | None = None,
    content_type: str | None = None,
    semantic_kind: str | None = None,
    text: str = "",
    success: bool = True,
    error: str | Exception | None = None,
    transport_error_type: str | None = None,
    error_class: str | None = None,
    retry_after: float | None = None,
    queue_age_ms: int | None = None,
    depth_at_enqueue: int | None = None,
    depth_at_send: int | None = None,
    task_class: str | None = None,
    backpressure_reason: str | None = None,
    reason: str | None = None,
    turn_generation: int | None = None,
    tool_use_id: str | None = None,
    part_index: int | None = None,
    part_count: int | None = None,
    render_mode: str | None = None,
    media: dict[str, Any] | None = None,
) -> None:
    """Append a single Telegram delivery audit row.

    Logging failures must never affect message delivery.
    """
    try:
        path: Path = config.telegram_delivery_audit_file
        path.parent.mkdir(parents=True, exist_ok=True)
        row: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": _utc_now(),
            "action": action,
            "success": success,
            "user_id": user_id,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "window_id": window_id,
            "task_type": task_type,
            "content_type": content_type,
            "semantic_kind": semantic_kind,
            "text_len": len(text or ""),
            "text_sha16": _sha(text or ""),
            "preview": _preview(text or ""),
        }
        optional: dict[str, Any] = {
            "reason": reason,
            "turn_generation": turn_generation,
            "tool_use_id": tool_use_id,
            "part_index": part_index,
            "part_count": part_count,
            "render_mode": render_mode,
            "transport_error_type": _transport_error_type(
                error,
                transport_error_type,
            ),
            "error_class": error_class
            if error_class is not None
            else (error.__class__.__name__ if isinstance(error, Exception) else None),
            "retry_after": retry_after
            if retry_after is not None
            else _retry_after_value(error),
            "queue_age_ms": queue_age_ms,
            "depth_at_enqueue": depth_at_enqueue,
            "depth_at_send": depth_at_send,
            "task_class": task_class,
            "backpressure_reason": backpressure_reason,
        }
        row.update({key: value for key, value in optional.items() if value is not None})
        if error:
            row["error"] = _preview(_sanitize_error_text(error), max_chars=180)
        if media:
            row["media"] = media
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - best-effort observability only
        logger.debug("Failed to write Telegram delivery audit: %s", exc)
