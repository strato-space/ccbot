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
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..delivery_audit import log_telegram_delivery
from ..session import session_manager
from ..tmux_manager import TmuxWindow, tmux_manager
from .callback_data import (
    CB_OMX_QUESTION_REFRESH,
    CB_OMX_QUESTION_SELECT,
    CB_OMX_QUESTION_SUBMIT,
    CB_OMX_QUESTION_TOGGLE,
)
from .message_sender import NO_LINK_PREVIEW
from .message_queue import open_new_turn_generation

logger = logging.getLogger(__name__)

_QUESTION_KIND = "omx.question/v1"
_ACTIVE_STATUSES = frozenset({"pending", "prompting"})
_TERMINAL_STATUSES = frozenset({"answered", "aborted", "error"})
_ERROR_STATUSES = frozenset({"error"})
_PANE_ID_RE = re.compile(r"^%\d+$")
_MAX_QUESTION_AGE_SECONDS = 24 * 60 * 60
_MAX_TEXT_CHARS = 3600
_STATE_PATH_FLAG = "--state-path"
_QUESTION_STATE_FILES = (
    "deep-interview-state.json",
    "ralplan-state.json",
    "ralph-state.json",
)

_QuestionKey = tuple[int, str]
_QuestionSelectionKey = tuple[int, str, str]

_question_msgs: dict[_QuestionKey, int] = {}
_question_windows: dict[_QuestionKey, str] = {}
_question_selections: dict[_QuestionSelectionKey, set[int]] = {}
_question_render_state: dict[_QuestionKey, tuple[str, tuple[int, ...]]] = {}
_question_prompt_deferrals: dict[tuple[int, str, str], float] = {}
_question_prompt_defer_audited: set[tuple[int, str, str, str]] = set()
_QUESTION_PROMPT_GATE_TIMEOUT_SECONDS = 45.0
QUESTION_PROMPT_TASK_TYPE = "question_prompt"
QUESTION_PROMPT_CONTENT_TYPE = "question"
QUESTION_PROMPT_SEMANTIC_KIND = "interactive_question"


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


def _question_surface_key(
    user_id: int,
    *,
    thread_id: int | None = None,
    chat_id: int | None = None,
    window_id: str = "",
) -> _QuestionKey:
    """Return the control-surface-aware key for a Telegram question artifact."""
    if window_id:
        surface_key, _, _ = session_manager.get_surface_coordinates_for_window(
            user_id,
            window_id,
        )
        if surface_key:
            return user_id, surface_key
    if thread_id is not None:
        return user_id, session_manager.make_surface_key(thread_id=thread_id)
    if chat_id is not None:
        return user_id, session_manager.make_surface_key(chat_id=chat_id)
    # Compatibility fallback for isolated unit tests and old private-chat state
    # where no chat coordinate was captured with the callback.
    return user_id, f"legacy:{_thread_key(thread_id)}"


def _drop_question_tracking(key: _QuestionKey) -> None:
    _question_msgs.pop(key, None)
    _question_windows.pop(key, None)
    _question_render_state.pop(key, None)
    for deferral_key in list(_question_prompt_deferrals):
        if deferral_key[0] == key[0] and deferral_key[1] == key[1]:
            _question_prompt_deferrals.pop(deferral_key, None)
    for audit_key in list(_question_prompt_defer_audited):
        if audit_key[0] == key[0] and audit_key[1] == key[1]:
            _question_prompt_defer_audited.discard(audit_key)
    for selection_key in [
        item for item in _question_selections if item[0] == key[0] and item[1] == key[1]
    ]:
        _question_selections.pop(selection_key, None)


def _is_message_not_modified_error(exc: Exception) -> bool:
    """Return whether Telegram reported an idempotent edit no-op."""
    return "message is not modified" in str(exc).casefold()


def _is_question_replacement_edit_error(exc: Exception) -> bool:
    """Return whether an edit failure means a replacement prompt is appropriate."""
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "message to edit not found",
            "message not found",
            "message can't be edited",
            "message cannot be edited",
            "message is inaccessible",
            "message_id_invalid",
        )
    )


def _question_deferral_key(
    key: _QuestionKey,
    record: OmxQuestionRecord,
) -> tuple[int, str, str]:
    return (key[0], key[1], record.question_id)


def _clear_question_deferral(
    key: _QuestionKey,
    question_id: str,
) -> None:
    deferral_key = (key[0], key[1], question_id)
    _question_prompt_deferrals.pop(deferral_key, None)
    for audit_key in list(_question_prompt_defer_audited):
        if audit_key[:3] == deferral_key:
            _question_prompt_defer_audited.discard(audit_key)


def has_visible_omx_question_artifact(
    user_id: int,
    *,
    thread_id: int | None = None,
    chat_id: int | None = None,
    window_id: str = "",
    question_id: str | None = None,
) -> bool:
    """Return whether Telegram already has a visible artifact for this question."""
    key = _question_surface_key(
        user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=window_id,
    )
    if key not in _question_msgs:
        return False
    if question_id is None:
        return True
    render_state = _question_render_state.get(key)
    return render_state is not None and render_state[0] == question_id


def _audit_question_artifact(
    *,
    action: str,
    user_id: int,
    chat_id: int | None,
    thread_id: int | None,
    window_id: str,
    record: OmxQuestionRecord,
    text: str,
    message_id: int | None = None,
    success: bool = True,
    error: str | None = None,
    reason: str | None = None,
) -> None:
    audit_text = f"question_id={record.question_id}\n{text}".strip()
    log_telegram_delivery(
        action=action,
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        message_id=message_id,
        window_id=window_id,
        task_type=QUESTION_PROMPT_TASK_TYPE,
        content_type=QUESTION_PROMPT_CONTENT_TYPE,
        semantic_kind=QUESTION_PROMPT_SEMANTIC_KIND,
        text=audit_text,
        success=success,
        error=error,
        reason=reason,
    )


