"""Per-user message queue management for ordered message delivery.

Provides a queue-based message processing system that ensures:
  - Messages are sent in receive order (FIFO)
  - Status messages always follow content messages
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Rate limiting is handled globally by AIORateLimiter on the Application.

Key components:
  - MessageTask: Dataclass representing a queued message task (with thread_id)
  - get_or_create_queue: Get or create queue and worker for a user
  - Message queue worker: Background task processing user's queue
  - Content task processing with tool_use/tool_result handling
  - Mutable artifact tracking and conversion (keyed by delivery surface)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal, TypedDict, cast
from pathlib import Path

from telegram import Bot, InputMediaPhoto, Message
from telegram.error import BadRequest, RetryAfter

from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..runtime_types import (
    ASSISTANT_FINAL_SEMANTIC_KIND,
    GENERATED_IMAGE_PREVIEW_CONTENT_TYPE,
    IMAGE_PREVIEW_SEMANTIC_KIND,
    TERMINAL_CONTROL_PANEL_CONTENT_TYPE,
    TERMINAL_CONTROL_SEMANTIC_KIND,
    USER_ECHO_SEMANTIC_KIND,
    WARNING_SEMANTIC_KIND,
    is_pre_final_visible_semantic_kind,
)
from ..state_schema import BINDING_STATE_BOUND
from ..terminal_parser import parse_status_line
from ..delivery_audit import log_telegram_delivery
from ..draft_streaming import (
    maybe_send_draft_preview,
    stop_draft_preview_state,
)
from ..config import config
from ..utils import atomic_write_json
from ..tmux_manager import tmux_manager
from ..typing_indicator import send_runtime_update_typing_once
from .message_sender import (
    FallbackDeliveryResult,
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_document,
    send_photo,
    send_with_fallback,
    send_with_fallback_result,
    strip_sentinels,
)

logger = logging.getLogger(__name__)
_ORIGINAL_SEND_WITH_FALLBACK = send_with_fallback
_STATE_CHAT_UNSET = object()
_assistant_final_delivery_failures: dict[tuple[int, int | str, str, int], str] = {}


class _FallbackAuditKwargs(TypedDict, total=False):
    success: bool
    error: str | Exception | None
    render_mode: str | None
    transport_outcome: str
    formatted_error: str | Exception | None
    plain_error: str | Exception | None


def _assistant_final_failure_key(user_id: int, task: MessageTask) -> tuple[int, int | str, str, int]:
    return (
        user_id,
        _topic_state_token(
            task.thread_id or 0,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        ),
        task.window_id or "",
        task.turn_generation,
    )


def pop_assistant_final_delivery_failure(
    user_id: int,
    *,
    thread_id: int | None,
    chat_id: int | None,
    surface_key: str | None,
    window_id: str,
    turn_generation: int,
) -> str | None:
    """Return and clear a terminal delivery failure recorded by the queue."""
    return _assistant_final_delivery_failures.pop(
        (
            user_id,
            _topic_state_token(
                thread_id or 0,
                chat_id=chat_id,
                surface_key=surface_key,
            ),
            window_id,
            turn_generation,
        ),
        None,
    )


def _record_assistant_final_delivery_failure(task: MessageTask, user_id: int, reason: str) -> None:
    if task.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND:
        _assistant_final_delivery_failures[_assistant_final_failure_key(user_id, task)] = reason


def _clear_assistant_final_delivery_failure(task: MessageTask, user_id: int) -> None:
    if task.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND:
        _assistant_final_delivery_failures.pop(_assistant_final_failure_key(user_id, task), None)


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


def _is_message_not_modified_error(exc: Exception) -> bool:
    """Return True for Telegram edit no-op errors.

    Telegram raises BadRequest when an edit would not change the rendered
    message. That is a successful idempotent status update for mutable
    artifacts; treating it as a failed edit causes duplicate status bubbles.
    """
    return "message is not modified" in str(exc).lower()


def _is_message_known_gone_error(exc: Exception) -> bool:
    """Return True when Telegram says the tracked message no longer exists."""
    text = str(exc).lower()
    return "message to delete not found" in text or "message not found" in text


def _retry_after_seconds(exc: RetryAfter) -> int:
    """Normalize PTB RetryAfter payloads to bounded seconds."""
    retry_after = exc.retry_after
    seconds = (
        retry_after
        if isinstance(retry_after, int | float)
        else int(retry_after.total_seconds())
    )
    return max(1, min(seconds, _IMAGE_PREVIEW_DELETE_RETRY_MAX_DELAY_SECONDS))


def _image_preview_retry_key(
    user_id: int,
    thread_id_or_0: int,
    info: ImagePreviewMessageInfo,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> tuple[int, int | str, int]:
    """Return the retry-debt key for one concrete Telegram preview message."""
    return (
        user_id,
        _topic_state_token(
            thread_id_or_0,
            chat_id=chat_id,
            surface_key=surface_key,
        ),
        info.message_id,
    )


def _image_preview_info_matches(
    current: ImagePreviewMessageInfo | None,
    expected: ImagePreviewMessageInfo,
) -> bool:
    """Return True when a tracked preview still points at the expected media."""
    return current == expected


def _discard_image_preview_retry_task(
    key: tuple[int, int, int],
    *,
    cancel: bool,
) -> None:
    """Forget optional retry debt, avoiding self-cancel from inside the retry task."""
    task = _image_preview_delete_retry_tasks.pop(key, None)
    if not cancel or task is None or task.done():
        return
    try:
        current_task = asyncio.current_task()
    except RuntimeError:
        current_task = None
    if task is not current_task:
        task.cancel()


def _discard_image_preview_retry_tasks_for_topic(
    user_id: int,
    thread_id_or_0: int,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
    cancel: bool = True,
) -> None:
    """Forget all retry debt associated with a topic."""
    token = _topic_state_token(
        thread_id_or_0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    stale_keys = [
        key
        for key in _image_preview_delete_retry_tasks
        if key[0] == user_id and key[1] == token
    ]
    for key in stale_keys:
        _discard_image_preview_retry_task(key, cancel=cancel)


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal[
        "content",
        "status_update",
        "status_clear",
        "commentary_update",
        "commentary_clear",
        "pending_input_update",
        "pending_input_clear",
        "ingress_receipt",
        "plan_update",
        "plan_clear",
        "commentary_close",
        "pre_final_close",
    ]
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    semantic_kind: str = ""
    warning_key: str | None = None
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    chat_id: int | None = None
    surface_key: str | None = None
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    image_caption: str | None = None
    document_data: list[tuple[str, str, bytes]] | None = None
    turn_generation: int = 0
    proof_id: str | None = None
    receipt_status: str = "pending"
    enqueued_at: float = field(default_factory=time.monotonic)
    depth_at_enqueue: int = 0
    retry_after_attempts: int = 0
    delivered_part_count: int = 0
    last_message_id: int | None = None
    images_sent: bool = False
    documents_sent: bool = False


# Per-user message queues and worker tasks
_message_queues: dict[int, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[int, asyncio.Task[None]] = {}
_queue_locks: dict[int, asyncio.Lock] = {}  # Protect drain/refill operations
_inflight_task_users: set[int] = set()
_inflight_tasks: dict[int, MessageTask] = {}
_mutable_coalesced_counts: dict[int, int] = {}

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

TopicStateKey = tuple[int, int | str]


# Status message tracking: delivery surface -> (message_id, window_id, last_text)
_status_msg_info: dict[TopicStateKey, tuple[int, str, str]] = {}

StatusHistoryKey = tuple[TopicStateKey, str, int]
STATUS_HISTORY_LIMIT = 8
STATUS_HISTORY_ITEM_MAX_CHARS = 160
STATUS_HISTORY_COMMAND_OUTPUT_MAX_CHARS = 256

# Delivered technical-status history for the compact mutable status artifact.
# This is intentionally not durable runtime history: it is only committed after
# Telegram accepts a send/edit/no-op for the current status bubble.
_technical_status_history: dict[StatusHistoryKey, tuple[str, ...]] = {}


_STATUS_ARTIFACTS_FILENAME = "status_message_artifacts.json"
_BARE_COMMAND_PREVIEW_RE = re.compile(
    r"^(?P<body>.+?)\s+(?P<footer>preview\s+\d+/\d+\s+lines?)$",
    re.IGNORECASE | re.DOTALL,
)
_STATUS_CODE_BLOCK_RE = re.compile(r"```[^\n`]*\n(?P<body>.*?)(?:\n```|```$)", re.DOTALL)


def _split_shell_chain_line(line: str) -> list[str]:
    """Split top-level shell chains so compact command previews stay useful."""
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
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    expanded: list[str] = []
    for line in lines:
        expanded.extend(_split_shell_chain_line(line))
    return expanded


def _status_artifacts_file() -> Path:
    return config.config_dir / _STATUS_ARTIFACTS_FILENAME


def _status_key_to_storage_key(key: TopicStateKey) -> str:
    return json.dumps([key[0], key[1]], ensure_ascii=False, separators=(",", ":"))


def _load_status_artifacts() -> dict[str, dict[str, object]]:
    path = _status_artifacts_file()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - best-effort recovery only
        logger.debug("Failed to load status artifact registry: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_status_artifacts(data: dict[str, dict[str, object]]) -> None:
    try:
        atomic_write_json(_status_artifacts_file(), data)
    except Exception as exc:  # pragma: no cover - delivery must not fail on registry I/O
        logger.debug("Failed to write status artifact registry: %s", exc)


def _persist_status_msg_info(
    key: TopicStateKey,
    info: tuple[int, str, str],
    *,
    content_type: str = "status",
    semantic_kind: str = "technical_status",
) -> None:
    data = _load_status_artifacts()
    data[_status_key_to_storage_key(key)] = {
        "message_id": info[0],
        "window_id": info[1],
        "last_text": info[2],
        "content_type": content_type,
        "semantic_kind": semantic_kind,
        "status_lane": _status_lane_suffix(content_type, semantic_kind) or "technical_status",
        "updated_at": time.time(),
    }
    _write_status_artifacts(data)


def _clear_persisted_status_msg_info(key: TopicStateKey) -> None:
    data = _load_status_artifacts()
    storage_key = _status_key_to_storage_key(key)
    if storage_key in data:
        data.pop(storage_key, None)
        _write_status_artifacts(data)


def _status_artifact_metadata(key: TopicStateKey) -> tuple[str, str]:
    raw = _load_status_artifacts().get(_status_key_to_storage_key(key))
    if isinstance(raw, dict):
        content_type = str(raw.get("content_type") or "status")
        semantic_kind = str(raw.get("semantic_kind") or "technical_status")
        return content_type, semantic_kind
    if isinstance(key[1], str) and "|status:" in key[1]:
        _prefix, _sep, suffix = key[1].partition("|status:")
        content_type, sep, semantic_kind = suffix.partition(":")
        if sep and content_type and semantic_kind:
            return content_type, semantic_kind
    return "status", "technical_status"


def _hydrate_status_msg_info(key: TopicStateKey, window_id: str) -> tuple[int, str, str] | None:
    raw = _load_status_artifacts().get(_status_key_to_storage_key(key))
    if not isinstance(raw, dict):
        return None
    try:
        message_id = int(raw.get("message_id"))
    except (TypeError, ValueError):
        _clear_persisted_status_msg_info(key)
        return None
    stored_window = str(raw.get("window_id") or "")
    if stored_window != window_id:
        return None
    last_text = str(raw.get("last_text") or "")
    info = (message_id, stored_window, last_text)
    _status_msg_info[key] = info
    return info

# Commentary message tracking: delivery surface -> (message_id, window_id, last_text)
_commentary_msg_info: dict[TopicStateKey, tuple[int, str, str]] = {}
# Extra commentary message ids for multi-part commentary artifacts.
_commentary_extra_msg_ids: dict[TopicStateKey, tuple[int, ...]] = {}

# Pending input preview tracking: delivery surface -> (message_id, window_id, last_text)
_pending_input_msg_info: dict[TopicStateKey, tuple[int, str, str]] = {}
# Last enqueued pending-input state per topic for queue-side dedupe.
_pending_input_enqueued: dict[TopicStateKey, tuple[str, str | None]] = {}

# Telegram ingress receipt tracking:
#   (user_id, delivery_surface_token, proof_id) -> (message_id, window_id, last_text)
_ingress_receipt_msg_info: dict[tuple[int, int | str, str], tuple[int, str, str]] = {}
_ingress_receipt_superseded: set[tuple[int, int | str, str]] = set()

# Plan update artifact tracking: delivery surface -> (message_id, window_id, last_text)
_plan_update_msg_info: dict[TopicStateKey, tuple[int, str, str]] = {}


@dataclass(frozen=True)
class ImagePreviewMessageInfo:
    """Tracked mutable image-preview progress bubble for one topic turn."""

    message_id: int
    window_id: str
    turn_generation: int
    media_signature: str
    caption_signature: str


# Image-preview progress tracking: delivery surface -> mutable media info.
# Runtime image previews are pre-final progress, not durable final results, so compact
# mode keeps only the latest preview bubble for the live topic turn.
_image_preview_msg_info: dict[TopicStateKey, ImagePreviewMessageInfo] = {}
# Retry debt for failed Telegram deletes of preview bubbles. Keyed by concrete
# message id so an old preview cleanup retry cannot erase a newer preview pointer.
_image_preview_delete_retry_tasks: dict[tuple[int, int | str, int], asyncio.Task[None]] = {}
_IMAGE_PREVIEW_DELETE_RETRY_MAX_ATTEMPTS = 3
_IMAGE_PREVIEW_DELETE_RETRY_MAX_DELAY_SECONDS = 10

# Warning tracking:
#   (user_id, delivery_surface_token, warning_key) -> (message_id, window_id, warning_text, repeat_count)
# Ordinary warnings keep the default "latest-warning" key so the durable
# latest-warning bubble semantics remain unchanged. Special families such as
# runtime discontinuities can opt into distinct keys to avoid collapsing
# separate events that happen to share the same text.
_warning_msg_info: dict[tuple[int, int | str, str], tuple[int, str, str, int]] = {}


def _session_has_method(name: str) -> bool:
    return callable(getattr(type(session_manager), name, None))


def _task_chat_id(user_id: int, task: MessageTask) -> int:
    if task.chat_id is not None:
        return task.chat_id
    return session_manager.resolve_chat_id(user_id, task.thread_id)


def _topic_state_token(
    thread_id_or_0: int,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> int | str:
    """Return a stable mutable-artifact namespace for one delivery surface.

    Telegram topic ids are scoped by chat, not global.  A bare ``thread_id``
    key therefore conflates unrelated topics across chats.  When a queue task
    carries the canonical surface key or the resolved chat id, key mutable
    bubbles and lane state by that surface.  The legacy token preserves old
    no-surface call sites/tests until their callers are upgraded.
    """
    if surface_key:
        return f"surface:{surface_key}"
    if chat_id is not None:
        if thread_id_or_0:
            return f"topic:{chat_id}:{thread_id_or_0}"
        return f"chat:{chat_id}"
    return thread_id_or_0


def _topic_state_key(
    user_id: int,
    thread_id_or_0: int,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> TopicStateKey:
    return (
        user_id,
        _topic_state_token(
            thread_id_or_0,
            chat_id=chat_id,
            surface_key=surface_key,
        ),
    )


def _task_state_key(user_id: int, task: MessageTask) -> TopicStateKey:
    return _topic_state_key(
        user_id,
        task.thread_id or 0,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )


def _status_lane_suffix(content_type: str, semantic_kind: str) -> str | None:
    """Return a non-default mutable status lane suffix.

    The historical technical status bubble keeps the legacy topic key for
    compatibility.  Special status panels (for example OMX workflow progress)
    need their own tracked Telegram message so command/tool status edits cannot
    overwrite them.
    """
    normalized_content = content_type or "status"
    normalized_semantic = semantic_kind or "technical_status"
    if normalized_content in {"status", "text"} and normalized_semantic == "technical_status":
        return None
    if normalized_content == TERMINAL_CONTROL_PANEL_CONTENT_TYPE or normalized_semantic == TERMINAL_CONTROL_SEMANTIC_KIND:
        return None
    return f"status:{normalized_content}:{normalized_semantic}"


def _status_tracking_key(
    base_key: TopicStateKey,
    *,
    content_type: str = "status",
    semantic_kind: str = "technical_status",
) -> TopicStateKey:
    suffix = _status_lane_suffix(content_type, semantic_kind)
    if suffix is None:
        return base_key
    return (base_key[0], f"{base_key[1]}|{suffix}")


def _status_lane_keys_for_surface(base_key: TopicStateKey) -> list[TopicStateKey]:
    prefix = f"{base_key[1]}|status:"
    keys = [base_key]
    keys.extend(
        key
        for key in list(_status_msg_info)
        if key[0] == base_key[0] and isinstance(key[1], str) and key[1].startswith(prefix)
    )
    data = _load_status_artifacts()
    for storage_key in data:
        try:
            raw = json.loads(storage_key)
        except Exception:
            continue
        if not isinstance(raw, list) or len(raw) != 2:
            continue
        candidate = (raw[0], raw[1])
        if (
            candidate[0] == base_key[0]
            and isinstance(candidate[1], str)
            and candidate[1].startswith(prefix)
            and candidate not in keys
        ):
            keys.append(candidate)
    return keys


def _status_history_key(
    skey: TopicStateKey,
    window_id: str,
    turn_generation: int,
) -> StatusHistoryKey:
    return (skey, window_id, int(turn_generation or 0))


def _status_history_surface_matches(skey: TopicStateKey, base_key: TopicStateKey) -> bool:
    if skey == base_key:
        return True
    if skey[0] != base_key[0]:
        return False
    return (
        isinstance(skey[1], str)
        and str(skey[1]).startswith(f"{base_key[1]}|status:")
    )


def _clear_status_history_for_key(
    skey: TopicStateKey,
    *,
    window_id: str | None = None,
) -> None:
    for history_key in list(_technical_status_history):
        if history_key[0] == skey and (window_id is None or history_key[1] == window_id):
            _technical_status_history.pop(history_key, None)


def _clear_status_history_for_surface(base_key: TopicStateKey) -> None:
    for history_key in list(_technical_status_history):
        if _status_history_surface_matches(history_key[0], base_key):
            _technical_status_history.pop(history_key, None)


def _queue_depth_for_user(user_id: int) -> int | None:
    queue = _message_queues.get(user_id)
    return queue.qsize() if queue is not None else None


def _task_matches_surface(
    task: MessageTask,
    thread_id_or_0: int,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> bool:
    if (task.thread_id or 0) != thread_id_or_0:
        return False
    if surface_key is not None:
        return task.surface_key == surface_key
    if chat_id is not None:
        return task.chat_id == chat_id
    return True


def _warning_state_key(
    user_id: int,
    thread_id_or_0: int,
    warning_key: str,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> tuple[int, int | str, str]:
    return (
        user_id,
        _topic_state_token(
            thread_id_or_0,
            chat_id=chat_id,
            surface_key=surface_key,
        ),
        warning_key,
    )


# Latest visible pre-final artifact kind per delivery surface. This keeps latest-only
# commentary chronologically correct relative to durable orchestration milestones:
# commentary may be edited in place only while it is still the latest visible
# pre-final artifact. Once another visible pre-final artifact lands after it,
# commentary updates must re-emit at the tail instead of rewriting history above it.
_latest_pre_final_visible_kind: dict[TopicStateKey, str] = {}

# Pre-final visible artifact closure: once a final assistant bubble lands in
# compact mode, no later visible pre-final artifact may surface below it until
# the next user turn reopens the lane.
_pre_final_visible_closed: set[TopicStateKey] = set()

# Technical status closure: once a final assistant bubble lands in compact mode,
# mutable status/progress artifacts must not reappear below that terminal turn
# artifact until the next user turn reopens the status lane.
_technical_status_closed: set[TopicStateKey] = set()

# Current per-topic turn generation. Incremented whenever a new user turn opens
# the terminal surface. Queue tasks carry the generation they belong to so that
# stale closes and stale pre-final/status artifacts cannot leak across turns.
_turn_generations: dict[TopicStateKey, int] = {}

# Flood control: user_id -> monotonic time when ban expires
_flood_until: dict[int, float] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10
MESSAGE_TASK_RETRY_AFTER_MAX_ATTEMPTS = 3
_TECHNICAL_CHURN_TASK_TYPES = {
    "status_update",
    "status_clear",
    "pending_input_update",
    "pending_input_clear",
    "commentary_clear",
    "plan_clear",
}
_TAIL_REEMIT_COMMENTARY_RE = re.compile(
    r"\b(?:both\s+)?reviewer\s+lanes\b"
    r"|\breview\s+lanes\b"
    r"|оба\s+reviewer\s+lanes"
    r"|жду\s+до\s+\d+\s+минут"
    r"|не\s+обрыва\w+"
    r"|\bwaiting\s+(?:up\s+to\s+)?\d+\s+(?:minutes?|mins?)\b",
    re.IGNORECASE,
)


def _should_reemit_commentary_at_tail(text: str) -> bool:
    """Return True when editing an old commentary bubble would look invisible."""
    return bool(_TAIL_REEMIT_COMMENTARY_RE.search(text or ""))


def _clear_warning_tracking_for_topic(
    user_id: int,
    thread_id_or_0: int,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Clear warning dedupe state for a topic."""
    token = _topic_state_token(
        thread_id_or_0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    stale_keys = [
        key
        for key in _warning_msg_info
        if key[0] == user_id and key[1] == token
    ]
    for key in stale_keys:
        _warning_msg_info.pop(key, None)


def _is_stale_turn_generation(
    task_generation: int,
    current_generation: int,
) -> bool:
    """Return True when a queued artifact belongs to an older turn."""
    return task_generation != current_generation


def get_message_queue(user_id: int) -> asyncio.Queue[MessageTask] | None:
    """Get the message queue for a user (if exists)."""
    return _message_queues.get(user_id)


def get_or_create_queue(bot: Bot, user_id: int) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a user."""
    if user_id not in _message_queues:
        _message_queues[user_id] = asyncio.Queue()
        _queue_locks[user_id] = asyncio.Lock()
        # Start worker task for this user
        _queue_workers[user_id] = asyncio.create_task(
            _message_queue_worker(bot, user_id)
        )
    return _message_queues[user_id]


def is_user_delivery_backlog_active(user_id: int) -> bool:
    """Return True when Telegram delivery is queued, in-flight, or flood-delayed."""
    queue = _message_queues.get(user_id)
    if queue is not None and not queue.empty():
        return True
    if user_id in _inflight_task_users:
        return True
    return _flood_until.get(user_id, 0) > time.monotonic()


def get_telegram_delivery_backlog_metrics(user_id: int | None = None) -> list[dict]:
    """Return payload-free per-user Telegram delivery backlog metrics."""
    now = time.monotonic()
    user_ids = (
        {user_id}
        if user_id is not None
        else set(_message_queues) | set(_inflight_tasks) | set(_flood_until)
    )
    snapshots: list[dict] = []
    for uid in sorted(user_ids):
        queue = _message_queues.get(uid)
        items = list(queue._queue) if queue is not None else []  # noqa: SLF001
        mutable_count = sum(
            1 for item in items if _mutable_coalesce_lane(item) is not None
        )
        durable_count = sum(
            1
            for item in items
            if item.task_type in {"content", "ingress_receipt"}
        )
        oldest_enqueued_at = min((item.enqueued_at for item in items), default=None)
        inflight = _inflight_tasks.get(uid)
        flood_remaining = max(0.0, _flood_until.get(uid, 0.0) - now)
        snapshots.append(
            {
                "user_id": uid,
                "queue_depth": len(items),
                "in_flight": inflight is not None,
                "in_flight_task_type": inflight.task_type if inflight else None,
                "in_flight_task_class": (
                    _retry_after_task_class(inflight) if inflight else None
                ),
                "oldest_queued_age_seconds": (
                    max(0.0, now - oldest_enqueued_at)
                    if oldest_enqueued_at is not None
                    else 0.0
                ),
                "mutable_count": mutable_count,
                "durable_count": durable_count,
                "flood_cooldown_remaining_seconds": flood_remaining,
                "collapsed_mutable_count": _mutable_coalesced_counts.get(uid, 0),
            }
        )
    return snapshots


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _message_retry_after_seconds(exc: RetryAfter) -> int:
    """Normalize Telegram RetryAfter to a positive integer delay."""
    retry_after = exc.retry_after
    seconds = (
        retry_after
        if isinstance(retry_after, int | float)
        else int(retry_after.total_seconds())
    )
    return max(1, seconds)


def _retry_after_task_class(task: MessageTask) -> str:
    """Classify queued tasks for RetryAfter handling/audit."""
    if task.task_type in {"content", "ingress_receipt"}:
        return "durable"
    if task.task_type in {
        "status_update",
        "commentary_update",
        "pending_input_update",
        "plan_update",
    }:
        return "mutable"
    return "ephemeral"


def _safe_task_chat_id(user_id: int, task: MessageTask) -> int:
    """Best-effort chat id for retry audit rows."""
    try:
        return _task_chat_id(user_id, task)
    except Exception:
        return task.chat_id or user_id


async def _requeue_task_at_front(
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    task: MessageTask,
) -> None:
    """Put the current task back at the front without corrupting join counts.

    `asyncio.Queue` has no public put-front operation.  Draining and refilling
    is safe here because each user has one worker and callers hold the queue
    lock.  Refilled already-queued items get an immediate compensating
    `task_done()`, matching the existing merge/reorder accounting pattern.
    The current task is intentionally not compensated here: the worker's
    `finally: queue.task_done()` completes the attempt that just failed, while
    the new put keeps the retry attempt tracked by `queue.join()`.
    """
    async with lock:
        items = _inspect_queue(queue)
        queue.put_nowait(task)
        for item in items:
            queue.put_nowait(item)
            queue.task_done()


async def _handle_retry_after_for_task(
    *,
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    user_id: int,
    task: MessageTask,
    exc: RetryAfter,
) -> None:
    """Retry or explicitly suppress a task after Telegram flood control."""
    retry_secs = _message_retry_after_seconds(exc)
    task.retry_after_attempts += 1
    task_class = _retry_after_task_class(task)

    if (
        task_class != "durable"
        and task.retry_after_attempts > MESSAGE_TASK_RETRY_AFTER_MAX_ATTEMPTS
    ):
        if task.task_type in {"pending_input_update", "pending_input_clear"}:
            _pending_input_enqueued.pop(_task_state_key(user_id, task), None)
        _audit_task_delivery(
            action="retry_suppressed",
            user_id=user_id,
            chat_id=_safe_task_chat_id(user_id, task),
            task=task,
            text=task.text or "\n\n".join(task.parts),
            success=False,
            error=exc,
            reason=(
                f"retry_after_suppressed:{task_class}:"
                f"attempts={task.retry_after_attempts}:retry_after={retry_secs}"
            ),
        )
        logger.warning(
            "Suppressing %s task after repeated RetryAfter: user=%d type=%s attempts=%d",
            task_class,
            user_id,
            task.task_type,
            task.retry_after_attempts,
        )
        return

    _flood_until[user_id] = time.monotonic() + retry_secs
    _audit_task_delivery(
        action="retry_scheduled",
        user_id=user_id,
        chat_id=_safe_task_chat_id(user_id, task),
        task=task,
        text=task.text or "\n\n".join(task.parts),
        success=False,
        error=exc,
        reason=(
            f"retry_after_scheduled:{task_class}:"
            f"attempt={task.retry_after_attempts}:retry_after={retry_secs}"
        ),
    )
    await _requeue_task_at_front(queue, lock, task)
    logger.warning(
        "RetryAfter for user %d: retrying %s task %s after %ds (attempt %d)",
        user_id,
        task_class,
        task.task_type,
        retry_secs,
        task.retry_after_attempts,
    )
    await asyncio.sleep(min(retry_secs, FLOOD_CONTROL_MAX_WAIT))


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if base.thread_id != candidate.thread_id:
        return False
    if base.chat_id != candidate.chat_id:
        return False
    if base.surface_key != candidate.surface_key:
        return False
    if base.turn_generation != candidate.turn_generation:
        return False
    if candidate.task_type != "content":
        return False
    if base.content_type != candidate.content_type:
        return False
    # tool_use/tool_result/command_execution break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    # - command_execution: command starts and completions are paired by tool_use_id
    #   and merging would collapse distinct command identities into one message
    if base.content_type in ("tool_use", "tool_result", "command_execution"):
        return False
    if candidate.content_type in ("tool_use", "tool_result", "command_execution"):
        return False
    if base.semantic_kind == WARNING_SEMANTIC_KIND:
        return False
    if candidate.semantic_kind == WARNING_SEMANTIC_KIND:
        return False
    if base.warning_key != candidate.warning_key:
        return False
    if base.image_data or candidate.image_data:
        return False
    if base.document_data or candidate.document_data:
        return False
    return True


_MUTABLE_COALESCING_BARRIER_TASK_TYPES = {
    "content",
    "ingress_receipt",
    "commentary_close",
    "pre_final_close",
}


def _mutable_coalesce_lane(task: MessageTask) -> str | None:
    if task.task_type in {"status_update", "status_clear"}:
        return f"status:{task.content_type}:{task.semantic_kind}"
    if task.task_type in {"commentary_update", "commentary_clear"}:
        return "commentary"
    if task.task_type in {"plan_update", "plan_clear"}:
        return "plan"
    if task.task_type in {"pending_input_update", "pending_input_clear"}:
        return "pending_input"
    return None


def _same_mutable_coalesce_lane(
    user_id: int,
    existing: MessageTask,
    new: MessageTask,
) -> bool:
    lane = _mutable_coalesce_lane(new)
    if lane is None or _mutable_coalesce_lane(existing) != lane:
        return False
    if _task_state_key(user_id, existing) != _task_state_key(user_id, new):
        return False
    if existing.window_id != new.window_id:
        return False
    return existing.turn_generation == new.turn_generation


async def _enqueue_coalescing_mutable_task(
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    user_id: int,
    task: MessageTask,
) -> int:
    """Enqueue a mutable task after replacing older same-lane queued updates."""
    collapsed = 0
    async with lock:
        items = _inspect_queue(queue)
        last_barrier_index = max(
            (
                index
                for index, item in enumerate(items)
                if item.task_type in _MUTABLE_COALESCING_BARRIER_TASK_TYPES
            ),
            default=-1,
        )
        remaining: list[MessageTask] = []
        for index, item in enumerate(items):
            if index > last_barrier_index and _same_mutable_coalesce_lane(
                user_id,
                item,
                task,
            ):
                collapsed += 1
                if item.task_type in {"pending_input_update", "pending_input_clear"}:
                    _pending_input_enqueued.pop(_task_state_key(user_id, item), None)
                queue.task_done()
                continue
            remaining.append(item)

        for item in remaining:
            queue.put_nowait(item)
            queue.task_done()
        task.depth_at_enqueue = len(remaining)
        queue.put_nowait(task)

    if collapsed:
        _mutable_coalesced_counts[user_id] = (
            _mutable_coalesced_counts.get(user_id, 0) + collapsed
        )
        _audit_task_delivery(
            action="coalesce",
            user_id=user_id,
            chat_id=_safe_task_chat_id(user_id, task),
            task=task,
            text=task.text or "\n\n".join(task.parts),
            reason=f"mutable_queue_coalesced:{_mutable_coalesce_lane(task)}:count={collapsed}",
        )
    return collapsed


def _is_final_barrier_droppable_task(task: MessageTask) -> bool:
    """Return True for obsolete same-turn mutable progress behind a final."""
    if task.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND:
        return False
    if task.semantic_kind == USER_ECHO_SEMANTIC_KIND:
        return False
    if task.semantic_kind == IMAGE_PREVIEW_SEMANTIC_KIND:
        return True
    if task.content_type == GENERATED_IMAGE_PREVIEW_CONTENT_TYPE:
        return False
    # Pending input previews belong to the future-input lane, not current-turn
    # output ordering, so the final barrier must not drop them.
    if task.task_type in {"pending_input_update", "pending_input_clear"}:
        return False
    return task.task_type in {
        "status_update",
        "commentary_update",
        "plan_update",
    }


async def _drop_queued_mutable_progress_before_final(
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    user_id: int,
    final_task: MessageTask,
) -> int:
    """Drop obsolete queued mutable progress for final_task's surface/turn."""
    dropped = 0
    tid = final_task.thread_id or 0
    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []
        for item in items:
            if (
                item.turn_generation == final_task.turn_generation
                and item.window_id == final_task.window_id
                and _task_matches_surface(
                    item,
                    tid,
                    chat_id=final_task.chat_id,
                    surface_key=final_task.surface_key,
                )
                and _is_final_barrier_droppable_task(item)
            ):
                dropped += 1
                _audit_task_delivery(
                    action="suppress",
                    user_id=user_id,
                    chat_id=_safe_task_chat_id(user_id, item),
                    task=item,
                    text=item.text or "\n\n".join(item.parts),
                    reason="final_barrier_dropped_queued_mutable_progress",
                )
                queue.task_done()
                continue
            remaining.append(item)

        for item in remaining:
            queue.put_nowait(item)
            queue.task_done()

    if dropped:
        logger.info(
            "Dropped %d queued mutable progress task(s) before assistant_final: user=%d thread=%s",
            dropped,
            user_id,
            final_task.thread_id,
        )
    return dropped


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            semantic_kind=first.semantic_kind,
            warning_key=first.warning_key,
            thread_id=first.thread_id,
            chat_id=first.chat_id,
            surface_key=first.surface_key,
            turn_generation=first.turn_generation,
            retry_after_attempts=first.retry_after_attempts,
        ),
        merge_count,
    )


