import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

from ccbot import draft_streaming
from ccbot.config import config
from ccbot.draft_streaming import (
    draft_id_for,
    is_draft_text_safe_to_show,
    mark_draft_surface_supported,
    maybe_clear_verified_draft_preview,
    maybe_send_draft_preview,
)
from ccbot.runtime_types import ASSISTANT_FINAL_SEMANTIC_KIND
from ccbot.handlers import message_queue as mq
from ccbot.handlers.message_queue import MessageTask


@pytest.fixture(autouse=True)
def draft_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "config_dir", tmp_path)
    monkeypatch.setattr(config, "telegram_delivery_audit_file", tmp_path / "audit.jsonl")
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "off", raising=False)
    monkeypatch.setattr(config, "telegram_draft_preview_allowed_surfaces", set(), raising=False)
    monkeypatch.setattr(config, "telegram_draft_preview_clear_allowed_surfaces", set(), raising=False)
    monkeypatch.setattr(config, "telegram_draft_preview_min_interval_seconds", 0.5, raising=False)
    monkeypatch.setattr(config, "telegram_draft_preview_retry_cooldown_seconds", 30, raising=False)
    monkeypatch.setattr(config, "telegram_draft_preview_timeout_cooldown_seconds", 10, raising=False)
    draft_streaming.clear_draft_preview_state()
    mq._status_msg_info.clear()
    mq._pre_final_visible_closed.clear()
    mq._technical_status_closed.clear()
    mq._turn_generations.clear()
    yield
    draft_streaming.clear_draft_preview_state()
    mq._status_msg_info.clear()
    mq._pre_final_visible_closed.clear()
    mq._technical_status_closed.clear()
    mq._turn_generations.clear()


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@pytest.mark.asyncio
async def test_send_message_draft_success_audits_transport_preview(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))

    result = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="Generating next paragraph",
        turn_generation=7,
        lane="technical_status",
        source_content_type="status",
        source_semantic_kind="technical_status",
    )

    assert result.sent is True
    assert result.draft_id and result.draft_id > 0
    call = bot.send_message_draft.await_args.kwargs
    assert call["chat_id"] == 123
    assert call["draft_id"] == result.draft_id
    assert "Generating" in call["text"]
    rows = _rows(config.telegram_delivery_audit_file)
    assert rows[-1]["action"] == "draft_preview"
    assert rows[-1]["content_type"] == "draft_preview"
    assert rows[-1]["semantic_kind"] == "telegram_draft_preview"
    assert rows[-1]["media"]["source_semantic_kind"] == "technical_status"


@pytest.mark.asyncio
async def test_send_message_draft_falls_back_to_plain_text_on_format_error(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    bot = SimpleNamespace(
        send_message_draft=AsyncMock(
            side_effect=[BadRequest("Can't parse entities"), True]
        )
    )

    result = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="**partial**",
        turn_generation=1,
        lane="technical_status",
    )

    assert result.sent is True
    assert bot.send_message_draft.await_count == 2
    first, second = bot.send_message_draft.await_args_list
    assert first.kwargs.get("parse_mode") == "MarkdownV2"
    assert "parse_mode" not in second.kwargs


@pytest.mark.asyncio
async def test_send_message_draft_retryafter_sets_surface_cooldown(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    bot = SimpleNamespace(send_message_draft=AsyncMock(side_effect=RetryAfter(5)))

    first = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="Working",
        turn_generation=1,
        lane="technical_status",
    )
    second = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="Working again",
        turn_generation=1,
        lane="technical_status",
    )

    assert first.status == "cooldown"
    assert second.status == "cooldown"
    assert bot.send_message_draft.await_count == 1


@pytest.mark.asyncio
async def test_send_message_draft_timeout_sets_short_degraded_backoff(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    monkeypatch.setattr(config, "telegram_draft_preview_timeout_cooldown_seconds", 3, raising=False)
    bot = SimpleNamespace(send_message_draft=AsyncMock(side_effect=TimedOut("slow")))

    result = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="Working",
        turn_generation=1,
        lane="technical_status",
    )

    assert result.status == "failed"
    assert draft_streaming._state.cooldown_until["c:123"] > 0


@pytest.mark.asyncio
async def test_send_message_draft_unsupported_method_disables_surface(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    bot = SimpleNamespace()

    result = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="Working",
        turn_generation=1,
        lane="technical_status",
    )

    assert result.status == "unsupported"
    caps = json.loads((config.config_dir / "draft_preview_capabilities.json").read_text())
    assert caps["c:123"]["status"] == "unsupported"


def test_draft_id_is_stable_per_surface_generation_lane_and_changes_next_turn():
    first = draft_id_for(chat_id=123, thread_id=5, turn_generation=1, lane="technical_status")
    assert first == draft_id_for(chat_id=123, thread_id=5, turn_generation=1, lane="technical_status")
    assert first != draft_id_for(chat_id=123, thread_id=5, turn_generation=2, lane="technical_status")
    assert first != draft_id_for(chat_id=123, thread_id=5, turn_generation=1, lane="commentary")
    assert first > 0


@pytest.mark.asyncio
async def test_draft_preview_is_latest_only_and_debounced(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    monkeypatch.setattr(config, "telegram_draft_preview_min_interval_seconds", 60, raising=False)
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))

    first = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="first",
        turn_generation=1,
        lane="technical_status",
    )
    second = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        text="second",
        turn_generation=1,
        lane="technical_status",
    )

    assert first.status == "sent"
    assert second.status == "debounced"
    assert bot.send_message_draft.await_count == 1
    assert draft_streaming._state.pending_text[("c:123", 1, "technical_status")] == "second"


