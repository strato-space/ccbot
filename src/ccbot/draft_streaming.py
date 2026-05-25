"""Optional Telegram ``sendMessageDraft`` transport previews.

Draft previews are transient Telegram transport signals.  They are never
runtime replay proof and never replace durable compact artifacts/final answers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from .config import config
from .delivery_audit import log_telegram_delivery
from .markdown_v2 import convert_markdown
from .transcript_parser import TranscriptParser

PARSE_MODE = "MarkdownV2"


def _strip_sentinels(text: str) -> str:
    for sentinel in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(sentinel, "")
    return text

logger = logging.getLogger(__name__)

DraftPreviewStatus = Literal[
    "disabled",
    "unsafe",
    "surface_not_allowed",
    "unsupported",
    "cooldown",
    "debounced",
    "closed",
    "sent",
    "failed",
]

DRAFT_PREVIEW_SEMANTIC_KIND = "telegram_draft_preview"
DRAFT_PREVIEW_CONTENT_TYPE = "draft_preview"
_CAPABILITIES_FILENAME = "draft_preview_capabilities.json"
_MAX_DRAFT_TEXT_CHARS = 4096
_PARSE_ERROR_MARKERS = (
    "can't parse entities",
    "can't find end of the entity",
    "bad markdown",
    "unsupported start tag",
)
_UNSUPPORTED_MARKERS = (
    "method is not available",
    "method not found",
    "not implemented",
    "can't use this method",
    "sendmessagedraft",
)
_SECRET_MARKERS_RE = re.compile(
    r"(?i)(?:telegram_bot_token|telegram_token|openai_api_key|api[_-]?key|"
    r"authorization:\s*bearer|x-api-key|sk-[A-Za-z0-9_-]{16,}|bot\d+:[A-Za-z0-9_-]+)"
)
_INTERNAL_MARKERS_RE = re.compile(
    r"(?is)(<\s*(?:skill|hook_prompt|system|developer|tool_result|tool_call)\b|"
    r"<!--\s*OMX:|\btool_call_id\b|\binternal payload\b)"
)
_RAW_CONTROL_RE = re.compile(
    r"(?is)^(?:\s*(?:↳\s*)?Tool Output\b|\s*Chunk ID:\s*|\s*Wall time:\s*|"
    r"\s*Original token count:\s*|\s*Process (?:exited|running)\b|\s*\{\s*\"(?:type|role|tool|messages?)\")"
)
_LOCAL_SECRET_PATH_RE = re.compile(
    r"(?i)(?:file://|/(?:data|home|root)/[^\s`<>]*(?:\.env|token|secret|credential|key)[^\s`<>]*)"
)
_REASONING_ONLY_RE = re.compile(r"(?is)^\s*\[?(?:reasoning|thinking|analysis)\]?\s*[:.]*\s*$")


@dataclass(frozen=True)
class DraftPreviewResult:
    status: DraftPreviewStatus
    surface_key: str
    draft_id: int | None = None
    reason: str | None = None
    sent: bool = False


@dataclass
class _RuntimeState:
    last_sent_at: dict[tuple[str, int, str], float]
    cooldown_until: dict[str, float]
    closed: set[tuple[str, int, str]]
    pending_text: dict[tuple[str, int, str], str]
    dropped_since_audit: dict[tuple[str, str], int]


_state = _RuntimeState(
    last_sent_at={},
    cooldown_until={},
    closed=set(),
    pending_text={},
    dropped_since_audit={},
)


def clear_draft_preview_state() -> None:
    """Reset in-memory draft state for tests/restarts."""
    _state.last_sent_at.clear()
    _state.cooldown_until.clear()
    _state.closed.clear()
    _state.pending_text.clear()
    _state.dropped_since_audit.clear()


def _canonical_surface_key(*, chat_id: int, thread_id: int | None) -> str:
    if thread_id is not None:
        return f"t:{chat_id}:{thread_id}"
    return f"c:{chat_id}"


def _surface_key(*, chat_id: int, thread_id: int | None, surface_key: str | None) -> str:
    return _canonical_surface_key(chat_id=chat_id, thread_id=thread_id)


def _surface_key_mismatch(
    *,
    chat_id: int,
    thread_id: int | None,
    surface_key: str | None,
) -> bool:
    provided = (surface_key or "").strip()
    if not provided:
        return False
    return provided != _canonical_surface_key(chat_id=chat_id, thread_id=thread_id)


def draft_id_for(
    *,
    chat_id: int,
    thread_id: int | None,
    turn_generation: int,
    lane: str,
) -> int:
    """Return a stable non-zero Telegram draft id for one surface/generation/lane."""
    seed = f"{chat_id}:{thread_id or 0}:{turn_generation}:{lane}".encode()
    value = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
    return value % 2_147_483_647 + 1


def _capabilities_path() -> Path:
    return config.config_dir / _CAPABILITIES_FILENAME


def _load_capabilities() -> dict[str, dict[str, Any]]:
    path = _capabilities_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - best-effort state only
        logger.debug("Failed to load draft preview capabilities: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_capabilities(data: dict[str, dict[str, Any]]) -> None:
    try:
        path = _capabilities_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - delivery must not depend on state I/O
        logger.debug("Failed to write draft preview capabilities: %s", exc)


def mark_draft_surface_supported(surface_key: str, *, clear_safe: bool = False) -> None:
    data = _load_capabilities()
    data[surface_key] = {
        "status": "supported",
        "clear_safe": bool(clear_safe),
        "updated_at": time.time(),
    }
    _write_capabilities(data)


def mark_draft_surface_unsupported(surface_key: str, *, reason: str = "unsupported") -> None:
    data = _load_capabilities()
    data[surface_key] = {
        "status": "unsupported",
        "reason": reason,
        "updated_at": time.time(),
    }
    _write_capabilities(data)


def _surface_capability(surface_key: str) -> dict[str, Any]:
    return _load_capabilities().get(surface_key, {})


def _mode() -> str:
    mode = str(getattr(config, "telegram_draft_preview_mode", "off") or "off").lower()
    return mode if mode in {"off", "probe", "on"} else "off"


def _allowed_surfaces() -> set[str]:
    return set(getattr(config, "telegram_draft_preview_allowed_surfaces", set()) or set())


def _min_interval_seconds() -> float:
    return float(getattr(config, "telegram_draft_preview_min_interval_seconds", 1.5) or 1.5)


def _retry_cooldown_seconds() -> float:
    return float(getattr(config, "telegram_draft_preview_retry_cooldown_seconds", 30) or 30)


def _timeout_cooldown_seconds() -> float:
    return float(getattr(config, "telegram_draft_preview_timeout_cooldown_seconds", 10) or 10)


def is_draft_text_safe_to_show(text: str) -> bool:
    """Return False for payloads that must never become a Telegram draft."""
    body = (text or "").strip()
    if not body:
        return False
    if _REASONING_ONLY_RE.match(body):
        return False
    if _INTERNAL_MARKERS_RE.search(body):
        return False
    if _SECRET_MARKERS_RE.search(body):
        return False
    if _RAW_CONTROL_RE.search(body):
        return False
    if _LOCAL_SECRET_PATH_RE.search(body):
        return False
    return True


def _audit(
    *,
    status: DraftPreviewStatus,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    surface: str,
    window_id: str | None,
    text: str,
    turn_generation: int,
    lane: str,
    draft_id: int | None = None,
    reason: str | None = None,
    success: bool = True,
    error: str | None = None,
    source_content_type: str | None = None,
    source_semantic_kind: str | None = None,
) -> None:
    if status not in {"sent", "failed", "unsupported"}:
        key = (surface, status)
        dropped = _state.dropped_since_audit.get(key, 0) + 1
        _state.dropped_since_audit[key] = dropped
        if dropped not in {1, 10, 100} and dropped % 100 != 0:
            return
    log_telegram_delivery(
        action="draft_preview" if status == "sent" else "draft_preview_suppress",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        task_type="telegram_transport",
        content_type=DRAFT_PREVIEW_CONTENT_TYPE,
        semantic_kind=DRAFT_PREVIEW_SEMANTIC_KIND,
        text=text,
        success=success,
        error=error,
        reason=reason or status,
        turn_generation=turn_generation,
        render_mode="sendMessageDraft",
        media={
            "draft_id": draft_id,
            "lane": lane,
            "surface_key": surface,
            "result": status,
            "source_content_type": source_content_type,
            "source_semantic_kind": source_semantic_kind,
        },
    )


def _surface_allowed_for_mode(
    *,
    mode: str,
    surface: str,
    chat_id: int,
    thread_id: int | None,
) -> tuple[bool, str | None]:
    if mode == "off":
        return False, "disabled"
    allowed = surface in _allowed_surfaces()
    cap = _surface_capability(surface)
    if cap.get("status") == "unsupported":
        return False, "unsupported"
    is_group_or_topic = thread_id is not None or chat_id < 0
    if mode == "probe":
        return (allowed, None if allowed else "surface_not_allowed")
    if is_group_or_topic:
        if not allowed:
            return False, "surface_not_allowed"
        if cap.get("status") != "supported":
            return False, "surface_not_capability_proven"
    return True, None


def _retry_after_seconds(exc: RetryAfter) -> float:
    retry_after = exc.retry_after
    if isinstance(retry_after, int | float):
        return float(retry_after)
    try:
        return float(retry_after.total_seconds())
    except Exception:
        return _retry_cooldown_seconds()


def _is_parse_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PARSE_ERROR_MARKERS)


def _is_unsupported_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _UNSUPPORTED_MARKERS)


async def maybe_send_draft_preview(
    bot: Bot,
    *,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    surface_key: str | None,
    window_id: str | None,
    text: str,
    turn_generation: int,
    lane: str,
    source_content_type: str | None = None,
    source_semantic_kind: str | None = None,
) -> DraftPreviewResult:
    """Best-effort sendMessageDraft preview with hard fallback-to-drop semantics."""
    surface = _surface_key(chat_id=chat_id, thread_id=thread_id, surface_key=surface_key)
    mode = _mode()
    draft_id = draft_id_for(
        chat_id=chat_id,
        thread_id=thread_id,
        turn_generation=turn_generation,
        lane=lane,
    )
    if mode == "off":
        return DraftPreviewResult("disabled", surface, draft_id, "disabled")
    if not is_draft_text_safe_to_show(text):
        _audit(
            status="unsafe",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text="[redacted unsafe draft payload]",
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            reason="unsafe_to_show",
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )
        return DraftPreviewResult("unsafe", surface, draft_id, "unsafe_to_show")

    if _surface_key_mismatch(chat_id=chat_id, thread_id=thread_id, surface_key=surface_key):
        _audit(
            status="surface_not_allowed",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text=text,
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            reason="surface_key_mismatch",
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )
        return DraftPreviewResult(
            "surface_not_allowed", surface, draft_id, "surface_key_mismatch"
        )

    key = (surface, turn_generation, lane)
    if key in _state.closed:
        _audit(
            status="closed",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text=text,
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            reason="draft_lane_closed",
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )
        return DraftPreviewResult("closed", surface, draft_id, "draft_lane_closed")

    allowed, block_reason = _surface_allowed_for_mode(
        mode=mode,
        surface=surface,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    if not allowed:
        status: DraftPreviewStatus = "unsupported" if block_reason == "unsupported" else "surface_not_allowed"
        _audit(
            status=status,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text=text,
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            reason=block_reason,
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )
        return DraftPreviewResult(status, surface, draft_id, block_reason)

    method = getattr(bot, "send_message_draft", None)
    if not callable(method):
        mark_draft_surface_unsupported(surface, reason="missing_method")
        _audit(
            status="unsupported",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text=text,
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            reason="missing_method",
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )
        return DraftPreviewResult("unsupported", surface, draft_id, "missing_method")

    now = time.monotonic()
    cooldown_until = _state.cooldown_until.get(surface, 0)
    if cooldown_until > now:
        _audit(
            status="cooldown",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text=text,
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            reason="cooldown",
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )
        return DraftPreviewResult("cooldown", surface, draft_id, "cooldown")

    last_sent_at = _state.last_sent_at.get(key)
    min_interval = _min_interval_seconds()
    if last_sent_at is not None and now - last_sent_at < min_interval:
        _state.pending_text[key] = text[-_MAX_DRAFT_TEXT_CHARS:]
        _audit(
            status="debounced",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text=text,
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            reason="debounced_latest_only",
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )
        return DraftPreviewResult("debounced", surface, draft_id, "debounced_latest_only")

    kwargs: dict[str, Any] = {}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    draft_text = text[-_MAX_DRAFT_TEXT_CHARS:]
    try:
        await method(
            chat_id=chat_id,
            draft_id=draft_id,
            text=convert_markdown(draft_text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except BadRequest as exc:
        if _is_parse_error(exc):
            try:
                await method(
                    chat_id=chat_id,
                    draft_id=draft_id,
                    text=_strip_sentinels(draft_text),
                    **kwargs,
                )
            except Exception as retry_exc:  # noqa: BLE001 - best-effort transport
                return _handle_draft_exception(
                    retry_exc,
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    surface=surface,
                    window_id=window_id,
                    text=text,
                    turn_generation=turn_generation,
                    lane=lane,
                    draft_id=draft_id,
                    source_content_type=source_content_type,
                    source_semantic_kind=source_semantic_kind,
                )
        else:
            return _handle_draft_exception(
                exc,
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id,
                surface=surface,
                window_id=window_id,
                text=text,
                turn_generation=turn_generation,
                lane=lane,
                draft_id=draft_id,
                source_content_type=source_content_type,
                source_semantic_kind=source_semantic_kind,
            )
    except Exception as exc:  # noqa: BLE001 - best-effort transport
        return _handle_draft_exception(
            exc,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface=surface,
            window_id=window_id,
            text=text,
            turn_generation=turn_generation,
            lane=lane,
            draft_id=draft_id,
            source_content_type=source_content_type,
            source_semantic_kind=source_semantic_kind,
        )

    _state.last_sent_at[key] = now
    _state.pending_text.pop(key, None)
    _state.dropped_since_audit.pop((surface, "debounced"), None)
    _audit(
        status="sent",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        surface=surface,
        window_id=window_id,
        text=text,
        turn_generation=turn_generation,
        lane=lane,
        draft_id=draft_id,
        reason="sent",
        source_content_type=source_content_type,
        source_semantic_kind=source_semantic_kind,
    )
    return DraftPreviewResult("sent", surface, draft_id, "sent", sent=True)


def _handle_draft_exception(
    exc: Exception,
    *,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    surface: str,
    window_id: str | None,
    text: str,
    turn_generation: int,
    lane: str,
    draft_id: int,
    source_content_type: str | None,
    source_semantic_kind: str | None,
) -> DraftPreviewResult:
    if isinstance(exc, RetryAfter):
        seconds = max(1.0, _retry_after_seconds(exc))
        _state.cooldown_until[surface] = time.monotonic() + seconds
        status: DraftPreviewStatus = "cooldown"
        reason = "retry_after"
    elif isinstance(exc, TimedOut | NetworkError):
        _state.cooldown_until[surface] = time.monotonic() + _timeout_cooldown_seconds()
        status = "failed"
        reason = "transport_timeout"
    elif isinstance(exc, BadRequest) and _is_unsupported_error(exc):
        mark_draft_surface_unsupported(surface, reason="bad_request_unsupported")
        status = "unsupported"
        reason = "bad_request_unsupported"
    else:
        status = "failed"
        reason = type(exc).__name__
    _audit(
        status=status,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        surface=surface,
        window_id=window_id,
        text=text,
        turn_generation=turn_generation,
        lane=lane,
        draft_id=draft_id,
        reason=reason,
        success=False,
        error=str(exc),
        source_content_type=source_content_type,
        source_semantic_kind=source_semantic_kind,
    )
    return DraftPreviewResult(status, surface, draft_id, reason)


def stop_draft_preview_state(
    *,
    chat_id: int,
    thread_id: int | None,
    surface_key: str | None,
    turn_generation: int,
    lane: str,
) -> None:
    """Close a draft lane locally without assuming Telegram empty-text clear is safe."""
    surface = _surface_key(chat_id=chat_id, thread_id=thread_id, surface_key=surface_key)
    key = (surface, turn_generation, lane)
    _state.closed.add(key)
    _state.pending_text.pop(key, None)


async def maybe_clear_verified_draft_preview(
    bot: Bot,
    *,
    user_id: int,
    chat_id: int,
    thread_id: int | None,
    surface_key: str | None,
    window_id: str | None,
    turn_generation: int,
    lane: str,
) -> DraftPreviewResult:
    """Close the local draft lane without sending an unsafe empty draft.

    Telegram empty-text draft semantics are quarantined until a production smoke
    proves they clear rather than create placeholder artifacts.  Closure is
    therefore local/audit-only and relies on Telegram's transient draft expiry.
    """
    del bot
    surface = _surface_key(chat_id=chat_id, thread_id=thread_id, surface_key=surface_key)
    draft_id = draft_id_for(
        chat_id=chat_id,
        thread_id=thread_id,
        turn_generation=turn_generation,
        lane=lane,
    )
    stop_draft_preview_state(
        chat_id=chat_id,
        thread_id=thread_id,
        surface_key=surface_key,
        turn_generation=turn_generation,
        lane=lane,
    )
    _audit(
        status="closed",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        surface=surface,
        window_id=window_id,
        text="",
        turn_generation=turn_generation,
        lane=lane,
        draft_id=draft_id,
        reason="clear_disabled_uses_expiry",
        source_content_type="draft_preview_clear",
        source_semantic_kind=DRAFT_PREVIEW_SEMANTIC_KIND,
    )
    return DraftPreviewResult("closed", surface, draft_id, "clear_disabled_uses_expiry")
