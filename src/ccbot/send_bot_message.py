"""CLI/API helper for sending result artifacts back to a ccbot chat.

This is intentionally an outbound Telegram delivery helper, not runtime input:
it reads ccbot's persisted control-surface routing coordinates and sends a
message/document through the Telegram Bot API. It does not write replay
evidence and does not inject text into tmux.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import io
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from telegram import Bot, InputFile
from telegram.constants import ParseMode

from .telegram_sender import split_message


_DEFAULT_ATTACHMENT_FILENAMES: dict[str, str] = {
    "document": "attachment.bin",
    "photo": "attachment.jpg",
    "video": "attachment.mp4",
    "audio": "attachment.mp3",
    "animation": "attachment.gif",
}

_MIME_TYPE_EXTENSIONS: dict[str, str] = {
    "application/gzip": "gz",
    "application/json": "json",
    "application/pdf": "pdf",
    "application/x-gtar": "tar",
    "application/x-gzip": "gz",
    "application/x-tar": "tar",
    "application/zip": "zip",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "text/csv": "csv",
    "text/plain": "txt",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}


class DeliveryTargetError(ValueError):
    """Raised when the CLI cannot resolve an unambiguous Telegram target."""


@dataclass(frozen=True)
class DeliveryTarget:
    """Resolved physical Telegram delivery coordinates."""

    chat_id: int
    message_thread_id: int | None = None
    user_id: int | None = None
    surface_key: str | None = None
    reason: str = ""


def _get_config() -> Any:
    from .config import config

    return config


def _read_state(state_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(state_path) if state_path is not None else _get_config().state_file
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _parse_optional_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise DeliveryTargetError(f"{field_name} must be an integer") from exc


def normalize_chat_id(chat_id: str | int) -> int:
    """Normalize Telegram chat ids, preserving the canonical -100 group form."""
    if chat_id is None:
        raise DeliveryTargetError("chat_id is required")
    raw = str(chat_id).strip().strip("\"'").strip()
    if not raw:
        raise DeliveryTargetError("chat_id is empty")

    negative = raw.startswith("-")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return int(raw)
    if negative:
        if digits.startswith("100"):
            return int(f"-{digits}")
        return int(f"-100{digits}")
    return int(digits)


def _force_supergroup_chat_id(chat_id: str | int) -> int:
    raw = str(chat_id).strip().strip("\"'").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return int(raw)
    if digits.startswith("100"):
        return int(f"-{digits}")
    return int(f"-100{digits}")


def _is_chat_not_found(error: Exception) -> bool:
    return "chat not found" in str(error).lower()


def _parse_surface_key(surface_key: str) -> tuple[str, int]:
    raw = (surface_key or "").strip()
    if raw.startswith("t:"):
        return "topic", int(raw[2:])
    if raw.startswith("c:"):
        return "chat", int(raw[2:])
    raise DeliveryTargetError(
        f"invalid surface_key {surface_key!r}; expected t:<thread_id> or c:<chat_id>"
    )


def _group_chat_ids(state: dict[str, Any]) -> dict[str, int]:
    raw = state.get("group_chat_ids")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _target_from_surface(
    *,
    user_id: int,
    surface_key: str,
    group_chat_ids: dict[str, int],
    reason: str,
) -> DeliveryTarget:
    kind, numeric_id = _parse_surface_key(surface_key)
    if kind == "chat":
        return DeliveryTarget(
            chat_id=normalize_chat_id(numeric_id),
            message_thread_id=None,
            user_id=user_id,
            surface_key=surface_key,
            reason=reason,
        )

    thread_id = numeric_id
    chat_id = group_chat_ids.get(f"{user_id}:{thread_id}", user_id)
    return DeliveryTarget(
        chat_id=normalize_chat_id(chat_id),
        message_thread_id=thread_id,
        user_id=user_id,
        surface_key=surface_key,
        reason=reason,
    )


def _surface_candidates(state: dict[str, Any]) -> list[DeliveryTarget]:
    raw_bindings = state.get("surface_bindings")
    if not isinstance(raw_bindings, dict):
        return []
    group_ids = _group_chat_ids(state)
    candidates: list[DeliveryTarget] = []
    seen: set[tuple[int, int | None]] = set()

    for raw_user_id, raw_surfaces in raw_bindings.items():
        if not isinstance(raw_surfaces, dict):
            continue
        try:
            user_id = int(raw_user_id)
        except (TypeError, ValueError):
            continue
        for surface_key, window_id in raw_surfaces.items():
            if not str(window_id or "").strip():
                continue
            try:
                target = _target_from_surface(
                    user_id=user_id,
                    surface_key=str(surface_key),
                    group_chat_ids=group_ids,
                    reason="state_surface_binding",
                )
            except (DeliveryTargetError, ValueError):
                continue
            dedupe_key = (target.chat_id, target.message_thread_id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidates.append(target)
    return candidates


def _routing_coordinate_candidates(state: dict[str, Any]) -> list[DeliveryTarget]:
    candidates: list[DeliveryTarget] = []
    seen: set[tuple[int, int | None]] = set()
    for key, chat_id in _group_chat_ids(state).items():
        raw_user_id, sep, raw_thread_id = key.partition(":")
        if not sep:
            continue
        try:
            user_id = int(raw_user_id)
            thread_id = int(raw_thread_id)
        except (TypeError, ValueError):
            continue
        target = DeliveryTarget(
            chat_id=normalize_chat_id(chat_id),
            message_thread_id=None if thread_id == 0 else thread_id,
            user_id=user_id,
            surface_key=f"c:{chat_id}" if thread_id == 0 else f"t:{thread_id}",
            reason="state_group_chat_id_fallback",
        )
        dedupe_key = (target.chat_id, target.message_thread_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        candidates.append(target)
    return candidates


def _describe_candidates(candidates: list[DeliveryTarget]) -> str:
    if not candidates:
        return "none"
    parts = []
    for target in candidates[:8]:
        thread = (
            f" thread_id={target.message_thread_id}"
            if target.message_thread_id is not None
            else ""
        )
        surface = f" surface={target.surface_key}" if target.surface_key else ""
        user = f" user_id={target.user_id}" if target.user_id is not None else ""
        parts.append(f"chat_id={target.chat_id}{thread}{surface}{user}")
    suffix = "" if len(candidates) <= 8 else f"; +{len(candidates) - 8} more"
    return "; ".join(parts) + suffix


def resolve_delivery_target(
    *,
    chat_id: str | int | None = None,
    message_thread_id: str | int | None = None,
    user_id: str | int | None = None,
    surface_key: str | None = None,
    state_path: str | Path | None = None,
) -> DeliveryTarget:
    """Resolve a hybrid explicit/default Telegram target for CLI sends."""
    explicit_thread_id = _parse_optional_int(
        message_thread_id,
        field_name="message_thread_id",
    )
    explicit_user_id = _parse_optional_int(user_id, field_name="user_id")

    if chat_id is not None and str(chat_id).strip():
        return DeliveryTarget(
            chat_id=normalize_chat_id(chat_id),
            message_thread_id=explicit_thread_id,
            user_id=explicit_user_id,
            surface_key=surface_key,
            reason="explicit_chat_id",
        )

    state = _read_state(state_path)
    group_ids = _group_chat_ids(state)

    if surface_key:
        kind, numeric_id = _parse_surface_key(surface_key)
        if kind == "chat":
            if explicit_thread_id is not None:
                raise DeliveryTargetError(
                    "--thread-id cannot be combined with chat surface c:<chat_id>"
                )
            return DeliveryTarget(
                chat_id=normalize_chat_id(numeric_id),
                message_thread_id=None,
                user_id=explicit_user_id,
                surface_key=surface_key,
                reason="explicit_surface_key",
            )

        if explicit_user_id is None:
            matching_users = sorted(
                {
                    int(key.split(":", 1)[0])
                    for key in group_ids
                    if key.endswith(f":{numeric_id}")
                    and key.split(":", 1)[0].lstrip("-").isdigit()
                }
            )
            if len(matching_users) == 1:
                explicit_user_id = matching_users[0]
            elif len(matching_users) > 1:
                raise DeliveryTargetError(
                    "surface topic target is ambiguous across users; pass --user-id"
                )
            else:
                raise DeliveryTargetError(
                    "surface topic target needs --user-id when no group_chat_ids entry exists"
                )
        return _target_from_surface(
            user_id=explicit_user_id,
            surface_key=surface_key,
            group_chat_ids=group_ids,
            reason="explicit_surface_key",
        )

    if explicit_user_id is not None:
        lookup_thread_id = explicit_thread_id if explicit_thread_id is not None else 0
        routed_chat_id = group_ids.get(f"{explicit_user_id}:{lookup_thread_id}")
        if routed_chat_id is not None:
            return DeliveryTarget(
                chat_id=normalize_chat_id(routed_chat_id),
                message_thread_id=explicit_thread_id,
                user_id=explicit_user_id,
                surface_key=f"t:{explicit_thread_id}"
                if explicit_thread_id is not None
                else f"c:{routed_chat_id}",
                reason="state_group_chat_id",
            )
        return DeliveryTarget(
            chat_id=normalize_chat_id(explicit_user_id),
            message_thread_id=explicit_thread_id,
            user_id=explicit_user_id,
            surface_key=None,
            reason="explicit_user_id_fallback",
        )

    candidates = _surface_candidates(state)
    if not candidates:
        candidates = _routing_coordinate_candidates(state)

    chat_surface_candidates = [
        candidate
        for candidate in candidates
        if candidate.surface_key and candidate.surface_key.startswith("c:")
    ]
    if len(chat_surface_candidates) == 1:
        return chat_surface_candidates[0]
    if len(candidates) == 1:
        return candidates[0]
    if len(chat_surface_candidates) > 1:
        candidates = chat_surface_candidates

    raise DeliveryTargetError(
        "Cannot resolve a unique ccbot delivery target from state; pass "
        "--chat-id/--thread-id or --surface-key. Candidates: "
        f"{_describe_candidates(candidates)}"
    )


def _resolve_parse_mode(parse_mode: str | None) -> ParseMode | None:
    if not parse_mode:
        return None
    pm = parse_mode.strip().upper()
    if pm == "HTML":
        return ParseMode.HTML
    if pm in {"MARKDOWNV2", "MARKDOWN_V2"}:
        return ParseMode.MARKDOWN_V2
    if pm == "MARKDOWN":
        return ParseMode.MARKDOWN
    if pm in {"TEXT", "PLAIN"}:
        return None
    return None


def _parse_base64_data_url(input_value: str) -> tuple[str | None, str] | None:
    if not input_value.startswith("data:"):
        return None
    comma_index = input_value.find(",")
    if comma_index == -1:
        return None
    header = input_value[len("data:") : comma_index]
    payload = input_value[comma_index + 1 :]
    parts = [part.strip() for part in header.split(";") if part.strip()]
    mime_type = parts[0] if parts and "/" in parts[0] else None
    if not any(part.lower() == "base64" for part in parts):
        return None
    return mime_type, payload


def _decode_file_base64(file_base64: str) -> tuple[bytes, str | None]:
    parsed = _parse_base64_data_url(file_base64)
    mime_type = parsed[0] if parsed else None
    payload = parsed[1] if parsed else file_base64
    normalized_payload = "".join(payload.split())
    if not normalized_payload:
        raise ValueError("file_base64 is empty")
    padding = (-len(normalized_payload)) % 4
    if padding:
        normalized_payload += "=" * padding
    try:
        return base64.b64decode(normalized_payload, validate=True), mime_type
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid file_base64 payload") from exc


def _normalize_file_type(file_type: str | None) -> str:
    normalized = (file_type or "document").strip().lower()
    aliases = {
        "animation": "animation",
        "audio": "audio",
        "doc": "document",
        "document": "document",
        "file": "document",
        "gif": "animation",
        "image": "photo",
        "photo": "photo",
        "pic": "photo",
        "video": "video",
    }
    return aliases.get(normalized, normalized)


def _default_attachment_filename(
    *,
    file_type: str,
    filename: str | None,
    mime_type: str | None = None,
) -> str:
    if filename:
        return filename
    normalized_mime = (mime_type or "").strip().lower()
    ext = _MIME_TYPE_EXTENSIONS.get(normalized_mime)
    if ext:
        return f"attachment.{ext}"
    return _DEFAULT_ATTACHMENT_FILENAMES[file_type]


def _build_message_url(
    *,
    chat_id: int | None,
    message_id: int | None,
    thread_id: int | None,
    chat_username: str | None = None,
) -> str | None:
    if not message_id:
        return None

    username = (chat_username or "").strip().lstrip("@")
    if username:
        if thread_id is not None:
            return f"https://t.me/{username}/{thread_id}/{message_id}"
        return f"https://t.me/{username}/{message_id}"

    if chat_id is None:
        return None
    raw = str(chat_id).strip()
    if raw.startswith("-100") and len(raw) > 4:
        internal_id = raw[4:]
        if thread_id is not None:
            return f"https://t.me/c/{internal_id}/{thread_id}/{message_id}"
        return f"https://t.me/c/{internal_id}/{message_id}"
    return None


def _message_result(msg: Any, target: DeliveryTarget) -> dict[str, Any]:
    msg_id = getattr(msg, "message_id", None)
    msg_chat = getattr(msg, "chat", None)
    msg_chat_id = getattr(msg_chat, "id", None)
    out_chat_id = int(msg_chat_id) if isinstance(msg_chat_id, int) else target.chat_id
    out_thread_id = getattr(msg, "message_thread_id", None)
    if out_thread_id is None:
        out_thread_id = target.message_thread_id

    result: dict[str, Any] = {
        "message_id": msg_id,
        "chat_id": str(out_chat_id),
    }
    if out_thread_id is not None:
        result["thread_id"] = out_thread_id
    url = _build_message_url(
        chat_id=out_chat_id,
        message_id=msg_id,
        thread_id=out_thread_id,
        chat_username=getattr(msg_chat, "username", None),
    )
    if url is not None:
        result["url"] = url
    return result


def _log_delivery(
    *,
    target: DeliveryTarget,
    message: str,
    content_type: str,
    success: bool,
    message_id: int | None = None,
    error: str | None = None,
) -> None:
    try:
        from .delivery_audit import log_telegram_delivery

        log_telegram_delivery(
            action="send_bot_message",
            user_id=target.user_id,
            chat_id=target.chat_id,
            thread_id=target.message_thread_id,
            message_id=message_id,
            task_type="cli",
            content_type=content_type,
            semantic_kind="external_cli_result",
            text=message,
            success=success,
            error=error,
            reason=target.reason,
        )
    except Exception:
        return


async def send_bot_message(
    *,
    message: str,
    chat_id: str | int | None = None,
    message_thread_id: str | int | None = None,
    reply_to_message_id: str | int | None = None,
    user_id: str | int | None = None,
    surface_key: str | None = None,
    parse_mode: str = "TEXT",
    disable_web_page_preview: bool = True,
    disable_notification: bool = True,
    token: str | None = None,
    file_path: str | None = None,
    file_base64: str | None = None,
    file_type: str | None = "document",
    filename: str | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Send text and optional file payload to a resolved ccbot Telegram target."""
    bot_token = token or os.getenv("TELEGRAM_TOKEN") or _get_config().telegram_bot_token
    if not bot_token:
        return {"status": "error", "message": "Missing TELEGRAM_BOT_TOKEN"}

    try:
        target = resolve_delivery_target(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            user_id=user_id,
            surface_key=surface_key,
            state_path=state_path,
        )
    except DeliveryTargetError as exc:
        return {"status": "error", "message": str(exc)}

    has_file_path = bool(file_path)
    has_file_base64 = bool(file_base64)
    if has_file_path and has_file_base64:
        return {
            "status": "error",
            "message": "Provide either file_path or file_base64, not both",
        }

    bot = Bot(token=bot_token)
    pm = _resolve_parse_mode(parse_mode)
    reply_to_id = _parse_optional_int(
        reply_to_message_id,
        field_name="reply_to_message_id",
    )

    async def _send_once(send_target: DeliveryTarget) -> dict[str, Any]:
        common_kwargs: dict[str, Any] = {
            "chat_id": send_target.chat_id,
            "message_thread_id": send_target.message_thread_id,
            "reply_to_message_id": reply_to_id,
            "parse_mode": pm,
            "disable_notification": disable_notification,
        }

        if has_file_path or has_file_base64:
            normalized_file_type = _normalize_file_type(file_type)
            if normalized_file_type not in _DEFAULT_ATTACHMENT_FILENAMES:
                return {
                    "status": "error",
                    "message": f"Unsupported file_type: {file_type}",
                }

            if has_file_path:
                path = Path(str(file_path)).expanduser()
                if not path.is_file():
                    return {
                        "status": "error",
                        "message": f"File not found: {file_path}",
                    }
                with path.open("rb") as handle:
                    attachment = InputFile(handle, filename=filename or path.name)
                    return await _send_attachment(
                        bot,
                        attachment=attachment,
                        file_type=normalized_file_type,
                        caption=message,
                        target=send_target,
                        common_kwargs=common_kwargs,
                    )

            try:
                file_bytes, mime_type = _decode_file_base64(file_base64 or "")
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
            attachment = InputFile(
                io.BytesIO(file_bytes),
                filename=_default_attachment_filename(
                    file_type=normalized_file_type,
                    filename=filename,
                    mime_type=mime_type,
                ),
            )
            return await _send_attachment(
                bot,
                attachment=attachment,
                file_type=normalized_file_type,
                caption=message,
                target=send_target,
                common_kwargs=common_kwargs,
            )

        sent_messages: list[dict[str, Any]] = []
        for part in split_message(message or ""):
            msg = await bot.send_message(
                text=part,
                disable_web_page_preview=disable_web_page_preview,
                **common_kwargs,
            )
            sent_messages.append(_message_result(msg, send_target))

        first = sent_messages[0] if sent_messages else {}
        result: dict[str, Any] = {
            "status": "success",
            "chat_id": first.get("chat_id", str(send_target.chat_id)),
            "messages": sent_messages,
            "target": asdict(send_target),
        }
        if first:
            result["message_id"] = first.get("message_id")
            if "thread_id" in first:
                result["thread_id"] = first["thread_id"]
            if "url" in first:
                result["url"] = first["url"]
        return result

    try:
        result = await _send_once(target)
    except Exception as exc:
        if _is_chat_not_found(exc):
            fallback = DeliveryTarget(
                chat_id=_force_supergroup_chat_id(target.chat_id),
                message_thread_id=target.message_thread_id,
                user_id=target.user_id,
                surface_key=target.surface_key,
                reason=f"{target.reason}:supergroup_fallback",
            )
            if fallback.chat_id != target.chat_id:
                try:
                    result = await _send_once(fallback)
                    target = fallback
                except Exception as fallback_exc:  # pragma: no cover
                    error = str(fallback_exc)
                    _log_delivery(
                        target=fallback,
                        message=message,
                        content_type=file_type or "text",
                        success=False,
                        error=error,
                    )
                    return {"status": "error", "message": error}
            else:
                error = str(exc)
                _log_delivery(
                    target=target,
                    message=message,
                    content_type=file_type or "text",
                    success=False,
                    error=error,
                )
                return {"status": "error", "message": error}
        else:
            error = str(exc)
            _log_delivery(
                target=target,
                message=message,
                content_type=file_type or "text",
                success=False,
                error=error,
            )
            return {"status": "error", "message": error}

    if result.get("status") == "success":
        _log_delivery(
            target=target,
            message=message,
            content_type=(file_type or "document")
            if (has_file_path or has_file_base64)
            else "text",
            success=True,
            message_id=result.get("message_id"),
        )
    else:
        _log_delivery(
            target=target,
            message=message,
            content_type=(file_type or "document")
            if (has_file_path or has_file_base64)
            else "text",
            success=False,
            error=str(result.get("message") or ""),
        )
    return result


