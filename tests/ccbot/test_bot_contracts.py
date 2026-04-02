"""Contract tests for preserved out-of-scope bot surfaces.

These tests freeze the compatibility boundary while Codex-specific work lands.
They cover voice handling, photo forwarding, topic close/rename cleanup, and
raw slash-command passthrough so refactors cannot silently change behavior in
shared modules.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot import bot as bot_mod


def _make_topic_update(
    *,
    thread_id: int = 42,
    user_id: int = 1,
    chat_type: str = "supergroup",
) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.id = 100
    update.message.chat.type = chat_type
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = update.message.chat
    return update


def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestBotRegistration:
    def test_create_bot_keeps_voice_photo_and_passthrough_handlers(self, monkeypatch):
        """Freeze the public routing surface exposed by create_bot()."""

        class _StubApplication:
            def __init__(self) -> None:
                self.handlers = []

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

        class _StubBuilder:
            def __init__(self) -> None:
                self._app = _StubApplication()

            def token(self, _token):
                return self

            def rate_limiter(self, _rate_limiter):
                return self

            def post_init(self, _callback):
                return self

            def post_shutdown(self, _callback):
                return self

            def build(self):
                return self._app

        monkeypatch.setattr(bot_mod.config, "telegram_bot_token", "test-token")
        monkeypatch.setattr(bot_mod.Application, "builder", lambda: _StubBuilder())

        app = bot_mod.create_bot()
        callbacks = {
            getattr(handler, "callback", None).__name__
            for handler in app.handlers
            if getattr(handler, "callback", None) is not None
        }

        assert "forward_command_handler" in callbacks
        assert "photo_handler" in callbacks
        assert "voice_handler" in callbacks
        assert "topic_closed_handler" in callbacks
        assert "topic_edited_handler" in callbacks


class TestTopicCleanup:
    @pytest.mark.asyncio
    async def test_topic_closed_kills_window_and_unbinds(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock) as mock_clear,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            window = MagicMock()
            window.window_id = "@7"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.kill_window = AsyncMock()

            await bot_mod.topic_closed_handler(update, context)

            mock_tmux.kill_window.assert_called_once_with("@7")
            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_clear.assert_called_once_with(1, 42, context.bot, context.user_data)

    @pytest.mark.asyncio
    async def test_topic_edited_icon_only_is_noop(self):
        update = _make_topic_update()
        update.message.forum_topic_edited = MagicMock()
        update.message.forum_topic_edited.name = None
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            await bot_mod.topic_edited_handler(update, context)

            mock_tmux.rename_window.assert_not_called()
            mock_sm.update_display_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_topic_edited_renames_window_and_display_name(self):
        update = _make_topic_update()
        update.message.forum_topic_edited = MagicMock()
        update.message.forum_topic_edited.name = "new-topic-name"
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "old-topic-name"
            mock_tmux.rename_window = AsyncMock(return_value=True)

            await bot_mod.topic_edited_handler(update, context)

            mock_tmux.rename_window.assert_called_once_with("@7", "new-topic-name")
            mock_sm.update_display_name.assert_called_once_with("@7", "new-topic-name")


class TestMediaForwarding:
    @pytest.mark.asyncio
    async def test_photo_forwarding_downloads_and_sends_attachment_path(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()
        update.message.caption = "look at this"

        photo = MagicMock(file_unique_id="photo-hires")
        photo.get_file = AsyncMock()
        photo_file = MagicMock()
        photo_file.download_to_drive = AsyncMock()
        photo.get_file.return_value = photo_file
        update.message.photo = [MagicMock(), photo]

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._IMAGES_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.photo_handler(update, context)

            expected_path = tmp_path / "1700000000_photo-hires.jpg"
            photo_file.download_to_drive.assert_called_once_with(expected_path)
            mock_sm.send_to_window.assert_called_once_with(
                "@7",
                f"look at this\n\n(image attached: {expected_path})",
            )

    @pytest.mark.asyncio
    async def test_voice_handler_requires_api_key(self):
        update = _make_topic_update()
        context = _make_context()
        update.message.voice = MagicMock()

        with (
            patch.object(bot_mod.config, "openai_api_key", ""),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
            patch("ccbot.bot.transcribe_voice", new_callable=AsyncMock) as mock_tx,
        ):
            await bot_mod.voice_handler(update, context)

            mock_reply.assert_called_once()
            mock_tx.assert_not_called()

    @pytest.mark.asyncio
    async def test_voice_handler_transcribes_and_sends_text(self):
        update = _make_topic_update()
        context = _make_context()
        update.message.voice = MagicMock()
        voice_file = MagicMock()
        voice_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg"))
        update.message.voice.get_file = AsyncMock(return_value=voice_file)

        with (
            patch.object(bot_mod.config, "openai_api_key", "sk-test"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.transcribe_voice", new_callable=AsyncMock, return_value="Hello from voice"),
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.voice_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@7", "Hello from voice")