async def _message_queue_worker(bot: Bot, user_id: int) -> None:
    """Process message tasks for a user sequentially."""
    queue = _message_queues[user_id]
    lock = _queue_locks[user_id]
    logger.info(f"Message queue worker started for user {user_id}")

    while True:
        try:
            task = await queue.get()
            _inflight_task_users.add(user_id)
            _inflight_tasks[user_id] = task
            try:
                # Flood control: drop status, wait for content
                flood_end = _flood_until.get(user_id, 0)
                if flood_end > 0:
                    remaining = flood_end - time.monotonic()
                    if remaining > 0:
                        if (
                            task.retry_after_attempts == 0
                            and task.task_type not in {
                                "content",
                                "commentary_update",
                                "plan_update",
                                "ingress_receipt",
                            }
                        ):
                            # Status is ephemeral — safe to drop
                            if task.task_type in {
                                "pending_input_update",
                                "pending_input_clear",
                            }:
                                _pending_input_enqueued.pop(
                                    _task_state_key(user_id, task),
                                    None,
                                )
                            continue
                        # Durable or retry-debt tasks wait for the flood gate.
                        logger.debug(
                            "Flood controlled: waiting %.0fs for content (user %d)",
                            remaining,
                            user_id,
                        )
                        await asyncio.sleep(remaining)
                    # Ban expired
                    _flood_until.pop(user_id, None)
                    logger.info("Flood control lifted for user %d", user_id)

                if task.task_type == "content":
                    # Try to merge consecutive content tasks
                    merged_task, merge_count = await _merge_content_tasks(
                        queue, task, lock
                    )
                    if merge_count > 0:
                        logger.debug(f"Merged {merge_count} tasks for user {user_id}")
                        # Mark merged tasks as done
                        for _ in range(merge_count):
                            queue.task_done()
                    await _process_content_task(bot, user_id, merged_task)
                elif task.task_type == "commentary_update":
                    await _process_commentary_update_task(bot, user_id, task)
                elif task.task_type == "pending_input_update":
                    await _process_pending_input_update_task(bot, user_id, task)
                elif task.task_type == "ingress_receipt":
                    await _process_ingress_receipt_task(bot, user_id, task)
                elif task.task_type == "plan_update":
                    await _process_plan_update_task(bot, user_id, task)
                elif task.task_type == "plan_clear":
                    await _do_clear_plan_update_message(
                        bot,
                        user_id,
                        task.thread_id or 0,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                elif task.task_type == "status_update":
                    await _process_status_update_task(bot, user_id, task)
                elif task.task_type == "status_clear":
                    _stop_task_draft_preview_state(user_id, task)
                    if task.chat_id is None and task.surface_key is None:
                        await _do_clear_status_message(
                            bot,
                            user_id,
                            task.thread_id or 0,
                            expected_window_id=task.window_id,
                        )
                    else:
                        await _do_clear_status_message(
                            bot,
                            user_id,
                            task.thread_id or 0,
                            chat_id=task.chat_id,
                            surface_key=task.surface_key,
                            expected_window_id=task.window_id,
                        )
                elif task.task_type == "commentary_clear":
                    await _do_clear_commentary_message(
                        bot,
                        user_id,
                        task.thread_id or 0,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                elif task.task_type == "pending_input_clear":
                    await _process_pending_input_clear_task(bot, user_id, task)
                elif task.task_type in {"commentary_close", "pre_final_close"}:
                    current_generation = current_turn_generation(
                        user_id,
                        task.thread_id,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                    if _is_stale_turn_generation(
                        task.turn_generation,
                        current_generation,
                    ):
                        logger.debug(
                            "Ignoring stale terminal close: user=%d thread=%s generation=%d current=%d",
                            user_id,
                            task.thread_id,
                            task.turn_generation,
                            current_generation,
                        )
                        continue
                    _mark_pre_final_visible_closed(
                        user_id,
                        task.thread_id,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                    _mark_technical_status_closed(
                        user_id,
                        task.thread_id,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                    await _do_clear_commentary_message(
                        bot,
                        user_id,
                        task.thread_id or 0,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                    await _do_clear_plan_update_message(
                        bot,
                        user_id,
                        task.thread_id or 0,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                    await _do_clear_image_preview_message(
                        bot,
                        user_id,
                        task.thread_id or 0,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    )
                    await _do_clear_status_message(
                        bot,
                        user_id,
                        task.thread_id or 0,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                        expected_window_id=task.window_id,
                    )
            except RetryAfter as e:
                await _handle_retry_after_for_task(
                    queue=queue,
                    lock=lock,
                    user_id=user_id,
                    task=task,
                    exc=e,
                )
            except Exception as e:
                logger.error(f"Error processing message task for user {user_id}: {e}")
            finally:
                _inflight_task_users.discard(user_id)
                _inflight_tasks.pop(user_id, None)
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(f"Message queue worker cancelled for user {user_id}")
            break
        except Exception as e:
            logger.error(f"Unexpected error in queue worker for user {user_id}: {e}")


async def flush_terminal_artifacts_before_new_turn(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> int:
    """Deliver queued terminal artifacts before a new user turn advances generation.

    A terminal assistant response is the close of the previous turn. It is not
    ephemeral progress noise, so it must not be stranded behind status updates
    and then stale-dropped after the next user message opens a new generation.

    The function extracts only already-queued terminal content tasks for the
    same topic, leaves all other tasks in order, and processes the terminal
    tasks while the current generation is still valid.
    """
    queue = get_message_queue(user_id)
    lock = _queue_locks.get(user_id)
    if queue is None or lock is None:
        return 0

    tid = thread_id or 0
    terminal_tasks: list[MessageTask] = []

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []
        for task in items:
            if (
                task.task_type == "content"
                and _task_matches_surface(
                    task,
                    tid,
                    chat_id=chat_id,
                    surface_key=surface_key,
                )
                and task.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND
            ):
                terminal_tasks.append(task)
            else:
                remaining.append(task)

        for task in remaining:
            queue.put_nowait(task)
            # The task was already counted when originally queued; compensate
            # for the new put() so queue.join() accounting remains correct.
            queue.task_done()

    for task in terminal_tasks:
        try:
            await _process_content_task(bot, user_id, task)
        finally:
            queue.task_done()

    if terminal_tasks:
        logger.info(
            "Flushed %d terminal artifact(s) before new turn: user=%d thread=%s",
            len(terminal_tasks),
            user_id,
            thread_id,
        )
    return len(terminal_tasks)


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


def _audit_task_delivery(
    *,
    action: str,
    user_id: int,
    chat_id: int,
    task: MessageTask | None,
    text: str = "",
    message_id: int | None = None,
    success: bool = True,
    error: str | Exception | None = None,
    thread_id: int | None = None,
    window_id: str | None = None,
    task_type: str | None = None,
    content_type: str | None = None,
    semantic_kind: str | None = None,
    reason: str | None = None,
    part_index: int | None = None,
    part_count: int | None = None,
    render_mode: str | None = None,
    transport_outcome: str | None = None,
    formatted_error: str | Exception | None = None,
    plain_error: str | Exception | None = None,
) -> None:
    log_telegram_delivery(
        action=action,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=(
            thread_id if thread_id is not None else (task.thread_id if task else None)
        ),
        message_id=message_id,
        window_id=(
            window_id if window_id is not None else (task.window_id if task else None)
        ),
        task_type=(
            task_type if task_type is not None else (task.task_type if task else None)
        ),
        content_type=(
            content_type
            if content_type is not None
            else (task.content_type if task else None)
        ),
        semantic_kind=(
            semantic_kind
            if semantic_kind is not None
            else (task.semantic_kind if task else None)
        ),
        text=text,
        success=success,
        error=error,
        reason=reason,
        turn_generation=(task.turn_generation if task else None),
        tool_use_id=(task.tool_use_id if task else None),
        part_index=part_index,
        part_count=part_count,
        render_mode=render_mode,
        transport_outcome=transport_outcome,
        formatted_error=formatted_error,
        plain_error=plain_error,
        queue_age_ms=(
            int(max(0.0, time.monotonic() - task.enqueued_at) * 1000)
            if task
            else None
        ),
        depth_at_enqueue=(task.depth_at_enqueue if task else None),
        depth_at_send=(_queue_depth_for_user(user_id) if task else None),
        task_class=(_retry_after_task_class(task) if task else None),
        backpressure_reason=(
            reason
            if reason and ("backpressure" in reason or "retry_after" in reason)
            else None
        ),
    )


def _audit_delivery_for_optional_task(
    *,
    action: str,
    user_id: int,
    chat_id: int,
    task: MessageTask | None,
    thread_id: int | None = None,
    message_id: int | None = None,
    window_id: str | None = None,
    task_type: str | None = None,
    content_type: str | None = None,
    semantic_kind: str | None = None,
    text: str = "",
    success: bool = True,
    error: str | Exception | None = None,
    reason: str | None = None,
    render_mode: str | None = None,
    transport_outcome: str | None = None,
    formatted_error: str | Exception | None = None,
    plain_error: str | Exception | None = None,
) -> None:
    """Audit a queue-backed delivery with queue context when a task is available."""
    if task is not None:
        _audit_task_delivery(
            action=action,
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=text,
            message_id=message_id,
            success=success,
            error=error,
            thread_id=thread_id,
            window_id=window_id,
            task_type=task_type,
            content_type=content_type,
            semantic_kind=semantic_kind,
            reason=reason,
            render_mode=render_mode,
            transport_outcome=transport_outcome,
            formatted_error=formatted_error,
            plain_error=plain_error,
        )
        return
    log_telegram_delivery(
        action=action,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        window_id=window_id,
        task_type=task_type,
        content_type=content_type,
        semantic_kind=semantic_kind,
        text=text,
        success=success,
        error=error,
        reason=reason,
        render_mode=render_mode,
        transport_outcome=transport_outcome,
        formatted_error=formatted_error,
        plain_error=plain_error,
    )


def _fallback_audit_kwargs(
    result: FallbackDeliveryResult,
    *,
    success_on_noop: bool = False,
) -> _FallbackAuditKwargs:
    legacy_error = result.plain_error or result.formatted_error
    success = result.success or (
        success_on_noop
        and result.transport_outcome in {"edit_noop", "fallback_edit_noop"}
    )
    return {
        "success": success,
        "render_mode": result.render_mode,
        "transport_outcome": result.transport_outcome,
        "formatted_error": result.formatted_error,
        "plain_error": result.plain_error,
        "error": legacy_error,
    }


def _state_chat_id_for_key(
    *,
    chat_id: int | None,
    state_chat_id: int | None | object,
) -> int | None:
    """Resolve optional state-chat sentinel into the concrete key chat id."""
    if state_chat_id is _STATE_CHAT_UNSET:
        return chat_id
    return cast(int | None, state_chat_id)


async def _edit_text_with_fallback_result(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    text: str,
    formatted_outcome: str = "edited",
    plain_outcome: str = "fallback_edited",
    noop_outcome: str = "edit_noop",
) -> FallbackDeliveryResult:
    """Edit Telegram text and return structured render/fallback outcome."""
    try:
        message = cast(
            Message | None,
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=_ensure_formatted(text),
                parse_mode=PARSE_MODE,
                link_preview_options=NO_LINK_PREVIEW,
            ),
        )
        return FallbackDeliveryResult(message, "markdown_v2", formatted_outcome)
    except RetryAfter:
        raise
    except Exception as formatted_error:
        if _is_message_not_modified_error(formatted_error):
            return FallbackDeliveryResult(
                None,
                "markdown_v2",
                noop_outcome,
                formatted_error=formatted_error,
            )
        try:
            message = cast(
                Message | None,
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=strip_sentinels(text),
                    link_preview_options=NO_LINK_PREVIEW,
                ),
            )
            return FallbackDeliveryResult(
                message,
                "plain_text",
                plain_outcome,
                formatted_error=formatted_error,
            )
        except RetryAfter:
            raise
        except Exception as plain_error:
            if _is_message_not_modified_error(plain_error):
                return FallbackDeliveryResult(
                    None,
                    "plain_text",
                    "fallback_edit_noop",
                    formatted_error=formatted_error,
                    plain_error=plain_error,
                )
            return FallbackDeliveryResult(
                None,
                None,
                "failed",
                formatted_error=formatted_error,
                plain_error=plain_error,
            )


async def _queue_send_with_fallback_result(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: object,
) -> FallbackDeliveryResult:
    """Send text with structured audit data while preserving old test patches.

    Older queue tests patch this module's ``send_with_fallback`` symbol to
    assert queue ordering/retry behavior.  Production code needs the newer
    structured result, but honoring that monkeypatch keeps those tests focused
    on queue semantics instead of audit plumbing.
    """
    if send_with_fallback is not _ORIGINAL_SEND_WITH_FALLBACK:
        message = await send_with_fallback(bot, chat_id, text, **kwargs)  # type: ignore[arg-type]
        if isinstance(message, FallbackDeliveryResult):
            return message
        return FallbackDeliveryResult(
            message,
            "markdown_v2" if message else None,
            "sent" if message else "failed",
        )
    return await send_with_fallback_result(bot, chat_id, text, **kwargs)  # type: ignore[arg-type]


def _is_poll_only_status_text(text: str) -> bool:
    """Return True for low-value empty write_stdin poll status updates."""
    compact = " ".join((text or "").split())
    return bool(
        re.fullmatch(
            r"(?:🛠\s*)?Tool\s+write_stdin\((?:session\s+[^,()]+,\s*)?poll\)",
            compact,
        )
    )


_TOOL_OUTPUT_METADATA_RE = re.compile(
    r"^(?:(?:Chunk ID|Wall time|Original token count):\s*|Process (?:exited|running)\b)"
)
_PREVIEW_FOOTER_RE = re.compile(r"^preview\s+\d+/\d+\s+lines?$", re.IGNORECASE)


def _looks_like_json_payload(text: str) -> bool:
    body = text.strip()
    if not body or body[0] not in "[{":
        return False
    try:
        json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return False
    return True


def _strip_outer_code_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if not lines:
        return ""
    if lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _render_clean_tool_output_status(text: str) -> str | None:
    """Render raw Tool Output pane status as a compact command-output status."""
    body = re.sub(r"^\s*↳\s*Tool Output\s*", "", text.strip()).strip()
    body = _strip_outer_code_fence(body)

    command: str | None = None
    preview_footer: str | None = None
    output_lines: list[str] = []
    in_output = False

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if output_lines:
                output_lines.append("")
            continue
        if stripped.startswith("```"):
            continue
        if _PREVIEW_FOOTER_RE.match(stripped):
            preview_footer = stripped
            continue
        if stripped.startswith("Command:"):
            command = stripped.removeprefix("Command:").strip()
            continue
        if _TOOL_OUTPUT_METADATA_RE.match(stripped):
            continue
        if stripped == "Output:":
            in_output = True
            continue
        # If there was no explicit Output: marker, preserve non-metadata lines
        # anyway; status polling may capture only the already-rendered output
        # portion from the Codex TUI.
        if in_output or command is None or stripped:
            output_lines.append(line)

    output = "\n".join(output_lines).strip()
    if not output and not command:
        return None

    code_payload = output or "completed · no output"
    lang = "json" if _looks_like_json_payload(code_payload) else "text"
    if command:
        command = _strip_command_shell_preamble(command)
        rendered = f"⌘ Command\n```sh\n{command}\n```\n↳ Output\n```{lang}\n{code_payload}\n```"
    else:
        rendered = f"⌘ Command output\n```{lang}\n{code_payload}\n```"
    if preview_footer:
        rendered = f"{rendered}\n{preview_footer}"
    return rendered


def _strip_command_shell_preamble(command: str) -> str:
    """Drop non-semantic shell strict-mode boilerplate when real command lines follow."""
    lines = (command or "").splitlines()
    first_content_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip():
            first_content_index = index
            break
    if first_content_index is None:
        return command
    if lines[first_content_index].strip() != "set -euo pipefail":
        return command
    if not any(line.strip() for line in lines[first_content_index + 1 :]):
        return command
    return "\n".join(lines[:first_content_index] + lines[first_content_index + 1 :]).strip()


def _normalize_command_shell_preamble(text: str) -> str:
    """Normalize the current command detail panel without touching outputs."""
    raw = text or ""
    stripped = raw.strip()
    if not stripped.startswith("⌘ Command") or stripped.startswith("⌘ Command output"):
        return text
    match = _STATUS_CODE_BLOCK_RE.search(raw)
    if not match:
        return text
    command = match.group("body")
    cleaned = _strip_command_shell_preamble(command)
    if cleaned == command or not cleaned:
        return text
    return f"{raw[: match.start('body')]}{cleaned}{raw[match.end('body') :]}"


def _normalize_bare_command_status(text: str) -> str:
    raw = (text or "").strip()
    if not raw.startswith("⌘ Command") or raw.startswith("⌘ Command output"):
        return text
    if "```" in raw:
        return _normalize_command_shell_preamble(text)
    body = raw.removeprefix("⌘ Command").strip()
    if not body:
        return text
    footer = ""
    match = _BARE_COMMAND_PREVIEW_RE.match(body)
    if match:
        body = match.group("body").strip()
        footer = match.group("footer").strip()
        if len([line for line in body.splitlines() if line.strip()]) <= 1 and re.match(
            r"^preview\s+1/\d+\s+lines?$", footer, re.IGNORECASE
        ):
            footer = ""
    body = _strip_command_shell_preamble(body)
    if not body:
        return text
    preview_lines = _shell_preview_lines(body)
    if preview_lines:
        body = "\n".join(preview_lines[:10])
        if len(preview_lines) > 10:
            footer = f"preview 10/{len(preview_lines)} lines"
    rendered = f"⌘ Command\n```sh\n{body}\n```"
    if footer:
        rendered = f"{rendered}\n{footer}"
    return rendered


def _normalize_bare_tool_status(text: str) -> str:
    raw = (text or "").strip()
    if not (raw.startswith("🛠 Tool") or raw.startswith("Tool ")):
        return text
    if "```" in raw:
        return text
    if raw.startswith("🛠 Tool"):
        body = raw.removeprefix("🛠 Tool").strip()
        if not body:
            return text
        return f"🛠 Tool\n```text\n{body}\n```"
    body = raw.removeprefix("Tool").strip()
    return f"🛠 Tool\n```text\n{body or raw}\n```"


def _status_audit_content_type(task: MessageTask) -> str:
    return task.content_type if task.content_type != "text" else "status"


def _status_audit_semantic_kind(task: MessageTask) -> str:
    return task.semantic_kind or "technical_status"


def _is_terminal_control_status_task(task: MessageTask) -> bool:
    return (
        task.semantic_kind == TERMINAL_CONTROL_SEMANTIC_KIND
        or task.content_type == TERMINAL_CONTROL_PANEL_CONTENT_TYPE
    )


def _is_draft_preview_eligible_status_task(task: MessageTask, text: str) -> bool:
    """Return True for high-frequency transient status lines eligible for drafts."""
    if task.task_type != "status_update":
        return False
    if task.content_type not in {"text", "status"}:
        return False
    if task.semantic_kind not in {"", "technical_status"}:
        return False
    body = (text or "").strip()
    if not body or "\n" in body or len(body) > 300:
        return False
    lowered = body.lower()
    if body.startswith("⌘ Command") or lowered.startswith("tool "):
        return False
    if "↳ tool output" in lowered:
        return False
    return True


def _stop_task_draft_preview_state(
    user_id: int,
    task: MessageTask,
    *,
    lane: str = "technical_status",
) -> None:
    """Close pending draft-preview state for a queue-owned lifecycle event."""
    try:
        chat_id = _task_chat_id(user_id, task)
    except Exception:  # pragma: no cover - close-only best effort for legacy tasks
        return
    thread_id = task.thread_id if (task.thread_id or 0) != 0 else None
    turn_generation = task.turn_generation or current_turn_generation(
        user_id,
        task.thread_id,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )
    stop_draft_preview_state(
        chat_id=chat_id,
        thread_id=thread_id,
        surface_key=task.surface_key,
        turn_generation=turn_generation,
        lane=lane,
    )

def _draft_preview_suppresses_durable_status(result_status: str) -> bool:
    return result_status == "sent"


def _normalize_technical_status_text(text: str) -> str:
    """Normalize pane-polled technical status into the human-facing ontology."""
    raw = (text or "").strip()
    if raw.startswith("↳ Tool Output"):
        return _render_clean_tool_output_status(raw) or ""
    return _normalize_bare_tool_status(_normalize_bare_command_status(text))


def _sanitize_status_history_preview(
    text: str,
    *,
    max_chars: int = STATUS_HISTORY_ITEM_MAX_CHARS,
    preserve_json_punctuation: bool = False,
) -> str:
    compact = " ".join((text or "").strip().split())
    compact = compact.replace("```", "′′′").replace("`", "′")
    if not preserve_json_punctuation:
        compact = compact.replace("[", "［").replace("]", "］")
    if len(compact) > max_chars:
        return compact[: max_chars - 1].rstrip() + "…"
    return compact


def _first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _is_path_like_output_preview(text: str) -> bool:
    """Return True when a command-output history preview is a locator token."""
    preview = (text or "").strip()
    if not preview or any(ch.isspace() for ch in preview):
        return False
    return preview.startswith((".omx/", "./", "../", "/data/", "/home/", "/tmp/", "~/", "file://"))


def _json_output_history_preview(text: str) -> str | None:
    body = (text or "").strip()
    if not body or body[0] not in "[{":
        return None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, (dict, list)):
        return None
    compact = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    return _sanitize_status_history_preview(
        compact,
        max_chars=STATUS_HISTORY_COMMAND_OUTPUT_MAX_CHARS,
        preserve_json_punctuation=True,
    )


def _command_output_preview_from_block(text: str) -> tuple[str, str]:
    json_preview = _json_output_history_preview(text)
    if json_preview:
        return json_preview, "json"
    first_line = _first_nonempty_line(text)
    path_preview = _sanitize_status_history_preview(
        first_line,
        max_chars=STATUS_HISTORY_COMMAND_OUTPUT_MAX_CHARS,
    )
    if _is_path_like_output_preview(path_preview):
        return path_preview, "path"
    return (
        _sanitize_status_history_preview(first_line),
        "prose",
    )


HERMES_TOOL_HISTORY_ICONS: dict[str, str] = {
    "browser_navigate": "🌐",
    "browser_screenshot": "📸",
    "browser_snapshot": "🌐",
    "execute_code": "🐍",
    "patch": "🔧",
    "read_file": "📖",
    "search_files": "🔎",
    "send_message": "📨",
    "skill_view": "📚",
    "skills_list": "📚",
    "terminal": "💻",
    "write_file": "✍️",
}


def _inline_code_status_history_item(label: str, preview: str) -> str | None:
    if not preview:
        return None
    return f"{label}: `{preview}`"


def _tool_history_label(name: str) -> str:
    normalized = (name or "").strip()
    if not normalized:
        return ""
    icon = HERMES_TOOL_HISTORY_ICONS.get(normalized)
    if icon:
        return f"{icon} {normalized}"
    return f"🛠 {normalized}"


def _command_output_history_item(preview: str, kind: str) -> str | None:
    if not preview:
        return None
    if kind in {"path", "json"}:
        return f"↳ {preview}"
    return f"↳ `{preview}`"


def _tool_history_name_and_payload(preview: str) -> tuple[str, str]:
    compact = (preview or "").strip()
    if not compact:
        return "", ""
    first, sep, rest = compact.partition(" ")
    if "(" in first:
        return first.split("(", 1)[0].strip(), compact
    if sep and re.match(r"^[A-Za-z0-9_.:-]+$", first):
        return first, rest.strip()
    return compact, ""


def _status_code_blocks(text: str) -> list[str]:
    return [match.group("body").strip() for match in _STATUS_CODE_BLOCK_RE.finditer(text or "")]


def _extract_status_history_item(text: str) -> str | None:
    """Extract one delivered-history line for eligible technical status text."""
    raw = (text or "").strip()
    if not raw:
        return None
    blocks = _status_code_blocks(raw)
    if raw.startswith("⌘ Command output"):
        preview, kind = _command_output_preview_from_block(blocks[0] if blocks else raw)
        return _command_output_history_item(preview, kind)
    if raw.startswith("⌘ Command"):
        if "↳ Output" in raw and len(blocks) >= 2:
            preview, kind = _command_output_preview_from_block(blocks[1])
            return _command_output_history_item(preview, kind)
        command_block = _strip_command_shell_preamble(blocks[0] if blocks else raw)
        preview = _sanitize_status_history_preview(_first_nonempty_line(command_block))
        return _inline_code_status_history_item("💻 terminal", preview)
    if raw.startswith("🛠 Tool") and blocks:
        preview = _sanitize_status_history_preview(_first_nonempty_line(blocks[0]))
        name, payload = _tool_history_name_and_payload(preview)
        label = _tool_history_label(name)
        if label and payload:
            return _inline_code_status_history_item(label, payload)
        return label or None
    tool_match = re.match(r"^(?:🛠\s*)?Tool\s+([A-Za-z0-9_.:-]+)(.*)$", raw, re.DOTALL)
    if tool_match:
        name = tool_match.group(1)
        preview = _sanitize_status_history_preview(tool_match.group(2).strip() or name)
        label = _tool_history_label(name)
        if preview and preview != name:
            return _inline_code_status_history_item(label, preview)
        return label
    return None


def _is_status_history_eligible_task(task: MessageTask) -> bool:
    return (
        task.task_type == "status_update"
        and task.content_type in {"text", "status"}
        and task.semantic_kind in {"", "technical_status"}
        and not _is_terminal_control_status_task(task)
    )


def _render_status_with_history(text: str, history: tuple[str, ...]) -> str:
    if not history:
        return text
    history_text = "\n".join(history)
    return f"{history_text}\n\n{text}"


def _prepare_status_history_candidate(
    task: MessageTask,
    skey: TopicStateKey,
    window_id: str,
    turn_generation: int,
    status_text: str,
) -> tuple[str, StatusHistoryKey | None, tuple[str, ...] | None]:
    """Build a rendered status with delivered-history candidate, without committing it."""
    if not _is_status_history_eligible_task(task):
        return status_text, None, None
    item = _extract_status_history_item(status_text)
    if not item:
        return status_text, None, None
    history_key = _status_history_key(skey, window_id, turn_generation)
    history = _technical_status_history.get(history_key, ())
    if not history or history[-1] != item:
        history = (*history, item)[-STATUS_HISTORY_LIMIT:]
    return _render_status_with_history(status_text, history), history_key, history


def _commit_status_history(
    history_key: StatusHistoryKey | None,
    candidate_history: tuple[str, ...] | None,
) -> None:
    if history_key is not None and candidate_history is not None:
        _technical_status_history[history_key] = candidate_history


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask) -> bool:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return True
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    sent = await send_photo(
        bot,
        chat_id,
        task.image_data,
        caption=task.image_caption,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )
    return sent is not None


def _image_preview_signature(media_type: str, raw_bytes: bytes) -> str:
    """Build a stable signature for preview-media dedupe/audit state."""
    digest = hashlib.sha256()
    digest.update(media_type.encode("utf-8", errors="replace"))
    digest.update(b"\0")
    digest.update(raw_bytes)
    return digest.hexdigest()


def _caption_signature(caption: str | None) -> str:
    """Build a stable signature for preview-caption dedupe/audit state."""
    return hashlib.sha256((caption or "").encode("utf-8")).hexdigest()


def _input_media_photo(
    raw_bytes: bytes,
    caption: str | None,
    *,
    formatted: bool,
) -> InputMediaPhoto:
    """Create a Telegram photo-media payload for image-preview edits."""
    kwargs: dict[str, str] = {}
    if caption:
        if formatted:
            kwargs = {"caption": _ensure_formatted(caption), "parse_mode": PARSE_MODE}
        else:
            kwargs = {"caption": strip_sentinels(caption)}
    return InputMediaPhoto(media=io.BytesIO(raw_bytes), **kwargs)


async def _edit_image_preview_media(
    bot: Bot,
    *,
    chat_id: int,
    message_id: int,
    raw_bytes: bytes,
    caption: str | None,
) -> str:
    """Edit an existing preview media bubble.

    Returns the render mode used for a successful edit. Re-raises Telegram
    edit no-op errors so the caller can audit them distinctly.
    """
    try:
        await bot.edit_message_media(
            chat_id=chat_id,
            message_id=message_id,
            media=_input_media_photo(raw_bytes, caption, formatted=True),
        )
        return "markdown"
    except RetryAfter:
        raise
    except Exception as formatted_error:
        if _is_message_not_modified_error(formatted_error):
            raise
        if not caption:
            raise
        try:
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=_input_media_photo(raw_bytes, caption, formatted=False),
            )
            return "plain"
        except RetryAfter:
            raise
        except Exception as plain_error:
            if _is_message_not_modified_error(plain_error):
                raise plain_error
            raise formatted_error


async def _send_or_edit_image_preview(
    bot: Bot,
    *,
    user_id: int,
    chat_id: int,
    task: MessageTask,
    window_id: str,
    thread_id_or_0: int,
) -> bool:
    """Send or edit the latest-only image-preview progress bubble.

    Image previews are pre-final progress artifacts. They should update one
    mutable Telegram media message inside the live topic/window/turn rather
    than stacking a new image for every runtime preview event.
    """
    if not task.image_data:
        return True

    image_data = task.image_data
    if len(image_data) > 1:
        _audit_task_delivery(
            action="truncate",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=task.image_caption or task.text or "",
            reason="multi_image_preview_truncated",
            part_index=0,
            part_count=len(image_data),
        )
    media_type, raw_bytes = image_data[0]
    caption = task.image_caption
    media_signature = _image_preview_signature(media_type, raw_bytes)
    caption_signature = _caption_signature(caption)
    state_key = _task_state_key(user_id, task)
    preview_key = state_key
    current_info = _image_preview_msg_info.get(preview_key)
    text = caption or task.text or ""

    can_edit = (
        current_info is not None
        and current_info.window_id == window_id
        and current_info.turn_generation == task.turn_generation
        and state_key not in _pre_final_visible_closed
    )
    if can_edit and current_info is not None:
        try:
            render_mode = await _edit_image_preview_media(
                bot,
                chat_id=chat_id,
                message_id=current_info.message_id,
                raw_bytes=raw_bytes,
                caption=caption,
            )
            _image_preview_msg_info[preview_key] = ImagePreviewMessageInfo(
                message_id=current_info.message_id,
                window_id=window_id,
                turn_generation=task.turn_generation,
                media_signature=media_signature,
                caption_signature=caption_signature,
            )
            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=text,
                message_id=current_info.message_id,
                render_mode=render_mode,
            )
            return True
        except RetryAfter:
            raise
        except Exception as edit_error:
            if _is_message_not_modified_error(edit_error):
                _image_preview_msg_info[preview_key] = ImagePreviewMessageInfo(
                    message_id=current_info.message_id,
                    window_id=window_id,
                    turn_generation=task.turn_generation,
                    media_signature=media_signature,
                    caption_signature=caption_signature,
                )
                _audit_task_delivery(
                    action="edit_noop",
                    user_id=user_id,
                    chat_id=chat_id,
                    task=task,
                    text=text,
                    message_id=current_info.message_id,
                    reason="message_not_modified",
                )
                return True

            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=text,
                message_id=current_info.message_id,
                success=False,
                error=edit_error,
                reason="image_preview_media_edit_failed",
            )
            try:
                await bot.delete_message(
                    chat_id=chat_id,
                    message_id=current_info.message_id,
                )
                _discard_image_preview_retry_task(
                    _image_preview_retry_key(
                        user_id,
                        thread_id_or_0,
                        current_info,
                        chat_id=task.chat_id,
                        surface_key=task.surface_key,
                    ),
                    cancel=True,
                )
                _image_preview_msg_info.pop(preview_key, None)
                _audit_task_delivery(
                    action="delete",
                    user_id=user_id,
                    chat_id=chat_id,
                    task=task,
                    text=text,
                    message_id=current_info.message_id,
                    reason="image_preview_replace_after_edit_failed",
                )
            except RetryAfter:
                raise
            except Exception as delete_error:
                # Fail closed against media stacking: keep the old tracked
                # preview and suppress this replacement rather than sending a
                # second media bubble below it.
                _audit_task_delivery(
                    action="delete",
                    user_id=user_id,
                    chat_id=chat_id,
                    task=task,
                    text=text,
                    message_id=current_info.message_id,
                    success=False,
                    error=delete_error,
                    reason="image_preview_delete_before_replace_failed",
                )
                _audit_task_delivery(
                    action="suppress",
                    user_id=user_id,
                    chat_id=chat_id,
                    task=task,
                    text=text,
                    message_id=current_info.message_id,
                    reason="image_preview_no_stack_after_delete_failed",
                )
                return True

    elif current_info is not None:
        # A tracked preview from another window/turn cannot be edited safely.
        # Delete it before opening a replacement bubble so stale preview state
        # cannot stack below the new progress image.
        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=current_info.message_id,
            )
            _discard_image_preview_retry_task(
                _image_preview_retry_key(
                    user_id,
                    thread_id_or_0,
                    current_info,
                    chat_id=task.chat_id,
                    surface_key=task.surface_key,
                ),
                cancel=True,
            )
            _audit_task_delivery(
                action="delete",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=text,
                message_id=current_info.message_id,
                reason="image_preview_clear_stale_before_send",
            )
            _image_preview_msg_info.pop(preview_key, None)
        except RetryAfter:
            raise
        except Exception as delete_error:
            _audit_task_delivery(
                action="delete",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=text,
                message_id=current_info.message_id,
                success=False,
                error=delete_error,
                reason="image_preview_stale_delete_failed",
            )
            _audit_task_delivery(
                action="suppress",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=text,
                message_id=current_info.message_id,
                reason="image_preview_no_stack_after_stale_delete_failed",
            )
            return True

    sent = await send_photo(
        bot,
        chat_id,
        [(media_type, raw_bytes)],
        caption=caption,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )
    message_id = getattr(sent, "message_id", None)
    _audit_task_delivery(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        task=task,
        text=text,
        message_id=message_id,
        success=message_id is not None,
        reason=(
            "image_preview_first_send"
            if current_info is None
            else "image_preview_replacement_send"
        ),
    )
    if message_id is None:
        _image_preview_msg_info.pop(preview_key, None)
        return False

    _image_preview_msg_info[preview_key] = ImagePreviewMessageInfo(
        message_id=message_id,
        window_id=window_id,
        turn_generation=task.turn_generation,
        media_signature=media_signature,
        caption_signature=caption_signature,
    )
    return True


