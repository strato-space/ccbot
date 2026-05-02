import asyncio
import base64
import json
from types import SimpleNamespace

from ccbot import send_bot_message as sender


class _FakeBot:
    def __init__(self, token: str, captured: dict):
        self.token = token
        self.captured = captured

    async def send_message(self, **kwargs):
        self.captured.update(kwargs)
        chat_id = kwargs.get("chat_id")
        return SimpleNamespace(
            message_id=99,
            message_thread_id=kwargs.get("message_thread_id"),
            chat=SimpleNamespace(id=chat_id),
        )

    async def _send_attachment(self, kind: str, attachment, **kwargs):
        self.captured.update(kwargs)
        self.captured["attachment_kind"] = kind
        self.captured["attachment_filename"] = getattr(attachment, "filename", None)
        self.captured["attachment_bytes"] = getattr(
            attachment,
            "input_file_content",
            None,
        )
        chat_id = kwargs.get("chat_id")
        return SimpleNamespace(
            message_id=101,
            message_thread_id=kwargs.get("message_thread_id"),
            chat=SimpleNamespace(id=chat_id),
        )

    async def send_document(self, document, **kwargs):
        return await self._send_attachment("document", document, **kwargs)

    async def send_photo(self, photo, **kwargs):
        return await self._send_attachment("photo", photo, **kwargs)

    async def send_video(self, video, **kwargs):
        return await self._send_attachment("video", video, **kwargs)

    async def send_audio(self, audio, **kwargs):
        return await self._send_attachment("audio", audio, **kwargs)

    async def send_animation(self, animation, **kwargs):
        return await self._send_attachment("animation", animation, **kwargs)


def _write_state(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_resolve_delivery_target_prefers_no_topics_main_chat(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {
            "surface_bindings": {
                "12345": {
                    "t:42": "@7",
                    "c:-100200300": "@8",
                }
            },
            "group_chat_ids": {"12345:42": -100200300},
        },
    )

    target = sender.resolve_delivery_target(state_path=state_path)

    assert target.chat_id == -100200300
    assert target.message_thread_id is None
    assert target.surface_key == "c:-100200300"


def test_resolve_delivery_target_single_topic_uses_group_coordinates(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"t:42": "@7"}},
            "group_chat_ids": {"12345:42": -100200300},
        },
    )

    target = sender.resolve_delivery_target(state_path=state_path)

    assert target.chat_id == -100200300
    assert target.message_thread_id == 42
    assert target.surface_key == "t:42"


def test_resolve_delivery_target_ambiguous_requires_explicit_target(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"t:42": "@7", "t:43": "@8"}},
            "group_chat_ids": {
                "12345:42": -100200300,
                "12345:43": -100200300,
            },
        },
    )

    try:
        sender.resolve_delivery_target(state_path=state_path)
    except sender.DeliveryTargetError as exc:
        assert "Cannot resolve a unique ccbot delivery target" in str(exc)
    else:
        raise AssertionError("ambiguous target should fail closed")


def test_send_bot_message_document_from_default_state(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.tar.gz"
    report_path.write_bytes(b"tarball")
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"t:42": "@7"}},
            "group_chat_ids": {"12345:42": -100200300},
        },
    )
    captured: dict = {}

    def _bot_factory(token: str):
        assert token == "token-123"
        return _FakeBot(token, captured)

    monkeypatch.setattr(sender, "Bot", _bot_factory)

    result = asyncio.run(
        sender.send_bot_message(
            message="result archive",
            token="token-123",
            file_path=str(report_path),
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert result["chat_id"] == "-100200300"
    assert result["thread_id"] == 42
    assert result["url"] == "https://t.me/c/200300/42/101"
    assert captured["chat_id"] == -100200300
    assert captured["message_thread_id"] == 42
    assert captured["attachment_kind"] == "document"
    assert captured["attachment_filename"] == "report.tar.gz"
    assert captured["caption"] == "result archive"


def test_send_bot_message_file_base64_photo(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    def _bot_factory(token: str):
        assert token == "token-456"
        return _FakeBot(token, captured)

    monkeypatch.setattr(sender, "Bot", _bot_factory)
    payload = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode("ascii")

    result = asyncio.run(
        sender.send_bot_message(
            message="",
            token="token-456",
            file_base64=payload,
            file_type="photo",
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert result["chat_id"] == "-100200300"
    assert "thread_id" not in result
    assert captured["attachment_kind"] == "photo"
    assert captured["attachment_filename"] == "attachment.png"
    assert captured["attachment_bytes"] == b"\x89PNG"
