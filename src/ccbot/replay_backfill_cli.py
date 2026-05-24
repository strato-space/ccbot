"""Operator-scoped replay/backfill CLI for missed terminal results.

The normal monitor is offset-based and must not be rewound casually. This CLI
lets an operator re-normalize an explicit replay slice/call_id and, after a
dry-run, deliver only operator-selected generated-image terminal media or text
assistant-final artifacts to Telegram with their own audit rows.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Iterable

from .config import config
from .codex_rollout import CodexRolloutNormalizer
from .delivery_audit import log_telegram_delivery
from .runtime_types import (
    ASSISTANT_FINAL_SEMANTIC_KIND,
    GENERATED_IMAGE_PREVIEW_CONTENT_TYPE,
    NormalizedEvent,
)
from .send_bot_message import send_bot_message
from .transcript_parser import TranscriptParser


@dataclass(frozen=True)
class ReplayRecord:
    """A parsed replay JSONL record plus byte-range provenance."""

    offset: int
    end_offset: int
    data: dict[str, Any]


@dataclass(frozen=True)
class BackfillCandidate:
    """A generated-image media event selected for replay delivery."""

    replay_path: Path
    offset: int
    end_offset: int
    thread_id: str
    call_id: str
    event: NormalizedEvent

    @property
    def media_sha256(self) -> str:
        if not self.event.image_data:
            return ""
        media_type, raw_bytes = self.event.image_data[0]
        digest = hashlib.sha256()
        digest.update(media_type.encode("utf-8", "replace"))
        digest.update(b"\0")
        digest.update(raw_bytes)
        return digest.hexdigest()


@dataclass(frozen=True)
class TextBackfillCandidate:
    """An assistant-final text event selected for replay delivery."""

    replay_path: Path
    offset: int
    end_offset: int
    thread_id: str
    turn_id: str
    event: NormalizedEvent

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.event.text.encode("utf-8")).hexdigest()


def _build_parser(prog: str = "ccbot replay-backfill") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Dry-run or deliver missed Codex terminal artifacts from an explicit "
            "replay JSONL slice without rewinding monitor state. Defaults to "
            "generated-image media; use --text-final for assistant-final text."
        ),
    )
    parser.add_argument("--replay-path", required=True, help="Codex rollout JSONL path")
    parser.add_argument(
        "--thread-id",
        help="Persisted Codex thread id; defaults to the replay filename stem",
    )
    parser.add_argument(
        "--call-id",
        action="append",
        default=[],
        help="Only consider this image_generation_end call_id (repeatable)",
    )
    parser.add_argument(
        "--byte-range",
        help="Optional byte range START:END, START:, or :END over the replay file",
    )
    parser.add_argument(
        "--deliver",
        action="store_true",
        help="Actually deliver selected replay candidate to Telegram. Default is dry-run.",
    )
    parser.add_argument(
        "--text-final",
        action="store_true",
        help=(
            "Replay assistant-final text instead of generated-image media. Requires "
            "--byte-range, --turn-id, or --text-sha256 to avoid broad delivery."
        ),
    )
    parser.add_argument(
        "--turn-id",
        action="append",
        default=[],
        help="Only consider this Codex turn_id when --text-final is used (repeatable)",
    )
    parser.add_argument(
        "--text-sha256",
        action="append",
        default=[],
        help="Only consider assistant-final text with this sha256 when --text-final is used",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Deliver even if a matching replay_backfill audit row already exists.",
    )
    parser.add_argument("--chat-id")
    parser.add_argument("--thread-id-telegram", "--message-thread-id", dest="message_thread_id")
    parser.add_argument("--surface-key")
    parser.add_argument("--user-id")
    parser.add_argument("--state-file")
    parser.add_argument("--token")
    parser.add_argument("--json", action="store_true", help="Print machine-readable result")
    return parser


def _parse_byte_range(raw: str | None) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    if ":" not in raw:
        raise ValueError("--byte-range must use START:END, START:, or :END")
    start_text, end_text = raw.split(":", 1)
    start = int(start_text) if start_text.strip() else None
    end = int(end_text) if end_text.strip() else None
    if start is not None and start < 0:
        raise ValueError("--byte-range start must be non-negative")
    if end is not None and end < 0:
        raise ValueError("--byte-range end must be non-negative")
    if start is not None and end is not None and end < start:
        raise ValueError("--byte-range end must be greater than or equal to start")
    return start, end


def _record_call_id(data: dict[str, Any]) -> str:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return ""
    value = payload.get("call_id") or payload.get("id") or ""
    return str(value).strip()


def _record_turn_id(data: dict[str, Any]) -> str:
    for key in ("turn_id", "turnId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return ""
    for key in ("turn_id", "turnId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for key in ("turn_id", "turnId"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _record_thread_id(data: dict[str, Any]) -> str:
    for key in ("id", "thread_id", "threadId", "conversation_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return ""
    for key in ("id", "thread_id", "threadId", "conversation_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = payload.get("meta")
    if isinstance(meta, dict):
        for key in ("id", "thread_id", "threadId"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _is_generated_image_end(data: dict[str, Any]) -> bool:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return False
    return str(payload.get("type") or "").strip() == "image_generation_end"


def _read_replay_records(
    replay_path: Path,
    *,
    byte_start: int | None = None,
    byte_end: int | None = None,
) -> list[ReplayRecord]:
    records: list[ReplayRecord] = []
    with replay_path.open("rb") as handle:
        if byte_start is not None:
            handle.seek(byte_start)
            if byte_start > 0:
                # Never parse from the middle of a JSON line. If START points
                # exactly at a JSON object, keep it; otherwise advance to the
                # next complete line.
                first = handle.read(1)
                if first != b"{":
                    handle.readline()
                else:
                    handle.seek(byte_start)
        while True:
            offset = handle.tell()
            if byte_end is not None and offset >= byte_end:
                break
            raw_line = handle.readline()
            if not raw_line:
                break
            end_offset = handle.tell()
            if byte_end is not None and end_offset > byte_end:
                break
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                continue
            data = TranscriptParser.parse_line(line)
            if isinstance(data, dict):
                records.append(ReplayRecord(offset=offset, end_offset=end_offset, data=data))
    return records


def _candidate_from_record(
    *,
    replay_path: Path,
    thread_id: str,
    record: ReplayRecord,
) -> BackfillCandidate | None:
    call_id = _record_call_id(record.data)
    events = CodexRolloutNormalizer.normalize_records([record.data], thread_id=thread_id)
    for event in events:
        if (
            event.content_type == GENERATED_IMAGE_PREVIEW_CONTENT_TYPE
            and event.image_data
        ):
            return BackfillCandidate(
                replay_path=replay_path,
                offset=record.offset,
                end_offset=record.end_offset,
                thread_id=thread_id,
                call_id=call_id,
                event=event,
            )
    return None


def collect_candidates(
    *,
    replay_path: Path,
    thread_id: str,
    call_ids: Iterable[str] = (),
    byte_start: int | None = None,
    byte_end: int | None = None,
) -> list[BackfillCandidate]:
    """Collect deliverable generated-image candidates from an explicit slice."""

    wanted_call_ids = {call_id.strip() for call_id in call_ids if call_id.strip()}
    candidates: list[BackfillCandidate] = []
    for record in _read_replay_records(
        replay_path,
        byte_start=byte_start,
        byte_end=byte_end,
    ):
        if not _is_generated_image_end(record.data):
            continue
        call_id = _record_call_id(record.data)
        if wanted_call_ids and call_id not in wanted_call_ids:
            continue
        candidate = _candidate_from_record(
            replay_path=replay_path,
            thread_id=thread_id,
            record=record,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _text_candidate_from_record(
    *,
    replay_path: Path,
    thread_id: str,
    record: ReplayRecord,
) -> TextBackfillCandidate | None:
    explicit_thread_id = _record_thread_id(record.data)
    if explicit_thread_id and explicit_thread_id != thread_id:
        return None
    events = CodexRolloutNormalizer.normalize_records([record.data], thread_id=thread_id)
    for event in events:
        if (
            event.semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND
            and event.role == "assistant"
            and event.text.strip()
        ):
            return TextBackfillCandidate(
                replay_path=replay_path,
                offset=record.offset,
                end_offset=record.end_offset,
                thread_id=thread_id,
                turn_id=_record_turn_id(record.data),
                event=event,
            )
    return None


def collect_text_candidates(
    *,
    replay_path: Path,
    thread_id: str,
    turn_ids: Iterable[str] = (),
    text_sha256: Iterable[str] = (),
    byte_start: int | None = None,
    byte_end: int | None = None,
) -> list[TextBackfillCandidate]:
    """Collect assistant-final text candidates from an explicit replay slice."""

    wanted_turn_ids = {turn_id.strip() for turn_id in turn_ids if turn_id.strip()}
    wanted_hashes = {text_hash.strip() for text_hash in text_sha256 if text_hash.strip()}
    candidates: list[TextBackfillCandidate] = []
    seen: set[tuple[str, str]] = set()
    for record in _read_replay_records(
        replay_path,
        byte_start=byte_start,
        byte_end=byte_end,
    ):
        candidate = _text_candidate_from_record(
            replay_path=replay_path,
            thread_id=thread_id,
            record=record,
        )
        if candidate is None:
            continue
        if wanted_turn_ids and candidate.turn_id not in wanted_turn_ids:
            continue
        if wanted_hashes and candidate.text_sha256 not in wanted_hashes:
            continue
        dedupe_key = (candidate.turn_id, candidate.text_sha256)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        candidates.append(candidate)
    return candidates


def _audit_key(candidate: BackfillCandidate) -> dict[str, Any]:
    return {
        "replay_path": str(candidate.replay_path),
        "byte_offset": candidate.offset,
        "byte_end_offset": candidate.end_offset,
        "thread_id": candidate.thread_id,
        "call_id": candidate.call_id,
        "media_sha256": candidate.media_sha256,
    }


def _text_audit_key(candidate: TextBackfillCandidate) -> dict[str, Any]:
    return {
        "replay_path": str(candidate.replay_path),
        "byte_offset": candidate.offset,
        "byte_end_offset": candidate.end_offset,
        "thread_id": candidate.thread_id,
        "turn_id": candidate.turn_id,
        "text_sha256": candidate.text_sha256,
    }


def _already_delivered(candidate: BackfillCandidate, *, audit_path: Path | None = None) -> bool:
    path = audit_path or config.telegram_delivery_audit_file
    if not path.exists():
        return False
    expected = _audit_key(candidate)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("action") != "replay_backfill" or not row.get("success"):
                    continue
                media = row.get("media")
                marker = media.get("replay_backfill") if isinstance(media, dict) else None
                if not isinstance(marker, dict):
                    continue
                if (
                    marker.get("replay_path") == expected["replay_path"]
                    and marker.get("call_id") == expected["call_id"]
                    and marker.get("media_sha256") == expected["media_sha256"]
                ):
                    return True
    except OSError:
        return False
    return False


def _text_already_delivered(
    candidate: TextBackfillCandidate,
    *,
    audit_path: Path | None = None,
) -> bool:
    path = audit_path or config.telegram_delivery_audit_file
    if not path.exists():
        return False
    expected = _text_audit_key(candidate)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("action") != "replay_backfill_text" or not row.get("success"):
                    continue
                media = row.get("media")
                marker = media.get("replay_backfill_text") if isinstance(media, dict) else None
                if not isinstance(marker, dict):
                    continue
                if (
                    marker.get("replay_path") == expected["replay_path"]
                    and marker.get("turn_id") == expected["turn_id"]
                    and marker.get("text_sha256") == expected["text_sha256"]
                ):
                    return True
    except OSError:
        return False
    return False


def _candidate_json(candidate: BackfillCandidate, *, status: str) -> dict[str, Any]:
    media_type = candidate.event.image_data[0][0] if candidate.event.image_data else None
    return {
        "status": status,
        "replay_path": str(candidate.replay_path),
        "byte_offset": candidate.offset,
        "byte_end_offset": candidate.end_offset,
        "thread_id": candidate.thread_id,
        "call_id": candidate.call_id,
        "content_type": candidate.event.content_type,
        "tool_use_id": candidate.event.tool_use_id,
        "media_type": media_type,
        "media_sha256": candidate.media_sha256,
        "caption": candidate.event.image_caption,
    }


def _text_candidate_json(
    candidate: TextBackfillCandidate,
    *,
    status: str,
) -> dict[str, Any]:
    duplicate_key = _text_audit_key(candidate)
    return {
        "status": status,
        "replay_path": str(candidate.replay_path),
        "byte_offset": candidate.offset,
        "byte_end_offset": candidate.end_offset,
        "thread_id": candidate.thread_id,
        "turn_id": candidate.turn_id,
        "content_type": candidate.event.content_type,
        "semantic_kind": candidate.event.semantic_kind,
        "text": candidate.event.text,
        "text_sha256": candidate.text_sha256,
        "duplicate_key": duplicate_key,
    }


async def _deliver_candidate(candidate: BackfillCandidate, args: argparse.Namespace) -> dict[str, Any]:
    media_type, raw_bytes = candidate.event.image_data[0]
    suffix = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(media_type, ".img")
    filename = f"{candidate.call_id or 'generated-image'}{suffix}"
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    result = await send_bot_message(
        message=candidate.event.image_caption or candidate.event.text or "",
        chat_id=args.chat_id,
        message_thread_id=args.message_thread_id,
        user_id=args.user_id,
        surface_key=args.surface_key,
        token=args.token,
        state_path=args.state_file,
        file_base64=encoded,
        file_type="photo",
        filename=filename,
    )
    success = result.get("status") == "success"
    log_telegram_delivery(
        action="replay_backfill",
        user_id=int(args.user_id) if args.user_id and str(args.user_id).lstrip("-").isdigit() else None,
        chat_id=int(result["chat_id"]) if str(result.get("chat_id", "")).lstrip("-").isdigit() else None,
        thread_id=int(result["thread_id"]) if str(result.get("thread_id", "")).isdigit() else None,
        message_id=int(result["message_id"]) if str(result.get("message_id", "")).isdigit() else None,
        task_type="operator_replay_backfill",
        content_type=candidate.event.content_type,
        semantic_kind=candidate.event.semantic_kind,
        text=candidate.event.image_caption or candidate.event.text or "",
        success=success,
        error=None if success else str(result.get("message") or ""),
        reason="operator_selected_replay_slice",
        tool_use_id=candidate.call_id or candidate.event.tool_use_id,
        media={"replay_backfill": _audit_key(candidate), "send_result": result.get("media")},
    )
    return {**_candidate_json(candidate, status="delivered" if success else "error"), "result": result}


async def _deliver_text_candidate(
    candidate: TextBackfillCandidate,
    args: argparse.Namespace,
) -> dict[str, Any]:
    result = await send_bot_message(
        message=candidate.event.text,
        chat_id=args.chat_id,
        message_thread_id=args.message_thread_id,
        user_id=args.user_id,
        surface_key=args.surface_key,
        token=args.token,
        state_path=args.state_file,
    )
    success = result.get("status") == "success"
    log_telegram_delivery(
        action="replay_backfill_text",
        user_id=int(args.user_id) if args.user_id and str(args.user_id).lstrip("-").isdigit() else None,
        chat_id=int(result["chat_id"]) if str(result.get("chat_id", "")).lstrip("-").isdigit() else None,
        thread_id=int(result["thread_id"]) if str(result.get("thread_id", "")).isdigit() else None,
        message_id=int(result["message_id"]) if str(result.get("message_id", "")).isdigit() else None,
        task_type="operator_replay_backfill_text",
        content_type=candidate.event.content_type,
        semantic_kind=candidate.event.semantic_kind,
        text=candidate.event.text,
        success=success,
        error=None if success else str(result.get("message") or ""),
        reason="operator_selected_text_replay_slice",
        tool_use_id=candidate.turn_id or candidate.text_sha256,
        media={"replay_backfill_text": _text_audit_key(candidate)},
    )
    return {
        **_text_candidate_json(candidate, status="delivered" if success else "error"),
        "result": result,
    }


def _validate_text_selection(args: argparse.Namespace) -> None:
    if args.byte_range or getattr(args, "turn_id", None) or getattr(args, "text_sha256", None):
        return
    raise ValueError("--text-final requires --byte-range, --turn-id, or --text-sha256")


def _target_json(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "chat_id": args.chat_id,
        "message_thread_id": args.message_thread_id,
        "surface_key": args.surface_key,
        "user_id": args.user_id,
    }


def _has_delivery_target(args: argparse.Namespace) -> bool:
    return any(_target_json(args).values())


async def _run_text_backfill(
    *,
    replay_path: Path,
    thread_id: str,
    byte_start: int | None,
    byte_end: int | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    _validate_text_selection(args)
    if args.deliver and not _has_delivery_target(args):
        raise ValueError(
            "--text-final --deliver requires --chat-id, --surface-key, or --user-id"
        )
    candidates = collect_text_candidates(
        replay_path=replay_path,
        thread_id=thread_id,
        turn_ids=getattr(args, "turn_id", ()),
        text_sha256=getattr(args, "text_sha256", ()),
        byte_start=byte_start,
        byte_end=byte_end,
    )
    if len(candidates) > 1 and not (getattr(args, "turn_id", None) or getattr(args, "text_sha256", None)):
        raise ValueError(
            "text replay selection matched multiple assistant finals; add --turn-id, "
            "--text-sha256, or a narrower --byte-range"
        )
    result: dict[str, Any] = {
        "mode": "deliver" if args.deliver else "dry_run",
        "replay_path": str(replay_path),
        "thread_id": thread_id,
        "target": _target_json(args),
        "candidate_count": len(candidates),
        "candidate_type": "assistant_final_text",
        "candidates": [],
    }
    for candidate in candidates:
        if not args.force and _text_already_delivered(candidate):
            result["candidates"].append(
                _text_candidate_json(candidate, status="duplicate_skipped")
            )
            continue
        if not args.deliver:
            result["candidates"].append(_text_candidate_json(candidate, status="dry_run"))
            continue
        result["candidates"].append(await _deliver_text_candidate(candidate, args))
    return result


async def run_backfill(args: argparse.Namespace) -> dict[str, Any]:
    replay_path = Path(args.replay_path).expanduser().resolve(strict=True)
    thread_id = (args.thread_id or replay_path.stem).strip()
    byte_start, byte_end = _parse_byte_range(args.byte_range)
    if getattr(args, "text_final", False):
        return await _run_text_backfill(
            replay_path=replay_path,
            thread_id=thread_id,
            byte_start=byte_start,
            byte_end=byte_end,
            args=args,
        )
    candidates = collect_candidates(
        replay_path=replay_path,
        thread_id=thread_id,
        call_ids=args.call_id,
        byte_start=byte_start,
        byte_end=byte_end,
    )
    result: dict[str, Any] = {
        "mode": "deliver" if args.deliver else "dry_run",
        "replay_path": str(replay_path),
        "thread_id": thread_id,
        "candidate_count": len(candidates),
        "candidates": [],
    }
    for candidate in candidates:
        if not args.force and _already_delivered(candidate):
            result["candidates"].append(_candidate_json(candidate, status="duplicate_skipped"))
            continue
        if not args.deliver:
            result["candidates"].append(_candidate_json(candidate, status="dry_run"))
            continue
        result["candidates"].append(await _deliver_candidate(candidate, args))
    return result


def replay_backfill_main(
    argv: list[str] | None = None,
    *,
    prog: str = "ccbot replay-backfill",
) -> int:
    parser = _build_parser(prog=prog)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(run_backfill(args))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(
            f"{result['mode']}: {result['candidate_count']} candidate(s) from "
            f"{result['replay_path']}"
        )
        for candidate in result["candidates"]:
            if result.get("candidate_type") == "assistant_final_text":
                print(
                    f"- {candidate['status']} offset={candidate['byte_offset']} "
                    f"turn_id={candidate['turn_id']} text_sha256={candidate['text_sha256']}"
                )
                continue
            print(
                f"- {candidate['status']} offset={candidate['byte_offset']} "
                f"call_id={candidate['call_id']} media={candidate['media_type']}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(replay_backfill_main(sys.argv[1:]))