async def _send_attachment(
    bot: Bot,
    *,
    attachment: InputFile,
    file_type: str,
    caption: str,
    target: DeliveryTarget,
    common_kwargs: dict[str, Any],
) -> dict[str, Any]:
    send_kwargs = dict(common_kwargs)
    send_kwargs["caption"] = caption or None
    if file_type == "document":
        msg = await bot.send_document(document=attachment, **send_kwargs)
    elif file_type == "photo":
        msg = await bot.send_photo(photo=attachment, **send_kwargs)
    elif file_type == "video":
        msg = await bot.send_video(video=attachment, **send_kwargs)
    elif file_type == "audio":
        msg = await bot.send_audio(audio=attachment, **send_kwargs)
    else:
        msg = await bot.send_animation(animation=attachment, **send_kwargs)
    return {
        "status": "success",
        **_message_result(msg, target),
        "target": asdict(target),
    }


def _read_message_from_args(args: argparse.Namespace) -> str | None:
    if args.message_file:
        return Path(args.message_file).read_text(encoding="utf-8")
    if args.message_flag is not None:
        return args.message_flag
    if args.message is not None:
        return args.message
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data
    return None


def _env_default(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccbot send_bot_message",
        description=(
            "Send text and/or a file to this ccbot instance's Telegram chat. "
            "Defaults to the persisted CCBOT_DIR state; explicit chat/thread "
            "arguments override state."
        ),
    )
    parser.add_argument("message", nargs="?", help="Message text")
    parser.add_argument("--message", dest="message_flag", help="Message text")
    parser.add_argument("--message-file", help="Read message text from a file")
    parser.add_argument("--chat-id", default=_env_default("CCBOT_SEND_CHAT_ID", "TELEGRAM_CHAT_ID"))
    parser.add_argument(
        "--thread-id",
        "--message-thread-id",
        dest="message_thread_id",
        default=_env_default("CCBOT_SEND_THREAD_ID", "TELEGRAM_THREAD_ID"),
    )
    parser.add_argument("--user-id", default=_env_default("CCBOT_SEND_USER_ID"))
    parser.add_argument("--surface-key", default=_env_default("CCBOT_SEND_SURFACE_KEY"))
    parser.add_argument("--reply-to-message-id")
    parser.add_argument("--parse-mode", default=_env_default("CCBOT_SEND_PARSE_MODE") or "TEXT")
    parser.add_argument(
        "--disable-web-page-preview",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--disable-notification",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--token", default=_env_default("TELEGRAM_TOKEN"))
    parser.add_argument("--file-path", "--attachment", dest="file_path")
    parser.add_argument("--file-base64")
    parser.add_argument("--file-type", default="document")
    parser.add_argument("--filename")
    parser.add_argument("--state-file")
    parser.add_argument("--json", action="store_true", help="Print full result JSON")
    return parser