def _should_override_question_deferral_for_timeout(
    key: _QuestionKey,
    record: OmxQuestionRecord,
    *,
    defer_reason: str | None,
) -> bool:
    if defer_reason != "pre_final_lane_open":
        return False
    first_seen = _question_prompt_deferrals.get(_question_deferral_key(key, record))
    return (
        first_seen is not None
        and time.monotonic() - first_seen >= _QUESTION_PROMPT_GATE_TIMEOUT_SECONDS
    )


def _record_question_prompt_deferral(
    *,
    key: _QuestionKey,
    user_id: int,
    chat_id: int | None,
    thread_id: int | None,
    window_id: str,
    record: OmxQuestionRecord,
    text: str,
    reason: str | None,
) -> None:
    resolved_reason = reason or "send_if_missing_false"
    deferral_key = _question_deferral_key(key, record)
    _question_prompt_deferrals.setdefault(deferral_key, time.monotonic())
    audit_key = (*deferral_key, resolved_reason)
    if audit_key in _question_prompt_defer_audited:
        return
    _question_prompt_defer_audited.add(audit_key)
    _audit_question_artifact(
        action="suppress",
        user_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        window_id=window_id,
        record=record,
        text=text,
        reason=resolved_reason,
    )


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
    renderer = _renderer_with_state_return_bridge(path, renderer)
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


def _session_state_dir_for_question(path: Path) -> Path | None:
    """Return the state directory that owns a session-scoped question path."""
    # .../.omx/state/sessions/<session-id>/questions/question-*.json
    if path.parent.name != "questions":
        return None
    session_state_dir = path.parent.parent
    if not session_state_dir.is_dir():
        return None
    if session_state_dir.parent.name != "sessions":
        return None
    return session_state_dir


def _session_id_for_question(path: Path) -> str:
    session_state_dir = _session_state_dir_for_question(path)
    return session_state_dir.name if session_state_dir is not None else ""