async def _send_task_documents(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send documents attached to a task, if any."""
    if not task.document_data:
        return
    logger.info(
        "Sending %d document(s) in thread %s",
        len(task.document_data),
        task.thread_id,
    )
    await send_document(
        bot,
        chat_id,
        task.document_data,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


async def _is_task_binding_active(
    user_id: int,
    window_id: str,
    thread_id: int | None,
    surface_key: str | None = None,
) -> bool:
    """Check whether a queued task still targets the current live binding."""
    if session_manager.is_external_binding_window_id(window_id) is True:
        if surface_key and _session_has_method("get_window_for_surface"):
            current_window_id = session_manager.get_window_for_surface(
                user_id,
                surface_key=surface_key,
            )
            if current_window_id != window_id:
                return False
            return (
                session_manager.get_surface_binding_state(
                    user_id,
                    surface_key=surface_key,
                )
                == BINDING_STATE_BOUND
            )
        if thread_id is None:
            return True
        current_window_id = session_manager.get_window_for_thread(user_id, thread_id)
        if current_window_id != window_id:
            return False
        return (
            session_manager.get_topic_binding_state(user_id, thread_id)
            == BINDING_STATE_BOUND
        )

    if await tmux_manager.find_window_by_id(window_id) is None:
        return False
    if surface_key and _session_has_method("get_window_for_surface"):
        current_window_id = session_manager.get_window_for_surface(
            user_id,
            surface_key=surface_key,
        )
        if current_window_id != window_id:
            return False
        return (
            session_manager.get_surface_binding_state(
                user_id,
                surface_key=surface_key,
            )
            == BINDING_STATE_BOUND
        )
    if thread_id is None:
        return True
    current_window_id = session_manager.get_window_for_thread(user_id, thread_id)
    if current_window_id != window_id:
        return False
    return (
        session_manager.get_topic_binding_state(user_id, thread_id)
        == BINDING_STATE_BOUND
    )


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    state_key = _task_state_key(user_id, task)
    is_terminal_artifact = task.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND
    current_generation = current_turn_generation(
        user_id,
        task.thread_id,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )
    if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
        logger.debug(
            "Dropping stale content task: user=%d window=%s thread=%s type=%s",
            user_id,
            wid,
            task.thread_id,
            task.content_type,
        )
        await _do_clear_status_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
            expected_window_id=wid,
        )
        await _do_clear_commentary_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_pending_input_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_plan_update_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_image_preview_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        _stop_task_draft_preview_state(user_id, task)
        _clear_warning_tracking_for_topic(
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        clear_tool_msg_ids_for_topic(user_id, task.thread_id)
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=_task_chat_id(user_id, task),
            task=task,
            text=task.text or "\n\n".join(task.parts),
            reason="stale_binding",
        )
        return
    if _is_stale_turn_generation(task.turn_generation, current_generation):
        logger.debug(
            "Dropping stale-turn content task: user=%d window=%s thread=%s semantic=%s generation=%d current=%d",
            user_id,
            wid,
            task.thread_id,
            task.semantic_kind,
            task.turn_generation,
            current_generation,
        )
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=_task_chat_id(user_id, task),
            task=task,
            text=task.text or "\n\n".join(task.parts),
            reason="stale_turn_generation",
        )
        return
    if (
        is_pre_final_visible_semantic_kind(task.semantic_kind)
        and state_key in _pre_final_visible_closed
    ):
        logger.debug(
            "Dropping pre-final content after terminal artifact: user=%d window=%s thread=%s semantic=%s",
            user_id,
            wid,
            task.thread_id,
            task.semantic_kind,
        )
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=_task_chat_id(user_id, task),
            task=task,
            text=task.text or "\n\n".join(task.parts),
            reason="pre_final_after_terminal",
        )
        return
    if task.semantic_kind == WARNING_SEMANTIC_KIND:
        await _process_warning_content_task(
            bot,
            user_id,
            task,
            window_id=wid,
            thread_id_or_0=tid,
        )
        return
    chat_id = _task_chat_id(user_id, task)

    if task.semantic_kind == IMAGE_PREVIEW_SEMANTIC_KIND and task.image_data:
        media_sent = await _send_or_edit_image_preview(
            bot,
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            window_id=wid,
            thread_id_or_0=tid,
        )
        if media_sent:
            _latest_pre_final_visible_kind[state_key] = task.semantic_kind
            await _check_and_send_status(
                bot,
                user_id,
                wid,
                task.thread_id,
                chat_id=task.chat_id,
                surface_key=task.surface_key,
                expected_turn_generation=task.turn_generation,
            )
            return

        logger.warning(
            "Image preview media send failed; falling back to text: user=%d thread=%s",
            user_id,
            task.thread_id,
        )
        _audit_task_delivery(
            action="send",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=task.image_caption or task.text or "",
            reason="image_preview_media_send_failed",
            success=False,
        )
        if not task.parts:
            fallback_text = task.text or task.image_caption or ""
            if fallback_text:
                task.parts = [fallback_text]
                task.text = fallback_text
        task.image_data = None
        task.image_caption = None

    is_generated_image_terminal_preview = (
        is_terminal_artifact
        and task.content_type == GENERATED_IMAGE_PREVIEW_CONTENT_TYPE
        and bool(task.image_data)
    )

    if is_generated_image_terminal_preview:
        media_sent = task.images_sent or await _send_task_images(bot, chat_id, task)
        if media_sent:
            task.images_sent = True
            _audit_task_delivery(
                action="send",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=task.image_caption or task.text or "",
                success=True,
            )
            _clear_assistant_final_delivery_failure(task, user_id)
            _mark_pre_final_visible_closed(
                user_id,
                task.thread_id,
                chat_id=task.chat_id,
                surface_key=task.surface_key,
            )
            _mark_technical_status_closed(
                user_id,
                task.thread_id,
                chat_id=task.chat_id,
                surface_key=task.surface_key,
            )
            await _do_clear_image_preview_message(
                bot,
                user_id,
                tid,
                chat_id=task.chat_id,
                surface_key=task.surface_key,
            )
            await _do_clear_status_message(
                bot,
                user_id,
                tid,
                chat_id=task.chat_id,
                surface_key=task.surface_key,
                expected_window_id=wid,
            )
            return

        logger.warning(
            "Generated-image preview media send failed; falling back to terminal text: "
            "user=%d thread=%s",
            user_id,
            task.thread_id,
        )
        _audit_task_delivery(
            action="send",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=task.image_caption or task.text or "",
            reason="generated_image_preview_media_send_failed",
            success=False,
        )
        if not task.parts:
            fallback_text = task.text or task.image_caption or ""
            if fallback_text:
                task.parts = [fallback_text]
                task.text = fallback_text
        task.image_data = None
        task.image_caption = None

    # 1. Handle tool/command result editing (merged parts are edited together)
    if task.content_type in {"tool_result", "command_execution"} and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(
                bot,
                user_id,
                tid,
                chat_id=task.chat_id,
                surface_key=task.surface_key,
                expected_window_id=wid,
            )
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            result = await _edit_text_with_fallback_result(
                bot,
                chat_id=chat_id,
                message_id=edit_msg_id,
                text=full_text,
            )
            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=full_text,
                message_id=edit_msg_id,
                **_fallback_audit_kwargs(result, success_on_noop=True),
            )
            if result.success or result.transport_outcome in {"edit_noop", "fallback_edit_noop"}:
                await _send_task_images(bot, chat_id, task)
                await _send_task_documents(bot, chat_id, task)
                await _check_and_send_status(
                    bot,
                    user_id,
                    wid,
                    task.thread_id,
                    chat_id=task.chat_id,
                    surface_key=task.surface_key,
                    expected_turn_generation=task.turn_generation,
                )
                return
            logger.debug(
                "Failed to edit tool msg %s, sending new: %s",
                edit_msg_id,
                result.plain_error or result.formatted_error,
            )
            # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = task.delivered_part_count == 0
    last_msg_id: int | None = task.last_message_id
    expected_parts = len(task.parts)
    for part in task.parts[task.delivered_part_count :]:
        current_generation = current_turn_generation(
            user_id,
            task.thread_id,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        if _is_stale_turn_generation(task.turn_generation, current_generation):
            logger.debug(
                "Aborting in-flight stale-turn content task: user=%d window=%s thread=%s semantic=%s generation=%d current=%d",
                user_id,
                wid,
                task.thread_id,
                task.semantic_kind,
                task.turn_generation,
                current_generation,
            )
            return
        if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
            logger.debug(
                "Aborting in-flight stale binding content task: user=%d window=%s thread=%s semantic=%s",
                user_id,
                wid,
                task.thread_id,
                task.semantic_kind,
            )
            return
        if (
            is_pre_final_visible_semantic_kind(task.semantic_kind)
            and state_key in _pre_final_visible_closed
        ):
            logger.debug(
                "Aborting in-flight pre-final content after terminal artifact: user=%d window=%s thread=%s semantic=%s",
                user_id,
                wid,
                task.thread_id,
                task.semantic_kind,
            )
            return
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if (
            first_part
            and not is_terminal_artifact
            and task.semantic_kind != USER_ECHO_SEMANTIC_KIND
        ):
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
                chat_id=chat_id,
                state_chat_id=task.chat_id,
                surface_key=task.surface_key,
            )
            if converted_msg_id is not None:
                last_msg_id = converted_msg_id
                task.last_message_id = converted_msg_id
                task.delivered_part_count += 1
                continue

        send_result = await _queue_send_with_fallback_result(
            bot,
            chat_id,
            part,
            **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
        )
        sent = send_result.message
        _audit_task_delivery(
            action="send",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=part,
            message_id=(sent.message_id if sent else None),
            **_fallback_audit_kwargs(send_result),
        )

        if sent:
            last_msg_id = sent.message_id
            task.last_message_id = sent.message_id
            task.delivered_part_count += 1

    # 3. Record tool/command start message ID for later editing
    if (
        last_msg_id
        and task.tool_use_id
        and task.content_type in {"tool_use", "command_execution"}
    ):
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    current_generation = current_turn_generation(
        user_id,
        task.thread_id,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )
    if _is_stale_turn_generation(task.turn_generation, current_generation):
        logger.debug(
            "Skipping stale-turn content images/status tail: user=%d window=%s thread=%s semantic=%s generation=%d current=%d",
            user_id,
            wid,
            task.thread_id,
            task.semantic_kind,
            task.turn_generation,
            current_generation,
        )
        return
    if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
        logger.debug(
            "Skipping stale binding content images/status tail: user=%d window=%s thread=%s semantic=%s",
            user_id,
            wid,
            task.thread_id,
            task.semantic_kind,
        )
        return
    if (
        is_pre_final_visible_semantic_kind(task.semantic_kind)
        and state_key in _pre_final_visible_closed
    ):
        logger.debug(
            "Skipping late pre-final content images/status tail: user=%d window=%s thread=%s semantic=%s",
            user_id,
            wid,
            task.thread_id,
            task.semantic_kind,
        )
        return
    images_sent = task.images_sent or await _send_task_images(bot, chat_id, task)
    task.images_sent = images_sent
    if not task.documents_sent:
        await _send_task_documents(bot, chat_id, task)
        task.documents_sent = True

    if (
        task.delivered_part_count > 0
        and is_pre_final_visible_semantic_kind(task.semantic_kind)
    ):
        _latest_pre_final_visible_kind[state_key] = task.semantic_kind

    final_delivery_complete = (
        (expected_parts == 0 and (not task.image_data or images_sent))
        or task.delivered_part_count >= expected_parts
    )

    if (
        is_terminal_artifact
        and task.last_message_id is not None
        and final_delivery_complete
    ):
        # Terminal ordering closes only after a final artifact has actually
        # been delivered in full. Closing earlier can hide commentary/status
        # without surfacing the complete final answer if Telegram send fails
        # partway through a multipart delivery.
        _mark_pre_final_visible_closed(
            user_id,
            task.thread_id,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        _mark_technical_status_closed(
            user_id,
            task.thread_id,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_image_preview_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_status_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
            expected_window_id=wid,
        )
        _clear_assistant_final_delivery_failure(task, user_id)
        return

    if is_terminal_artifact and (
        not final_delivery_complete or task.last_message_id is None
    ):
        reason = (
            "assistant_final_delivery_incomplete"
            if not final_delivery_complete
            else "assistant_final_delivery_missing_message"
        )
        _record_assistant_final_delivery_failure(task, user_id, reason)
        return

    # 5. After content, check and send status
    await _check_and_send_status(
        bot,
        user_id,
        wid,
        task.thread_id,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
        expected_turn_generation=task.turn_generation,
    )


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=_state_chat_id_for_key(chat_id=chat_id, state_chat_id=state_chat_id),
        surface_key=surface_key,
    )
    info = _status_msg_info.pop(skey, None)
    if info is None:
        info = _hydrate_status_msg_info(skey, window_id)
        if info is not None:
            _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    if stored_wid != window_id:
        _status_msg_info[skey] = info
        return None
    _clear_persisted_status_msg_info(skey)
    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)

    result = await _edit_text_with_fallback_result(
        bot,
        chat_id=chat_id,
        message_id=msg_id,
        text=content_text,
    )
    log_telegram_delivery(
        action="edit",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=(thread_id_or_0 or None),
        message_id=msg_id,
        window_id=window_id,
        task_type="content",
        text=content_text,
        **_fallback_audit_kwargs(result, success_on_noop=True),
    )
    if result.success or result.transport_outcome in {"edit_noop", "fallback_edit_noop"}:
        return msg_id
    logger.debug("Failed to convert status to content: %s", result.plain_error or result.formatted_error)
    # Message might be deleted or too old, caller will send new message
    return None


def _render_warning_text(base_text: str, repeat_count: int) -> str:
    """Render warning text with optional repeat counter footer."""
    if repeat_count > 2:
        return f"{base_text}\n\n×{repeat_count}"
    return base_text


async def _process_warning_content_task(
    bot: Bot,
    user_id: int,
    task: MessageTask,
    *,
    window_id: str,
    thread_id_or_0: int,
) -> None:
    """Deduplicate repeated warning bubbles and add a counter for N>2."""
    text = (task.text or "\n\n".join(task.parts)).strip()
    if not text and not task.image_data and not task.document_data:
        return

    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = _task_chat_id(user_id, task)
    warning_key = task.warning_key or "latest-warning"
    wkey = _warning_state_key(
        user_id,
        thread_id_or_0,
        warning_key,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )
    current = _warning_msg_info.get(wkey)
    same_warning = (
        current is not None and text and current[1] == window_id and current[2] == text
    )

    if task.image_data and not same_warning:
        try:
            await _send_task_images(bot, chat_id, task)
        except RetryAfter:
            raise
        except Exception as exc:
            logger.warning("Failed to send warning screenshot(s): %s", exc)

    if task.document_data and not same_warning:
        try:
            await _send_task_documents(bot, chat_id, task)
        except RetryAfter:
            raise
        except Exception as exc:
            logger.warning("Failed to send warning document(s): %s", exc)

    if current is not None and text:
        msg_id, stored_wid, last_text, repeat_count = current
        if stored_wid == window_id and last_text == text:
            new_count = repeat_count + 1
            _warning_msg_info[wkey] = (msg_id, stored_wid, last_text, new_count)
            if new_count <= 2:
                return

            rendered = _render_warning_text(text, new_count)
            result = await _edit_text_with_fallback_result(
                bot,
                chat_id=chat_id,
                message_id=msg_id,
                text=rendered,
            )
            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=rendered,
                message_id=msg_id,
                content_type="warning",
                semantic_kind=WARNING_SEMANTIC_KIND,
                **_fallback_audit_kwargs(result, success_on_noop=True),
            )
            if result.success or result.transport_outcome in {"edit_noop", "fallback_edit_noop"}:
                return
            _warning_msg_info.pop(wkey, None)

    if not text:
        return

    send_result = await _queue_send_with_fallback_result(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    sent = send_result.message
    _audit_task_delivery(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        task=task,
        text=text,
        message_id=(sent.message_id if sent else None),
        **_fallback_audit_kwargs(send_result),
    )
    if sent:
        _warning_msg_info[wkey] = (sent.message_id, window_id, text, 1)


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    base_state_key = _task_state_key(user_id, task)
    state_key = _status_tracking_key(
        base_state_key,
        content_type=_status_audit_content_type(task),
        semantic_kind=_status_audit_semantic_kind(task),
    )
    current_generation = current_turn_generation(
        user_id,
        task.thread_id,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )
    if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
        logger.debug(
            "Dropping stale status task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_status_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
            expected_window_id=wid,
        )
        await _do_clear_commentary_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_pending_input_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_plan_update_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        await _do_clear_image_preview_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        _stop_task_draft_preview_state(user_id, task)
        _clear_warning_tracking_for_topic(
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=_task_chat_id(user_id, task),
            task=task,
            text=task.text or "",
            content_type=_status_audit_content_type(task),
            semantic_kind=_status_audit_semantic_kind(task),
            reason="stale_binding",
        )
        return
    if _is_stale_turn_generation(task.turn_generation, current_generation):
        logger.debug(
            "Dropping stale-turn status task: user=%d window=%s thread=%s generation=%d current=%d",
            user_id,
            wid,
            task.thread_id,
            task.turn_generation,
            current_generation,
        )
        await _do_clear_status_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
            expected_window_id=wid,
        )
        _stop_task_draft_preview_state(user_id, task)
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=_task_chat_id(user_id, task),
            task=task,
            text=task.text or "",
            content_type=_status_audit_content_type(task),
            semantic_kind=_status_audit_semantic_kind(task),
            reason="stale_turn_generation",
        )
        return
    if base_state_key in _technical_status_closed and not _is_terminal_control_status_task(task):
        logger.debug(
            "Dropping technical status after terminal artifact: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_status_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
            expected_window_id=wid,
        )
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=_task_chat_id(user_id, task),
            task=task,
            text=task.text or "",
            content_type=_status_audit_content_type(task),
            semantic_kind=_status_audit_semantic_kind(task),
            reason="technical_status_closed",
        )
        return
    chat_id = _task_chat_id(user_id, task)
    skey = state_key
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        _stop_task_draft_preview_state(user_id, task)
        await _do_clear_status_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
            expected_window_id=wid,
        )
        return

    current_info = _status_msg_info.get(skey)
    if current_info is None:
        current_info = _hydrate_status_msg_info(skey, wid)

    if _is_poll_only_status_text(status_text):
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=status_text,
            content_type=_status_audit_content_type(task),
            semantic_kind=_status_audit_semantic_kind(task),
            reason=(
                "poll_without_existing_status"
                if current_info is None
                else "poll_does_not_replace_existing_status"
            ),
        )
        return

    status_text = _normalize_technical_status_text(status_text)
    if not status_text:
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=task.text or "",
            content_type=_status_audit_content_type(task),
            semantic_kind=_status_audit_semantic_kind(task),
            reason="empty_after_status_normalization",
        )
        return
    status_text, history_key, candidate_history = _prepare_status_history_candidate(
        task,
        skey,
        wid,
        task.turn_generation or current_generation,
        status_text,
    )

    draft_result = None
    if _is_draft_preview_eligible_status_task(task, status_text):
        draft_result = await maybe_send_draft_preview(
            bot,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=tid if tid != 0 else None,
            surface_key=task.surface_key,
            window_id=wid,
            text=status_text,
            turn_generation=task.turn_generation,
            lane="technical_status",
            source_content_type=_status_audit_content_type(task),
            source_semantic_kind=_status_audit_semantic_kind(task),
        )
        if (
            config.telegram_draft_preview_mode == "on"
            and current_info is not None
            and _draft_preview_suppresses_durable_status(draft_result.status)
        ):
            _audit_task_delivery(
                action="suppress",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=status_text,
                content_type=_status_audit_content_type(task),
                semantic_kind=_status_audit_semantic_kind(task),
                reason="draft_preview_transport",
            )
            return

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            _clear_status_history_for_key(skey, window_id=stored_wid)
            await _do_clear_status_message(
                bot,
                user_id,
                tid,
                chat_id=task.chat_id,
                surface_key=task.surface_key,
                expected_window_id=stored_wid,
            )
            if await _do_send_status_message(
                bot,
                user_id,
                tid,
                wid,
                status_text,
                chat_id=chat_id,
                state_chat_id=task.chat_id,
                surface_key=task.surface_key,
                content_type=_status_audit_content_type(task),
                semantic_kind=_status_audit_semantic_kind(task),
                audit_task=task,
            ):
                _commit_status_history(history_key, candidate_history)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            if "esc to interrupt" in status_text.lower():
                await send_runtime_update_typing_once(
                    bot,
                    user_id,
                    chat_id=chat_id,
                    thread_id=tid if tid != 0 else None,
                    surface_key=task.surface_key,
                    window_id=wid,
                )
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(status_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                _audit_task_delivery(
                    action="edit",
                    user_id=user_id,
                    chat_id=chat_id,
                    task=task,
                    text=status_text,
                    message_id=msg_id,
                    content_type=_status_audit_content_type(task),
                    semantic_kind=_status_audit_semantic_kind(task),
                    render_mode="markdown_v2",
                    transport_outcome="edited",
                )
                _status_msg_info[skey] = (msg_id, wid, status_text)
                _persist_status_msg_info(skey, (msg_id, wid, status_text), content_type=_status_audit_content_type(task), semantic_kind=_status_audit_semantic_kind(task))
                _commit_status_history(history_key, candidate_history)
            except RetryAfter:
                raise
            except (BadRequest, Exception) as exc:
                if _is_message_not_modified_error(exc):
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                    _persist_status_msg_info(skey, (msg_id, wid, status_text), content_type=_status_audit_content_type(task), semantic_kind=_status_audit_semantic_kind(task))
                    _commit_status_history(history_key, candidate_history)
                    _audit_task_delivery(
                        action="edit_noop",
                        user_id=user_id,
                        chat_id=chat_id,
                        task=task,
                        text=status_text,
                        message_id=msg_id,
                        reason="message_not_modified",
                        render_mode="markdown_v2",
                        transport_outcome="edit_noop",
                        formatted_error=exc,
                    )
                    return
                if _is_message_known_gone_error(exc):
                    logger.debug("Tracked status message %s is gone: %s", msg_id, exc)
                    _status_msg_info.pop(skey, None)
                    _clear_persisted_status_msg_info(skey)
                    _clear_status_history_for_key(skey, window_id=stored_wid)
                    if await _do_send_status_message(
                        bot,
                        user_id,
                        tid,
                        wid,
                        status_text,
                        chat_id=chat_id,
                        state_chat_id=task.chat_id,
                        surface_key=task.surface_key,
                        content_type=_status_audit_content_type(task),
                        semantic_kind=_status_audit_semantic_kind(task),
                        audit_task=task,
                    ):
                        _commit_status_history(history_key, candidate_history)
                    return
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=status_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                    _persist_status_msg_info(skey, (msg_id, wid, status_text), content_type=_status_audit_content_type(task), semantic_kind=_status_audit_semantic_kind(task))
                    _commit_status_history(history_key, candidate_history)
                    _audit_task_delivery(
                        action="edit",
                        user_id=user_id,
                        chat_id=chat_id,
                        task=task,
                        text=status_text,
                        message_id=msg_id,
                        content_type=_status_audit_content_type(task),
                        semantic_kind=_status_audit_semantic_kind(task),
                        render_mode="plain_text",
                        transport_outcome="fallback_edited",
                        formatted_error=exc,
                    )
                except RetryAfter:
                    raise
                except (BadRequest, Exception) as e:
                    if _is_message_not_modified_error(e):
                        _status_msg_info[skey] = (msg_id, wid, status_text)
                        _persist_status_msg_info(skey, (msg_id, wid, status_text), content_type=_status_audit_content_type(task), semantic_kind=_status_audit_semantic_kind(task))
                        _commit_status_history(history_key, candidate_history)
                        _audit_task_delivery(
                            action="edit_noop",
                            user_id=user_id,
                            chat_id=chat_id,
                            task=task,
                            text=status_text,
                            message_id=msg_id,
                            content_type=_status_audit_content_type(task),
                            semantic_kind=_status_audit_semantic_kind(task),
                            reason="message_not_modified",
                            render_mode="plain_text",
                            transport_outcome="fallback_edit_noop",
                            formatted_error=exc,
                            plain_error=e,
                        )
                        return
                    if _is_message_known_gone_error(e):
                        logger.debug(
                            "Tracked status message %s is gone after plain fallback: %s",
                            msg_id,
                            e,
                        )
                        _status_msg_info.pop(skey, None)
                        _clear_persisted_status_msg_info(skey)
                        _clear_status_history_for_key(skey, window_id=stored_wid)
                        if await _do_send_status_message(
                            bot,
                            user_id,
                            tid,
                            wid,
                            status_text,
                            chat_id=chat_id,
                            state_chat_id=task.chat_id,
                            surface_key=task.surface_key,
                            content_type=_status_audit_content_type(task),
                            semantic_kind=_status_audit_semantic_kind(task),
                            audit_task=task,
                        ):
                            _commit_status_history(history_key, candidate_history)
                        return
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info[skey] = (msg_id, stored_wid, last_text)
                    _persist_status_msg_info(skey, (msg_id, stored_wid, last_text), content_type=_status_audit_content_type(task), semantic_kind=_status_audit_semantic_kind(task))
                    _audit_task_delivery(
                        action="edit_failed_preserved",
                        user_id=user_id,
                        chat_id=chat_id,
                        task=task,
                        text=status_text,
                        message_id=msg_id,
                        success=False,
                        error=e,
                        content_type=_status_audit_content_type(task),
                        semantic_kind=_status_audit_semantic_kind(task),
                        reason="status_edit_failed_old_maybe_visible",
                        render_mode=None,
                        transport_outcome="failed",
                        formatted_error=exc,
                        plain_error=e,
                    )
    else:
        # No existing status message, send new
        if await _do_send_status_message(
            bot,
            user_id,
            tid,
            wid,
            status_text,
            chat_id=chat_id,
            state_chat_id=task.chat_id,
            surface_key=task.surface_key,
            content_type=_status_audit_content_type(task),
            semantic_kind=_status_audit_semantic_kind(task),
            audit_task=task,
        ):
            _commit_status_history(history_key, candidate_history)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    content_type: str = "status",
    semantic_kind: str = "technical_status",
    audit_task: MessageTask | None = None,
) -> bool:
    """Send a new status message and track it (internal, called from worker)."""
    if _is_poll_only_status_text(text):
        return False
    text = _normalize_technical_status_text(text)
    if not text:
        return False
    base_skey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=_state_chat_id_for_key(chat_id=chat_id, state_chat_id=state_chat_id),
        surface_key=surface_key,
    )
    skey = _status_tracking_key(
        base_skey,
        content_type=content_type,
        semantic_kind=semantic_kind,
    )
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message,
    # including process restarts where only the persisted artifact registry survived.
    old = _status_msg_info.pop(skey, None)
    if old is not None and old[1] != window_id:
        _status_msg_info[skey] = old
        old = None
    if old is None:
        old = _hydrate_status_msg_info(skey, window_id)
        if old is not None:
            _status_msg_info.pop(skey, None)
    if old:
        _clear_persisted_status_msg_info(skey)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    if "esc to interrupt" in text.lower():
        await send_runtime_update_typing_once(
            bot,
            user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            surface_key=surface_key,
            window_id=window_id,
        )
    send_result = await _queue_send_with_fallback_result(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    sent = send_result.message
    _audit_delivery_for_optional_task(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        task=audit_task,
        thread_id=thread_id,
        message_id=(sent.message_id if sent else None),
        window_id=window_id,
        task_type="status_update",
        content_type=content_type,
        semantic_kind=semantic_kind,
        text=text,
        **_fallback_audit_kwargs(send_result),
    )
    if sent:
        _status_msg_info[skey] = (sent.message_id, window_id, text)
        _persist_status_msg_info(skey, (sent.message_id, window_id, text), content_type=content_type, semantic_kind=semantic_kind)
        return True
    return False


async def _process_commentary_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Keep only the latest visible commentary artifact in a topic."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    state_key = _task_state_key(user_id, task)
    current_generation = current_turn_generation(
        user_id,
        task.thread_id,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )
    if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
        logger.debug(
            "Dropping stale commentary task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_commentary_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return
    if _is_stale_turn_generation(task.turn_generation, current_generation):
        logger.debug(
            "Dropping stale-turn commentary task: user=%d window=%s thread=%s generation=%d current=%d",
            user_id,
            wid,
            task.thread_id,
            task.turn_generation,
            current_generation,
        )
        await _do_clear_commentary_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return

    commentary_parts = [part for part in task.parts if part]
    if not commentary_parts and task.text:
        commentary_parts = [task.text]
    commentary_text = "\n\n".join(commentary_parts)
    if not commentary_text:
        await _do_clear_commentary_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return

    if state_key in _pre_final_visible_closed:
        logger.debug(
            "Dropping commentary after final answer: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        return

    ckey = state_key
    current_info = _commentary_msg_info.get(ckey)
    extra_ids = _commentary_extra_msg_ids.get(ckey, ())
    if current_info:
        msg_id, stored_wid, last_text = current_info
        if (
            stored_wid == wid
            and last_text == commentary_text
            and len(extra_ids) == max(0, len(commentary_parts) - 1)
        ):
            return
        latest_kind = _latest_pre_final_visible_kind.get(ckey, "")
        if (
            stored_wid == wid
            and latest_kind == "commentary"
            and len(commentary_parts) == 1
            and not extra_ids
            and not _should_reemit_commentary_at_tail(commentary_text)
        ):
            chat_id = _task_chat_id(user_id, task)
            result = await _edit_text_with_fallback_result(
                bot,
                chat_id=chat_id,
                message_id=msg_id,
                text=commentary_text,
            )
            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                message_id=msg_id,
                content_type="commentary",
                semantic_kind="commentary",
                text=commentary_text,
                **_fallback_audit_kwargs(result, success_on_noop=True),
            )
            if result.success or result.transport_outcome in {"edit_noop", "fallback_edit_noop"}:
                _commentary_msg_info[ckey] = (msg_id, wid, commentary_text)
                return
            _commentary_msg_info.pop(ckey, None)

    await _do_send_commentary_message(
        bot,
        user_id,
        tid,
        wid,
        commentary_parts,
        commentary_text,
        chat_id=_task_chat_id(user_id, task),
        state_chat_id=task.chat_id,
        surface_key=task.surface_key,
        audit_task=task,
    )


async def _do_send_commentary_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    parts: list[str],
    full_text: str,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    audit_task: MessageTask | None = None,
) -> None:
    """Send a new commentary bubble when in-place reuse is unavailable."""
    ckey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=_state_chat_id_for_key(chat_id=chat_id, state_chat_id=state_chat_id),
        surface_key=surface_key,
    )
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    old = _commentary_msg_info.pop(ckey, None)
    old_extra = _commentary_extra_msg_ids.pop(ckey, ())
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    for extra_id in old_extra:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=extra_id)
        except Exception:
            pass

    sent_ids: list[int] = []
    for part in parts:
        send_result = await _queue_send_with_fallback_result(
            bot,
            chat_id,
            part,
            **_send_kwargs(thread_id),  # type: ignore[arg-type]
        )
        sent = send_result.message
        _audit_delivery_for_optional_task(
            action="send",
            user_id=user_id,
            chat_id=chat_id,
            task=audit_task,
            thread_id=thread_id,
            message_id=(sent.message_id if sent else None),
            window_id=window_id,
            task_type="commentary_update",
            content_type="commentary",
            semantic_kind="commentary",
            text=part,
            **_fallback_audit_kwargs(send_result),
        )
        if sent:
            sent_ids.append(sent.message_id)
    if sent_ids:
        _commentary_msg_info[ckey] = (sent_ids[0], window_id, full_text)
        _commentary_extra_msg_ids[ckey] = tuple(sent_ids[1:])
        _latest_pre_final_visible_kind[ckey] = "commentary"


async def _process_plan_update_task(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> None:
    """Keep a dedicated mutable Codex plan artifact updated in place."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    state_key = _task_state_key(user_id, task)
    current_generation = current_turn_generation(
        user_id,
        task.thread_id,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )
    if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
        logger.debug(
            "Dropping stale plan-update task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_plan_update_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return
    if _is_stale_turn_generation(task.turn_generation, current_generation):
        logger.debug(
            "Dropping stale-turn plan-update task: user=%d window=%s thread=%s generation=%d current=%d",
            user_id,
            wid,
            task.thread_id,
            task.turn_generation,
            current_generation,
        )
        await _do_clear_plan_update_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return
    if state_key in _pre_final_visible_closed:
        logger.debug(
            "Dropping plan update after final answer: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        return

    plan_text = task.text or ""
    if not plan_text:
        await _do_clear_plan_update_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return

    pkey = state_key
    current_info = _plan_update_msg_info.get(pkey)
    if current_info:
        msg_id, stored_wid, last_text = current_info
        if stored_wid == wid and last_text == plan_text:
            return
        if stored_wid == wid:
            chat_id = _task_chat_id(user_id, task)
            result = await _edit_text_with_fallback_result(
                bot,
                chat_id=chat_id,
                message_id=msg_id,
                text=plan_text,
            )
            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                message_id=msg_id,
                content_type="plan_update",
                semantic_kind="plan_update",
                text=plan_text,
                **_fallback_audit_kwargs(result, success_on_noop=True),
            )
            if result.success or result.transport_outcome in {"edit_noop", "fallback_edit_noop"}:
                _plan_update_msg_info[pkey] = (msg_id, wid, plan_text)
                _latest_pre_final_visible_kind[pkey] = "plan_update"
                return
            _plan_update_msg_info.pop(pkey, None)

    await _do_send_plan_update_message(
        bot,
        user_id,
        tid,
        wid,
        plan_text,
        chat_id=_task_chat_id(user_id, task),
        state_chat_id=task.chat_id,
        surface_key=task.surface_key,
        audit_task=task,
    )


async def _do_send_plan_update_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    audit_task: MessageTask | None = None,
) -> None:
    """Send a new dedicated plan artifact."""
    state_chat_for_key = _state_chat_id_for_key(
        chat_id=chat_id,
        state_chat_id=state_chat_id,
    )
    pkey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=state_chat_for_key,
        surface_key=surface_key,
    )
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    old = _plan_update_msg_info.pop(pkey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass

    send_result = await _queue_send_with_fallback_result(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    sent = send_result.message
    _audit_delivery_for_optional_task(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        task=audit_task,
        thread_id=thread_id,
        message_id=(sent.message_id if sent else None),
        window_id=window_id,
        task_type="plan_update",
        content_type="plan_update",
        semantic_kind="plan_update",
        text=text,
        **_fallback_audit_kwargs(send_result),
    )
    if sent:
        _plan_update_msg_info[pkey] = (sent.message_id, window_id, text)
        _latest_pre_final_visible_kind[pkey] = "plan_update"


async def _process_pending_input_update_task(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> None:
    """Keep a dedicated pending-input preview artifact updated in place.

    Pending-input preview belongs to the topic input queue, not to the current
    assistant turn generation. It must survive turn transitions while the
    queue still shows actionable follow-up text.
    """
    wid = task.window_id or ""
    tid = task.thread_id or 0
    pkey = _task_state_key(user_id, task)
    if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
        logger.debug(
            "Dropping stale pending-input task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_pending_input_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return

    pending_text = task.text or ""
    if not pending_text:
        await _do_clear_pending_input_message(
            bot,
            user_id,
            tid,
            chat_id=task.chat_id,
            surface_key=task.surface_key,
        )
        return

    current_info = _pending_input_msg_info.get(pkey)
    if current_info:
        msg_id, stored_wid, last_text = current_info
        if stored_wid == wid and last_text == pending_text:
            return
        if stored_wid == wid:
            chat_id = _task_chat_id(user_id, task)
            result = await _edit_text_with_fallback_result(
                bot,
                chat_id=chat_id,
                message_id=msg_id,
                text=pending_text,
            )
            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                message_id=msg_id,
                content_type="pending_input",
                semantic_kind="pending_input",
                text=pending_text,
                **_fallback_audit_kwargs(result, success_on_noop=True),
            )
            if result.success or result.transport_outcome in {"edit_noop", "fallback_edit_noop"}:
                _pending_input_msg_info[pkey] = (msg_id, wid, pending_text)
                return
            _pending_input_msg_info.pop(pkey, None)

    await _do_send_pending_input_message(
        bot,
        user_id,
        tid,
        wid,
        pending_text,
        chat_id=_task_chat_id(user_id, task),
        state_chat_id=task.chat_id,
        surface_key=task.surface_key,
        audit_task=task,
    )


def _runtime_input_target_hint(window_id: str) -> str:
    if not window_id:
        return ""
    descriptor = session_manager.get_process_descriptor(window_id)
    window_name = str(getattr(descriptor, "window_name", "") or "").strip()
    if not window_name:
        display_name = str(session_manager.get_display_name(window_id) or "").strip()
        if display_name and display_name != window_id:
            window_name = display_name
    cwd = str(getattr(descriptor, "cwd", "") or "").strip()
    parts = [window_id]
    if window_name and window_name != window_id:
        parts.append(window_name)
    if cwd:
        parts.append(cwd)
    hint = "target: " + " · ".join(parts)
    if len(hint) > 180:
        hint = hint[:179].rstrip() + "…"
    return hint


def _render_ingress_receipt_text(
    text: str,
    status: str,
    *,
    target_hint: str = "",
) -> str:
    compact = " ".join((text or "").split())
    if len(compact) > 120:
        compact = compact[:119].rstrip() + "…"
    target_line = f" → {target_hint}" if target_hint else ""
    if status == "confirmed":
        return f"✅ Accepted{target_line}\n\n{compact}"
    if status in {"delayed_runtime", "delivered_no_ack"}:
        return (
            "⏳ Delivered; waiting for Codex replay ACK"
            f"{target_line}\n\n{compact}"
        )
    if status == "expired_without_ack":
        return (
            "⚠️ Delivered, but Codex replay ACK did not arrive"
            f"{target_line}\n\n{compact}"
        )
    if status in {"queued_after_tool", "queued_runtime"}:
        return f"⏭ Queue{target_line}\n\n{compact}"
    if status == "composer_staged":
        return f"📝 Staged in Codex composer; not yet persisted{target_line}\n\n{compact}"
    if status == "failed":
        return f"❌ Runtime input was not confirmed{target_line}\n\n{compact}"
    return f"↗ Steer{target_line}\n\n{compact}"


async def _process_ingress_receipt_task(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> None:
    """Send/edit a current-update receipt that is not pre-ACK user echo."""
    wid = task.window_id or ""
    proof_id = task.proof_id or ""
    if not proof_id:
        return
    if not await _is_task_binding_active(user_id, wid, task.thread_id, task.surface_key):
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=_task_chat_id(user_id, task),
            task=task,
            text=task.text or "",
            content_type="ingress_receipt",
            semantic_kind="telegram_ingress_receipt",
            reason="stale_binding",
        )
        return

    text = _render_ingress_receipt_text(
        task.text or "",
        task.receipt_status,
        target_hint=_runtime_input_target_hint(wid).removeprefix("target: "),
    )
    rkey = (
        *_task_state_key(user_id, task),
        proof_id,
    )
    current = _ingress_receipt_msg_info.get(rkey)
    chat_id = _task_chat_id(user_id, task)
    if task.receipt_status == "superseded":
        _ingress_receipt_superseded.add(rkey)
        if current:
            msg_id, stored_wid, _ = current
            _ingress_receipt_msg_info.pop(rkey, None)
            if stored_wid == wid:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    _audit_task_delivery(
                        action="delete",
                        user_id=user_id,
                        chat_id=chat_id,
                        task=task,
                        text=task.text or "",
                        message_id=msg_id,
                        content_type="ingress_receipt",
                        semantic_kind="telegram_ingress_receipt",
                        reason="replay_user_echo_delivered_first",
                    )
                except Exception:
                    logger.debug("Failed to delete superseded ingress receipt")
        return

    if rkey in _ingress_receipt_superseded:
        _audit_task_delivery(
            action="suppress",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=task.text or "",
            content_type="ingress_receipt",
            semantic_kind="telegram_ingress_receipt",
            reason="replay_user_echo_delivered_first",
        )
        return
    if current:
        msg_id, stored_wid, last_text = current
        if stored_wid == wid and last_text == text:
            return
        if stored_wid == wid:
            result = await _edit_text_with_fallback_result(
                bot,
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
            )
            _audit_task_delivery(
                action="edit",
                user_id=user_id,
                chat_id=chat_id,
                task=task,
                text=text,
                message_id=msg_id,
                content_type="ingress_receipt",
                semantic_kind="telegram_ingress_receipt",
                **_fallback_audit_kwargs(result, success_on_noop=True),
            )
            if result.success or result.transport_outcome in {"edit_noop", "fallback_edit_noop"}:
                _ingress_receipt_msg_info[rkey] = (msg_id, wid, text)
                session_manager.update_fast_proof_receipt_message_id(proof_id, msg_id)
                return
            _ingress_receipt_msg_info.pop(rkey, None)

    send_result = await _queue_send_with_fallback_result(
        bot,
        chat_id,
        text,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )
    sent = send_result.message
    _audit_task_delivery(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        task=task,
        text=text,
        message_id=(sent.message_id if sent else None),
        content_type="ingress_receipt",
        semantic_kind="telegram_ingress_receipt",
        **_fallback_audit_kwargs(send_result),
    )
    if sent:
        _ingress_receipt_msg_info[rkey] = (sent.message_id, wid, text)
        session_manager.update_fast_proof_receipt_message_id(proof_id, sent.message_id)
        log_telegram_delivery(
            action="runtime_input_latency",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=task.thread_id,
            message_id=sent.message_id,
            window_id=wid,
            task_type="ingress_receipt",
            content_type="runtime_input_stage",
            semantic_kind="ingress_receipt_api_success",
            text=task.text or "",
            reason=f"proof:{proof_id}",
        )


async def _process_pending_input_clear_task(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> None:
    """Clear pending-input preview only when the clear still belongs to the active topic state."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    if task.window_id and not await _is_task_binding_active(
        user_id, wid, task.thread_id, task.surface_key
    ):
        logger.debug(
            "Dropping stale pending-input clear: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        return
    await _do_clear_pending_input_message(
        bot,
        user_id,
        tid,
        chat_id=task.chat_id,
        surface_key=task.surface_key,
    )


async def _do_send_pending_input_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    audit_task: MessageTask | None = None,
) -> None:
    """Send a new pending-input preview artifact."""
    pkey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=_state_chat_id_for_key(chat_id=chat_id, state_chat_id=state_chat_id),
        surface_key=surface_key,
    )
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    old = _pending_input_msg_info.pop(pkey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass

    send_result = await _queue_send_with_fallback_result(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    sent = send_result.message
    _audit_delivery_for_optional_task(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        task=audit_task,
        thread_id=thread_id,
        message_id=(sent.message_id if sent else None),
        window_id=window_id,
        task_type="pending_input_update",
        content_type="pending_input",
        semantic_kind="pending_input",
        text=text,
        **_fallback_audit_kwargs(send_result),
    )
    if sent:
        _pending_input_msg_info[pkey] = (sent.message_id, window_id, text)
        _pending_input_enqueued[pkey] = (window_id, text)
        return
    # If delivery fails, clear dedupe pin so the next poll can retry the same
    # payload instead of getting suppressed by stale enqueue state.
    _pending_input_enqueued.pop(pkey, None)


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
    expected_window_id: str | None = None,
) -> None:
    """Delete tracked mutable status messages for a delivery surface.

    The public clear operation closes the whole pre-final status lane for the
    surface, including dedicated sub-lanes such as OMX workflow panels.
    """
    base_skey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    keys = _status_lane_keys_for_surface(base_skey)
    resolved_chat_id = chat_id
    for skey in keys:
        info = _status_msg_info.pop(skey, None)
        if info is not None and expected_window_id is not None and info[1] != expected_window_id:
            _status_msg_info[skey] = info
            continue
        if info is None:
            persisted = _load_status_artifacts().get(_status_key_to_storage_key(skey))
            if isinstance(persisted, dict):
                try:
                    message_id = int(persisted.get("message_id"))
                except (TypeError, ValueError):
                    message_id = 0
                stored_window = str(persisted.get("window_id") or "")
                last_text = str(persisted.get("last_text") or "")
                if (
                    message_id
                    and stored_window
                    and (expected_window_id is None or stored_window == expected_window_id)
                ):
                    info = (message_id, stored_window, last_text)
                elif message_id and stored_window:
                    continue
        content_type, semantic_kind = _status_artifact_metadata(skey)
        _clear_persisted_status_msg_info(skey)
        _clear_status_history_for_key(skey)
        if not info:
            continue
        msg_id = info[0]
        if resolved_chat_id is None:
            resolved_chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=resolved_chat_id, message_id=msg_id)
            log_telegram_delivery(
                action="delete",
                user_id=user_id,
                chat_id=resolved_chat_id,
                thread_id=thread_id_or_0 or None,
                message_id=msg_id,
                window_id=info[1],
                task_type="status_clear",
                content_type=content_type,
                semantic_kind=semantic_kind,
                reason="clear_status",
            )
        except Exception as e:
            log_telegram_delivery(
                action="delete",
                user_id=user_id,
                chat_id=resolved_chat_id,
                thread_id=thread_id_or_0 or None,
                message_id=msg_id,
                window_id=info[1],
                task_type="status_clear",
                content_type=content_type,
                semantic_kind=semantic_kind,
                success=False,
                error=e,
                reason="clear_status_failed",
            )
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _do_clear_commentary_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Delete the tracked commentary message for a user/topic."""
    ckey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    info = _commentary_msg_info.pop(ckey, None)
    extra_ids = _commentary_extra_msg_ids.pop(ckey, ())
    if _latest_pre_final_visible_kind.get(ckey) == "commentary":
        _latest_pre_final_visible_kind.pop(ckey, None)
    if info:
        msg_id = info[0]
        if chat_id is None:
            chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete commentary message {msg_id}: {e}")
        for extra_id in extra_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=extra_id)
            except Exception as e:
                logger.debug(f"Failed to delete commentary message {extra_id}: {e}")


async def _do_clear_plan_update_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Delete the tracked plan update artifact for a user/topic."""
    pkey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    info = _plan_update_msg_info.pop(pkey, None)
    if info:
        if chat_id is None:
            chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=info[0])
            log_telegram_delivery(
                action="delete",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id_or_0 or None,
                message_id=info[0],
                window_id=info[1],
                task_type="plan_clear",
                content_type="plan_update",
                semantic_kind="plan_update",
                reason="clear_plan_update",
            )
        except Exception as e:
            log_telegram_delivery(
                action="delete",
                user_id=user_id,
                chat_id=chat_id,
                thread_id=thread_id_or_0 or None,
                message_id=info[0],
                window_id=info[1],
                task_type="plan_clear",
                content_type="plan_update",
                semantic_kind="plan_update",
                success=False,
                error=e,
                reason="clear_plan_update_failed",
            )
            logger.debug(f"Failed to delete plan update message {info[0]}: {e}")
    if _latest_pre_final_visible_kind.get(pkey) == "plan_update":
        _latest_pre_final_visible_kind.pop(pkey, None)


async def _do_clear_image_preview_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    info: ImagePreviewMessageInfo | None = None,
    reason: str = "clear_image_preview",
    schedule_retry: bool = True,
) -> None:
    """Delete tracked image-preview media while preserving retryable state."""
    await _clear_image_preview_message_result(
        bot,
        user_id,
        thread_id_or_0,
        chat_id=chat_id,
        state_chat_id=state_chat_id,
        surface_key=surface_key,
        info=info,
        reason=reason,
        schedule_retry=schedule_retry,
    )


async def _clear_image_preview_message_result(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    info: ImagePreviewMessageInfo | None = None,
    reason: str = "clear_image_preview",
    schedule_retry: bool = True,
) -> bool:
    """Delete tracked image-preview media.

    Returns True when tracking is gone or the specific Telegram message is
    authoritatively gone. Returns False when cleanup debt remains.
    """
    state_chat_for_key = _state_chat_id_for_key(
        chat_id=chat_id,
        state_chat_id=state_chat_id,
    )
    pkey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=state_chat_for_key,
        surface_key=surface_key,
    )
    latest_key = pkey
    if info is None:
        info = _image_preview_msg_info.get(pkey)
    current_info = _image_preview_msg_info.get(pkey)
    owns_current_tracking = _image_preview_info_matches(current_info, info)
    if (
        owns_current_tracking
        and _latest_pre_final_visible_kind.get(latest_key) == IMAGE_PREVIEW_SEMANTIC_KIND
    ):
        _latest_pre_final_visible_kind.pop(latest_key, None)
    if not info:
        return True

    if chat_id is None:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=info.message_id)
        if _image_preview_info_matches(_image_preview_msg_info.get(pkey), info):
            _image_preview_msg_info.pop(pkey, None)
        _discard_image_preview_retry_task(
            _image_preview_retry_key(
                user_id,
                thread_id_or_0,
                info,
                chat_id=state_chat_for_key,
                surface_key=surface_key,
            ),
            cancel=True,
        )
        log_telegram_delivery(
            action="delete",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id_or_0 or None,
            message_id=info.message_id,
            window_id=info.window_id,
            task_type="image_preview_clear",
            content_type="image_preview",
            semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
            reason=reason,
        )
        return True
    except RetryAfter as e:
        log_telegram_delivery(
            action="delete",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id_or_0 or None,
            message_id=info.message_id,
            window_id=info.window_id,
            task_type="image_preview_clear",
            content_type="image_preview",
            semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
            success=False,
            error=e,
            reason=f"{reason}_retry_after",
        )
        logger.debug(
            "Deferring image-preview delete after RetryAfter for message %s: %s",
            info.message_id,
            e,
        )
        if schedule_retry:
            _schedule_image_preview_delete_retry(
                bot,
                user_id,
                thread_id_or_0,
                info,
                chat_id=chat_id,
                state_chat_id=state_chat_for_key,
                surface_key=surface_key,
                delay_seconds=_retry_after_seconds(e),
            )
        return False
    except Exception as e:
        known_gone = _is_message_known_gone_error(e)
        if known_gone:
            if _image_preview_info_matches(_image_preview_msg_info.get(pkey), info):
                _image_preview_msg_info.pop(pkey, None)
            _discard_image_preview_retry_task(
                _image_preview_retry_key(
                    user_id,
                    thread_id_or_0,
                    info,
                    chat_id=state_chat_for_key,
                    surface_key=surface_key,
                ),
                cancel=True,
            )
        log_telegram_delivery(
            action="delete",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id_or_0 or None,
            message_id=info.message_id,
            window_id=info.window_id,
            task_type="image_preview_clear",
            content_type="image_preview",
            semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
            success=False,
            error=e,
            reason=(
                f"{reason}_already_gone"
                if known_gone
                else f"{reason}_failed"
            ),
        )
        logger.debug(
            "Failed to delete image-preview message %s: %s",
            info.message_id,
            e,
        )
        if known_gone:
            return True
        if schedule_retry:
            _schedule_image_preview_delete_retry(
                bot,
                user_id,
                thread_id_or_0,
                info,
                chat_id=chat_id,
                state_chat_id=state_chat_for_key,
                surface_key=surface_key,
                delay_seconds=1,
            )
        return False


def _schedule_image_preview_delete_retry(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    info: ImagePreviewMessageInfo,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    delay_seconds: int,
) -> None:
    """Schedule bounded cleanup debt for a concrete preview message."""
    retry_key = _image_preview_retry_key(
        user_id,
        thread_id_or_0,
        info,
        chat_id=_state_chat_id_for_key(chat_id=chat_id, state_chat_id=state_chat_id),
        surface_key=surface_key,
    )
    existing = _image_preview_delete_retry_tasks.get(retry_key)
    if existing is not None and not existing.done():
        return
    delay = max(1, min(delay_seconds, _IMAGE_PREVIEW_DELETE_RETRY_MAX_DELAY_SECONDS))
    _image_preview_delete_retry_tasks[retry_key] = asyncio.create_task(
        _retry_image_preview_delete(
            bot,
            user_id,
            thread_id_or_0,
            info,
            chat_id=chat_id,
            state_chat_id=state_chat_id,
            surface_key=surface_key,
            initial_delay_seconds=delay,
        )
    )


async def _retry_image_preview_delete(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    info: ImagePreviewMessageInfo,
    *,
    chat_id: int | None = None,
    state_chat_id: int | None | object = _STATE_CHAT_UNSET,
    surface_key: str | None = None,
    initial_delay_seconds: int,
) -> None:
    """Retry preview deletion a bounded number of times without blocking queues."""
    retry_key = _image_preview_retry_key(
        user_id,
        thread_id_or_0,
        info,
        chat_id=_state_chat_id_for_key(chat_id=chat_id, state_chat_id=state_chat_id),
        surface_key=surface_key,
    )
    delay = initial_delay_seconds
    try:
        for attempt in range(1, _IMAGE_PREVIEW_DELETE_RETRY_MAX_ATTEMPTS + 1):
            await asyncio.sleep(delay)
            cleaned = await _clear_image_preview_message_result(
                bot,
                user_id,
                thread_id_or_0,
                chat_id=chat_id,
                state_chat_id=state_chat_id,
                surface_key=surface_key,
                info=info,
                reason=f"clear_image_preview_retry_{attempt}",
                schedule_retry=False,
            )
            if cleaned:
                return
            delay = min(
                delay * 2,
                _IMAGE_PREVIEW_DELETE_RETRY_MAX_DELAY_SECONDS,
            )
        log_telegram_delivery(
            action="delete",
            user_id=user_id,
            chat_id=(
                chat_id
                if chat_id is not None
                else session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
            ),
            thread_id=thread_id_or_0 or None,
            message_id=info.message_id,
            window_id=info.window_id,
            task_type="image_preview_clear",
            content_type="image_preview",
            semantic_kind=IMAGE_PREVIEW_SEMANTIC_KIND,
            success=False,
            reason="clear_image_preview_retry_exhausted",
        )
    except asyncio.CancelledError:
        raise
    finally:
        if _image_preview_delete_retry_tasks.get(retry_key) is asyncio.current_task():
            _image_preview_delete_retry_tasks.pop(retry_key, None)


async def _do_clear_pending_input_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Delete the tracked pending-input preview message for a user/topic."""
    pkey = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    _pending_input_enqueued.pop(pkey, None)
    info = _pending_input_msg_info.pop(pkey, None)
    if info:
        msg_id = info[0]
        if chat_id is None:
            chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete pending-input message {msg_id}: {e}")


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
    expected_turn_generation: int | None = None,
) -> None:
    """Check terminal for status line and send status message if present."""
    tid = thread_id or 0
    state_key = _topic_state_key(
        user_id,
        tid,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    if state_key in _technical_status_closed:
        return
    if expected_turn_generation is not None:
        current_generation = current_turn_generation(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )
        if _is_stale_turn_generation(expected_turn_generation, current_generation):
            return
    # Skip if there are more messages pending in the queue
    queue = _message_queues.get(user_id)
    if queue and not queue.empty():
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    status_line = parse_status_line(pane_text)
    if status_line:
        if expected_turn_generation is not None:
            current_generation = current_turn_generation(
                user_id,
                thread_id,
                chat_id=chat_id,
                surface_key=surface_key,
            )
            if _is_stale_turn_generation(expected_turn_generation, current_generation):
                return
        await _do_send_status_message(
            bot,
            user_id,
            tid,
            window_id,
            status_line,
            chat_id=chat_id,
            surface_key=surface_key,
        )


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    semantic_kind: str = "",
    text: str | None = None,
    thread_id: int | None = None,
    chat_id: int | None = None,
    surface_key: str | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    image_caption: str | None = None,
    document_data: list[tuple[str, str, bytes]] | None = None,
    turn_generation: int = 0,
    warning_key: str | None = None,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d, window_id=%s, content_type=%s",
        user_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id)

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        semantic_kind=semantic_kind,
        warning_key=warning_key,
        thread_id=thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
        image_data=image_data,
        image_caption=image_caption,
        document_data=document_data,
        turn_generation=turn_generation,
    )
    if semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND:
        _clear_assistant_final_delivery_failure(task, user_id)
        await _drop_queued_mutable_progress_before_final(
            queue,
            _queue_locks[user_id],
            user_id,
            task,
        )
    task.depth_at_enqueue = queue.qsize()
    queue.put_nowait(task)


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
    chat_id: int | None = None,
    surface_key: str | None = None,
    turn_generation: int = 0,
    content_type: str = "status",
    semantic_kind: str = "technical_status",
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0
    state_key = _topic_state_key(
        user_id,
        tid,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    terminal_control = (
        semantic_kind == TERMINAL_CONTROL_SEMANTIC_KIND
        or content_type == TERMINAL_CONTROL_PANEL_CONTENT_TYPE
    )
    if status_text and state_key in _technical_status_closed and not terminal_control:
        return

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        info = _status_msg_info.get(state_key)
        if info and info[1] == window_id and info[2] == status_text:
            return

    queue = get_or_create_queue(bot, user_id)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            content_type=content_type,
            semantic_kind=semantic_kind,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(
            task_type="status_clear",
            window_id=window_id,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )

    await _enqueue_coalescing_mutable_task(
        queue,
        _queue_locks[user_id],
        user_id,
        task,
    )


async def enqueue_ingress_receipt(
    bot: Bot,
    user_id: int,
    window_id: str,
    text: str,
    *,
    proof_id: str,
    receipt_status: str = "pending",
    thread_id: int | None = None,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Enqueue a high-priority Telegram ingress receipt/update.

    The task is allowed to pass stale technical status churn but not already
    queued terminal assistant-final content for the same topic.
    """
    queue = get_or_create_queue(bot, user_id)
    task = MessageTask(
        task_type="ingress_receipt",
        text=text,
        window_id=window_id,
        thread_id=thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
        proof_id=proof_id,
        receipt_status=receipt_status,
        turn_generation=current_turn_generation(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        ),
    )
    lock = _queue_locks[user_id]
    tid = thread_id or 0
    async with lock:
        items = _inspect_queue(queue)
        last_terminal_index = -1
        for index, item in enumerate(items):
            if (
                (item.thread_id or 0) == tid
                and _task_matches_surface(
                    item,
                    tid,
                    chat_id=chat_id,
                    surface_key=surface_key,
                )
                and item.task_type == "content"
                and item.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND
            ):
                last_terminal_index = index

        prefix = items[: last_terminal_index + 1]
        suffix = items[last_terminal_index + 1 :]
        reordered = list(prefix)
        placed = False
        for item in suffix:
            if (
                not placed
                and _task_matches_surface(
                    item,
                    tid,
                    chat_id=chat_id,
                    surface_key=surface_key,
                )
                and item.task_type in _TECHNICAL_CHURN_TASK_TYPES
            ):
                reordered.append(task)
                placed = True
            reordered.append(item)
        if not placed:
            reordered.append(task)
        for item in reordered:
            if item is task:
                task.depth_at_enqueue = queue.qsize()
            queue.put_nowait(item)
            if item is not task:
                queue.task_done()


async def enqueue_commentary_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    commentary_text: str | None,
    *,
    parts: list[str] | None = None,
    thread_id: int | None = None,
    chat_id: int | None = None,
    surface_key: str | None = None,
    turn_generation: int = 0,
) -> None:
    """Enqueue latest-only commentary replacement for a topic."""
    tid = thread_id or 0
    state_key = _topic_state_key(
        user_id,
        tid,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    if commentary_text and state_key in _pre_final_visible_closed:
        return

    queue = get_or_create_queue(bot, user_id)

    if commentary_text:
        task = MessageTask(
            task_type="commentary_update",
            text=commentary_text,
            parts=list(parts or []),
            window_id=window_id,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(
            task_type="commentary_clear",
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )

    await _enqueue_coalescing_mutable_task(
        queue,
        _queue_locks[user_id],
        user_id,
        task,
    )


async def enqueue_plan_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    plan_text: str | None,
    thread_id: int | None = None,
    chat_id: int | None = None,
    surface_key: str | None = None,
    turn_generation: int = 0,
) -> None:
    """Enqueue a dedicated mutable Codex plan update artifact."""
    tid = thread_id or 0
    state_key = _topic_state_key(
        user_id,
        tid,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    if plan_text and state_key in _pre_final_visible_closed:
        return

    queue = get_or_create_queue(bot, user_id)
    if plan_text:
        task = MessageTask(
            task_type="plan_update",
            text=plan_text,
            window_id=window_id,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(
            task_type="plan_clear",
            window_id=window_id,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
        )
    await _enqueue_coalescing_mutable_task(
        queue,
        _queue_locks[user_id],
        user_id,
        task,
    )


async def enqueue_pending_input_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    pending_input_text: str | None,
    thread_id: int | None = None,
    chat_id: int | None = None,
    surface_key: str | None = None,
    turn_generation: int = 0,
) -> None:
    """Enqueue a dedicated pending-input preview artifact update."""
    tid = thread_id or 0
    pkey = _topic_state_key(
        user_id,
        tid,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        # Non-content updates are dropped during flood control; do not pin dedupe
        # state here or we may suppress the first post-flood refresh.
        _pending_input_enqueued.pop(pkey, None)
        return
    dedupe_key = (window_id, pending_input_text)
    if _pending_input_enqueued.get(pkey) == dedupe_key:
        return
    if pending_input_text:
        info = _pending_input_msg_info.get(pkey)
        if info and info[1] == window_id and info[2] == pending_input_text:
            _pending_input_enqueued[pkey] = dedupe_key
            return

    queue = get_or_create_queue(bot, user_id)
    _pending_input_enqueued[pkey] = dedupe_key

    if pending_input_text:
        task = MessageTask(
            task_type="pending_input_update",
            text=pending_input_text,
            window_id=window_id,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(
            task_type="pending_input_clear",
            window_id=window_id,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
        )

    await _enqueue_coalescing_mutable_task(
        queue,
        _queue_locks[user_id],
        user_id,
        task,
    )


async def enqueue_commentary_close(
    bot: Bot,
    user_id: int,
    thread_id: int | None = None,
    turn_generation: int = 0,
) -> None:
    """Close the commentary lane in queue order and clear the visible artifact."""
    await enqueue_pre_final_close(
        bot,
        user_id,
        thread_id=thread_id,
        turn_generation=turn_generation,
    )


async def enqueue_pre_final_close(
    bot: Bot,
    user_id: int,
    thread_id: int | None = None,
    turn_generation: int = 0,
) -> None:
    """Close the visible pre-final artifact lane in queue order."""
    queue = get_or_create_queue(bot, user_id)
    queue.put_nowait(
        MessageTask(
            task_type="pre_final_close",
            thread_id=thread_id,
            turn_generation=turn_generation,
        )
    )


def _mark_pre_final_visible_closed(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Prevent visible pre-final artifacts from surfacing until the next user turn."""
    _pre_final_visible_closed.add(
        _topic_state_key(
            user_id,
            thread_id or 0,
            chat_id=chat_id,
            surface_key=surface_key,
        )
    )


def _mark_technical_status_closed(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Prevent technical status artifacts from surfacing until the next user turn."""
    key = _topic_state_key(
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    _technical_status_closed.add(key)
    _clear_status_history_for_surface(key)
    resolved_chat_id = chat_id
    if resolved_chat_id is None:
        try:
            resolved_chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        except Exception:  # pragma: no cover - legacy close path best-effort only
            resolved_chat_id = None
    if resolved_chat_id is not None:
        stop_draft_preview_state(
            chat_id=resolved_chat_id,
            thread_id=thread_id,
            surface_key=surface_key,
            turn_generation=current_turn_generation(
                user_id,
                thread_id,
                chat_id=chat_id,
                surface_key=surface_key,
            ),
            lane="technical_status",
        )


def _mark_commentary_closed(
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Backward-compatible alias for terminal surface closure."""
    _mark_pre_final_visible_closed(user_id, thread_id)
    _mark_technical_status_closed(user_id, thread_id)


def reopen_pre_final_visible_lane(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Allow visible pre-final artifacts to surface again for the next turn."""
    key = _topic_state_key(
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    _pre_final_visible_closed.discard(key)
    _technical_status_closed.discard(key)
    _latest_pre_final_visible_kind.pop(key, None)


def reopen_commentary_lane(
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Backward-compatible alias for pre-final visible artifact reopening."""
    reopen_pre_final_visible_lane(user_id, thread_id)


def clear_pre_final_visible_lane_state(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
    forget_image_preview: bool = True,
) -> None:
    """Clear pre-final artifact visibility state for a topic during teardown."""
    legacy_key = (user_id, thread_id or 0)
    key = _topic_state_key(
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    _pre_final_visible_closed.discard(key)
    _technical_status_closed.discard(key)
    _turn_generations.pop(key, None)
    _latest_pre_final_visible_kind.pop(key, None)
    _clear_status_history_for_surface(key)
    _pending_input_enqueued.pop(key, None)
    _pending_input_enqueued.pop(legacy_key, None)
    _plan_update_msg_info.pop(key, None)
    if forget_image_preview:
        _image_preview_msg_info.pop(key, None)
        _image_preview_msg_info.pop(legacy_key, None)
        _discard_image_preview_retry_tasks_for_topic(
            user_id,
            thread_id or 0,
            chat_id=chat_id,
            surface_key=surface_key,
        )


def is_pre_final_visible_lane_closed(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> bool:
    """Return True when pre-final/status lanes are closed for this topic."""
    key = _topic_state_key(
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    return key in _pre_final_visible_closed or key in _technical_status_closed


def current_turn_generation(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> int:
    """Return the current turn generation for a topic."""
    return _turn_generations.get(
        _topic_state_key(
            user_id,
            thread_id or 0,
            chat_id=chat_id,
            surface_key=surface_key,
        ),
        0,
    )


def open_new_turn_generation(
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> int:
    """Advance to the next turn generation and reopen the terminal surface."""
    key = _topic_state_key(
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    generation = _turn_generations.get(key, 0) + 1
    _turn_generations[key] = generation
    reopen_pre_final_visible_lane(
        user_id,
        thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    # Plan artifacts are latest-only within one assistant turn. Reusing an old
    # plan bubble across a new user turn edits history above the current chat
    # tail, making the plan appear missing even though Telegram accepted the
    # edit. Drop only the pointer; the old message remains as historical
    # evidence and the next plan update opens a fresh visible bubble.
    _plan_update_msg_info.pop(key, None)
    _clear_status_history_for_surface(key)
    return generation


def clear_commentary_lane_state(
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Clear tracked commentary plus the shared pre-final visible lane state."""
    key = _topic_state_key(user_id, thread_id or 0)
    clear_pre_final_visible_lane_state(user_id, thread_id)
    _commentary_msg_info.pop(key, None)
    _commentary_extra_msg_ids.pop(key, None)


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = _topic_state_key(user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)
    _clear_persisted_status_msg_info(skey)
    _clear_status_history_for_key(skey)


async def clear_status_message(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Delete any tracked status message for a topic when a bot handle is available."""
    if bot is None:
        clear_status_msg_info(user_id, thread_id)
        return
    await _do_clear_status_message(bot, user_id, thread_id or 0)


async def clear_commentary_message(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Delete any tracked commentary message for a topic when a bot handle exists."""
    if bot is None:
        key = _topic_state_key(user_id, thread_id or 0)
        _commentary_msg_info.pop(key, None)
        _commentary_extra_msg_ids.pop(key, None)
        return
    await _do_clear_commentary_message(bot, user_id, thread_id or 0)


async def clear_plan_update_message(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Delete any tracked plan update artifact for a topic when a bot handle exists."""
    if bot is None:
        _plan_update_msg_info.pop(_topic_state_key(user_id, thread_id or 0), None)
        return
    await _do_clear_plan_update_message(bot, user_id, thread_id or 0)


async def clear_image_preview_message(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> bool:
    """Delete any tracked image-preview progress bubble when a bot handle exists."""
    key = _topic_state_key(
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    if bot is None:
        _image_preview_msg_info.pop(key, None)
        _discard_image_preview_retry_tasks_for_topic(
            user_id,
            thread_id or 0,
            chat_id=chat_id,
            surface_key=surface_key,
        )
        return True
    return await _clear_image_preview_message_result(
        bot,
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )


async def open_new_turn_generation_with_cleanup(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> int:
    """Advance turn generation after clearing live pre-final image preview media.

    This helper is the bot-backed new-turn path. The synchronous
    ``open_new_turn_generation`` intentionally preserves stale image-preview
    tracking so a later preview can delete-before-replace if a caller has no bot
    handle available.
    """
    thread_id_or_0 = thread_id or 0
    preview_key = _topic_state_key(
        user_id,
        thread_id_or_0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    old_info = _image_preview_msg_info.get(preview_key)
    generation = open_new_turn_generation(
        user_id,
        thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    if bot is not None and old_info is not None:
        await _clear_image_preview_message_result(
            bot,
            user_id,
            thread_id_or_0,
            chat_id=chat_id,
            surface_key=surface_key,
            info=old_info,
        )
    return generation


async def clear_pending_input_message(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
    *,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    """Delete any tracked pending-input preview for a topic when a bot handle exists."""
    pkey = _topic_state_key(
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    if bot is None:
        _pending_input_msg_info.pop(pkey, None)
        _pending_input_enqueued.pop(pkey, None)
        return
    await _do_clear_pending_input_message(
        bot,
        user_id,
        thread_id or 0,
        chat_id=chat_id,
        surface_key=surface_key,
    )


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    _inflight_task_users.clear()
    _inflight_tasks.clear()
    _mutable_coalesced_counts.clear()
    _status_msg_info.clear()
    _technical_status_history.clear()
    _commentary_msg_info.clear()
    _commentary_extra_msg_ids.clear()
    _plan_update_msg_info.clear()
    _image_preview_msg_info.clear()
    for retry_task in list(_image_preview_delete_retry_tasks.values()):
        if not retry_task.done():
            retry_task.cancel()
    _image_preview_delete_retry_tasks.clear()
    _pending_input_msg_info.clear()
    _pending_input_enqueued.clear()
    _warning_msg_info.clear()
    _latest_pre_final_visible_kind.clear()
    _tool_msg_ids.clear()
    _pre_final_visible_closed.clear()
    _technical_status_closed.clear()
    _turn_generations.clear()
    logger.info("Message queue workers stopped")
