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
  - Status message tracking and conversion (keyed by (user_id, thread_id))
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..runtime_types import (
    ASSISTANT_FINAL_SEMANTIC_KIND,
    WARNING_SEMANTIC_KIND,
    is_pre_final_visible_semantic_kind,
)
from ..state_schema import BINDING_STATE_BOUND
from ..terminal_parser import parse_status_line
from ..delivery_audit import log_telegram_delivery
from ..tmux_manager import tmux_manager
from .message_sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_photo,
    send_with_fallback,
    strip_sentinels,
)

logger = logging.getLogger(__name__)


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


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
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    turn_generation: int = 0


# Per-user message queues and worker tasks
_message_queues: dict[int, asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[int, asyncio.Task[None]] = {}
_queue_locks: dict[int, asyncio.Lock] = {}  # Protect drain/refill operations

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# Commentary message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_commentary_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}
# Extra commentary message ids for multi-part commentary artifacts.
_commentary_extra_msg_ids: dict[tuple[int, int], tuple[int, ...]] = {}

# Pending input preview tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_pending_input_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}
# Last enqueued pending-input state per topic for queue-side dedupe.
_pending_input_enqueued: dict[tuple[int, int], tuple[str, str | None]] = {}

# Plan update artifact tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_plan_update_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}
# Warning tracking:
#   (user_id, thread_id_or_0, warning_key) -> (message_id, window_id, warning_text, repeat_count)
# Ordinary warnings keep the default "latest-warning" key so the durable
# latest-warning bubble semantics remain unchanged. Special families such as
# runtime discontinuities can opt into distinct keys to avoid collapsing
# separate events that happen to share the same text.
_warning_msg_info: dict[tuple[int, int, str], tuple[int, str, str, int]] = {}

# Latest visible pre-final artifact kind per topic. This keeps latest-only
# commentary chronologically correct relative to durable orchestration milestones:
# commentary may be edited in place only while it is still the latest visible
# pre-final artifact. Once another visible pre-final artifact lands after it,
# commentary updates must re-emit at the tail instead of rewriting history above it.
_latest_pre_final_visible_kind: dict[tuple[int, int], str] = {}

# Pre-final visible artifact closure: once a final assistant bubble lands in
# compact mode, no later visible pre-final artifact may surface below it until
# the next user turn reopens the lane.
_pre_final_visible_closed: set[tuple[int, int]] = set()

# Technical status closure: once a final assistant bubble lands in compact mode,
# mutable status/progress artifacts must not reappear below that terminal turn
# artifact until the next user turn reopens the status lane.
_technical_status_closed: set[tuple[int, int]] = set()

# Current per-topic turn generation. Incremented whenever a new user turn opens
# the terminal surface. Queue tasks carry the generation they belong to so that
# stale closes and stale pre-final/status artifacts cannot leak across turns.
_turn_generations: dict[tuple[int, int], int] = {}

# Flood control: user_id -> monotonic time when ban expires
_flood_until: dict[int, float] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10


def _clear_warning_tracking_for_topic(
    user_id: int,
    thread_id_or_0: int,
) -> None:
    """Clear warning dedupe state for a topic."""
    stale_keys = [
        key
        for key in _warning_msg_info
        if key[0] == user_id and key[1] == thread_id_or_0
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


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if base.thread_id != candidate.thread_id:
        return False
    if base.turn_generation != candidate.turn_generation:
        return False
    if candidate.task_type != "content":
        return False
    if base.content_type != candidate.content_type:
        return False
    # tool_use/tool_result break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    if base.content_type in ("tool_use", "tool_result"):
        return False
    if candidate.content_type in ("tool_use", "tool_result"):
        return False
    if base.semantic_kind == WARNING_SEMANTIC_KIND:
        return False
    if candidate.semantic_kind == WARNING_SEMANTIC_KIND:
        return False
    if base.warning_key != candidate.warning_key:
        return False
    if base.image_data or candidate.image_data:
        return False
    return True


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
            turn_generation=first.turn_generation,
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
            try:
                # Flood control: drop status, wait for content
                flood_end = _flood_until.get(user_id, 0)
                if flood_end > 0:
                    remaining = flood_end - time.monotonic()
                    if remaining > 0:
                        if task.task_type not in {"content", "commentary_update", "plan_update"}:
                            # Status is ephemeral — safe to drop
                            if task.task_type in {
                                "pending_input_update",
                                "pending_input_clear",
                            }:
                                _pending_input_enqueued.pop(
                                    (user_id, task.thread_id or 0),
                                    None,
                                )
                            continue
                        # Content is actual Claude output — wait then send
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
                elif task.task_type == "plan_update":
                    await _process_plan_update_task(bot, user_id, task)
                elif task.task_type == "plan_clear":
                    await _do_clear_plan_update_message(bot, user_id, task.thread_id or 0)
                elif task.task_type == "status_update":
                    await _process_status_update_task(bot, user_id, task)
                elif task.task_type == "status_clear":
                    await _do_clear_status_message(bot, user_id, task.thread_id or 0)
                elif task.task_type == "commentary_clear":
                    await _do_clear_commentary_message(
                        bot, user_id, task.thread_id or 0
                    )
                elif task.task_type == "pending_input_clear":
                    await _process_pending_input_clear_task(bot, user_id, task)
                elif task.task_type in {"commentary_close", "pre_final_close"}:
                    current_generation = current_turn_generation(user_id, task.thread_id)
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
                    _mark_pre_final_visible_closed(user_id, task.thread_id)
                    _mark_technical_status_closed(user_id, task.thread_id)
                    await _do_clear_commentary_message(
                        bot, user_id, task.thread_id or 0
                    )
                    await _do_clear_plan_update_message(bot, user_id, task.thread_id or 0)
                    await _do_clear_status_message(bot, user_id, task.thread_id or 0)
            except RetryAfter as e:
                retry_secs = (
                    e.retry_after
                    if isinstance(e.retry_after, int)
                    else int(e.retry_after.total_seconds())
                )
                if retry_secs > FLOOD_CONTROL_MAX_WAIT:
                    _flood_until[user_id] = time.monotonic() + retry_secs
                    logger.warning(
                        "Flood control for user %d: retry_after=%ds, "
                        "pausing queue until ban expires",
                        user_id,
                        retry_secs,
                    )
                else:
                    logger.warning(
                        "Flood control for user %d: waiting %ds",
                        user_id,
                        retry_secs,
                    )
                    await asyncio.sleep(retry_secs)
            except Exception as e:
                logger.error(f"Error processing message task for user {user_id}: {e}")
            finally:
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
                and (task.thread_id or 0) == tid
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
    error: str | None = None,
    thread_id: int | None = None,
    window_id: str | None = None,
    task_type: str | None = None,
    content_type: str | None = None,
    semantic_kind: str | None = None,
) -> None:
    log_telegram_delivery(
        action=action,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=(task.thread_id if task else thread_id),
        message_id=message_id,
        window_id=(task.window_id if task else window_id),
        task_type=(task.task_type if task else task_type),
        content_type=(task.content_type if task else content_type),
        semantic_kind=(task.semantic_kind if task else semantic_kind),
        text=text,
        success=success,
        error=error,
    )


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


async def _is_task_binding_active(
    user_id: int,
    window_id: str,
    thread_id: int | None,
) -> bool:
    """Check whether a queued task still targets the current live binding."""
    if session_manager.is_external_binding_window_id(window_id) is True:
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
    is_terminal_artifact = task.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND
    current_generation = current_turn_generation(user_id, task.thread_id)
    if not await _is_task_binding_active(user_id, wid, task.thread_id):
        logger.debug(
            "Dropping stale content task: user=%d window=%s thread=%s type=%s",
            user_id,
            wid,
            task.thread_id,
            task.content_type,
        )
        await _do_clear_status_message(bot, user_id, tid)
        await _do_clear_commentary_message(bot, user_id, tid)
        await _do_clear_pending_input_message(bot, user_id, tid)
        await _do_clear_plan_update_message(bot, user_id, tid)
        _clear_warning_tracking_for_topic(user_id, tid)
        clear_tool_msg_ids_for_topic(user_id, task.thread_id)
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
        return
    if (
        is_pre_final_visible_semantic_kind(task.semantic_kind)
        and (user_id, tid) in _pre_final_visible_closed
    ):
        logger.debug(
            "Dropping pre-final content after terminal artifact: user=%d window=%s thread=%s semantic=%s",
            user_id,
            wid,
            task.thread_id,
            task.semantic_kind,
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
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    text=_ensure_formatted(full_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                _audit_task_delivery(
                    action="edit",
                    user_id=user_id,
                    chat_id=chat_id,
                    task=task,
                    text=full_text,
                    message_id=edit_msg_id,
                )
                await _send_task_images(bot, chat_id, task)
                await _check_and_send_status(
                    bot,
                    user_id,
                    wid,
                    task.thread_id,
                    expected_turn_generation=task.turn_generation,
                )
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    # Fallback: plain text with sentinels stripped
                    plain_text = strip_sentinels(task.text or full_text)
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    await _send_task_images(bot, chat_id, task)
                    await _check_and_send_status(
                        bot,
                        user_id,
                        wid,
                        task.thread_id,
                        expected_turn_generation=task.turn_generation,
                    )
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    delivered_parts = 0
    expected_parts = len(task.parts)
    for part in task.parts:
        current_generation = current_turn_generation(user_id, task.thread_id)
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
        if not await _is_task_binding_active(user_id, wid, task.thread_id):
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
            and (user_id, tid) in _pre_final_visible_closed
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
        if first_part and not is_terminal_artifact:
            first_part = False
            converted_msg_id = await _convert_status_to_content(
                bot,
                user_id,
                tid,
                wid,
                part,
            )
            if converted_msg_id is not None:
                last_msg_id = converted_msg_id
                delivered_parts += 1
                continue

        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
        )
        _audit_task_delivery(
            action="send",
            user_id=user_id,
            chat_id=chat_id,
            task=task,
            text=part,
            message_id=(sent.message_id if sent else None),
            success=sent is not None,
        )

        if sent:
            last_msg_id = sent.message_id
            delivered_parts += 1

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    current_generation = current_turn_generation(user_id, task.thread_id)
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
    if not await _is_task_binding_active(user_id, wid, task.thread_id):
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
        and (user_id, tid) in _pre_final_visible_closed
    ):
        logger.debug(
            "Skipping late pre-final content images/status tail: user=%d window=%s thread=%s semantic=%s",
            user_id,
            wid,
            task.thread_id,
            task.semantic_kind,
        )
        return
    await _send_task_images(bot, chat_id, task)

    if delivered_parts > 0 and is_pre_final_visible_semantic_kind(task.semantic_kind):
        _latest_pre_final_visible_kind[(user_id, tid)] = task.semantic_kind

    final_delivery_complete = (
        expected_parts == 0 or delivered_parts == expected_parts
    )

    if is_terminal_artifact and last_msg_id is not None and final_delivery_complete:
        # Terminal ordering closes only after a final artifact has actually
        # been delivered in full. Closing earlier can hide commentary/status
        # without surfacing the complete final answer if Telegram send fails
        # partway through a multipart delivery.
        _mark_pre_final_visible_closed(user_id, task.thread_id)
        _mark_technical_status_closed(user_id, task.thread_id)
        await _do_clear_status_message(bot, user_id, tid)
        return

    # 5. After content, check and send status
    await _check_and_send_status(
        bot,
        user_id,
        wid,
        task.thread_id,
        expected_turn_generation=task.turn_generation,
    )


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
    if stored_wid != window_id:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=_ensure_formatted(content_text),
            parse_mode=PARSE_MODE,
            link_preview_options=NO_LINK_PREVIEW,
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
        )
        return msg_id
    except RetryAfter:
        raise
    except Exception:
        try:
            # Fallback to plain text with sentinels stripped
            plain = strip_sentinels(content_text)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return msg_id
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
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
    if not text and not task.image_data:
        return

    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    warning_key = task.warning_key or "latest-warning"
    wkey = (user_id, thread_id_or_0, warning_key)
    current = _warning_msg_info.get(wkey)
    same_warning = (
        current is not None
        and text
        and current[1] == window_id
        and current[2] == text
    )

    if task.image_data and not same_warning:
        try:
            await _send_task_images(bot, chat_id, task)
        except RetryAfter:
            raise
        except Exception as exc:
            logger.warning("Failed to send warning screenshot(s): %s", exc)

    if current is not None and text:
        msg_id, stored_wid, last_text, repeat_count = current
        if stored_wid == window_id and last_text == text:
            new_count = repeat_count + 1
            _warning_msg_info[wkey] = (msg_id, stored_wid, last_text, new_count)
            if new_count <= 2:
                return

            rendered = _render_warning_text(text, new_count)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(rendered),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=rendered,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    return
                except RetryAfter:
                    raise
                except Exception:
                    _warning_msg_info.pop(wkey, None)

    if not text:
        return

    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    _audit_task_delivery(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        task=task,
        text=text,
        message_id=(sent.message_id if sent else None),
        success=sent is not None,
    )
    if sent:
        _warning_msg_info[wkey] = (sent.message_id, window_id, text, 1)


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    current_generation = current_turn_generation(user_id, task.thread_id)
    if not await _is_task_binding_active(user_id, wid, task.thread_id):
        logger.debug(
            "Dropping stale status task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_status_message(bot, user_id, tid)
        await _do_clear_commentary_message(bot, user_id, tid)
        await _do_clear_pending_input_message(bot, user_id, tid)
        await _do_clear_plan_update_message(bot, user_id, tid)
        _clear_warning_tracking_for_topic(user_id, tid)
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
        await _do_clear_status_message(bot, user_id, tid)
        return
    if (user_id, tid) in _technical_status_closed:
        logger.debug(
            "Dropping technical status after terminal artifact: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_status_message(bot, user_id, tid)
        return
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid)
        return

    current_info = _status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid)
            await _do_send_status_message(bot, user_id, tid, wid, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            # Send typing indicator when Claude is working
            if "esc to interrupt" in status_text.lower():
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except RetryAfter:
                    raise
                except Exception:
                    pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(status_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                log_telegram_delivery(
                    action="edit",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=task.thread_id,
                    message_id=msg_id,
                    window_id=wid,
                    task_type="status_update",
                    content_type="status",
                    semantic_kind="technical_status",
                    text=status_text,
                )
                _status_msg_info[skey] = (msg_id, wid, status_text)
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=status_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                except RetryAfter:
                    raise
                except Exception as e:
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info.pop(skey, None)
                    await _do_send_status_message(bot, user_id, tid, wid, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, tid, wid, status_text)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _status_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    # Send typing indicator when Claude is working
    if "esc to interrupt" in text.lower():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter:
            raise
        except Exception:
            pass
    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    log_telegram_delivery(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=(sent.message_id if sent else None),
        window_id=window_id,
        task_type="status_update",
        content_type="status",
        semantic_kind="technical_status",
        text=text,
        success=sent is not None,
    )
    if sent:
        _status_msg_info[skey] = (sent.message_id, window_id, text)


async def _process_commentary_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Keep only the latest visible commentary artifact in a topic."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    current_generation = current_turn_generation(user_id, task.thread_id)
    if not await _is_task_binding_active(user_id, wid, task.thread_id):
        logger.debug(
            "Dropping stale commentary task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_commentary_message(bot, user_id, tid)
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
        await _do_clear_commentary_message(bot, user_id, tid)
        return

    commentary_parts = [part for part in task.parts if part]
    if not commentary_parts and task.text:
        commentary_parts = [task.text]
    commentary_text = "\n\n".join(commentary_parts)
    if not commentary_text:
        await _do_clear_commentary_message(bot, user_id, tid)
        return

    if (user_id, tid) in _pre_final_visible_closed:
        logger.debug(
            "Dropping commentary after final answer: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        return

    ckey = (user_id, tid)
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
        if stored_wid == wid and latest_kind == "commentary" and len(commentary_parts) == 1 and not extra_ids:
            chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(commentary_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                log_telegram_delivery(
                    action="edit",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=task.thread_id,
                    message_id=msg_id,
                    window_id=wid,
                    task_type="commentary_update",
                    content_type="commentary",
                    semantic_kind="commentary",
                    text=commentary_text,
                )
                _commentary_msg_info[ckey] = (msg_id, wid, commentary_text)
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=commentary_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _commentary_msg_info[ckey] = (msg_id, wid, commentary_text)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    _commentary_msg_info.pop(ckey, None)

    await _do_send_commentary_message(bot, user_id, tid, wid, commentary_parts, commentary_text)


async def _do_send_commentary_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    parts: list[str],
    full_text: str,
) -> None:
    """Send a new commentary bubble when in-place reuse is unavailable."""
    ckey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
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
        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_send_kwargs(thread_id),  # type: ignore[arg-type]
        )
        log_telegram_delivery(
            action="send",
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=(sent.message_id if sent else None),
            window_id=window_id,
            task_type="commentary_update",
            content_type="commentary",
            semantic_kind="commentary",
            text=part,
            success=sent is not None,
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
    current_generation = current_turn_generation(user_id, task.thread_id)
    if not await _is_task_binding_active(user_id, wid, task.thread_id):
        logger.debug(
            "Dropping stale plan-update task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_plan_update_message(bot, user_id, tid)
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
        await _do_clear_plan_update_message(bot, user_id, tid)
        return
    if (user_id, tid) in _pre_final_visible_closed:
        logger.debug(
            "Dropping plan update after final answer: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        return

    plan_text = task.text or ""
    if not plan_text:
        await _do_clear_plan_update_message(bot, user_id, tid)
        return

    pkey = (user_id, tid)
    current_info = _plan_update_msg_info.get(pkey)
    if current_info:
        msg_id, stored_wid, last_text = current_info
        if stored_wid == wid and last_text == plan_text:
            return
        if stored_wid == wid:
            chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(plan_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                log_telegram_delivery(
                    action="edit",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=task.thread_id,
                    message_id=msg_id,
                    window_id=wid,
                    task_type="plan_update",
                    content_type="plan_update",
                    semantic_kind="plan_update",
                    text=plan_text,
                )
                _plan_update_msg_info[pkey] = (msg_id, wid, plan_text)
                _latest_pre_final_visible_kind[pkey] = "plan_update"
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=plan_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _plan_update_msg_info[pkey] = (msg_id, wid, plan_text)
                    _latest_pre_final_visible_kind[pkey] = "plan_update"
                    return
                except RetryAfter:
                    raise
                except Exception:
                    _plan_update_msg_info.pop(pkey, None)

    await _do_send_plan_update_message(bot, user_id, tid, wid, plan_text)


async def _do_send_plan_update_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new dedicated plan artifact."""
    pkey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    old = _plan_update_msg_info.pop(pkey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass

    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    log_telegram_delivery(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=(sent.message_id if sent else None),
        window_id=window_id,
        task_type="plan_update",
        content_type="plan_update",
        semantic_kind="plan_update",
        text=text,
        success=sent is not None,
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
    if not await _is_task_binding_active(user_id, wid, task.thread_id):
        logger.debug(
            "Dropping stale pending-input task: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        await _do_clear_pending_input_message(bot, user_id, tid)
        return

    pending_text = task.text or ""
    if not pending_text:
        await _do_clear_pending_input_message(bot, user_id, tid)
        return

    pkey = (user_id, tid)
    current_info = _pending_input_msg_info.get(pkey)
    if current_info:
        msg_id, stored_wid, last_text = current_info
        if stored_wid == wid and last_text == pending_text:
            return
        if stored_wid == wid:
            chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(pending_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                log_telegram_delivery(
                    action="edit",
                    user_id=user_id,
                    chat_id=chat_id,
                    thread_id=task.thread_id,
                    message_id=msg_id,
                    window_id=wid,
                    task_type="pending_input_update",
                    content_type="pending_input",
                    semantic_kind="pending_input",
                    text=pending_text,
                )
                _pending_input_msg_info[pkey] = (msg_id, wid, pending_text)
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=pending_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _pending_input_msg_info[pkey] = (msg_id, wid, pending_text)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    _pending_input_msg_info.pop(pkey, None)

    await _do_send_pending_input_message(bot, user_id, tid, wid, pending_text)


async def _process_pending_input_clear_task(
    bot: Bot,
    user_id: int,
    task: MessageTask,
) -> None:
    """Clear pending-input preview only when the clear still belongs to the active topic state."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    if task.window_id and not await _is_task_binding_active(user_id, wid, task.thread_id):
        logger.debug(
            "Dropping stale pending-input clear: user=%d window=%s thread=%s",
            user_id,
            wid,
            task.thread_id,
        )
        return
    await _do_clear_pending_input_message(bot, user_id, tid)


async def _do_send_pending_input_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new pending-input preview artifact."""
    pkey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    old = _pending_input_msg_info.pop(pkey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass

    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    log_telegram_delivery(
        action="send",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=(sent.message_id if sent else None),
        window_id=window_id,
        task_type="pending_input_update",
        content_type="pending_input",
        semantic_kind="pending_input",
        text=text,
        success=sent is not None,
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
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if info:
        msg_id = info[0]
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _do_clear_commentary_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the tracked commentary message for a user/topic."""
    ckey = (user_id, thread_id_or_0)
    info = _commentary_msg_info.pop(ckey, None)
    extra_ids = _commentary_extra_msg_ids.pop(ckey, ())
    if _latest_pre_final_visible_kind.get(ckey) == "commentary":
        _latest_pre_final_visible_kind.pop(ckey, None)
    if info:
        msg_id = info[0]
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
) -> None:
    """Delete the tracked plan update artifact for a user/topic."""
    pkey = (user_id, thread_id_or_0)
    info = _plan_update_msg_info.pop(pkey, None)
    if info:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=info[0])
        except Exception as e:
            logger.debug(f"Failed to delete plan update message {info[0]}: {e}")
    if _latest_pre_final_visible_kind.get(pkey) == "plan_update":
        _latest_pre_final_visible_kind.pop(pkey, None)


async def _do_clear_pending_input_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the tracked pending-input preview message for a user/topic."""
    pkey = (user_id, thread_id_or_0)
    _pending_input_enqueued.pop(pkey, None)
    info = _pending_input_msg_info.pop(pkey, None)
    if info:
        msg_id = info[0]
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
    expected_turn_generation: int | None = None,
) -> None:
    """Check terminal for status line and send status message if present."""
    tid = thread_id or 0
    if (user_id, tid) in _technical_status_closed:
        return
    if expected_turn_generation is not None:
        current_generation = current_turn_generation(user_id, thread_id)
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
            current_generation = current_turn_generation(user_id, thread_id)
            if _is_stale_turn_generation(expected_turn_generation, current_generation):
                return
        await _do_send_status_message(bot, user_id, tid, window_id, status_line)


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
    image_data: list[tuple[str, bytes]] | None = None,
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
        image_data=image_data,
        turn_generation=turn_generation,
    )
    queue.put_nowait(task)


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
    turn_generation: int = 0,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(user_id, 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0
    if status_text and (user_id, tid) in _technical_status_closed:
        return

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    queue = get_or_create_queue(bot, user_id)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(task_type="status_clear", thread_id=thread_id)

    queue.put_nowait(task)


async def enqueue_commentary_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    commentary_text: str | None,
    *,
    parts: list[str] | None = None,
    thread_id: int | None = None,
    turn_generation: int = 0,
) -> None:
    """Enqueue latest-only commentary replacement for a topic."""
    tid = thread_id or 0
    if commentary_text and (user_id, tid) in _pre_final_visible_closed:
        return

    queue = get_or_create_queue(bot, user_id)

    if commentary_text:
        task = MessageTask(
            task_type="commentary_update",
            text=commentary_text,
            parts=list(parts or []),
            window_id=window_id,
            thread_id=thread_id,
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(task_type="commentary_clear", thread_id=thread_id)

    queue.put_nowait(task)


async def enqueue_plan_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    plan_text: str | None,
    thread_id: int | None = None,
    turn_generation: int = 0,
) -> None:
    """Enqueue a dedicated mutable Codex plan update artifact."""
    tid = thread_id or 0
    if plan_text and (user_id, tid) in _pre_final_visible_closed:
        return

    queue = get_or_create_queue(bot, user_id)
    if plan_text:
        task = MessageTask(
            task_type="plan_update",
            text=plan_text,
            window_id=window_id,
            thread_id=thread_id,
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(
            task_type="plan_clear",
            window_id=window_id,
            thread_id=thread_id,
            turn_generation=turn_generation,
        )
    queue.put_nowait(task)


async def enqueue_pending_input_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    pending_input_text: str | None,
    thread_id: int | None = None,
    turn_generation: int = 0,
) -> None:
    """Enqueue a dedicated pending-input preview artifact update."""
    tid = thread_id or 0
    pkey = (user_id, tid)
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
            turn_generation=turn_generation,
        )
    else:
        task = MessageTask(
            task_type="pending_input_clear",
            window_id=window_id,
            thread_id=thread_id,
            turn_generation=turn_generation,
        )

    queue.put_nowait(task)


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
) -> None:
    """Prevent visible pre-final artifacts from surfacing until the next user turn."""
    _pre_final_visible_closed.add((user_id, thread_id or 0))


def _mark_technical_status_closed(
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Prevent technical status artifacts from surfacing until the next user turn."""
    _technical_status_closed.add((user_id, thread_id or 0))


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
) -> None:
    """Allow visible pre-final artifacts to surface again for the next turn."""
    _pre_final_visible_closed.discard((user_id, thread_id or 0))
    _technical_status_closed.discard((user_id, thread_id or 0))
    _latest_pre_final_visible_kind.pop((user_id, thread_id or 0), None)


def reopen_commentary_lane(
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Backward-compatible alias for pre-final visible artifact reopening."""
    reopen_pre_final_visible_lane(user_id, thread_id)


def clear_pre_final_visible_lane_state(
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Clear pre-final artifact visibility state for a topic during teardown."""
    key = (user_id, thread_id or 0)
    _pre_final_visible_closed.discard(key)
    _technical_status_closed.discard(key)
    _turn_generations.pop(key, None)
    _latest_pre_final_visible_kind.pop(key, None)
    _pending_input_enqueued.pop(key, None)
    _plan_update_msg_info.pop(key, None)


def is_pre_final_visible_lane_closed(
    user_id: int,
    thread_id: int | None = None,
) -> bool:
    """Return True when pre-final/status lanes are closed for this topic."""
    key = (user_id, thread_id or 0)
    return key in _pre_final_visible_closed or key in _technical_status_closed


def current_turn_generation(
    user_id: int,
    thread_id: int | None = None,
) -> int:
    """Return the current turn generation for a topic."""
    return _turn_generations.get((user_id, thread_id or 0), 0)


def open_new_turn_generation(
    user_id: int,
    thread_id: int | None = None,
) -> int:
    """Advance to the next turn generation and reopen the terminal surface."""
    key = (user_id, thread_id or 0)
    generation = _turn_generations.get(key, 0) + 1
    _turn_generations[key] = generation
    reopen_pre_final_visible_lane(user_id, thread_id)
    return generation


def clear_commentary_lane_state(
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Backward-compatible alias for pre-final artifact lane cleanup."""
    clear_pre_final_visible_lane_state(user_id, thread_id)


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)


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
        _commentary_msg_info.pop((user_id, thread_id or 0), None)
        _commentary_extra_msg_ids.pop((user_id, thread_id or 0), None)
        return
    await _do_clear_commentary_message(bot, user_id, thread_id or 0)


async def clear_plan_update_message(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Delete any tracked plan update artifact for a topic when a bot handle exists."""
    if bot is None:
        _plan_update_msg_info.pop((user_id, thread_id or 0), None)
        return
    await _do_clear_plan_update_message(bot, user_id, thread_id or 0)


async def clear_pending_input_message(
    bot: Bot | None,
    user_id: int,
    thread_id: int | None = None,
) -> None:
    """Delete any tracked pending-input preview for a topic when a bot handle exists."""
    if bot is None:
        pkey = (user_id, thread_id or 0)
        _pending_input_msg_info.pop(pkey, None)
        _pending_input_enqueued.pop(pkey, None)
        return
    await _do_clear_pending_input_message(bot, user_id, thread_id or 0)


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
    _status_msg_info.clear()
    _commentary_msg_info.clear()
    _commentary_extra_msg_ids.clear()
    _plan_update_msg_info.clear()
    _pending_input_msg_info.clear()
    _pending_input_enqueued.clear()
    _warning_msg_info.clear()
    _latest_pre_final_visible_kind.clear()
    _tool_msg_ids.clear()
    _pre_final_visible_closed.clear()
    _technical_status_closed.clear()
    _turn_generations.clear()
    logger.info("Message queue workers stopped")
