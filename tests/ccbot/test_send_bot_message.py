import asyncio
import base64
import contextlib
import io
import json
from types import SimpleNamespace

from telegram.error import BadRequest

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


def test_normalize_chat_id_preserves_basic_group_ids():
    assert sender.normalize_chat_id("-12345") == -12345
    assert sender.normalize_chat_id("-10012345") == -10012345


def test_resolve_delivery_target_basic_group_main_chat_surface(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"c:-12345": "@8"}},
        },
    )

    target = sender.resolve_delivery_target(state_path=state_path)

    assert target.chat_id == -12345
    assert target.message_thread_id is None
    assert target.surface_key == "c:-12345"


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


def test_resolve_delivery_target_chat_qualified_topic_needs_no_group_map(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"t:-100200300:42": "@7"}},
            "group_chat_ids": {},
        },
    )

    target = sender.resolve_delivery_target(state_path=state_path)

    assert target.chat_id == -100200300
    assert target.message_thread_id == 42
    assert target.surface_key == "t:-100200300:42"


def test_resolve_delivery_target_explicit_chat_qualified_surface_key(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(state_path, {"group_chat_ids": {}})

    target = sender.resolve_delivery_target(
        state_path=state_path,
        surface_key="t:-100200300:42",
    )

    assert target.chat_id == -100200300
    assert target.message_thread_id == 42
    assert target.user_id is None


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

    def _bot_factory(token: str, **_kwargs):
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

    def _bot_factory(token: str, **_kwargs):
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

    def _bot_factory(token: str, **_kwargs):
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


def test_send_bot_message_video_auto_probes_and_returns_media_evidence(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    video_path = tmp_path / "namazu.mp4"
    video_path.write_bytes(b"mp4")
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    def _probe(path):
        assert path == video_path
        return {"width": 720, "height": 1280, "duration": 55}

    class FakeBot:
        def __init__(self, token, request=None):
            captured["token"] = token

        async def send_video(self, video, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message_id=8463,
                message_thread_id=555,
                chat=SimpleNamespace(id=kwargs["chat_id"], username=None),
                video=SimpleNamespace(
                    width=kwargs["width"],
                    height=kwargs["height"],
                    duration=kwargs["duration"],
                    mime_type="video/mp4",
                    file_size=18706819,
                    thumbnail=SimpleNamespace(width=180, height=320, file_size=54321),
                ),
            )

    monkeypatch.setattr(sender, "_probe_video_metadata", _probe)
    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="Namazu final preview",
            token="token-video",
            file_path=str(video_path),
            file_type="video",
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert captured["width"] == 720
    assert captured["height"] == 1280
    assert captured["duration"] == 55
    assert captured["supports_streaming"] is True
    assert result["media"]["request"] == {
        "type": "video",
        "method": "send_video",
        "width": 720,
        "height": 1280,
        "duration": 55,
        "supports_streaming": True,
        "source": "ffprobe",
    }
    assert result["media"]["telegram"]["video"]["width"] == 720
    assert result["media"]["telegram"]["video"]["height"] == 1280
    assert result["media"]["telegram"]["video"]["duration"] == 55
    assert result["media"]["telegram"]["video"]["mime_type"] == "video/mp4"
    assert result["media"]["telegram"]["thumbnail"] == {
        "width": 180,
        "height": 320,
        "file_size": 54321,
    }
    assert result["media"]["evidence_status"] == "complete"


def test_send_bot_message_video_explicit_metadata_and_thumbnail_override_probe(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    video_path = tmp_path / "clip.mp4"
    thumbnail_path = tmp_path / "thumb.jpg"
    video_path.write_bytes(b"mp4")
    thumbnail_path.write_bytes(b"jpg")
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    monkeypatch.setattr(
        sender,
        "_probe_video_metadata",
        lambda _path: {"width": 1, "height": 1, "duration": 1},
    )

    class FakeBot:
        def __init__(self, token, request=None):
            pass

        async def send_video(self, video, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message_id=101,
                chat=SimpleNamespace(id=kwargs["chat_id"], username=None),
                video=SimpleNamespace(
                    width=720,
                    height=1280,
                    duration=56,
                    mime_type="video/mp4",
                    thumbnail=None,
                ),
            )

    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="explicit metadata",
            token="token-video",
            file_path=str(video_path),
            file_type="video",
            state_path=state_path,
            video_width="720",
            video_height="1280",
            video_duration="55.6",
            video_thumbnail_path=str(thumbnail_path),
            video_supports_streaming=False,
        )
    )

    assert result["status"] == "success"
    assert captured["width"] == 720
    assert captured["height"] == 1280
    assert captured["duration"] == 56
    assert captured["supports_streaming"] is False
    assert captured["thumbnail"].filename == "thumb.jpg"
    assert result["media"]["request"]["source"] == "explicit+ffprobe+thumbnail"
    assert result["media"]["request"]["thumbnail"] == {
        "provided": True,
        "filename": "thumb.jpg",
        "path": str(thumbnail_path),
    }


def test_video_probe_failure_is_bounded_and_best_effort(monkeypatch, tmp_path):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"mp4")

    def _timeout(*_args, **_kwargs):
        raise sender.subprocess.TimeoutExpired(cmd="ffprobe", timeout=0.01)

    monkeypatch.setattr(sender.subprocess, "run", _timeout)

    assert sender._probe_video_metadata(video_path) == {}


