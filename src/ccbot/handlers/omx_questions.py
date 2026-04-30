"""Telegram bridge for OMX-owned blocking question records.

OMX 0.15.x renders questions in a temporary tmux pane, but the durable
contract is the JSON record under ``.omx/state/.../questions``.  The bot uses
that record as the source of truth so Telegram users can answer the same
question even when the split pane is not visible in the captured window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..session import session_manager
from ..tmux_manager import TmuxWindow, tmux_manager
from .callback_data import (
    CB_OMX_QUESTION_REFRESH,
    CB_OMX_QUESTION_SELECT,
    CB_OMX_QUESTION_SUBMIT,
    CB_OMX_QUESTION_TOGGLE,
)
from .message_sender import NO_LINK_PREVIEW

logger = logging.getLogger(__name__)

_QUESTION_KIND = "omx.question/v1"
_ACTIVE_STATUSES = frozenset({"pending", "prompting"})
_PANE_ID_RE = re.compile(r"^%\d+$")
_MAX_QUESTION_AGE_SECONDS = 24 * 60 * 60
_MAX_TEXT_CHARS = 3600

_question_msgs: dict[tuple[int, int], int] = {}
_question_windows: dict[tuple[int, int], str] = {}
_question_selections: dict[tuple[int, int, str], set[int]] = {}
_question_render_state: dict[tuple[int, int], tuple[str, tuple[int, ...]]] = {}


@dataclass(frozen=True)
class OmxQuestionOption:
    label: str
    value: str
    description: str = ""


@dataclass(frozen=True)
class OmxQuestionRecord:
    path: Path
    question_id: str
    status: str
    header: str
    question: str
    options: tuple[OmxQuestionOption, ...]
    allow_other: bool
    other_label: str
    multi_select: bool
    source: str
    renderer: dict[str, Any]
    updated_at: str

    @property
    def short_id(self) -> str:
        return self.question_id[-8:] if len(self.question_id) >= 8 else self.question_id


def _thread_key(thread_id: int | None) -> int:
    return thread_id or 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _clip(text: str, *, max_chars: int = _MAX_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _parse_dt(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _recent_enough(record: OmxQuestionRecord) -> bool:
    dt = _parse_dt(record.updated_at)
    if dt is None:
        return True
    return (datetime.now(UTC) - dt).total_seconds() <= _MAX_QUESTION_AGE_SECONDS


def _load_question_record(path: Path) -> OmxQuestionRecord | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to read OMX question record: %s", path, exc_info=True)
        return None
    if not isinstance(payload, dict) or payload.get("kind") != _QUESTION_KIND:
        return None
    question_id = _safe_str(payload.get("question_id")).strip()
    question = _safe_str(payload.get("question")).strip()
    status = _safe_str(payload.get("status")).strip()
    if not question_id or not question or not status:
        return None
    raw_options = payload.get("options") if isinstance(payload.get("options"), list) else []
    options: list[OmxQuestionOption] = []
    for index, raw in enumerate(raw_options):
        if isinstance(raw, str):
            label = raw.strip()
            value = label
            description = ""
        elif isinstance(raw, dict):
            label = _safe_str(raw.get("label")).strip()
            value = _safe_str(raw.get("value")).strip() or label
            description = _safe_str(raw.get("description")).strip()
        else:
            continue
        if label and value:
            options.append(
                OmxQuestionOption(label=label, value=value, description=description)
            )
        else:
            logger.debug("Skipping invalid OMX question option %s in %s", index, path)
    renderer = payload.get("renderer") if isinstance(payload.get("renderer"), dict) else {}
    return OmxQuestionRecord(
        path=path,
        question_id=question_id,
        status=status,
        header=_safe_str(payload.get("header")).strip(),
        question=question,
        options=tuple(options),
        allow_other=payload.get("allow_other") is not False,
        other_label=_safe_str(payload.get("other_label")).strip() or "Other",
        multi_select=bool(payload.get("multi_select"))
        or payload.get("type") == "multi-answerable",
        source=_safe_str(payload.get("source")).strip(),
        renderer=renderer,
        updated_at=_safe_str(payload.get("updated_at")).strip(),
    )


def _candidate_question_paths(cwd: str) -> list[Path]:
    root = Path(cwd)
    if not root.is_dir():
        return []
    state_root = root / ".omx" / "state" / "sessions"
    if not state_root.is_dir():
        return []
    return sorted(
        state_root.glob("*/questions/question-*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )


def _pane_matches_window(record: OmxQuestionRecord, window: TmuxWindow) -> bool:
    pane_id = _safe_str(getattr(window, "pane_id", "")).strip()
    if not pane_id:
        return True
    renderer_target = _safe_str(record.renderer.get("target")).strip()
    return_target = _safe_str(record.renderer.get("return_target")).strip()
    if renderer_target and renderer_target == pane_id:
        return True
    if return_target and return_target == pane_id:
        return True
    # Pending records can exist before the renderer is attached; cwd + recency
    # are the only durable linkage available in that short interval.
    return not renderer_target and not return_target


def find_active_omx_question(
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
) -> OmxQuestionRecord | None:
    """Return the newest active OMX question for a tmux window cwd."""
    suffix = (question_suffix or "").strip()
    for path in _candidate_question_paths(window.cwd):
        record = _load_question_record(path)
        if record is None:
            continue
        if record.status not in _ACTIVE_STATUSES:
            continue
        if suffix and record.short_id != suffix and record.question_id != suffix:
            continue
        if not _recent_enough(record):
            continue
        if not _pane_matches_window(record, window):
            continue
        return record
    return None


def _answer_payload(
    record: OmxQuestionRecord, selected_indices: list[int]
) -> dict[str, Any]:
    selected_options = [record.options[index] for index in selected_indices]
    selected_labels = [option.label for option in selected_options]
    selected_values = [option.value for option in selected_options]
    if record.multi_select:
        return {
            "kind": "multi",
            "value": selected_values,
            "selected_labels": selected_labels,
            "selected_values": selected_values,
        }
    selected = selected_options[0]
    return {
        "kind": "option",
        "value": selected.value,
        "selected_labels": [selected.label],
        "selected_values": [selected.value],
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"OMX question record is not an object: {path}")
    return payload


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _injection_text(answer: dict[str, Any]) -> str:
    value = answer.get("value")
    if isinstance(value, list):
        raw = ", ".join(str(item) for item in value)
    else:
        raw = str(value or "")
    raw = " ".join(raw.split())
    return f"[omx question answered] {raw}".strip()


async def _tmux_send_line(target: str, text: str) -> None:
    if not _PANE_ID_RE.match(target):
        return
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "send-keys",
        "-t",
        target,
        "-l",
        text,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    if proc.returncode != 0:
        return
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "send-keys",
        "-t",
        target,
        "Enter",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def _tmux_kill_pane(target: str, *, return_target: str = "") -> None:
    if not _PANE_ID_RE.match(target) or target == return_target:
        return
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-pane",
        "-t",
        target,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def answer_omx_question(
    record: OmxQuestionRecord, selected_indices: list[int]
) -> dict[str, Any]:
    if not selected_indices:
        raise ValueError("No OMX question option selected")
    if any(index < 0 or index >= len(record.options) for index in selected_indices):
        raise ValueError("OMX question option is out of range")
    if not record.multi_select and len(selected_indices) != 1:
        raise ValueError("Single-answerable OMX question needs exactly one option")

    answer = _answer_payload(record, selected_indices)
    payload = _read_json_object(record.path)
    payload["status"] = "answered"
    payload["updated_at"] = _now_iso()
    payload["answer"] = answer
    payload.pop("error", None)
    _write_json_object(record.path, payload)

    transport = _safe_str(record.renderer.get("return_transport")).strip()
    return_target = _safe_str(record.renderer.get("return_target")).strip()
    if transport == "tmux-send-keys" and return_target:
        await _tmux_send_line(return_target, _injection_text(answer))

    renderer_target = _safe_str(record.renderer.get("target")).strip()
    if (
        _safe_str(record.renderer.get("renderer")).strip() == "tmux-pane"
        and renderer_target
    ):
        await _tmux_kill_pane(renderer_target, return_target=return_target)
    return answer


def _build_question_text(
    record: OmxQuestionRecord, selected: set[int] | None = None
) -> str:
    selected = selected or set()
    lines = ["❓ OMX Question"]
    if record.header:
        lines.extend(["", record.header])
    lines.extend(["", record.question.strip(), ""])
    if record.options:
        lines.append("Options:")
        for index, option in enumerate(record.options, start=1):
            marker = "☑" if index - 1 in selected else "☐"
            prefix = marker if record.multi_select else f"{index}."
            lines.append(f"{prefix} {option.label}")
            if option.description:
                lines.append(f"   {option.description}")
    if record.allow_other:
        lines.append("")
        lines.append(f"Other is available in the tmux UI as: {record.other_label}")
    if record.multi_select:
        lines.append("")
        lines.append("Select one or more options, then tap Submit.")
    if record.source:
        lines.append(f"\nsource: {record.source}")
    return _clip("\n".join(lines).strip())


def _callback_payload(
    prefix: str,
    record: OmxQuestionRecord,
    window_id: str,
    index: int | None = None,
) -> str:
    parts = [prefix]
    if index is not None:
        parts.append(str(index))
    parts.extend([record.short_id, window_id])
    payload = ":".join(parts)
    if len(payload) > 64:
        raise ValueError(
            f"OMX question callback payload exceeds Telegram limit: {payload!r}"
        )
    return payload


def _build_question_keyboard(
    record: OmxQuestionRecord,
    window_id: str,
    *,
    selected: set[int] | None = None,
) -> InlineKeyboardMarkup:
    selected = selected or set()
    rows: list[list[InlineKeyboardButton]] = []
    if record.multi_select:
        for index, option in enumerate(record.options):
            marker = "☑" if index in selected else "☐"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{marker} {index + 1}. {option.label}"[:64],
                        callback_data=_callback_payload(
                            CB_OMX_QUESTION_TOGGLE,
                            record,
                            window_id,
                            index,
                        ),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Submit",
                    callback_data=_callback_payload(
                        CB_OMX_QUESTION_SUBMIT, record, window_id
                    ),
                ),
                InlineKeyboardButton(
                    "🔄",
                    callback_data=_callback_payload(
                        CB_OMX_QUESTION_REFRESH, record, window_id
                    ),
                ),
            ]
        )
    else:
        for index, option in enumerate(record.options):
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{index + 1}. {option.label}"[:64],
                        callback_data=_callback_payload(
                            CB_OMX_QUESTION_SELECT,
                            record,
                            window_id,
                            index,
                        ),
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄",
                    callback_data=_callback_payload(
                        CB_OMX_QUESTION_REFRESH, record, window_id
                    ),
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


async def clear_omx_question_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    key = (user_id, _thread_key(thread_id))
    msg_id = _question_msgs.pop(key, None)
    _question_windows.pop(key, None)
    _question_render_state.pop(key, None)
    for selection_key in [item for item in _question_selections if item[:2] == key]:
        _question_selections.pop(selection_key, None)
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            logger.debug("Failed to delete OMX question message", exc_info=True)


async def handle_omx_question_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    record: OmxQuestionRecord | None = None,
) -> bool:
    """Send/update an OMX question prompt for the bound window, if active."""
    window = await tmux_manager.find_window_by_id(window_id)
    if not window:
        await clear_omx_question_msg(user_id, bot, thread_id)
        return False
    if record is None:
        record = find_active_omx_question(window)
    key = (user_id, _thread_key(thread_id))
    if record is None:
        if _question_msgs.get(key):
            await clear_omx_question_msg(user_id, bot, thread_id)
        return False

    selection_key = (*key, record.question_id)
    selected = (
        _question_selections.get(selection_key, set()) if record.multi_select else set()
    )
    text = _build_question_text(record, selected)
    keyboard = _build_question_keyboard(record, window_id, selected=selected)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    thread_kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
    existing_msg_id = _question_msgs.get(key)
    render_state = (record.question_id, tuple(sorted(selected)))
    if existing_msg_id:
        if _question_render_state.get(key) == render_state:
            return True
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            _question_windows[key] = window_id
            _question_render_state[key] = render_state
            return True
        except Exception:
            logger.debug("Failed to edit OMX question message", exc_info=True)
            _question_msgs.pop(key, None)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception:
        logger.error("Failed to send OMX question message", exc_info=True)
        return False
    if sent:
        _question_msgs[key] = sent.message_id
        _question_windows[key] = window_id
        _question_render_state[key] = render_state
        return True
    return False


def _parse_question_callback(data: str) -> tuple[str, int | None, str, str] | None:
    for prefix in (
        CB_OMX_QUESTION_SELECT,
        CB_OMX_QUESTION_TOGGLE,
        CB_OMX_QUESTION_SUBMIT,
        CB_OMX_QUESTION_REFRESH,
    ):
        marker = f"{prefix}:"
        if not data.startswith(marker):
            continue
        tail = data[len(marker) :]
        parts = tail.split(":")
        if prefix in {CB_OMX_QUESTION_SELECT, CB_OMX_QUESTION_TOGGLE}:
            if len(parts) != 3:
                return None
            try:
                index = int(parts[0])
            except ValueError:
                return None
            return prefix, index, parts[1], parts[2]
        if len(parts) != 2:
            return None
        return prefix, None, parts[0], parts[1]
    return None


def _thread_id_from_update(update: Update) -> int | None:
    message = update.callback_query.message if update.callback_query else None
    value = getattr(message, "message_thread_id", None)
    return value if isinstance(value, int) else None


async def handle_omx_question_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    query = update.callback_query
    user = update.effective_user
    if not query or not query.data or not user:
        return False
    parsed = _parse_question_callback(query.data)
    if parsed is None:
        return False
    action, index, question_suffix, window_id = parsed
    thread_id = _thread_id_from_update(update)
    window = await tmux_manager.find_window_by_id(window_id)
    if not window:
        await query.answer("Window not found", show_alert=True)
        return True
    record = find_active_omx_question(window, question_suffix=question_suffix)
    if record is None:
        await query.answer("Question is no longer active", show_alert=True)
        await clear_omx_question_msg(user.id, context.bot, thread_id)
        return True

    key = (user.id, _thread_key(thread_id), record.question_id)
    if action == CB_OMX_QUESTION_REFRESH:
        await handle_omx_question_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            record=record,
        )
        await query.answer("Refreshed")
        return True

    if action == CB_OMX_QUESTION_TOGGLE:
        if index is None or index < 0 or index >= len(record.options):
            await query.answer("Invalid option", show_alert=True)
            return True
        selected = set(_question_selections.get(key, set()))
        if index in selected:
            selected.remove(index)
        else:
            selected.add(index)
        _question_selections[key] = selected
        await handle_omx_question_ui(
            context.bot,
            user.id,
            window_id,
            thread_id,
            record=record,
        )
        await query.answer("Updated")
        return True

    if action == CB_OMX_QUESTION_SUBMIT:
        selected = sorted(_question_selections.get(key, set()))
        if not selected:
            await query.answer("Select at least one option", show_alert=True)
            return True
    else:
        if index is None or index < 0 or index >= len(record.options):
            await query.answer("Invalid option", show_alert=True)
            return True
        selected = [index]

    answer = await answer_omx_question(record, selected)
    selected_labels = answer.get("selected_labels")
    if isinstance(selected_labels, list):
        label_text = ", ".join(str(label) for label in selected_labels)
    else:
        label_text = str(answer.get("value", ""))
    text = f"✅ OMX Question answered\n\n{record.question}\n\nAnswer: {label_text}".strip()
    try:
        await query.edit_message_text(
            text=_clip(text),
            reply_markup=None,
            link_preview_options=NO_LINK_PREVIEW,
        )
    except Exception:
        logger.debug("Failed to edit answered OMX question message", exc_info=True)
    await query.answer("Answered")
    await clear_omx_question_msg(user.id, None, thread_id)
    _question_selections.pop(key, None)
    return True
