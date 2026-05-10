import asyncio
import base64
import json
from types import SimpleNamespace

from ccbot import send_bot_message as sender
from ccbot import config as config_mod


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


def test_resolve_delivery_target_single_main_chat_surface(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"c:-100200300": "@8"}},
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


def test_resolve_delivery_target_topic_without_group_chat_id_fails_closed(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"t:42": "@7"}}},
    )

    try:
        sender.resolve_delivery_target(state_path=state_path)
    except sender.DeliveryTargetError as exc:
        assert "Cannot resolve Telegram group chat_id" in str(exc)
    else:
        raise AssertionError("topic without group chat_id must fail closed")


def test_resolve_delivery_target_user_thread_without_group_chat_id_fails_closed(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(state_path, {"group_chat_ids": {}})

    try:
        sender.resolve_delivery_target(
            user_id="12345",
            message_thread_id="42",
            state_path=state_path,
        )
    except sender.DeliveryTargetError as exc:
        assert "Cannot resolve Telegram group chat_id" in str(exc)
    else:
        raise AssertionError("--user-id/--thread-id without group chat_id must fail closed")


def test_resolve_delivery_target_explicit_chat_id_allows_thread_without_state(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(state_path, {})

    target = sender.resolve_delivery_target(
        chat_id="-100200300",
        message_thread_id="42",
        state_path=state_path,
    )

    assert target.chat_id == -100200300
    assert target.message_thread_id == 42
    assert target.reason == "explicit_chat_id"


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


def test_send_bot_message_gif_file_type_uses_animation_alias(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    gif_path = tmp_path / "result.gif"
    gif_path.write_bytes(b"GIF89a")
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    def _bot_factory(token: str):
        assert token == "token-456"
        return _FakeBot(token, captured)

    monkeypatch.setattr(sender, "Bot", _bot_factory)

    result = asyncio.run(
        sender.send_bot_message(
            message="animation result",
            token="token-456",
            file_path=str(gif_path),
            file_type="gif",
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert captured["attachment_kind"] == "animation"
    assert captured["attachment_filename"] == "result.gif"
    assert captured["caption"] == "animation result"


def test_send_bot_message_explicit_token_and_chat_id_does_not_need_config(
    monkeypatch,
    tmp_path,
):
    captured: dict = {}

    def _bot_factory(token: str):
        assert token == "token-explicit"
        return _FakeBot(token, captured)

    def _broken_config():
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

    monkeypatch.setattr(sender, "Bot", _bot_factory)
    monkeypatch.setattr(sender, "_get_config", _broken_config)

    result = asyncio.run(
        sender.send_bot_message(
            message="hello",
            token="token-explicit",
            chat_id="-100200300",
            message_thread_id="42",
            state_path=tmp_path / "missing-state.json",
        )
    )

    assert result["status"] == "success"
    assert captured["chat_id"] == -100200300
    assert captured["message_thread_id"] == 42


def test_send_bot_message_missing_token_returns_error_dict(monkeypatch):
    def _broken_config():
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.setattr(sender, "_get_config", _broken_config)

    result = asyncio.run(
        sender.send_bot_message(
            message="hello",
            chat_id="-100200300",
        )
    )

    assert result == {"status": "error", "message": "Missing TELEGRAM_BOT_TOKEN"}


def test_telegram_token_env_is_scrubbed_from_runtime_children():
    assert "TELEGRAM_TOKEN" in config_mod.SENSITIVE_ENV_VARS


def test_send_alias_help_uses_short_command_name():
    help_text = sender._build_parser(prog="ccbot send").format_help()

    assert "usage: ccbot send " in help_text
    assert "usage: ccbot send_bot_message" not in help_text


def test_legacy_send_bot_message_help_keeps_legacy_command_name():
    help_text = sender._build_parser().format_help()

    assert "usage: ccbot send_bot_message " in help_text


def test_send_bot_message_edit_text_message(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    class FakeBot:
        def __init__(self, token):
            captured["token"] = token

        async def edit_message_text(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message_id=777,
                chat=SimpleNamespace(id=kwargs["chat_id"], username=None),
            )

    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="updated text",
            token="token-edit",
            edit_message_id="777",
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert result["message_id"] == 777
    assert captured["message_id"] == 777
    assert captured["text"] == "updated text"
    assert captured["chat_id"] == -100200300


def test_send_bot_message_edit_document_attachment(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    report_path = tmp_path / "report.pdf"
    report_path.write_bytes(b"pdf")
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"t:42": "@7"}},
            "group_chat_ids": {"12345:42": -100200300},
        },
    )
    captured: dict = {}

    class FakeBot:
        def __init__(self, token):
            captured["token"] = token

        async def edit_message_media(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message_id=888,
                message_thread_id=42,
                chat=SimpleNamespace(id=kwargs["chat_id"], username=None),
            )

    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="new report caption",
            token="token-edit",
            edit_message_id="888",
            file_path=str(report_path),
            file_type="document",
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert result["message_id"] == 888
    assert result["thread_id"] == 42
    assert captured["message_id"] == 888
    assert captured["chat_id"] == -100200300
    assert "message_thread_id" not in captured
    assert captured["media"].caption == "new report caption"


def test_send_alias_help_includes_edit_message_id():
    help_text = sender._build_parser(prog="ccbot send").format_help()

    assert "--edit-message-id" in help_text