def test_send_bot_message_video_bad_metadata_fails_without_sending(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"mp4")
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    class FakeBot:
        def __init__(self, token, request=None):
            pass

        async def send_video(self, video, **kwargs):  # pragma: no cover
            captured["sent"] = True
            raise AssertionError("send_video should not be called")

    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="bad metadata",
            token="token-video",
            file_path=str(video_path),
            file_type="video",
            state_path=state_path,
            video_width="-1",
        )
    )

    assert result == {"status": "error", "message": "video_width must be a positive integer"}
    assert "sent" not in captured


def test_send_bot_message_main_json_error_returns_nonzero(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"mp4")
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )

    class FakeBot:
        def __init__(self, token, request=None):
            pass

        async def send_video(self, video, **kwargs):  # pragma: no cover
            raise AssertionError("send_video should not be called")

    monkeypatch.setattr(sender, "Bot", FakeBot)
    stdout = io.StringIO()
    stderr = io.StringIO()

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = sender.send_bot_message_main(
            [
                "--token",
                "token-video",
                "--message",
                "",
                "--state-file",
                str(state_path),
                "--file-path",
                str(video_path),
                "--file-type",
                "video",
                "--video-width",
                "0",
                "--json",
            ],
            prog="ccbot send",
        )

    assert code == 1
    assert json.loads(stdout.getvalue()) == {
        "status": "error",
        "message": "video_width must be a positive integer",
    }
    assert stderr.getvalue() == ""


def test_send_bot_message_video_weak_telegram_response_is_request_only(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"mp4")
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    monkeypatch.setattr(
        sender,
        "_probe_video_metadata",
        lambda _path: {"width": 720, "height": 1280, "duration": 55},
    )

    class FakeBot:
        def __init__(self, token, request=None):
            pass

        async def send_video(self, video, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message_id=101,
                chat=SimpleNamespace(id=kwargs["chat_id"], username=None),
            )

    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="weak telegram response",
            token="token-video",
            file_path=str(video_path),
            file_type="video",
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert captured["width"] == 720
    assert result["media"]["request"]["width"] == 720
    assert result["media"]["telegram"] == {}
    assert result["media"]["evidence_status"] == "request_only"


