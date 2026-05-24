from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

import pytest

from ccbot import replay_backfill_cli as cli
from ccbot import config as config_mod


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _record(call_id: str, prompt: str = "Primary request: frame") -> dict:
    return {
        "timestamp": "2026-05-22T08:30:06.540Z",
        "type": "event_msg",
        "payload": {
            "type": "image_generation_end",
            "call_id": call_id,
            "status": "completed",
            "revised_prompt": prompt,
            "result": base64.b64encode(_PNG_BYTES).decode("ascii"),
        },
    }


def _assistant_final(text: str, *, turn_id: str = "turn-1", thread_id: str = "thread-1") -> dict:
    return {
        "timestamp": "2026-05-24T09:22:54.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "turn_id": turn_id,
            "meta": {"thread_id": thread_id},
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _write_jsonl(path: Path, records: list[dict]) -> list[int]:
    offsets: list[int] = []
    with path.open("wb") as handle:
        for record in records:
            offsets.append(handle.tell())
            handle.write(json.dumps(record).encode("utf-8") + b"\n")
    return offsets


def test_collect_candidates_filters_by_call_id_and_range(tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    offsets = _write_jsonl(replay, [_record("ig_first"), _record("ig_second")])

    candidates = cli.collect_candidates(
        replay_path=replay,
        thread_id="thread-1",
        call_ids=["ig_second"],
        byte_start=offsets[1],
    )

    assert [candidate.call_id for candidate in candidates] == ["ig_second"]
    assert candidates[0].offset == offsets[1]
    assert candidates[0].event.image_data == [("image/png", _PNG_BYTES)]


def test_replay_backfill_dry_run_does_not_deliver(monkeypatch, tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    _write_jsonl(replay, [_record("ig_first")])
    calls: list[dict] = []

    async def fake_send_bot_message(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "chat_id": "-1001", "message_id": 10}

    monkeypatch.setattr(cli, "send_bot_message", fake_send_bot_message)
    args = argparse.Namespace(
        replay_path=str(replay),
        thread_id="thread-1",
        call_id=[],
        byte_range=None,
        deliver=False,
        force=False,
        chat_id="-1001",
        message_thread_id="42",
        surface_key=None,
        user_id=None,
        state_file=None,
        token="token",
        json=True,
    )

    result = asyncio.run(cli.run_backfill(args))

    assert result["mode"] == "dry_run"
    assert result["candidates"][0]["status"] == "dry_run"
    assert calls == []


def test_replay_backfill_delivers_and_records_dedupe_audit(monkeypatch, tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    _write_jsonl(replay, [_record("ig_first")])
    audit = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(config_mod.config, "telegram_delivery_audit_file", audit)
    monkeypatch.setattr(cli.config, "telegram_delivery_audit_file", audit)
    calls: list[dict] = []

    async def fake_send_bot_message(**kwargs):
        calls.append(kwargs)
        return {
            "status": "success",
            "chat_id": "-1001",
            "thread_id": 42,
            "message_id": 10,
        }

    monkeypatch.setattr(cli, "send_bot_message", fake_send_bot_message)
    args = argparse.Namespace(
        replay_path=str(replay),
        thread_id="thread-1",
        call_id=[],
        byte_range=None,
        deliver=True,
        force=False,
        chat_id="-1001",
        message_thread_id="42",
        surface_key=None,
        user_id=None,
        state_file=None,
        token="token",
        json=True,
    )

    first = asyncio.run(cli.run_backfill(args))
    second = asyncio.run(cli.run_backfill(args))

    assert first["candidates"][0]["status"] == "delivered"
    assert second["candidates"][0]["status"] == "duplicate_skipped"
    assert len(calls) == 1
    assert calls[0]["file_type"] == "photo"
    assert calls[0]["filename"] == "ig_first.png"
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    assert rows[-1]["action"] == "replay_backfill"
    assert rows[-1]["media"]["replay_backfill"]["call_id"] == "ig_first"


def test_text_replay_backfill_dry_run_selects_assistant_final(tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    offsets = _write_jsonl(
        replay,
        [_assistant_final("final one", turn_id="turn-1")],
    )
    args = argparse.Namespace(
        replay_path=str(replay),
        thread_id="thread-1",
        call_id=[],
        byte_range=f"{offsets[0]}:",
        text_final=True,
        turn_id=[],
        text_sha256=[],
        deliver=False,
        force=False,
        chat_id="-1001",
        message_thread_id="42",
        surface_key=None,
        user_id=None,
        state_file=None,
        token="token",
        json=True,
    )

    result = asyncio.run(cli.run_backfill(args))

    assert result["candidate_type"] == "assistant_final_text"
    assert result["target"] == {
        "chat_id": "-1001",
        "message_thread_id": "42",
        "surface_key": None,
        "user_id": None,
    }
    candidate = result["candidates"][0]
    assert candidate["status"] == "dry_run"
    assert candidate["turn_id"] == "turn-1"
    assert candidate["text"] == "final one"
    assert candidate["duplicate_key"]["text_sha256"] == candidate["text_sha256"]


def test_text_replay_backfill_delivers_and_records_text_audit(monkeypatch, tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    _write_jsonl(replay, [_assistant_final("missed final", turn_id="turn-1")])
    audit = tmp_path / "telegram_delivery_audit.jsonl"
    monkeypatch.setattr(config_mod.config, "telegram_delivery_audit_file", audit)
    monkeypatch.setattr(cli.config, "telegram_delivery_audit_file", audit)
    calls: list[dict] = []

    async def fake_send_bot_message(**kwargs):
        calls.append(kwargs)
        return {
            "status": "success",
            "chat_id": "-1001",
            "thread_id": 42,
            "message_id": 20,
        }

    monkeypatch.setattr(cli, "send_bot_message", fake_send_bot_message)
    args = argparse.Namespace(
        replay_path=str(replay),
        thread_id="thread-1",
        call_id=[],
        byte_range=None,
        text_final=True,
        turn_id=["turn-1"],
        text_sha256=[],
        deliver=True,
        force=False,
        chat_id="-1001",
        message_thread_id="42",
        surface_key=None,
        user_id=None,
        state_file=None,
        token="token",
        json=True,
    )

    first = asyncio.run(cli.run_backfill(args))
    second = asyncio.run(cli.run_backfill(args))

    assert first["candidates"][0]["status"] == "delivered"
    assert second["candidates"][0]["status"] == "duplicate_skipped"
    assert len(calls) == 1
    assert calls[0]["message"] == "missed final"
    assert "file_type" not in calls[0]
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    assert rows[-1]["action"] == "replay_backfill_text"
    assert rows[-1]["media"]["replay_backfill_text"]["turn_id"] == "turn-1"


def test_text_replay_backfill_wrong_thread_has_no_candidates(tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    _write_jsonl(
        replay,
        [_assistant_final("wrong thread final", turn_id="turn-1", thread_id="other")],
    )

    candidates = cli.collect_text_candidates(
        replay_path=replay,
        thread_id="thread-1",
        turn_ids=["turn-1"],
    )

    assert candidates == []


def test_text_replay_backfill_rejects_broad_range_with_multiple_finals(tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    _write_jsonl(
        replay,
        [
            _assistant_final("first final", turn_id="turn-1"),
            _assistant_final("second final", turn_id="turn-2"),
        ],
    )
    args = argparse.Namespace(
        replay_path=str(replay),
        thread_id="thread-1",
        call_id=[],
        byte_range="0:",
        text_final=True,
        turn_id=[],
        text_sha256=[],
        deliver=False,
        force=False,
        chat_id="-1001",
        message_thread_id="42",
        surface_key=None,
        user_id=None,
        state_file=None,
        token="token",
        json=True,
    )

    with pytest.raises(ValueError, match="multiple assistant finals"):
        asyncio.run(cli.run_backfill(args))


def test_text_replay_backfill_deliver_requires_target(tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    _write_jsonl(replay, [_assistant_final("final", turn_id="turn-1")])
    args = argparse.Namespace(
        replay_path=str(replay),
        thread_id="thread-1",
        call_id=[],
        byte_range=None,
        text_final=True,
        turn_id=["turn-1"],
        text_sha256=[],
        deliver=True,
        force=False,
        chat_id=None,
        message_thread_id=None,
        surface_key=None,
        user_id=None,
        state_file=None,
        token="token",
        json=True,
    )

    with pytest.raises(ValueError, match="requires --chat-id"):
        asyncio.run(cli.run_backfill(args))


def test_media_replay_backfill_ignores_text_records_by_default(tmp_path):
    replay = tmp_path / "thread-1.jsonl"
    _write_jsonl(replay, [_assistant_final("text only", turn_id="turn-1")])

    candidates = cli.collect_candidates(replay_path=replay, thread_id="thread-1")

    assert candidates == []
