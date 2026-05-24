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
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from telegram import (
    Bot,
    InputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest

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

_DEFAULT_VIDEO_PROBE_TIMEOUT_SECONDS = 10.0


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


@dataclass(frozen=True)
class VideoSendMetadata:
    """Metadata sent with Telegram video uploads and recorded as request evidence."""

    width: int | None = None
    height: int | None = None
    duration: int | None = None
    supports_streaming: bool | None = None
    thumbnail_path: Path | None = None
    source: str | None = None

    def send_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.width is not None:
            kwargs["width"] = self.width
        if self.height is not None:
            kwargs["height"] = self.height
        if self.duration is not None:
            kwargs["duration"] = self.duration
        if self.supports_streaming is not None:
            kwargs["supports_streaming"] = self.supports_streaming
        if self.thumbnail_path is not None:
            kwargs["thumbnail"] = InputFile(
                self.thumbnail_path.read_bytes(),
                filename=self.thumbnail_path.name,
            )
        return kwargs

    def request_evidence(self, *, method: str = "send_video") -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "type": "video",
            "method": method,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "supports_streaming": self.supports_streaming,
        }
        if self.source:
            evidence["source"] = self.source
        if self.thumbnail_path is not None:
            evidence["thumbnail"] = {
                "provided": True,
                "filename": self.thumbnail_path.name,
                "path": str(self.thumbnail_path),
            }
        return {key: value for key, value in evidence.items() if value is not None}


def _get_config() -> Any:
    from .config import config

    return config


def _get_default_state_file() -> Path:
    try:
        return _get_config().state_file
    except ValueError as exc:
        raise DeliveryTargetError(
            f"Cannot load ccbot config to resolve default state file: {exc}"
        ) from exc


def _get_default_bot_token() -> str:
    try:
        return str(_get_config().telegram_bot_token or "")
    except ValueError:
        return ""