def test_send_bot_message_edit_video_attachment_includes_media_metadata(
    monkeypatch,
    tmp_path,
):
    state_path = tmp_path / "state.json"
    video_path = tmp_path / "clip.mp4"
    thumbnail_path = tmp_path / "thumb.jpg"
    video_path.write_bytes(b"mp4")
    thumbnail_path.write_bytes(b"jpg")
    _write_state(
        state_path,
        {
            "surface_bindings": {"12345": {"t:42": "@7"}},
            "group_chat_ids": {"12345:42": -100200300},
        },
    )
    captured: dict = {}

    class FakeBot:
        def __init__(self, token, request=None):
            pass

        async def edit_message_media(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message_id=888,
                message_thread_id=42,
                chat=SimpleNamespace(id=kwargs["chat_id"], username=None),
                video=SimpleNamespace(
                    width=720,
                    height=1280,
                    duration=55,
                    mime_type="video/mp4",
                    thumbnail=None,
                ),
            )

    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="edited video",
            token="token-edit",
            edit_message_id="888",
            file_path=str(video_path),
            file_type="video",
            state_path=state_path,
            video_width=720,
            video_height=1280,
            video_duration=55,
            video_thumbnail_path=str(thumbnail_path),
        )
    )

    assert result["status"] == "success"
    assert captured["message_id"] == 888
    assert captured["media"].width == 720
    assert captured["media"].height == 1280
    assert captured["media"]._duration.total_seconds() == 55
    assert captured["media"].supports_streaming is True
    assert captured["media"].thumbnail.filename == "thumb.jpg"
    assert result["media"]["request"]["method"] == "edit_message_media"
    assert result["media"]["request"]["thumbnail"]["path"] == str(thumbnail_path)
    assert result["media"]["evidence_status"] == "complete"


def test_send_bot_message_explicit_token_and_chat_id_does_not_need_config(
    monkeypatch,
    tmp_path,
):
    captured: dict = {}

    def _bot_factory(token: str, **_kwargs):
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


def test_send_bot_message_basic_group_uses_original_chat_id_before_fallback(
    monkeypatch,
    tmp_path,
):
    seen_chat_ids: list[int] = []

    class FakeBot:
        def __init__(self, token, request=None):
            assert token == "token-basic-group"

        async def send_message(self, **kwargs):
            seen_chat_ids.append(kwargs["chat_id"])
            if len(seen_chat_ids) == 1:
                raise BadRequest("Chat not found")
            return SimpleNamespace(
                message_id=123,
                message_thread_id=kwargs.get("message_thread_id"),
                chat=SimpleNamespace(id=kwargs["chat_id"]),
            )

    monkeypatch.setattr(sender, "Bot", FakeBot)

    result = asyncio.run(
        sender.send_bot_message(
            message="hello",
            token="token-basic-group",
            chat_id="-12345",
            state_path=tmp_path / "missing-state.json",
        )
    )

    assert seen_chat_ids == [-12345, -10012345]
    assert result["status"] == "success"
    assert result["target"]["reason"] == "explicit_chat_id:supergroup_fallback"


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
        def __init__(self, token, request=None):
            captured["token"] = token
            captured["request"] = request

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
        def __init__(self, token, request=None):
            captured["token"] = token
            captured["request"] = request

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
    assert captured["media"].media.field_tuple[0] == "report.pdf"
    assert captured["media"].media.mimetype == "application/pdf"


def test_send_alias_help_includes_edit_message_id():
    help_text = sender._build_parser(prog="ccbot send").format_help()

    assert "--edit-message-id" in help_text


def test_send_alias_help_includes_video_metadata_flags():
    help_text = sender._build_parser(prog="ccbot send").format_help()

    assert "--video-width" in help_text
    assert "--video-height" in help_text
    assert "--video-duration" in help_text
    assert "--thumbnail-path" in help_text
    assert "--supports-streaming" in help_text


def test_send_bot_message_uses_proxy_env(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        {"surface_bindings": {"12345": {"c:-100200300": "@7"}}},
    )
    captured: dict = {}

    class FakeRequest:
        def __init__(self, **kwargs):
            captured["request_kwargs"] = kwargs

    class FakeBot:
        def __init__(self, token, request=None):
            captured["token"] = token
            captured["request"] = request

        async def send_message(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                message_id=999,
                chat=SimpleNamespace(id=kwargs["chat_id"], username=None),
            )

    monkeypatch.setattr(sender, "HTTPXRequest", FakeRequest)
    monkeypatch.setattr(sender, "Bot", FakeBot)
    monkeypatch.setenv("CCBOT_TELEGRAM_PROXY", "http://127.0.0.1:10809")

    result = asyncio.run(
        sender.send_bot_message(
            message="proxied",
            token="token-proxy",
            state_path=state_path,
        )
    )

    assert result["status"] == "success"
    assert captured["request_kwargs"]["proxy"] == "http://127.0.0.1:10809"
    assert captured["request"] is not None