def send_bot_message_main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        message = _read_message_from_args(args)
    except OSError as exc:
        parser.exit(1, f"Error reading message file: {exc}\n")

    if message is None:
        if args.file_path or args.file_base64:
            message = ""
        else:
            parser.exit(
                2,
                "Message text, --message-file, stdin, --file-path, or "
                "--file-base64 is required\n",
            )

    if args.file_path and args.file_base64:
        parser.exit(2, "Use either --file-path or --file-base64, not both\n")

    try:
        result = asyncio.run(
            send_bot_message(
                message=message,
                chat_id=args.chat_id,
                message_thread_id=args.message_thread_id,
                reply_to_message_id=args.reply_to_message_id,
                user_id=args.user_id,
                surface_key=args.surface_key,
                parse_mode=args.parse_mode,
                disable_web_page_preview=args.disable_web_page_preview,
                disable_notification=args.disable_notification,
                token=args.token,
                file_path=args.file_path,
                file_base64=args.file_base64,
                file_type=args.file_type,
                filename=args.filename,
                state_path=args.state_file,
            )
        )
    except ValueError as exc:
        parser.exit(1, f"Error: {exc}\n")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif result.get("status") == "success":
        thread = f" thread_id={result.get('thread_id')}" if result.get("thread_id") is not None else ""
        print(
            f"Sent message_id={result.get('message_id')} "
            f"chat_id={result.get('chat_id')}{thread}"
        )
        if result.get("url"):
            print(result["url"])
    else:
        print(f"Failed to send: {result.get('message')}", file=sys.stderr)
        return 1
    return 0
