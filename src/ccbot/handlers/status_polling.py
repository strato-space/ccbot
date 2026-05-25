"""Terminal status line polling for thread-bound windows.

Provides background polling of terminal status lines for all active users:
  - Detects Claude Code status (working, waiting, etc.)
  - Detects interactive UIs (permission prompts) not triggered via JSONL
  - Updates status messages in Telegram
  - Polls thread_bindings (each topic = one window)
  - Periodically probes topic existence via unpin_all_forum_topic_messages
    (silent no-op when no pins); cleans up deleted topics (kills tmux window
    + unbinds thread)

Key components:
  - STATUS_POLL_INTERVAL: Polling frequency (1 second)
  - TOPIC_CHECK_INTERVAL: Topic existence probe frequency (60 seconds)
  - status_poll_loop: Background polling task
  - update_status_message: Poll and enqueue status updates
"""

import asyncio
import logging
import re
import shlex
import time
from pathlib import Path

from telegram import Bot
from telegram.error import BadRequest

from ..runtime_discontinuity import (
    build_discontinuity_image_data,
    build_live_surface_loss_fallback_summary,
    build_live_surface_loss_notice,
    build_runtime_termination_fallback_summary,
    extract_codex_termination_summary_from_rollout,
    extract_nonzero_exit_code_from_rollout,
    extract_terminal_tail_block,
    is_codex_termination_summary_text,
)
from ..runtime_types import (
    OMX_WORKFLOW_PANEL_CONTENT_TYPE,
    OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
    TERMINAL_CONTROL_PANEL_CONTENT_TYPE,
    TERMINAL_CONTROL_SEMANTIC_KIND,
    WARNING_SEMANTIC_KIND,
    runtime_capability_registry,
)
from ..omx_workflow_status import (
    parse_omx_statusline,
    read_omx_workflow_status,
    render_omx_workflow_status,
)
from ..session import session_manager
from ..input_safety import update_window_input_safety_snapshot
from ..terminal_parser import (
    classify_input_surface,
    extract_pending_input_preview,
    extract_terminal_control_observation,
)
from ..tmux_manager import tmux_manager
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .omx_questions import handle_omx_question_ui
from .cleanup import clear_topic_state
from .message_sender import send_photo, send_with_fallback
from .message_queue import (
    current_turn_generation,
    enqueue_content_message,
    enqueue_pending_input_update,
    enqueue_status_update,
    get_message_queue,
    is_pre_final_visible_lane_closed,
)

logger = logging.getLogger(__name__)

# Status polling interval
STATUS_POLL_INTERVAL = 1.0  # seconds - faster response (rate limiting at send layer)

# Topic existence probe interval
TOPIC_CHECK_INTERVAL = 60.0  # seconds

# Runtime-presence tracking per user-bound live window. This lets us emit a
# discontinuity notice once when a live Codex pane falls back to shell while
# preserving normal status polling for active panes, without colliding multiple
# no-topics main-chat surfaces that all share `thread_id is None`.
_runtime_presence: dict[tuple[int, str], bool] = {}
_last_pane_text: dict[str, str] = {}
_USAGE_LIMIT_NOTICE_RE = re.compile(
    r"(you've hit your usage limit|purchase more credits|credits?\b.*try again at|usage to purchase more credits)",
    re.IGNORECASE,
)
_SHELL_PROMPT_RE = re.compile(r"^[^\n]*[#$>]\s*$")
_SHELL_COMMAND_NAMES = {"bash", "dash", "fish", "sh", "zsh"}


def _clip_pending_input_line(text: str, *, max_chars: int = 96) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _clip_pending_input_hint(text: str, *, max_chars: int = 120) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _build_pending_input_text(pane_text: str) -> str | None:
    preview = extract_pending_input_preview(pane_text)
    if preview.is_empty:
        return None

    sections: list[tuple[str, tuple[str, ...]]] = [
        ("Messages to be submitted after next tool call", preview.pending_steers),
        ("Messages to be submitted at end of turn", preview.rejected_steers),
        ("Queued follow-up messages", preview.queued_messages),
    ]
    parts = ["⏭ Pending input"]
    for title, messages in sections:
        if not messages:
            continue
        shown = list(messages[:3])
        parts.extend(["", title])
        parts.extend(f"↳ {_clip_pending_input_line(message)}" for message in shown)
        if len(messages) > len(shown):
            parts.append(f"preview {len(shown)}/{len(messages)} messages")
    if preview.edit_hint:
        parts.extend(["", _clip_pending_input_hint(preview.edit_hint)])
    return "\n".join(parts)