def _read_state(state_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(state_path) if state_path is not None else _get_default_state_file()
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


def _parse_optional_positive_int(value: Any, *, field_name: str) -> int | None:
    parsed = _parse_optional_int(value, field_name=field_name)
    if parsed is not None and parsed <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return parsed


def _duration_to_telegram_seconds(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive number of seconds") from exc
    if seconds <= 0:
        raise ValueError(f"{field_name} must be a positive number of seconds")
    return max(1, int(round(seconds)))


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
        return int(f"-{digits}")
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


def _parse_surface_key(surface_key: str) -> tuple[str, int | None, int | None]:
    raw = (surface_key or "").strip()
    if raw.startswith("t:"):
        parts = raw[2:].split(":")
        if len(parts) == 1:
            return "topic", None, int(parts[0])
        if len(parts) == 2:
            return "topic", int(parts[0]), int(parts[1])
        raise DeliveryTargetError(
            f"invalid surface_key {surface_key!r}; expected "
            "t:<thread_id>, t:<chat_id>:<thread_id>, or c:<chat_id>"
        )
    if raw.startswith("c:"):
        return "chat", int(raw[2:]), None
    raise DeliveryTargetError(
        f"invalid surface_key {surface_key!r}; expected "
        "t:<thread_id>, t:<chat_id>:<thread_id>, or c:<chat_id>"
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
    user_id: int | None,
    surface_key: str,
    group_chat_ids: dict[str, int],
    reason: str,
) -> DeliveryTarget:
    kind, chat_id, thread_id = _parse_surface_key(surface_key)
    if kind == "chat":
        return DeliveryTarget(
            chat_id=normalize_chat_id(chat_id),
            message_thread_id=None,
            user_id=user_id,
            surface_key=surface_key,
            reason=reason,
        )

    assert thread_id is not None
    resolved_chat_id = chat_id
    if resolved_chat_id is None:
        if user_id is None:
            raise DeliveryTargetError(
                "surface topic target needs --user-id when no chat-qualified "
                "surface key is provided"
            )
        route_key = f"{user_id}:{thread_id}"
        resolved_chat_id = group_chat_ids.get(route_key)
    else:
        route_key = f"{user_id}:{thread_id}" if user_id is not None else f"?:{thread_id}"
    if resolved_chat_id is None:
        raise DeliveryTargetError(
            "Cannot resolve Telegram group chat_id for topic surface "
            f"{surface_key!r}; missing group_chat_ids[{route_key!r}]. "
            "Pass --chat-id explicitly."
        )
    return DeliveryTarget(
        chat_id=normalize_chat_id(resolved_chat_id),
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
            except ValueError as exc:
                if isinstance(exc, DeliveryTargetError):
                    raise
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
            surface_key=f"c:{chat_id}" if thread_id == 0 else f"t:{chat_id}:{thread_id}",
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
        kind, surface_chat_id, surface_thread_id = _parse_surface_key(surface_key)
        if kind == "chat":
            assert surface_chat_id is not None
            if explicit_thread_id is not None:
                raise DeliveryTargetError(
                    "--thread-id cannot be combined with chat surface c:<chat_id>"
                )
            return DeliveryTarget(
                chat_id=normalize_chat_id(surface_chat_id),
                message_thread_id=None,
                user_id=explicit_user_id,
                surface_key=surface_key,
                reason="explicit_surface_key",
            )

        assert surface_thread_id is not None
        if explicit_user_id is None and surface_chat_id is None:
            matching_users = sorted(
                {
                    int(key.split(":", 1)[0])
                    for key in group_ids
                    if key.endswith(f":{surface_thread_id}")
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
        if explicit_thread_id is not None:
            raise DeliveryTargetError(
                "Cannot resolve Telegram group chat_id for --user-id/--thread-id; "
                f"missing group_chat_ids['{explicit_user_id}:{explicit_thread_id}']. "
                "Pass --chat-id explicitly."
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

    if len(candidates) == 1:
        return candidates[0]

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


def _video_probe_timeout_seconds() -> float:
    raw = os.getenv("CCBOT_VIDEO_PROBE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_VIDEO_PROBE_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        return _DEFAULT_VIDEO_PROBE_TIMEOUT_SECONDS
    return timeout if timeout > 0 else _DEFAULT_VIDEO_PROBE_TIMEOUT_SECONDS


def _probe_video_metadata(path: Path) -> dict[str, int]:
    """Return safe Telegram video metadata by probing one local file.

    The probe is best-effort and bounded. Failure returns an empty dict so
    generic file delivery continues, while JSON request evidence remains honest.
    """

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_video_probe_timeout_seconds(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    streams = payload.get("streams")
    stream = streams[0] if isinstance(streams, list) and streams else {}
    if not isinstance(stream, dict):
        stream = {}
    metadata: dict[str, int] = {}
    for source_key, output_key in (("width", "width"), ("height", "height")):
        try:
            value = int(stream.get(source_key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            metadata[output_key] = value
    duration = stream.get("duration")
    if duration in (None, "N/A"):
        fmt = payload.get("format")
        if isinstance(fmt, dict):
            duration = fmt.get("duration")
    try:
        parsed_duration = _duration_to_telegram_seconds(
            duration,
            field_name="video_duration",
        )
    except ValueError:
        parsed_duration = None
    if parsed_duration is not None:
        metadata["duration"] = parsed_duration
    return metadata


def _resolve_video_send_metadata(
    *,
    path: Path | None,
    width: Any = None,
    height: Any = None,
    duration: Any = None,
    thumbnail_path: str | Path | None = None,
    supports_streaming: bool | None = None,
    auto_probe: bool = True,
) -> VideoSendMetadata:
    explicit_width = _parse_optional_positive_int(width, field_name="video_width")
    explicit_height = _parse_optional_positive_int(height, field_name="video_height")
    explicit_duration = _duration_to_telegram_seconds(
        duration,
        field_name="video_duration",
    )
    probe_metadata: dict[str, int] = {}
    if auto_probe and path is not None:
        probe_metadata = _probe_video_metadata(path)

    resolved_thumbnail: Path | None = None
    if thumbnail_path:
        resolved_thumbnail = Path(str(thumbnail_path)).expanduser()
        if not resolved_thumbnail.is_file():
            raise ValueError(f"Video thumbnail file not found: {thumbnail_path}")

    source_parts: list[str] = []
    if any(value is not None for value in (explicit_width, explicit_height, explicit_duration)):
        source_parts.append("explicit")
    if probe_metadata:
        source_parts.append("ffprobe")
    if resolved_thumbnail is not None:
        source_parts.append("thumbnail")

    return VideoSendMetadata(
        width=explicit_width if explicit_width is not None else probe_metadata.get("width"),
        height=explicit_height if explicit_height is not None else probe_metadata.get("height"),
        duration=explicit_duration
        if explicit_duration is not None
        else probe_metadata.get("duration"),
        supports_streaming=True if supports_streaming is None else supports_streaming,
        thumbnail_path=resolved_thumbnail,
        source="+".join(source_parts) if source_parts else "default",
    )


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


def _optional_media_int(media: Any, name: str) -> int | None:
    value = getattr(media, name, None)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_media_str(media: Any, name: str) -> str | None:
    value = getattr(media, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _telegram_video_evidence(msg: Any) -> dict[str, Any]:
    video = getattr(msg, "video", None)
    if video is None:
        return {}
    video_evidence = {
        key: value
        for key, value in {
            "width": _optional_media_int(video, "width"),
            "height": _optional_media_int(video, "height"),
            "duration": _optional_media_int(video, "duration"),
            "mime_type": _optional_media_str(video, "mime_type"),
            "file_size": _optional_media_int(video, "file_size"),
        }.items()
        if value is not None
    }
    out: dict[str, Any] = {"video": video_evidence}
    thumbnail = getattr(video, "thumbnail", None)
    if thumbnail is not None:
        thumbnail_evidence = {
            key: value
            for key, value in {
                "width": _optional_media_int(thumbnail, "width"),
                "height": _optional_media_int(thumbnail, "height"),
                "file_size": _optional_media_int(thumbnail, "file_size"),
            }.items()
            if value is not None
        }
        if thumbnail_evidence:
            out["thumbnail"] = thumbnail_evidence
    return out


def _video_evidence_status(telegram_media: dict[str, Any]) -> str:
    video = telegram_media.get("video")
    if not isinstance(video, dict):
        return "request_only"
    required = ("width", "height", "duration", "mime_type")
    if all(video.get(key) is not None for key in required):
        return "complete"
    if any(video.get(key) is not None for key in required):
        return "partial"
    return "request_only"


def _message_result(
    msg: Any,
    target: DeliveryTarget,
    *,
    media_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    if media_request is not None:
        telegram_media = _telegram_video_evidence(msg)
        result["media"] = {
            "request": media_request,
            "telegram": telegram_media,
            "evidence_status": _video_evidence_status(telegram_media),
        }
    return result


def _log_delivery(
    *,
    target: DeliveryTarget,
    message: str,
    content_type: str,
    success: bool,
    message_id: int | None = None,
    error: str | None = None,
    media: dict[str, Any] | None = None,
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
            media=media,
        )
    except Exception:
        return


async def send_bot_message(
    *,
    message: str,
    chat_id: str | int | None = None,
    message_thread_id: str | int | None = None,
    reply_to_message_id: str | int | None = None,
    edit_message_id: str | int | None = None,
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
    video_width: str | int | None = None,
    video_height: str | int | None = None,
    video_duration: str | int | float | None = None,
    video_thumbnail_path: str | Path | None = None,
    video_supports_streaming: bool | None = None,
    video_auto_probe: bool = True,
) -> dict[str, Any]:
    """Send text and optional file payload to a resolved ccbot Telegram target."""
    bot_token = token or os.getenv("TELEGRAM_TOKEN") or _get_default_bot_token()
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

    telegram_proxy = _env_default(
        "CCBOT_TELEGRAM_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "HTTP_PROXY",
    )
    request = HTTPXRequest(proxy=telegram_proxy) if telegram_proxy else None
    bot = Bot(token=bot_token, request=request)
    pm = _resolve_parse_mode(parse_mode)
    reply_to_id = _parse_optional_int(
        reply_to_message_id,
        field_name="reply_to_message_id",
    )
    edit_id = _parse_optional_int(
        edit_message_id,
        field_name="edit_message_id",
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
                try:
                    video_metadata = (
                        _resolve_video_send_metadata(
                            path=path,
                            width=video_width,
                            height=video_height,
                            duration=video_duration,
                            thumbnail_path=video_thumbnail_path,
                            supports_streaming=video_supports_streaming,
                            auto_probe=video_auto_probe,
                        )
                        if normalized_file_type == "video"
                        else None
                    )
                except ValueError as exc:
                    return {"status": "error", "message": str(exc)}
                if edit_id is not None:
                    return await _edit_attachment(
                        bot,
                        attachment=path.read_bytes(),
                        attachment_filename=filename or path.name,
                        file_type=normalized_file_type,
                        caption=message,
                        target=send_target,
                        common_kwargs=common_kwargs,
                        edit_message_id=edit_id,
                        video_metadata=video_metadata,
                    )
                with path.open("rb") as handle:
                    attachment = InputFile(handle, filename=filename or path.name)
                    return await _send_attachment(
                        bot,
                        attachment=attachment,
                        file_type=normalized_file_type,
                        caption=message,
                        target=send_target,
                        common_kwargs=common_kwargs,
                        video_metadata=video_metadata,
                    )

            try:
                file_bytes, mime_type = _decode_file_base64(file_base64 or "")
            except ValueError as exc:
                return {"status": "error", "message": str(exc)}
            try:
                video_metadata = (
                    _resolve_video_send_metadata(
                        path=None,
                        width=video_width,
                        height=video_height,
                        duration=video_duration,
                        thumbnail_path=video_thumbnail_path,
                        supports_streaming=video_supports_streaming,
                        auto_probe=False,
                    )
                    if normalized_file_type == "video"
                    else None
                )
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
            if edit_id is not None:
                return await _edit_attachment(
                    bot,
                    attachment=file_bytes,
                    attachment_filename=_default_attachment_filename(
                        file_type=normalized_file_type,
                        filename=filename,
                        mime_type=mime_type,
                    ),
                    file_type=normalized_file_type,
                    caption=message,
                    target=send_target,
                    common_kwargs=common_kwargs,
                    edit_message_id=edit_id,
                    video_metadata=video_metadata,
                )
            return await _send_attachment(
                bot,
                attachment=attachment,
                file_type=normalized_file_type,
                caption=message,
                target=send_target,
                common_kwargs=common_kwargs,
                video_metadata=video_metadata,
            )

        if edit_id is not None:
            edit_kwargs = dict(common_kwargs)
            edit_kwargs.pop("disable_notification", None)
            edit_kwargs.pop("reply_to_message_id", None)
            edit_kwargs.pop("message_thread_id", None)
            msg = await bot.edit_message_text(
                message_id=edit_id,
                text=message or "",
                disable_web_page_preview=disable_web_page_preview,
                **edit_kwargs,
            )
            return {
                "status": "success",
                **_message_result(msg, send_target),
                "target": asdict(send_target),
            }

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
            media=result.get("media") if isinstance(result.get("media"), dict) else None,
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
    attachment: InputFile | bytes,
    attachment_filename: str | None = None,
    file_type: str,
    caption: str,
    target: DeliveryTarget,
    common_kwargs: dict[str, Any],
    video_metadata: VideoSendMetadata | None = None,
) -> dict[str, Any]:
    send_kwargs = dict(common_kwargs)
    send_kwargs["caption"] = caption or None
    media_request: dict[str, Any] | None = None
    if file_type == "document":
        msg = await bot.send_document(document=attachment, **send_kwargs)
    elif file_type == "photo":
        msg = await bot.send_photo(photo=attachment, **send_kwargs)
    elif file_type == "video":
        if video_metadata is not None:
            send_kwargs.update(video_metadata.send_kwargs())
            media_request = video_metadata.request_evidence(method="send_video")
        msg = await bot.send_video(video=attachment, **send_kwargs)
    elif file_type == "audio":
        msg = await bot.send_audio(audio=attachment, **send_kwargs)
    else:
        msg = await bot.send_animation(animation=attachment, **send_kwargs)
    return {
        "status": "success",
        **_message_result(msg, target, media_request=media_request),
        "target": asdict(target),
    }


def _input_media_for_attachment(
    *,
    attachment: InputFile | bytes,
    attachment_filename: str | None = None,
    file_type: str,
    caption: str,
    parse_mode: ParseMode | None,
    video_metadata: VideoSendMetadata | None = None,
) -> Any:
    kwargs = {
        "media": attachment,
        "caption": caption or None,
        "parse_mode": parse_mode,
        "filename": attachment_filename,
    }
    if file_type == "document":
        return InputMediaDocument(**kwargs)
    if file_type == "photo":
        return InputMediaPhoto(**kwargs)
    if file_type == "video":
        if video_metadata is not None:
            kwargs.update(video_metadata.send_kwargs())
        return InputMediaVideo(**kwargs)
    if file_type == "audio":
        return InputMediaAudio(**kwargs)
    return InputMediaAnimation(**kwargs)


async def _edit_attachment(
    bot: Bot,
    *,
    attachment: InputFile | bytes,
    attachment_filename: str | None = None,
    file_type: str,
    caption: str,
    target: DeliveryTarget,
    common_kwargs: dict[str, Any],
    edit_message_id: int,
    video_metadata: VideoSendMetadata | None = None,
) -> dict[str, Any]:
    edit_kwargs = dict(common_kwargs)
    edit_kwargs.pop("disable_notification", None)
    edit_kwargs.pop("reply_to_message_id", None)
    edit_kwargs.pop("message_thread_id", None)
    parse_mode = edit_kwargs.pop("parse_mode", None)
    media = _input_media_for_attachment(
        attachment=attachment,
        attachment_filename=attachment_filename,
        file_type=file_type,
        caption=caption,
        parse_mode=parse_mode,
        video_metadata=video_metadata,
    )
    msg = await bot.edit_message_media(
        message_id=edit_message_id,
        media=media,
        **edit_kwargs,
    )
    return {
        "status": "success",
        **_message_result(
            msg,
            target,
            media_request=video_metadata.request_evidence(method="edit_message_media")
            if file_type == "video" and video_metadata is not None
            else None,
        ),
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


def _build_parser(prog: str = "ccbot send_bot_message") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
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
    parser.add_argument(
        "--edit-message-id",
        help="Edit an existing Telegram message instead of sending a new one",
    )
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
    parser.add_argument(
        "--video-width",
        help="Explicit Telegram send_video width in pixels; overrides ffprobe",
    )
    parser.add_argument(
        "--video-height",
        help="Explicit Telegram send_video height in pixels; overrides ffprobe",
    )
    parser.add_argument(
        "--video-duration",
        help="Explicit Telegram send_video duration in seconds; overrides ffprobe",
    )
    parser.add_argument(
        "--video-thumbnail-path",
        "--thumbnail-path",
        dest="video_thumbnail_path",
        help="JPEG thumbnail to upload with --file-type video",
    )
    parser.add_argument(
        "--video-supports-streaming",
        "--supports-streaming",
        dest="video_supports_streaming",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Pass supports_streaming to Telegram send_video (default: true for videos)",
    )
    parser.add_argument(
        "--video-auto-probe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-probe local video width/height/duration with bounded ffprobe",
    )
    parser.add_argument("--state-file")
    parser.add_argument("--json", action="store_true", help="Print full result JSON")
    return parser


def send_bot_message_main(
    argv: list[str] | None = None,
    *,
    prog: str = "ccbot send_bot_message",
) -> int:
    parser = _build_parser(prog=prog)
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
                edit_message_id=args.edit_message_id,
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
                video_width=args.video_width,
                video_height=args.video_height,
                video_duration=args.video_duration,
                video_thumbnail_path=args.video_thumbnail_path,
                video_supports_streaming=args.video_supports_streaming,
                video_auto_probe=args.video_auto_probe,
            )
        )
    except ValueError as exc:
        parser.exit(1, f"Error: {exc}\n")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result.get("status") != "success":
            return 1
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