@pytest.mark.asyncio
async def test_draft_preview_stops_on_final_answer_success_without_assuming_empty_clear(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))

    result = await maybe_clear_verified_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        turn_generation=1,
        lane="technical_status",
    )

    assert result.status == "closed"
    bot.send_message_draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_clear_attempt_is_best_effort_and_not_delivery_proof(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    monkeypatch.setattr(
        config,
        "telegram_draft_preview_clear_allowed_surfaces",
        {"c:123"},
        raising=False,
    )
    mark_draft_surface_supported("c:123", clear_safe=True)
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))

    result = await maybe_clear_verified_draft_preview(
        bot,
        user_id=1,
        chat_id=123,
        thread_id=None,
        surface_key=None,
        window_id="@1",
        turn_generation=1,
        lane="technical_status",
    )

    assert result.status == "sent"
    assert bot.send_message_draft.await_args.kwargs["text"] == ""
    rows = _rows(config.telegram_delivery_audit_file)
    assert rows[-1]["semantic_kind"] == "telegram_draft_preview"
    assert rows[-1]["media"]["source_content_type"] == "draft_preview_clear"


@pytest.mark.asyncio
async def test_probe_requires_operator_approved_surface(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "probe", raising=False)
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))

    blocked = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=-100,
        thread_id=42,
        surface_key="t:-100:42",
        window_id="@1",
        text="Working",
        turn_generation=1,
        lane="technical_status",
    )
    monkeypatch.setattr(
        config,
        "telegram_draft_preview_allowed_surfaces",
        {"t:-100:42"},
        raising=False,
    )
    allowed = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=-100,
        thread_id=42,
        surface_key="t:-100:42",
        window_id="@1",
        text="Working",
        turn_generation=1,
        lane="technical_status",
    )

    assert blocked.status == "surface_not_allowed"
    assert allowed.status == "sent"
    assert bot.send_message_draft.await_count == 1


@pytest.mark.asyncio
async def test_group_topic_support_not_inferred_from_ptb_signature(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    monkeypatch.setattr(
        config,
        "telegram_draft_preview_allowed_surfaces",
        {"t:-100:42"},
        raising=False,
    )
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))

    blocked = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=-100,
        thread_id=42,
        surface_key="t:-100:42",
        window_id="@1",
        text="Working",
        turn_generation=1,
        lane="technical_status",
    )
    mark_draft_surface_supported("t:-100:42")
    sent = await maybe_send_draft_preview(
        bot,
        user_id=1,
        chat_id=-100,
        thread_id=42,
        surface_key="t:-100:42",
        window_id="@1",
        text="Working",
        turn_generation=1,
        lane="technical_status",
    )

    assert blocked.status == "surface_not_allowed"
    assert blocked.reason == "surface_not_capability_proven"
    assert sent.status == "sent"
    assert bot.send_message_draft.await_count == 1


def test_draft_preview_rejects_hidden_internal_and_raw_control_payloads():
    assert not is_draft_text_safe_to_show("<skill><name>hidden</name></skill>")
    assert not is_draft_text_safe_to_show("↳ Tool Output\nsecret-ish debug")
    assert not is_draft_text_safe_to_show("OPENAI_API_KEY=sk-secretvalue000000")
    assert not is_draft_text_safe_to_show("[reasoning]")
    assert is_draft_text_safe_to_show("Working on the next paragraph")


@pytest.mark.asyncio
async def test_final_answer_never_uses_draft_as_terminal_delivery(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    bot = SimpleNamespace(send_message_draft=AsyncMock(return_value=True))
    sent = SimpleNamespace(message_id=99)
    task = MessageTask(
        task_type="content",
        window_id="@1",
        parts=["Final answer"],
        content_type="text",
        semantic_kind=ASSISTANT_FINAL_SEMANTIC_KIND,
        turn_generation=1,
        chat_id=123,
    )

    monkeypatch.setattr(mq, "_is_task_binding_active", AsyncMock(return_value=True))
    monkeypatch.setattr(mq, "current_turn_generation", lambda *a, **k: 1)
    monkeypatch.setattr(mq, "send_with_fallback", AsyncMock(return_value=sent))
    monkeypatch.setattr(mq, "_do_clear_image_preview_message", AsyncMock(return_value=True))
    monkeypatch.setattr(mq, "_do_clear_status_message", AsyncMock(return_value=None))

    await mq._process_content_task(bot, 1, task)

    bot.send_message_draft.assert_not_awaited()
    mq.send_with_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_draft_on_mode_can_suppress_durable_edit(monkeypatch):
    monkeypatch.setattr(config, "telegram_draft_preview_mode", "on", raising=False)
    task = MessageTask(
        task_type="status_update",
        window_id="@1",
        text="Working on response",
        turn_generation=1,
        chat_id=123,
    )
    bot = SimpleNamespace(edit_message_text=AsyncMock(), send_message_draft=AsyncMock())

    mq._status_msg_info[(1, "chat:123")] = (55, "@1", "Old status")
    monkeypatch.setattr(mq, "_is_task_binding_active", AsyncMock(return_value=True))
    monkeypatch.setattr(mq, "current_turn_generation", lambda *a, **k: 1)
    monkeypatch.setattr(
        mq,
        "maybe_send_draft_preview",
        AsyncMock(return_value=SimpleNamespace(status="sent")),
    )

    await mq._process_status_update_task(bot, 1, task)

    mq.maybe_send_draft_preview.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