def _extract_usage_limit_notice(pane_text: str) -> str | None:
    """Extract a durable usage-limit banner from visible terminal text."""
    if not pane_text:
        return None
    lines = [line.rstrip() for line in pane_text.splitlines()]
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or not _USAGE_LIMIT_NOTICE_RE.search(stripped):
            continue
        collected: list[str] = []
        for candidate in lines[idx:]:
            piece = candidate.strip()
            if not piece:
                if collected:
                    break
                continue
            if piece.startswith("›") and collected:
                break
            collected.append(piece)
        if not collected:
            continue
        first = collected[0]
        if first.startswith("■"):
            first = "⚠️" + first[1:]
        elif not first.startswith("⚠️"):
            first = f"⚠️ {first}"
        collected[0] = first
        return "\n".join(collected)
    return None


def _clear_runtime_presence_for_window(user_id: int, window_id: str) -> None:
    _runtime_presence.pop((user_id, window_id), None)


def mark_runtime_presence_active(
    user_id: int,
    thread_id: int | None,
    window_id: str,
) -> None:
    """Mark a control surface as having an active live runtime.

    This supplements poll-based detection for short-lived Codex turns whose
    live activity can complete between polling intervals. Once live delivery
    observes any Codex event on a bound surface, a later transition back to a
    shell prompt can be treated as a real runtime stop.
    """
    _runtime_presence[(user_id, window_id)] = True


