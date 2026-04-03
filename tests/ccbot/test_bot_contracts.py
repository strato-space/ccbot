"""Contract tests for preserved out-of-scope bot surfaces.

These tests freeze the compatibility boundary while Codex-specific work lands.
They cover voice handling, photo forwarding, topic close/rename cleanup, and
raw slash-command passthrough so refactors cannot silently change behavior in
shared modules.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, User

from ccbot import bot as bot_mod
from ccbot.handlers.callback_data import append_bind_flow_token
from ccbot.state_schema import (
    BINDING_STATE_NONE,
    TOPIC_POLICY_MANUAL_BIND_REQUIRED,
)


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

    def test_build_bot_commands_advertises_only_codex_core_lane(self):
        commands = bot_mod.build_bot_commands()
        names = [command.command for command in commands]

        assert names == [
            "start",
            "history",
            "screenshot",
            "esc",
            "bind",
            "unbind",
            "clear",
            "compact",
            "diff",
            "init",
            "review",
            "status",
        ]
        assert "kill" not in names
        assert "usage" not in names

    @pytest.mark.asyncio
    async def test_post_init_registers_codex_core_lane_commands(self):
        application = MagicMock()
        application.bot = AsyncMock()
        application.bot.rate_limiter = MagicMock(
            _base_limiter=MagicMock(max_rate=1, _level=0)
        )

        class _StubMonitor:
            def set_message_callback(self, _callback) -> None:
                return None

            def start(self) -> None:
                return None

            def stop(self) -> None:
                return None

        dummy_task = MagicMock()

        def _capture_task(coro):
            coro.close()
            return dummy_task

        with (
            patch("ccbot.bot.session_manager.resolve_stale_ids", new_callable=AsyncMock),
            patch("ccbot.bot.SessionMonitor", return_value=_StubMonitor()),
            patch("ccbot.bot.asyncio.create_task", side_effect=_capture_task),
        ):
            await bot_mod.post_init(application)

        command_names = [
            command.command
            for command in application.bot.set_my_commands.await_args.args[0]
        ]
        assert "status" in command_names
        assert "diff" in command_names
        assert "review" in command_names
        assert "usage" not in command_names
        assert "model" not in command_names


class TestCommandSurface:
    @pytest.mark.asyncio
    async def test_start_command_describes_codex_tmux_core_lane(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await bot_mod.start_command(update, context)

        mock_reply.assert_awaited_once()
        text = mock_reply.await_args.args[1]
        assert "Codex tmux control" in text
        assert "Each topic controls one live tmux window" in text
        assert "core lane" in text
        assert "Claude Code Monitor" not in text

    @pytest.mark.asyncio
    async def test_usage_command_points_codex_users_to_status(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_window_state.return_value = SimpleNamespace(runtime_kind="codex")

            await bot_mod.usage_command(update, context)

        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_awaited_once()
        assert "/status" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_usage_command_fails_closed_when_runtime_metadata_is_missing(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.window_states = {}

            await bot_mod.usage_command(update, context)

        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_awaited_once()
        assert "registered runtime metadata" in mock_reply.await_args.args[1]
        assert "/status" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_bind_command_re_enables_implicit_bind_and_starts_flow(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.get_topic_bind_flow_credentials.return_value = (1, "nonce123")
            mock_tmux.list_windows = AsyncMock(return_value=[])

            await bot_mod.bind_command(update, context)

        mock_sm.allow_implicit_bind.assert_called_once_with(1, 42)
        mock_sm.start_topic_bind_flow.assert_called_once_with(1, 42)
        mock_reply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_text_handler_manual_policy_blocks_implicit_bind(self):
        update = _make_topic_update()
        update.message.text = "hello"
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.get_topic_policy.return_value = TOPIC_POLICY_MANUAL_BIND_REQUIRED
            mock_sm.get_topic_binding_state.return_value = BINDING_STATE_NONE

            await bot_mod.text_handler(update, context)

        mock_tmux.list_windows.assert_not_called()
        mock_reply.assert_awaited_once()
        assert "manually unbound" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_window_picker_cancel_sets_manual_bind_required(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            bot_mod.CB_WIN_CANCEL,
            version=1,
            nonce="nonce123",
        )
        update.callback_query.answer = AsyncMock()
        update.callback_query.message = MagicMock(
            message_thread_id=42,
            chat=update.effective_chat,
        )
        context = _make_context()
        context.user_data = {
            "_pending_thread_id": 42,
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            await bot_mod.callback_handler(update, context)

        mock_sm.require_manual_bind.assert_called_once_with(1, 42)

    @pytest.mark.asyncio
    async def test_stale_bind_flow_callback_is_rejected(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            bot_mod.CB_WIN_CANCEL,
            version=3,
            nonce="stale999",
        )
        update.callback_query.answer = AsyncMock()
        update.callback_query.message = MagicMock(
            message_thread_id=42,
            chat=update.effective_chat,
        )
        context = _make_context()
        context.user_data = {
            "_pending_thread_id": 42,
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = False

            await bot_mod.callback_handler(update, context)

        mock_edit.assert_not_awaited()
        mock_sm.require_manual_bind.assert_not_called()
        update.callback_query.answer.assert_awaited_once_with(
            "Stale bind flow, use /bind again",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_cancel_callback_recovers_topic_from_callback_message_context(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            bot_mod.CB_WIN_CANCEL,
            version=2,
            nonce="nonce123",
        )
        update.callback_query.answer = AsyncMock()
        update.callback_query.message = MagicMock(
            message_thread_id=42,
            chat=update.effective_chat,
        )
        context = _make_context()
        context.user_data = {
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True

            await bot_mod.callback_handler(update, context)

        mock_sm.validate_topic_bind_flow_callback.assert_called_once_with(
            1,
            42,
            2,
            "nonce123",
        )
        mock_sm.require_manual_bind.assert_called_once_with(1, 42)


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


class TestRuntimeInputRouting:
    @pytest.mark.asyncio
    async def test_esc_command_uses_runtime_input_driver(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@7"))
            mock_sm.send_special_key_to_window = AsyncMock(
                return_value=(True, "Sent Escape")
            )

            await bot_mod.esc_command(update, context)

            mock_sm.send_special_key_to_window.assert_awaited_once_with("@7", "Escape")
            mock_tmux.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_handler_fails_closed_when_blocked_prompt_visible(self):
        update = _make_topic_update()
        update.message.text = "continue"
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
            patch("ccbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_handle_ui,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@7"))
            mock_tmux.capture_pane = AsyncMock(
                return_value="OpenAI Codex\n› ping\n■ Approval required\n"
            )

            await bot_mod.text_handler(update, context)

        mock_handle_ui.assert_awaited_once_with(context.bot, 1, "@7", 42)
        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_awaited_once()
        assert (
            "Terminal prompt is waiting for a decision"
            in mock_reply.await_args.args[1]
        )

    @pytest.mark.asyncio
    async def test_usage_command_is_runtime_gated_for_codex(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_window_state.return_value = SimpleNamespace(runtime_kind="codex")

            await bot_mod.usage_command(update, context)

        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_awaited_once()
        assert "/status" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_usage_command_forwards_to_claude_runtime(self):
        update = _make_topic_update()
        update.message.text = "/usage"
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_window_state.return_value = SimpleNamespace(runtime_kind="claude")
            mock_sm.send_to_window = AsyncMock(return_value=(True, "Sent to @7"))

            await bot_mod.usage_command(update, context)

        mock_sm.send_to_window.assert_awaited_once_with("@7", "/usage")
        assert "Claude usage modal requested" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_interactive_ui_callback_missing_window_fails_closed(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = f"{bot_mod.CB_ASK_UP}@7"
        update.callback_query.answer = AsyncMock()
        update.callback_query.message = MagicMock(message_thread_id=42, chat=update.effective_chat)
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)

            await bot_mod.callback_handler(update, context)

        update.callback_query.answer.assert_awaited_once_with(
            "Window not found", show_alert=True
        )
        mock_sm.send_special_key_to_window.assert_not_called()


class TestLauncherRegistration:
    def test_infer_runtime_kind_from_wrapped_command(self):
        assert bot_mod.infer_runtime_kind_from_command("env FOO=1 codex") == "codex"
        assert bot_mod.infer_runtime_kind_from_command("bash -lc codex") == "codex"
        assert bot_mod.infer_runtime_kind_from_command("/usr/local/bin/codex --json") == "codex"
        assert bot_mod.infer_runtime_kind_from_command("uvx codex --help") == "codex"
        assert bot_mod.infer_runtime_kind_from_command("claude --dangerously-skip-permissions") == "claude"

    @pytest.mark.asyncio
    async def test_create_and_bind_window_registers_codex_without_hook_wait(self):
        query = MagicMock(spec=CallbackQuery)
        query.answer = AsyncMock()
        context = _make_context()
        user = MagicMock(spec=User)
        user.id = 1

        with (
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
            patch.object(bot_mod.config, "claude_command", "codex"),
        ):
            mock_tmux.create_window = AsyncMock(
                return_value=(True, "Created window 'proj' at /tmp/project", "proj", "@7")
            )
            mock_sm.register_live_process = MagicMock()
            mock_sm.wait_for_session_map_entry = AsyncMock(return_value=True)

            await bot_mod._create_and_bind_window(
                query,
                context,
                user,
                "/tmp/project",
                pending_thread_id=None,
            )

        mock_sm.register_live_process.assert_called_once_with(
            "@7",
            "/tmp/project",
            window_name="proj",
            runtime_kind="codex",
            thread_id="",
        )
        mock_sm.wait_for_session_map_entry.assert_not_awaited()


class TestThreadPickerFlow:
    @staticmethod
    def _make_callback_update(data: str, thread_id: int = 42) -> MagicMock:
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = data
        update.callback_query.answer = AsyncMock()
        update.callback_query.message = MagicMock(
            message_thread_id=thread_id,
            chat=update.effective_chat,
        )
        return update

    @pytest.mark.asyncio
    async def test_directory_confirm_switches_to_thread_picker(self):
        update = self._make_callback_update(
            append_bind_flow_token(
                bot_mod.CB_DIR_CONFIRM,
                version=2,
                nonce="nonce123",
            )
        )
        context = _make_context()
        context.user_data = {
            "_pending_thread_id": 42,
            bot_mod.BROWSE_PATH_KEY: "/tmp/project",
        }
        thread = SimpleNamespace(
            thread_id="thread-1",
            summary="Existing Codex thread",
            message_count=12,
            file_path="/tmp/project/thread-1.jsonl",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.build_thread_picker", return_value=("picker", MagicMock())),
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            mock_sm.get_topic_bind_flow_credentials.return_value = (2, "nonce123")
            mock_sm.list_threads_for_directory = AsyncMock(return_value=[thread])

            await bot_mod.callback_handler(update, context)

        assert context.user_data[bot_mod.STATE_KEY] == bot_mod.STATE_SELECTING_THREAD
        assert context.user_data[bot_mod.THREADS_KEY] == [thread]
        mock_sm.list_threads_for_directory.assert_awaited_once_with("/tmp/project")
        mock_edit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thread_picker_resume_uses_selected_thread_id(self):
        update = self._make_callback_update(
            append_bind_flow_token(
                f"{bot_mod.CB_THREAD_SELECT}0",
                version=2,
                nonce="nonce123",
            )
        )
        context = _make_context()
        context.user_data = {
            "_pending_thread_id": 42,
            "_selected_path": "/tmp/project",
            bot_mod.THREADS_KEY: [SimpleNamespace(thread_id="thread-1")],
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot._create_and_bind_window", new_callable=AsyncMock) as mock_create,
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            await bot_mod.callback_handler(update, context)

        mock_create.assert_awaited_once()
        assert mock_create.await_args.kwargs["resume_session_id"] == "thread-1"

    @pytest.mark.asyncio
    async def test_thread_picker_fresh_thread_omits_resume_id(self):
        update = self._make_callback_update(
            append_bind_flow_token(
                bot_mod.CB_THREAD_NEW,
                version=2,
                nonce="nonce123",
            )
        )
        context = _make_context()
        context.user_data = {
            "_pending_thread_id": 42,
            "_selected_path": "/tmp/project",
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot._create_and_bind_window", new_callable=AsyncMock) as mock_create,
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            await bot_mod.callback_handler(update, context)

        mock_create.assert_awaited_once()
        assert mock_create.await_args.kwargs == {}