def _renderer_with_state_return_bridge(
    path: Path,
    renderer: dict[str, Any],
) -> dict[str, Any]:
    """Fill a missing return bridge from session-scoped OMX mode state.

    A Codex/OMX runtime can write an ``error`` question record before the
    renderer pane exists (for example stale ``OMX_QUESTION_RETURN_PANE`` or a
    misleading "no attached client" failure).  In that case there is no
    renderer payload, but the session mode state may still contain the current
    same-window leader pane.  Telegram can safely use that pane as a
    best-effort return bridge, provided later window matching proves it belongs
    to the currently bound tmux window.
    """
    if _safe_str(renderer.get("return_target")).strip():
        return dict(renderer)
    session_state_dir = _session_state_dir_for_question(path)
    if session_state_dir is None:
        return dict(renderer)
    for filename in _QUESTION_STATE_FILES:
        try:
            payload = json.loads((session_state_dir / filename).read_text("utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        pane_id = _safe_str(payload.get("tmux_pane_id")).strip()
        if not _PANE_ID_RE.match(pane_id):
            continue
        enriched = dict(renderer)
        enriched.setdefault("return_transport", "tmux-send-keys")
        enriched["return_target"] = pane_id
        window_id = _safe_str(payload.get("tmux_window_id")).strip()
        if window_id:
            enriched["return_window_id"] = window_id
        enriched["return_source"] = filename
        return enriched
    return dict(renderer)


def _candidate_question_paths(cwd: str) -> list[Path]:
    root = Path(cwd)
    if not root.is_dir():
        return []
    state_root = root / ".omx" / "state"
    candidates: list[Path] = []
    root_questions = state_root / "questions"
    if root_questions.is_dir():
        candidates.extend(root_questions.glob("question-*.json"))
    sessions_root = state_root / "sessions"
    if sessions_root.is_dir():
        candidates.extend(sessions_root.glob("*/questions/question-*.json"))
    return sorted(
        {path for path in candidates},
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )


async def _list_pane_processes(window_id: str) -> list[tuple[str, int]]:
    """Return pane ids and owning PIDs for panes in a tmux window."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-t",
            window_id,
            "-F",
            "#{pane_id}\t#{pane_pid}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except Exception:
        logger.debug("Failed to list tmux pane processes for %s", window_id, exc_info=True)
        return []
    if proc.returncode != 0:
        return []
    panes: list[tuple[str, int]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        pane_id, sep, pid_text = line.partition("\t")
        if not sep or not _PANE_ID_RE.match(pane_id):
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid > 0:
            panes.append((pane_id, pid))
    return panes


def _cmdline_for_pid(pid: int) -> list[str]:
    """Read a process command line without invoking a shell."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _env_for_pid(pid: int) -> dict[str, str]:
    """Read a process environment without invoking a shell."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for part in raw.split(b"\0"):
        if not part or b"=" not in part:
            continue
        key, value = part.split(b"=", 1)
        try:
            result[key.decode("utf-8", errors="replace")] = value.decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            continue
    return result


def _child_pids(pid: int) -> list[int]:
    """Return direct child PIDs using procfs."""
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text("utf-8")
    except OSError:
        return []
    result: list[int] = []
    for item in raw.split():
        try:
            child = int(item)
        except ValueError:
            continue
        if child > 0:
            result.append(child)
    return result


def _descendant_pids(root_pid: int, *, max_nodes: int = 64) -> list[int]:
    """Return root plus descendants, bounded for live-controller safety."""
    seen: set[int] = set()
    pending = [root_pid]
    result: list[int] = []
    while pending and len(result) < max_nodes:
        pid = pending.pop(0)
        if pid in seen or pid <= 0:
            continue
        seen.add(pid)
        result.append(pid)
        pending.extend(_child_pids(pid))
    return result


def _question_paths_from_omx_env(env: dict[str, str]) -> list[Path]:
    root = _safe_str(env.get("OMX_ROOT")).strip()
    session_id = _safe_str(env.get("OMX_SESSION_ID")).strip()
    if not root or not session_id:
        return []
    questions_dir = Path(root) / ".omx" / "state" / "sessions" / session_id / "questions"
    if not questions_dir.is_dir():
        return []
    return sorted(
        questions_dir.glob("question-*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )


async def _candidate_question_paths_from_window_processes(
    window: TmuxWindow,
) -> list[Path]:
    """Find OMX run-root question records from live same-window process env."""
    candidates: list[Path] = []
    seen: set[Path] = set()
    for _pane_id, pane_pid in await _list_pane_processes(window.window_id):
        for pid in _descendant_pids(pane_pid):
            for path in _question_paths_from_omx_env(_env_for_pid(pid)):
                if path in seen:
                    continue
                seen.add(path)
                candidates.append(path)
    return sorted(
        candidates,
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )


def _state_path_from_question_ui_cmdline(argv: list[str]) -> Path | None:
    """Extract the durable OMX question record path from a helper-pane cmdline."""
    if not argv:
        return None
    has_question = "question" in argv
    has_ui = "--ui" in argv
    if not has_question or not has_ui:
        return None
    for index, arg in enumerate(argv):
        if arg == _STATE_PATH_FLAG and index + 1 < len(argv):
            candidate = argv[index + 1]
            break
        if arg.startswith(f"{_STATE_PATH_FLAG}="):
            candidate = arg.split("=", 1)[1]
            break
    else:
        return None
    if not candidate:
        return None
    path = Path(candidate)
    if not path.is_absolute() or path.suffix != ".json":
        return None
    return path


async def _capture_renderer_pane(target: str) -> str | None:
    """Capture a renderer pane by id for recovery validation only."""
    if not _PANE_ID_RE.match(target):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-p",
            "-t",
            target,
            "-S",
            "-80",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except Exception:
        logger.debug("Failed to capture OMX renderer pane %s", target, exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace")


async def _tmux_pane_exists(target: str) -> bool:
    if not _PANE_ID_RE.match(target):
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-p",
            "-t",
            target,
            "#{pane_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except Exception:
        logger.debug("Failed to check tmux pane %s", target, exc_info=True)
        return False
    return proc.returncode == 0 and stdout.decode("utf-8", errors="replace").strip() == target


async def _launch_renderer_pane(
    record: OmxQuestionRecord,
    window: TmuxWindow,
) -> str | None:
    """Best-effort create a same-window OMX question UI helper pane."""
    return_target = _safe_str(record.renderer.get("return_target")).strip()
    if not _PANE_ID_RE.match(return_target):
        return None
    if not await _tmux_pane_exists(return_target):
        return None
    omx = shutil.which("omx") or "omx"
    session_id = _session_id_for_question(record.path)
    env_args: list[str] = [f"OMX_QUESTION_RETURN_PANE={return_target}"]
    if session_id:
        env_args.append(f"OMX_SESSION_ID={session_id}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "split-window",
            "-t",
            return_target,
            "-v",
            "-l",
            "16",
            "-P",
            "-F",
            "#{pane_id}",
            "-c",
            window.cwd,
            "env",
            *env_args,
            omx,
            "question",
            "--ui",
            "--state-path",
            str(record.path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except Exception:
        logger.debug("Failed to launch OMX question renderer pane", exc_info=True)
        return None
    if proc.returncode != 0:
        logger.debug(
            "OMX question renderer pane launch failed: %s",
            stderr.decode("utf-8", errors="replace").strip(),
        )
        return None
    pane_id = stdout.decode("utf-8", errors="replace").strip()
    return pane_id if _PANE_ID_RE.match(pane_id) else None


async def _ensure_renderer_for_unrendered_error(
    record: OmxQuestionRecord,
    window: TmuxWindow,
) -> OmxQuestionRecord:
    """Open a local helper pane for renderer-start failures when safe."""
    if not _is_unrendered_question_runtime_error(record):
        return record
    if _safe_str(record.renderer.get("target")).strip():
        return record
    return_target = _safe_str(record.renderer.get("return_target")).strip()
    pane_ids = {
        pane_id
        for pane_id in getattr(window, "pane_ids", ())
        if isinstance(pane_id, str) and pane_id.strip()
    }
    if return_target not in pane_ids:
        return record
    pane_id = await _launch_renderer_pane(record, window)
    if pane_id is None:
        return record
    try:
        payload = _read_json_object(record.path)
        payload["status"] = "prompting"
        payload["updated_at"] = _now_iso()
        payload["renderer"] = {
            "renderer": "tmux-pane",
            "target": pane_id,
            "return_target": return_target,
            "return_transport": "tmux-send-keys",
        }
        payload.pop("error", None)
        _write_json_object(record.path, payload)
    except Exception:
        logger.debug("Failed to update OMX question renderer record", exc_info=True)
        return record
    return _load_question_record(record.path) or record


def _question_error_payload(record: OmxQuestionRecord) -> dict[str, Any]:
    try:
        payload = _read_json_object(record.path)
    except Exception:
        return {}
    error = payload.get("error")
    return error if isinstance(error, dict) else {}


def _is_timeout_error_record(record: OmxQuestionRecord) -> bool:
    if record.status != "error":
        return False
    error = _question_error_payload(record)
    code = _safe_str(error.get("code")).strip()
    message = _safe_str(error.get("message")).strip().casefold()
    return code == "question_runtime_failed" and "timed out waiting" in message


def _is_unrendered_question_runtime_error(record: OmxQuestionRecord) -> bool:
    """Return True for renderer-start failures that Telegram can own."""
    if record.status != "error":
        return False
    error = _question_error_payload(record)
    code = _safe_str(error.get("code")).strip()
    message = _safe_str(error.get("message")).strip().casefold()
    if code != "question_runtime_failed":
        return False
    return (
        "cannot open a visible renderer" in message
        or "no attached client" in message
        or "outside an attached tmux pane" in message
    )


def _record_visible_in_renderer(record: OmxQuestionRecord, pane_text: str) -> bool:
    haystack = " ".join((pane_text or "").split()).casefold()
    if not haystack:
        return False
    question_lines = [line.strip() for line in record.question.splitlines() if line.strip()]
    if question_lines:
        question_anchor = max(question_lines, key=len)
        if " ".join(question_anchor.split()).casefold() not in haystack:
            return False
    option_labels = [
        " ".join(option.label.split()).casefold()
        for option in record.options
        if option.label.strip()
    ]
    if option_labels and not any(label in haystack for label in option_labels):
        return False
    return True


async def _is_recoverable_live_renderer_error(record: OmxQuestionRecord) -> bool:
    """Return whether a timeout/error record is still answerable in tmux."""
    if _is_unrendered_question_runtime_error(record):
        return bool(_safe_str(record.renderer.get("return_target")).strip())
    if not _is_timeout_error_record(record):
        return False
    renderer_target = _safe_str(record.renderer.get("target")).strip()
    pane_text = await _capture_renderer_pane(renderer_target)
    if not pane_text:
        return False
    return _record_visible_in_renderer(record, pane_text)


def _load_matching_question_record_from_path(
    path: Path,
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
    statuses: frozenset[str] | None = None,
) -> OmxQuestionRecord | None:
    suffix = (question_suffix or "").strip()
    allowed_statuses = statuses or _ACTIVE_STATUSES
    record = _load_question_record(path)
    if record is None:
        return None
    if record.status not in allowed_statuses:
        return None
    if suffix and record.short_id != suffix and record.question_id != suffix:
        return None
    if not _recent_enough(record):
        return None
    if not _pane_matches_window(record, window):
        return None
    return record


async def find_omx_question_for_window(
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
    statuses: frozenset[str] | None = None,
) -> OmxQuestionRecord | None:
    """Return the newest matching OMX question for a tmux window.

    The durable record normally lives below ``<cwd>/.omx/state``.  OMX CLI
    sessions launched from a run directory can instead render a split-pane UI
    whose process carries the exact ``--state-path`` under ``.omx-runs``.  That
    helper pane is still tmux topology inside the bound window, not a separate
    control surface, so it is safe to use only as a locator for the durable
    record.
    """
    record = find_omx_question(
        window,
        question_suffix=question_suffix,
        statuses=statuses,
    )
    if record is not None:
        return record
    for state_path in await _candidate_question_paths_from_window_processes(window):
        record = _load_matching_question_record_from_path(
            state_path,
            window,
            question_suffix=question_suffix,
            statuses=statuses,
        )
        if record is not None:
            return record
    pane_ids = {
        pane_id
        for pane_id in getattr(window, "pane_ids", ())
        if isinstance(pane_id, str) and pane_id.strip()
    }
    if not pane_ids:
        return None
    candidates: list[OmxQuestionRecord] = []
    for pane_id, pid in await _list_pane_processes(window.window_id):
        if pane_id not in pane_ids:
            continue
        state_path = _state_path_from_question_ui_cmdline(_cmdline_for_pid(pid))
        if state_path is None:
            continue
        record = _load_matching_question_record_from_path(
            state_path,
            window,
            question_suffix=question_suffix,
            statuses=statuses,
        )
        if record is not None:
            candidates.append(record)
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: item.path.stat().st_mtime if item.path.exists() else 0.0,
        reverse=True,
    )[0]


async def find_active_omx_question_for_window(
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
) -> OmxQuestionRecord | None:
    """Return the newest active OMX question, including helper-pane records."""
    return await find_omx_question_for_window(
        window,
        question_suffix=question_suffix,
        statuses=_ACTIVE_STATUSES,
    )


async def find_recoverable_omx_question_for_window(
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
) -> OmxQuestionRecord | None:
    """Return a terminal timeout/error prompt that still has a live renderer."""
    record = await find_omx_question_for_window(
        window,
        question_suffix=question_suffix,
        statuses=_ERROR_STATUSES,
    )
    if record is None:
        return None
    if await _is_recoverable_live_renderer_error(record):
        return record
    return None


async def find_answerable_omx_question_for_window(
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
) -> OmxQuestionRecord | None:
    """Return an active prompt or a timeout/error prompt still answerable in tmux."""
    record = await find_active_omx_question_for_window(
        window,
        question_suffix=question_suffix,
    )
    if record is not None:
        return record
    return await find_recoverable_omx_question_for_window(
        window,
        question_suffix=question_suffix,
    )


def _pane_matches_window(record: OmxQuestionRecord, window: TmuxWindow) -> bool:
    pane_ids = {
        pane_id
        for pane_id in getattr(window, "pane_ids", ())
        if isinstance(pane_id, str) and pane_id.strip()
    }
    pane_id = _safe_str(getattr(window, "pane_id", "")).strip()
    if pane_id:
        pane_ids.add(pane_id)
    renderer_target = _safe_str(record.renderer.get("target")).strip()
    return_target = _safe_str(record.renderer.get("return_target")).strip()
    return_window_id = _safe_str(record.renderer.get("return_window_id")).strip()
    if return_window_id and return_window_id != window.window_id:
        return False
    referenced_panes = {value for value in (renderer_target, return_target) if value}
    if referenced_panes and pane_ids:
        return bool(referenced_panes & pane_ids)
    if referenced_panes:
        return False
    # Pending records can exist before the renderer is attached; cwd + recency
    # are the only durable linkage available in that short interval.
    return True


def find_omx_question(
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
    statuses: frozenset[str] | None = None,
) -> OmxQuestionRecord | None:
    """Return the newest matching OMX question for a tmux window cwd."""
    suffix = (question_suffix or "").strip()
    allowed_statuses = statuses or _ACTIVE_STATUSES
    for path in _candidate_question_paths(window.cwd):
        record = _load_question_record(path)
        if record is None:
            continue
        if record.status not in allowed_statuses:
            continue
        if suffix and record.short_id != suffix and record.question_id != suffix:
            continue
        if not _recent_enough(record):
            continue
        if not _pane_matches_window(record, window):
            continue
        return record
    return None


def find_active_omx_question(
    window: TmuxWindow,
    *,
    question_suffix: str | None = None,
) -> OmxQuestionRecord | None:
    """Return the newest active OMX question for a tmux window cwd."""
    return find_omx_question(window, question_suffix=question_suffix)


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


def _other_answer_payload(record: OmxQuestionRecord, text: str) -> dict[str, Any]:
    value = " ".join(text.split())
    return {
        "kind": "other",
        "value": value,
        "selected_labels": [record.other_label],
        "selected_values": [value],
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


async def _tmux_send_line(target: str, text: str) -> bool:
    if not _PANE_ID_RE.match(target):
        return False
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
        return False
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
    return proc.returncode == 0


async def _tmux_send_key(target: str, key: str) -> bool:
    if not _PANE_ID_RE.match(target):
        return False
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "send-keys",
        "-t",
        target,
        key,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    return proc.returncode == 0


_RENDERER_OPTION_RE = re.compile(
    r"^(?P<prefix>\s*›?\s*)\[(?P<checked>[ xX])\]\s+"
    r"(?P<number>\d+)\."
)


def _parse_renderer_options(pane_text: str) -> tuple[int | None, dict[int, bool]]:
    highlighted: int | None = None
    checked: dict[int, bool] = {}
    for line in (pane_text or "").splitlines():
        match = _RENDERER_OPTION_RE.match(line)
        if not match:
            continue
        number = int(match.group("number"))
        index = number - 1
        checked[index] = match.group("checked").lower() == "x"
        if "›" in match.group("prefix"):
            highlighted = index
    return highlighted, checked


async def _sync_renderer_toggle(
    record: OmxQuestionRecord,
    index: int,
    *,
    desired_selected: bool,
) -> bool:
    """Best-effort mirror a Telegram multi-select toggle in the local UI pane."""
    target = _safe_str(record.renderer.get("target")).strip()
    if not _PANE_ID_RE.match(target):
        return False
    pane_text = await _capture_renderer_pane(target)
    if pane_text is None:
        return False
    highlighted, checked = _parse_renderer_options(pane_text)
    if checked.get(index) is desired_selected:
        return True
    if highlighted is None or index not in checked:
        return False
    delta = index - highlighted
    key = "Down" if delta > 0 else "Up"
    for _ in range(abs(delta)):
        if not await _tmux_send_key(target, key):
            return False
        await asyncio.sleep(0.03)
    return await _tmux_send_key(target, "Space")


async def _tmux_kill_pane(target: str, *, return_target: str = "") -> bool:
    if not _PANE_ID_RE.match(target) or target == return_target:
        return False
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "kill-pane",
        "-t",
        target,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    return proc.returncode == 0


async def _close_renderer_pane(record: OmxQuestionRecord, *, return_target: str = "") -> None:
    renderer_target = _safe_str(record.renderer.get("target")).strip()
    if (
        _safe_str(record.renderer.get("renderer")).strip() == "tmux-pane"
        and renderer_target
    ):
        killed = await _tmux_kill_pane(renderer_target, return_target=return_target)
        if not killed:
            logger.debug(
                "OMX question renderer pane was not killed: question_id=%s target=%s return_target=%s",
                record.question_id,
                renderer_target,
                return_target,
            )


async def _bridge_answer_to_runtime(
    record: OmxQuestionRecord,
    answer: dict[str, Any],
    *,
    window_id: str = "",
    warning_context: str = "",
) -> None:
    transport = _safe_str(record.renderer.get("return_transport")).strip()
    return_target = _safe_str(record.renderer.get("return_target")).strip()
    if transport != "tmux-send-keys" or not return_target:
        await _close_renderer_pane(record, return_target=return_target)
        return

    injection = _injection_text(answer)
    if window_id:
        # The local question pane can be the active pane while the Telegram callback
        # is handled. Close it before using the normal runtime input path so Codex
        # receives a real submit/ACK flow in the return pane instead of a raw
        # tmux paste that can remain stuck in the composer.
        await _close_renderer_pane(record, return_target=return_target)
        sent, message = await session_manager.send_to_window(window_id, injection)
        if not sent:
            logger.warning(
                "Failed to submit OMX question %sanswer via runtime input: "
                "question_id=%s window_id=%s message=%s",
                f"{warning_context} " if warning_context else "",
                record.question_id,
                window_id,
                message,
            )
        return

    injected = await _tmux_send_line(return_target, injection)
    if not injected:
        logger.warning(
            "Failed to inject OMX question %sanswer: question_id=%s return_target=%s",
            f"{warning_context} " if warning_context else "",
            record.question_id,
            return_target,
        )
    await _close_renderer_pane(record, return_target=return_target)


async def answer_omx_question(
    record: OmxQuestionRecord,
    selected_indices: list[int],
    *,
    window_id: str = "",
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

    await _bridge_answer_to_runtime(record, answer, window_id=window_id)
    return answer


async def answer_omx_question_other(
    record: OmxQuestionRecord,
    text: str,
    *,
    window_id: str = "",
) -> dict[str, Any]:
    """Answer an active OMX question with free-form Telegram text as Other."""
    value = " ".join(text.split())
    if not value:
        raise ValueError("Other OMX question answer cannot be empty")
    if not record.allow_other:
        raise ValueError("OMX question does not allow Other answers")

    answer = _other_answer_payload(record, value)
    payload = _read_json_object(record.path)
    payload["status"] = "answered"
    payload["updated_at"] = _now_iso()
    payload["answer"] = answer
    payload.pop("error", None)
    _write_json_object(record.path, payload)

    await _bridge_answer_to_runtime(
        record,
        answer,
        window_id=window_id,
        warning_context="Other",
    )
    return answer


def _build_question_text(
    record: OmxQuestionRecord, selected: set[int] | None = None
) -> str:
    selected = selected or set()
    lines = ["❓ OMX Question"]
    if record.status == "error":
        renderer_target = _safe_str(record.renderer.get("target")).strip()
        if not renderer_target and _is_unrendered_question_runtime_error(record):
            lines.extend(
                [
                    "",
                    "⚠️ The local OMX question renderer did not open, but Telegram can recover the question and bridge the answer back to the bound runtime pane.",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "⚠️ The durable question record timed out, but the local renderer is still visible. Answering here will recover it.",
                ]
            )
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
        lines.append(
            f"Other is available as: {record.other_label}. "
            "Reply with text in this Telegram thread to answer as Other."
        )
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
    *,
    chat_id: int | None = None,
    window_id: str = "",
) -> None:
    key = _question_surface_key(
        user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=window_id,
    )
    previous_state = _question_render_state.get(key)
    msg_id = _question_msgs.pop(key, None)
    _drop_question_tracking(key)
    if bot and msg_id:
        resolved_chat_id = (
            chat_id
            if chat_id is not None
            else session_manager.resolve_chat_id(user_id, thread_id)
        )
        try:
            await bot.delete_message(chat_id=resolved_chat_id, message_id=msg_id)
            log_telegram_delivery(
                action="delete",
                user_id=user_id,
                chat_id=resolved_chat_id,
                thread_id=thread_id,
                message_id=msg_id,
                window_id=window_id,
                task_type=QUESTION_PROMPT_TASK_TYPE,
                content_type=QUESTION_PROMPT_CONTENT_TYPE,
                semantic_kind=QUESTION_PROMPT_SEMANTIC_KIND,
                text=(
                    f"question_id={previous_state[0]}"
                    if previous_state is not None
                    else ""
                ),
            )
        except Exception:
            logger.debug("Failed to delete OMX question message", exc_info=True)


def _answered_label_text(record: OmxQuestionRecord) -> str:
    try:
        payload = _read_json_object(record.path)
    except Exception:
        payload = {}
    answer = payload.get("answer") if isinstance(payload.get("answer"), dict) else {}
    selected_labels = answer.get("selected_labels") if isinstance(answer, dict) else None
    if isinstance(selected_labels, list) and selected_labels:
        return ", ".join(str(label) for label in selected_labels)
    value = answer.get("value") if isinstance(answer, dict) else ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value or "")


def _build_terminal_question_text(record: OmxQuestionRecord) -> str:
    if record.status == "answered":
        label_text = _answered_label_text(record)
        return _clip(
            f"✅ OMX Question answered\n\n{record.question}\n\nAnswer: {label_text}".strip()
        )
    if record.status == "aborted":
        return _clip(f"⚠️ OMX Question aborted\n\n{record.question}".strip())
    try:
        payload = _read_json_object(record.path)
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    message = _safe_str(error.get("message")).strip() if isinstance(error, dict) else ""
    parts = ["❌ OMX Question error", "", record.question]
    if message:
        parts.extend(["", message])
    return _clip("\n".join(parts).strip())


async def _edit_terminal_question_artifact(
    bot: Bot,
    *,
    user_id: int,
    key: _QuestionKey,
    msg_id: int,
    record: OmxQuestionRecord,
    thread_id: int | None,
    window_id: str,
    chat_id: int | None = None,
) -> bool:
    resolved_chat_id = (
        chat_id
        if chat_id is not None
        else session_manager.resolve_chat_id(user_id, thread_id)
    )
    text = _build_terminal_question_text(record)
    try:
        await bot.edit_message_text(
            chat_id=resolved_chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=None,
            link_preview_options=NO_LINK_PREVIEW,
        )
        _audit_question_artifact(
            action="edit",
            user_id=user_id,
            chat_id=resolved_chat_id,
            thread_id=thread_id,
            message_id=msg_id,
            window_id=window_id,
            record=record,
            text=text,
            reason="terminal_question_state",
        )
        _drop_question_tracking(key)
        return True
    except Exception as exc:
        logger.debug("Failed to edit terminal OMX question artifact", exc_info=True)
        _audit_question_artifact(
            action="edit",
            user_id=user_id,
            chat_id=resolved_chat_id,
            thread_id=thread_id,
            message_id=msg_id,
            window_id=window_id,
            record=record,
            text=text,
            success=False,
            error=str(exc),
            reason="terminal_question_state",
        )
        return False


async def _edit_active_question_artifact(
    bot: Bot,
    *,
    user_id: int,
    key: _QuestionKey,
    msg_id: int,
    record: OmxQuestionRecord,
    window_id: str,
    thread_id: int | None,
    chat_id: int | None = None,
    selected: set[int] | None = None,
) -> str:
    """Edit an existing active question artifact and repair local tracking.

    Callback updates already carry the Telegram message id that the user tapped.
    Use that id as the artifact identity so multi-select toggles remain mutable
    updates instead of falling back to a new prompt send after process restart or
    lost in-memory tracking.
    """
    selected = selected or set()
    render_state = (record.question_id, tuple(sorted(selected)))
    resolved_chat_id = (
        chat_id
        if chat_id is not None
        else session_manager.resolve_chat_id(user_id, thread_id)
    )
    try:
        edit_text = _build_question_text(record, selected)
        await bot.edit_message_text(
            chat_id=resolved_chat_id,
            message_id=msg_id,
            text=edit_text,
            reply_markup=_build_question_keyboard(record, window_id, selected=selected),
            link_preview_options=NO_LINK_PREVIEW,
        )
    except Exception as exc:
        if _is_message_not_modified_error(exc):
            pass
        elif _is_question_replacement_edit_error(exc):
            logger.debug(
                "OMX question artifact edit requires replacement",
                exc_info=True,
            )
            return "replace"
        else:
            logger.debug("Failed to edit active OMX question artifact", exc_info=True)
            _question_msgs[key] = msg_id
            _question_windows[key] = window_id
            _audit_question_artifact(
                action="edit",
                user_id=user_id,
                chat_id=resolved_chat_id,
                thread_id=thread_id,
                message_id=msg_id,
                window_id=window_id,
                record=record,
                text=edit_text,
                success=False,
                error=str(exc),
            )
            return "failed"
    _question_msgs[key] = msg_id
    _question_windows[key] = window_id
    _question_render_state[key] = render_state
    _clear_question_deferral(key, record.question_id)
    _audit_question_artifact(
        action="edit",
        user_id=user_id,
        chat_id=resolved_chat_id,
        thread_id=thread_id,
        message_id=msg_id,
        window_id=window_id,
        record=record,
        text=edit_text,
    )
    return "ok"


async def handle_omx_question_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
    *,
    record: OmxQuestionRecord | None = None,
    chat_id: int | None = None,
    send_if_missing: bool = True,
    defer_reason: str | None = None,
) -> bool:
    """Send/update an OMX question prompt for the bound window, if active."""
    window = await tmux_manager.find_window_by_id(window_id)
    if not window:
        await clear_omx_question_msg(user_id, bot, thread_id, window_id=window_id)
        return False
    key = _question_surface_key(
        user_id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=window_id,
    )
    if record is None:
        record = await find_answerable_omx_question_for_window(window)
    if record is None:
        existing_msg_id = _question_msgs.get(key)
        previous_state = _question_render_state.get(key)
        if existing_msg_id and previous_state:
            terminal_record = await find_omx_question_for_window(
                window,
                question_suffix=previous_state[0],
                statuses=_TERMINAL_STATUSES,
            )
            if terminal_record is not None:
                await _edit_terminal_question_artifact(
                    bot,
                    user_id=user_id,
                    key=key,
                    msg_id=existing_msg_id,
                    record=terminal_record,
                    thread_id=thread_id,
                    window_id=window_id,
                    chat_id=chat_id,
                )
                return True
        if existing_msg_id:
            await clear_omx_question_msg(
                user_id,
                bot,
                thread_id,
                window_id=window_id,
            )
        return False

    record = await _ensure_renderer_for_unrendered_error(record, window)

    selection_key = (*key, record.question_id)
    selected = (
        _question_selections.get(selection_key, set()) if record.multi_select else set()
    )
    text = _build_question_text(record, selected)
    keyboard = _build_question_keyboard(record, window_id, selected=selected)
    resolved_chat_id = (
        chat_id
        if chat_id is not None
        else session_manager.resolve_chat_id(user_id, thread_id)
    )
    thread_kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
    existing_msg_id = _question_msgs.get(key)
    render_state = (record.question_id, tuple(sorted(selected)))
    if existing_msg_id:
        if _question_render_state.get(key) == render_state:
            return True
        edit_result = await _edit_active_question_artifact(
            bot,
            user_id=user_id,
            key=key,
            msg_id=existing_msg_id,
            record=record,
            window_id=window_id,
            thread_id=thread_id,
            chat_id=resolved_chat_id,
            selected=selected,
        )
        if edit_result == "ok":
            return True
        if edit_result == "failed":
            return False
        _question_msgs.pop(key, None)
    if (
        not send_if_missing
        and _should_override_question_deferral_for_timeout(
            key,
            record,
            defer_reason=defer_reason,
        )
    ):
        send_if_missing = True
        defer_reason = "pre_final_gate_timeout"
    if not send_if_missing:
        logger.debug(
            "Deferring new OMX question artifact while earlier Telegram artifacts drain: "
            "user=%d window=%s thread=%s question_id=%s",
            user_id,
            window_id,
            thread_id,
            record.question_id,
        )
        _record_question_prompt_deferral(
            key=key,
            user_id=user_id,
            chat_id=resolved_chat_id,
            thread_id=thread_id,
            window_id=window_id,
            record=record,
            text=text,
            reason=defer_reason,
        )
        return True
    try:
        sent = await bot.send_message(
            chat_id=resolved_chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.error("Failed to send OMX question message", exc_info=True)
        _audit_question_artifact(
            action="send",
            user_id=user_id,
            chat_id=resolved_chat_id,
            thread_id=thread_id,
            window_id=window_id,
            record=record,
            text=text,
            success=False,
            error=str(exc),
            reason=defer_reason,
        )
        return False
    if sent:
        _question_msgs[key] = sent.message_id
        _question_windows[key] = window_id
        _question_render_state[key] = render_state
        _clear_question_deferral(key, record.question_id)
        _audit_question_artifact(
            action="send",
            user_id=user_id,
            chat_id=resolved_chat_id,
            thread_id=thread_id,
            message_id=sent.message_id,
            window_id=window_id,
            record=record,
            text=text,
            reason=defer_reason,
        )
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


def _chat_id_from_update(update: Update) -> int | None:
    message = update.callback_query.message if update.callback_query else None
    chat_id = getattr(message, "chat_id", None)
    if isinstance(chat_id, int):
        return chat_id
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if isinstance(chat_id, int):
        return chat_id
    effective_chat = getattr(update, "effective_chat", None)
    chat_id = getattr(effective_chat, "id", None)
    return chat_id if isinstance(chat_id, int) else None


def _message_id_from_update(update: Update) -> int | None:
    message = update.callback_query.message if update.callback_query else None
    message_id = getattr(message, "message_id", None)
    return message_id if isinstance(message_id, int) else None


def _callback_window_authorized(
    user_id: int,
    *,
    window_id: str,
    thread_id: int | None,
    chat_id: int | None,
) -> bool:
    try:
        bound_window = (
            session_manager.resolve_window_for_surface(user_id, thread_id=thread_id)
            if thread_id is not None
            else session_manager.resolve_window_for_surface(user_id, chat_id=chat_id)
            if chat_id is not None
            else None
        )
    except ValueError:
        return False
    return bound_window == window_id


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
    chat_id = _chat_id_from_update(update)
    if not _callback_window_authorized(
        user.id,
        window_id=window_id,
        thread_id=thread_id,
        chat_id=chat_id,
    ):
        await query.answer("Question is not bound to this surface", show_alert=True)
        return True
    window = await tmux_manager.find_window_by_id(window_id)
    if not window:
        await query.answer("Window not found", show_alert=True)
        return True
    record = await find_answerable_omx_question_for_window(
        window,
        question_suffix=question_suffix,
    )
    if record is None:
        await query.answer("Question is no longer active", show_alert=True)
        await clear_omx_question_msg(
            user.id,
            context.bot,
            thread_id,
            chat_id=chat_id,
            window_id=window_id,
        )
        return True

    surface_key = _question_surface_key(
        user.id,
        thread_id=thread_id,
        chat_id=chat_id,
        window_id=window_id,
    )
    key = (*surface_key, record.question_id)
    if action == CB_OMX_QUESTION_REFRESH:
        selected = (
            set(_question_selections.get(key, set())) if record.multi_select else set()
        )
        msg_id = _message_id_from_update(update)
        refreshed = (
            await _edit_active_question_artifact(
                context.bot,
                user_id=user.id,
                key=surface_key,
                msg_id=msg_id,
                record=record,
                window_id=window_id,
                thread_id=thread_id,
                chat_id=chat_id,
                selected=selected,
            )
            if msg_id is not None
            else False
        )
        if refreshed == "failed":
            await query.answer("Could not update question prompt; please retry", show_alert=True)
            return True
        if refreshed != "ok":
            await handle_omx_question_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                record=record,
                chat_id=chat_id,
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
        synced = await _sync_renderer_toggle(
            record,
            index,
            desired_selected=index in selected,
        )
        if not synced:
            logger.debug(
                "OMX question Telegram toggle was not mirrored to renderer: "
                "question_id=%s index=%s",
                record.question_id,
                index,
            )
        msg_id = _message_id_from_update(update)
        edited = (
            await _edit_active_question_artifact(
                context.bot,
                user_id=user.id,
                key=surface_key,
                msg_id=msg_id,
                record=record,
                window_id=window_id,
                thread_id=thread_id,
                chat_id=chat_id,
                selected=selected,
            )
            if msg_id is not None
            else False
        )
        if edited == "failed":
            await query.answer("Could not update question prompt; please retry", show_alert=True)
            return True
        if edited != "ok":
            await handle_omx_question_ui(
                context.bot,
                user.id,
                window_id,
                thread_id,
                record=record,
                chat_id=chat_id,
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

    open_new_turn_generation(user.id, thread_id)
    answer = await answer_omx_question(record, selected, window_id=window_id)
    selected_labels = answer.get("selected_labels")
    if isinstance(selected_labels, list):
        label_text = ", ".join(str(label) for label in selected_labels)
    else:
        label_text = str(answer.get("value", ""))
    text = f"✅ OMX Question answered\n\n{record.question}\n\nAnswer: {label_text}".strip()
    msg_id = _message_id_from_update(update)
    try:
        await query.edit_message_text(
            text=_clip(text),
            reply_markup=None,
            link_preview_options=NO_LINK_PREVIEW,
        )
        if msg_id is not None:
            _audit_question_artifact(
                action="edit",
                user_id=user.id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=msg_id,
                window_id=window_id,
                record=record,
                text=text,
                reason="answered",
            )
    except Exception as exc:
        logger.debug("Failed to edit answered OMX question message", exc_info=True)
        if msg_id is not None:
            _audit_question_artifact(
                action="edit",
                user_id=user.id,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=msg_id,
                window_id=window_id,
                record=record,
                text=text,
                success=False,
                error=str(exc),
                reason="answered",
            )
    await query.answer("Answered")
    await clear_omx_question_msg(
        user.id,
        None,
        thread_id,
        chat_id=chat_id,
        window_id=window_id,
    )
    _question_selections.pop(key, None)
    return True