def _last_nonempty_pane_line(pane_text: str) -> str:
    for line in reversed((pane_text or "").splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _command_basename(command: str) -> str:
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        tokens = (command or "").split()
    if not tokens:
        return ""
    return Path(tokens[0]).name.casefold()


def _codex_surface_looks_active(pane_text: str, *, pane_command: str = "") -> bool:
    """Best-effort live Codex detection from pane text.

    Production Codex panes often scroll the initial ``OpenAI Codex`` banner out
    of view while still showing the active footer/status surface (for example
    ``gpt-5.4 high · 22% left`` or ``tab to queue message``). A bare ``❯``
    can also be a normal shell prompt, so shell commands are treated as a dead
    Codex input plane regardless of stale Codex-looking scrollback.
    """
    text = pane_text or ""
    if not text.strip():
        return False
    if is_codex_termination_summary_text(text):
        return False

    surface = classify_input_surface(text)
    command_name = _command_basename(pane_command)
    if command_name in _SHELL_COMMAND_NAMES:
        return False

    if surface.kind in {"busy", "input_ready", "blocked_prompt"}:
        return True

    last_line = _last_nonempty_pane_line(text)
    if not last_line:
        return False
    if _SHELL_PROMPT_RE.match(last_line):
        return False
    return True


def _raw_discontinuity_text(
    *,
    resolved: object | None,
    pane_text: str | None,
) -> str | None:
    thread_id = str(getattr(resolved, "thread_id", "") or "").strip()
    file_path = str(getattr(resolved, "file_path", "") or "").strip()
    if thread_id and file_path:
        raw = extract_codex_termination_summary_from_rollout(
            file_path,
            thread_id=thread_id,
        )
        if raw:
            return raw
    return extract_terminal_tail_block(pane_text)


def _nonzero_exit_code(resolved: object | None) -> int | None:
    file_path = str(getattr(resolved, "file_path", "") or "").strip()
    if not file_path:
        return None
    return extract_nonzero_exit_code_from_rollout(file_path)


async def _enqueue_discontinuity_warning(
    bot: Bot,
    *,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    text: str | None,
    warning_key: str,
    chat_id: int | None = None,
    surface_key: str | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
) -> None:
    parts = [text] if text else []
    await enqueue_content_message(
        bot=bot,
        user_id=user_id,
        window_id=window_id,
        parts=parts,
        content_type="warning",
        semantic_kind=WARNING_SEMANTIC_KIND,
        text=text,
        thread_id=thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
        image_data=image_data,
        turn_generation=current_turn_generation(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        ),
        warning_key=warning_key,
    )


async def _maybe_enqueue_runtime_exit_warning(
    bot: Bot,
    *,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    pane_command: str,
    pane_text: str,
    chat_id: int | None = None,
    surface_key: str | None = None,
) -> None:
    presence_key = (user_id, window_id)
    descriptor = session_manager.get_process_descriptor(window_id)
    expected_runtime = str(
        getattr(descriptor, "runtime_kind", "") or ""
    ).strip()
    observed = _runtime_presence.get(presence_key)

    pane_command_text = pane_command if isinstance(pane_command, str) else ""
    current_runtime = runtime_capability_registry.known_runtime_kind_from_command(
        pane_command_text
    )
    runtime_active = expected_runtime == "codex" and (
        current_runtime == expected_runtime
        or _codex_surface_looks_active(pane_text, pane_command=pane_command_text)
    )

    if runtime_active:
        _runtime_presence[presence_key] = True
        return

    previously_active = bool(observed)
    _runtime_presence[presence_key] = False
    if not previously_active:
        return

    resolved = await session_manager.resolve_thread_for_window(window_id)
    raw_text = _raw_discontinuity_text(resolved=resolved, pane_text=pane_text)
    exit_code = _nonzero_exit_code(resolved)
    image_data = await build_discontinuity_image_data(pane_text)
    if raw_text is None:
        text = build_runtime_termination_fallback_summary(exit_code=exit_code)
    else:
        text = raw_text
    logger.info(
        "Runtime exit warning enqueued: user=%d window=%s thread=%s pane_command=%s surface_kind=%s last_line=%r replay_summary=%s exit_code=%s",
        user_id,
        window_id,
        thread_id,
        pane_command_text,
        classify_input_surface(pane_text).kind,
        _last_nonempty_pane_line(pane_text),
        bool(raw_text),
        exit_code,
    )
    await _enqueue_discontinuity_warning(
        bot,
        user_id=user_id,
        window_id=window_id,
        thread_id=thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
        text=text,
        image_data=image_data,
        warning_key=f"runtime-discontinuity:exit:{window_id}",
    )


async def _transition_missing_window_binding(
    bot: Bot,
    *,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    surface_key: str | None = None,
    chat_id: int | None = None,
) -> None:
    resolved = await session_manager.resolve_thread_for_window(window_id)
    resolved_thread_id = str(getattr(resolved, "thread_id", "") or "").strip()
    resolved_file_path = str(getattr(resolved, "file_path", "") or "").strip()
    resolved_runtime_kind = (
        str(getattr(resolved, "runtime_kind", "") or "codex").strip() or "codex"
    )
    resolved_summary = (
        str(getattr(resolved, "summary", "") or resolved_thread_id).strip()
        or resolved_thread_id
    )
    resolved_cwd = str(getattr(resolved, "cwd", "") or "").strip()
    replay_readable = bool(
        resolved and resolved_thread_id and resolved_file_path and Path(resolved_file_path).exists()
    )
    pane_snapshot = _last_pane_text.pop(window_id, None)
    raw_text = _raw_discontinuity_text(
        resolved=resolved,
        pane_text=pane_snapshot,
    )
    exit_code = _nonzero_exit_code(resolved)
    image_data = await build_discontinuity_image_data(pane_snapshot)

    if replay_readable and resolved is not None and resolved_thread_id:
        if thread_id is None:
            resolved_chat_id = chat_id if chat_id is not None else session_manager.resolve_chat_id(user_id, None)
            rebound_window_id = session_manager.bind_external_surface(
                user_id,
                runtime_kind=resolved_runtime_kind,
                source_thread_id=resolved_thread_id,
                summary=resolved_summary,
                cwd=resolved_cwd,
                file_path=resolved_file_path,
                read_only=True,
                surface_key=surface_key,
                chat_id=resolved_chat_id,
            )
        else:
            rebound_window_id = session_manager.bind_external_surface(
                user_id,
                runtime_kind=resolved_runtime_kind,
                source_thread_id=resolved_thread_id,
                summary=resolved_summary,
                cwd=resolved_cwd,
                file_path=resolved_file_path,
                read_only=True,
                surface_key=surface_key,
                thread_id=thread_id,
            )
        await clear_topic_state(user_id, thread_id, bot)
        if raw_text:
            await _enqueue_discontinuity_warning(
                bot,
                user_id=user_id,
                window_id=rebound_window_id,
                thread_id=thread_id,
                chat_id=chat_id,
                surface_key=surface_key,
                text=raw_text,
                image_data=image_data,
                warning_key=f"runtime-discontinuity:window-loss:{window_id}",
            )
        await _enqueue_discontinuity_warning(
            bot,
            user_id=user_id,
            window_id=rebound_window_id,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            text=(
                build_live_surface_loss_notice(
                    window_name=session_manager.get_display_name(window_id),
                    replay_readable=replay_readable,
                )
                if raw_text
                else build_live_surface_loss_fallback_summary(
                    window_name=session_manager.get_display_name(window_id),
                    replay_readable=replay_readable,
                    exit_code=exit_code,
                )
            ),
            image_data=None if raw_text else image_data,
            warning_key=f"runtime-discontinuity:window-loss-notice:{window_id}",
        )
        logger.info(
            "Converted stale tmux binding to external continuity: user=%d thread=%s window_id=%s external=%s replay_readable=%s",
            user_id,
            thread_id,
            window_id,
            rebound_window_id,
            replay_readable,
        )
        _clear_runtime_presence_for_window(user_id, window_id)
        return

    resolved_chat_id = chat_id if thread_id is None and chat_id is not None else session_manager.resolve_chat_id(user_id, thread_id)
    if image_data:
        await send_photo(
            bot,
            resolved_chat_id,
            image_data,
            **({"message_thread_id": thread_id} if thread_id is not None else {}),
        )
    await send_with_fallback(
        bot,
        resolved_chat_id,
        build_live_surface_loss_fallback_summary(
            window_name=session_manager.get_display_name(window_id),
            replay_readable=False,
            exit_code=exit_code,
        ),
        **({"message_thread_id": thread_id} if thread_id is not None else {}),
    )
    if thread_id is None:
        if surface_key is not None:
            session_manager.unbind_surface(user_id, surface_key=surface_key)
        else:
            resolved_chat_id = chat_id if chat_id is not None else session_manager.resolve_chat_id(user_id, None)
            if resolved_chat_id != user_id:
                session_manager.unbind_surface(user_id, chat_id=resolved_chat_id)
            else:
                logger.warning(
                    "Unable to resolve chat id for stale main-chat binding "
                    "(user=%d, window_id=%s)",
                    user_id,
                    window_id,
                )
    else:
        if surface_key is not None:
            session_manager.unbind_surface(user_id, surface_key=surface_key)
        else:
            session_manager.unbind_thread(user_id, thread_id)
    await clear_topic_state(user_id, thread_id, bot)
    _clear_runtime_presence_for_window(user_id, window_id)
    logger.info(
        "Detached stale binding with no replay continuity: user=%d thread=%s window_id=%s",
        user_id,
        thread_id,
        window_id,
    )


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    chat_id: int | None = None,
    surface_key: str | None = None,
    skip_status: bool = False,
) -> None:
    """Poll terminal and check for interactive UIs and status updates.

    UI detection always happens regardless of skip_status. When skip_status=True,
    only UI detection runs (used when message queue is non-empty to avoid
    flooding the queue with status updates).

    Also detects permission prompt UIs (not triggered via JSONL) and enters
    interactive mode when found.
    """
    turn_generation = current_turn_generation(
        user_id,
        thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    surface_identity_kwargs: dict[str, object] = {}
    if chat_id is not None:
        surface_identity_kwargs["chat_id"] = chat_id
    if surface_key:
        surface_identity_kwargs["surface_key"] = surface_key
    if session_manager.is_external_binding_window_id(window_id) is True:
        _clear_runtime_presence_for_window(user_id, window_id)
        if not skip_status:
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                None,
                thread_id=thread_id,
                chat_id=chat_id,
                surface_key=surface_key,
                turn_generation=turn_generation,
            )
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        _clear_runtime_presence_for_window(user_id, window_id)
        # Window gone, enqueue clear (unless skipping status)
        if not skip_status:
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                None,
                thread_id=thread_id,
                chat_id=chat_id,
                surface_key=surface_key,
                turn_generation=turn_generation,
            )
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        # Transient capture failure - keep existing status message
        return
    _last_pane_text[window_id] = pane_text
    surface = classify_input_surface(pane_text)
    update_window_input_safety_snapshot(
        window_id=window_id,
        input_surface_kind=surface.kind,
        active_question_state="unknown",
        source="status_poll",
    )
    pending_input_text = _build_pending_input_text(pane_text)
    await enqueue_pending_input_update(
        bot,
        user_id,
        window_id,
        pending_input_text,
        thread_id=thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )


    terminal_control = extract_terminal_control_observation(pane_text)
    if terminal_control and not skip_status:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            terminal_control.text,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
            content_type=TERMINAL_CONTROL_PANEL_CONTENT_TYPE,
            semantic_kind=TERMINAL_CONTROL_SEMANTIC_KIND,
        )
        return

    if not skip_status:
        cwd = str(getattr(w, "cwd", "") or "").strip()
        omx_workflow_status = (
            read_omx_workflow_status(Path(cwd), pane_text=pane_text)
            if cwd
            else parse_omx_statusline(pane_text)
        )
        if omx_workflow_status is not None:
            await enqueue_status_update(
                bot,
                user_id,
                window_id,
                render_omx_workflow_status(omx_workflow_status),
                thread_id=thread_id,
                chat_id=chat_id,
                surface_key=surface_key,
                turn_generation=turn_generation,
                content_type=OMX_WORKFLOW_PANEL_CONTENT_TYPE,
                semantic_kind=OMX_WORKFLOW_STATUS_SEMANTIC_KIND,
            )
            return

    usage_limit_notice = _extract_usage_limit_notice(pane_text)
    if usage_limit_notice:
        await clear_interactive_msg(
            user_id,
            bot,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )
        await enqueue_content_message(
            bot=bot,
            user_id=user_id,
            window_id=window_id,
            parts=[usage_limit_notice],
            content_type="warning",
            semantic_kind=WARNING_SEMANTIC_KIND,
            text=usage_limit_notice,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
            warning_key=f"usage-limit:{window_id}",
        )
        return

    await _maybe_enqueue_runtime_exit_warning(
        bot,
        user_id=user_id,
        window_id=window_id,
        thread_id=thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
        pane_command=getattr(w, "pane_current_command", ""),
        pane_text=pane_text,
    )

    turn_generation = current_turn_generation(
        user_id,
        thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    pre_final_lane_open = (
        turn_generation > 0
        and not is_pre_final_visible_lane_closed(
            user_id,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )
    )
    send_missing_question = not skip_status and not pre_final_lane_open
    question_kwargs: dict[str, object] = {
        "send_if_missing": send_missing_question,
    }
    if chat_id is not None:
        question_kwargs["chat_id"] = chat_id
    if skip_status:
        question_kwargs["defer_reason"] = "queue_not_empty"
    elif pre_final_lane_open:
        question_kwargs["defer_reason"] = "pre_final_lane_open"

    if await handle_omx_question_ui(
        bot,
        user_id,
        window_id,
        thread_id,
        **question_kwargs,
    ):
        update_window_input_safety_snapshot(
            window_id=window_id,
            input_surface_kind=surface.kind,
            active_question_state="active",
            source="question_record_scan",
        )
        return
    update_window_input_safety_snapshot(
        window_id=window_id,
        input_surface_kind=surface.kind,
        active_question_state="none",
        source="question_record_scan",
    )

    interactive_window = get_interactive_window(
        user_id,
        thread_id,
        chat_id=chat_id,
        surface_key=surface_key,
    )
    should_check_new_ui = True

    if interactive_window == window_id:
        # User is in interactive mode for THIS window
        if surface.kind == "blocked_prompt":
            # Interactive UI still showing — skip status update (user is interacting)
            return
        # Interactive UI gone — clear interactive mode, fall through to status check.
        # Don't re-check for new UI this cycle (the old one just disappeared).
        await clear_interactive_msg(
            user_id,
            bot,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )
        should_check_new_ui = False
    elif interactive_window is not None:
        # User is in interactive mode for a DIFFERENT window (window switched)
        # Clear stale interactive mode
        await clear_interactive_msg(
            user_id,
            bot,
            thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
        )

    # Check for blocked prompt surfaces (interactive UI or prompt-visible errors).
    if should_check_new_ui and surface.kind == "blocked_prompt":
        logger.debug(
            "Blocked prompt detected in polling (user=%d, window=%s, thread=%s)",
            user_id,
            window_id,
            thread_id,
        )
        await handle_interactive_ui(
            bot,
            user_id,
            window_id,
            thread_id,
            **surface_identity_kwargs,
        )
        return

    # Normal status line check — skip if queue is non-empty
    if skip_status:
        return

    if surface.kind == "busy" and surface.status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            surface.status_line,
            thread_id=thread_id,
            chat_id=chat_id,
            surface_key=surface_key,
            turn_generation=turn_generation,
        )
    # If no status line, keep existing status message (don't clear on transient state)


async def status_poll_loop(bot: Bot) -> None:
    """Background task to poll terminal status for all thread-bound windows."""
    logger.info("Status polling started (interval: %ss)", STATUS_POLL_INTERVAL)
    last_topic_check = 0.0
    while True:
        try:
            # Periodic topic existence probe
            now = time.monotonic()
            if now - last_topic_check >= TOPIC_CHECK_INTERVAL:
                last_topic_check = now
                for binding in list(session_manager.iter_topic_bindings()):
                    user_id = binding.user_id
                    thread_id = binding.thread_id
                    wid = binding.window_id
                    surface_key = getattr(binding, "surface_key", "") or None
                    chat_id = getattr(binding, "chat_id", None)
                    try:
                        if thread_id is None:
                            continue
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=chat_id
                            if chat_id is not None
                            else session_manager.resolve_chat_id(user_id, thread_id),
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if "Topic_id_invalid" in str(e):
                            # Topic deleted — kill window, unbind, and clean up state
                            w = await tmux_manager.find_window_by_id(wid)
                            if w:
                                await tmux_manager.kill_window(w.window_id)
                            if surface_key is not None:
                                session_manager.unbind_surface(
                                    user_id,
                                    surface_key=surface_key,
                                )
                            else:
                                session_manager.unbind_thread(user_id, thread_id)
                            await clear_topic_state(user_id, thread_id, bot)
                            logger.info(
                                "Topic deleted: killed window_id '%s' and "
                                "unbound thread %d for user %d",
                                wid,
                                thread_id,
                                user_id,
                            )
                        else:
                            logger.debug(
                                "Topic probe error for %s: %s",
                                wid,
                                e,
                            )
                    except Exception as e:
                        logger.debug(
                            "Topic probe error for %s: %s",
                            wid,
                            e,
                        )

            for binding in list(session_manager.iter_topic_bindings()):
                user_id = binding.user_id
                thread_id = binding.thread_id
                wid = binding.window_id
                surface_key = getattr(binding, "surface_key", None)
                chat_id = getattr(binding, "chat_id", None)
                try:
                    if session_manager.is_external_binding_window_id(wid) is True:
                        _clear_runtime_presence_for_window(user_id, wid)
                        continue
                    # Clean up stale bindings (window no longer exists)
                    w = await tmux_manager.find_window_by_id(wid)
                    if not w:
                        if surface_key is None or chat_id is None:
                            (
                                resolved_surface_key,
                                resolved_chat_id,
                                resolved_thread_id,
                            ) = session_manager.get_surface_coordinates_for_window(
                                user_id,
                                wid,
                            )
                            surface_key = surface_key or resolved_surface_key
                            chat_id = chat_id if chat_id is not None else resolved_chat_id
                        else:
                            resolved_thread_id = None
                        await _transition_missing_window_binding(
                            bot,
                            user_id=user_id,
                            thread_id=thread_id if thread_id is not None else resolved_thread_id,
                            window_id=wid,
                            surface_key=surface_key,
                            chat_id=chat_id,
                        )
                        continue

                    # UI detection happens unconditionally in update_status_message.
                    # Status enqueue is skipped inside update_status_message when
                    # interactive UI is detected (returns early) or when queue is non-empty.
                    queue = get_message_queue(user_id)
                    skip_status = queue is not None and not queue.empty()

                    await update_status_message(
                        bot,
                        user_id,
                        wid,
                        thread_id=thread_id,
                        chat_id=chat_id,
                        surface_key=surface_key,
                        skip_status=skip_status,
                    )
                except Exception as e:
                    logger.debug(
                        f"Status update error for user {user_id} "
                        f"thread {thread_id}: {e}"
                    )
        except Exception as e:
            logger.error(f"Status poll loop error: {e}")

        await asyncio.sleep(STATUS_POLL_INTERVAL)
