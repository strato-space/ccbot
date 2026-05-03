"""Contract tests for preserved out-of-scope bot surfaces.

These tests freeze the compatibility boundary while Codex-specific work lands.
They cover voice handling, photo/document forwarding, topic close/rename
cleanup, and raw slash-command passthrough so refactors cannot silently change
behavior in shared modules.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import CallbackQuery, MessageEntity, User
from telegram.error import BadRequest

from ccbot import bot as bot_mod
from ccbot.handlers.callback_data import append_bind_flow_token
from ccbot.runtime_types import NormalizedEvent
from ccbot.telegram_delivery_policy import apply_telegram_delivery_policy
from ccbot.state_schema import (
    BINDING_STATE_NONE,
    TOPIC_POLICY_IMPLICIT_BIND_ALLOWED,
    TOPIC_POLICY_MANUAL_BIND_REQUIRED,
)


def _make_topic_update(
    *,
    thread_id: int = 42,
    user_id: int = 1,
    chat_type: str = "supergroup",
    text: str | None = None,
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
    update.message.text = text
    update.message.entities = []
    update.effective_chat = update.message.chat
    return update


def _make_main_chat_update(
    *,
    user_id: int = 1,
    chat_id: int = -100200,
    chat_type: str = "supergroup",
    text: str | None = None,
) -> MagicMock:
    update = _make_topic_update(
        thread_id=None,
        user_id=user_id,
        chat_type=chat_type,
        text=text,
    )
    update.message.chat.id = chat_id
    update.effective_chat.id = chat_id
    return update


def _make_context(*, bot_username: str = "ccbot", bot_id: int = 999) -> MagicMock:
    context = MagicMock()
    context.bot = AsyncMock()
    context.bot.username = bot_username
    context.bot.id = bot_id
    context.user_data = {}
    return context


def _write_test_webp(path) -> None:
    from PIL import Image

    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(path, format="WEBP")


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
                self.proxy_url = None
                self.get_updates_proxy_url = None

            def token(self, _token):
                return self

            def rate_limiter(self, _rate_limiter):
                return self

            def proxy(self, proxy_url):
                self.proxy_url = proxy_url
                return self

            def get_updates_proxy(self, proxy_url):
                self.get_updates_proxy_url = proxy_url
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
        assert "document_handler" in callbacks
        assert "sticker_handler" in callbacks
        assert "audio_handler" in callbacks
        assert "video_handler" in callbacks
        assert "voice_handler" in callbacks
        assert "topic_closed_handler" in callbacks
        assert "topic_edited_handler" in callbacks

        ordered_callbacks = [
            getattr(handler, "callback", None).__name__
            for handler in app.handlers
            if getattr(handler, "callback", None) is not None
        ]
        unsupported_index = ordered_callbacks.index("unsupported_content_handler")
        assert ordered_callbacks.index("audio_handler") < unsupported_index
        assert ordered_callbacks.index("video_handler") < unsupported_index

    def test_create_bot_applies_telegram_proxy_from_env(self, monkeypatch):
        """Freeze explicit proxy wiring for PTB/HTTPX bootstrap."""

        class _StubApplication:
            def __init__(self) -> None:
                self.handlers = []

            def add_handler(self, handler) -> None:
                self.handlers.append(handler)

        class _StubBuilder:
            def __init__(self) -> None:
                self._app = _StubApplication()
                self.proxy_url = None
                self.get_updates_proxy_url = None

            def token(self, _token):
                return self

            def rate_limiter(self, _rate_limiter):
                return self

            def proxy(self, proxy_url):
                self.proxy_url = proxy_url
                return self

            def get_updates_proxy(self, proxy_url):
                self.get_updates_proxy_url = proxy_url
                return self

            def post_init(self, _callback):
                return self

            def post_shutdown(self, _callback):
                return self

            def build(self):
                return self._app

        builder = _StubBuilder()
        monkeypatch.setattr(bot_mod.config, "telegram_bot_token", "test-token")
        monkeypatch.setattr(bot_mod.Application, "builder", lambda: builder)
        monkeypatch.setenv("CCBOT_TELEGRAM_PROXY", "socks5h://127.0.0.1:10810")

        bot_mod.create_bot()

        assert builder.proxy_url == "socks5h://127.0.0.1:10810"
        assert builder.get_updates_proxy_url == "socks5h://127.0.0.1:10810"

    def test_build_bot_commands_advertises_only_codex_core_lane(self):
        with patch.object(bot_mod.config, "claude_command", "codex"):
            commands = bot_mod.build_bot_commands()
            names = [command.command for command in commands]

        assert names == [
            "start",
            "history",
            "screenshot",
            "esc",
            "bind",
            "unbind",
            "resume",
            "rename",
            "clear",
            "compact",
            "diff",
            "exit",
            "init",
            "review",
            "status",
        ]
        assert "kill" not in names
        assert "usage" not in names

    def test_build_bot_commands_hides_codex_passthrough_for_non_codex_lane(self):
        with patch.object(bot_mod.config, "claude_command", "fast-agent"):
            commands = bot_mod.build_bot_commands()
            names = [command.command for command in commands]

        assert names == [
            "start",
            "history",
            "screenshot",
            "esc",
            "bind",
            "unbind",
            "resume",
            "rename",
        ]
        assert "status" not in names

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
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch(
                "ccbot.bot.session_manager.resolve_stale_ids", new_callable=AsyncMock
            ),
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
        assert "exit" in command_names
        assert "review" in command_names
        assert "resume" in command_names
        assert "rename" in command_names
        assert "usage" not in command_names
        assert "model" not in command_names


class TestSurfacePendingSlots:
    def test_put_pending_slot_uses_keyword_surface_key(self):
        context = _make_context()
        surface = bot_mod.ControlSurface(
            kind="group_topic",
            chat_id=100,
            thread_id=42,
            legacy_scope_id=42,
            surface_key="t:42",
            label="topic",
            is_shared_group=True,
            supports_bind_flow=True,
        )

        with (
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot._session_has_method", return_value=True),
        ):
            record = {"text": "hello", "revision": 1, "status": "pending"}
            mock_sm.set_surface_pending_slot.return_value = record

            result = bot_mod._put_pending_slot(context, 1, surface, "hello")

        assert result == record
        mock_sm.set_surface_pending_slot.assert_called_once_with(
            1,
            "hello",
            surface_key="t:42",
        )

    def test_pending_slot_fallback_uses_domain_normalizer(self):
        context = _make_context()
        surface = bot_mod.ControlSurface(
            kind="group_topic",
            chat_id=100,
            thread_id=42,
            legacy_scope_id=42,
            surface_key="t:42",
            label="topic",
            is_shared_group=True,
            supports_bind_flow=True,
        )
        context.user_data[bot_mod.PENDING_SURFACE_SLOTS_KEY] = {
            "t:42": {
                "text": "old",
                "revision": "not-an-int",
                "status": "unexpected",
                "consumed_by_activation_id": "stale",
            }
        }

        with patch("ccbot.bot._session_has_method", return_value=False):
            record = bot_mod._put_pending_slot(context, 1, surface, "hello")
            peeked = bot_mod._peek_pending_slot(context, 1, surface)
            consumed = bot_mod._consume_pending_slot(
                context,
                1,
                surface,
                "activation-1",
            )

        assert record == {
            "text": "hello",
            "revision": 2,
            "status": "pending",
            "consumed_by_activation_id": "",
        }
        assert peeked == record
        assert consumed == "hello"
        assert context.user_data[bot_mod.PENDING_SURFACE_SLOTS_KEY]["t:42"] == {
            "text": "hello",
            "revision": 2,
            "status": "consumed",
            "consumed_by_activation_id": "activation-1",
        }


class TestCommandSurface:
    @pytest.mark.asyncio
    async def test_start_command_describes_codex_tmux_core_lane(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await bot_mod.start_command(update, context)

        mock_reply.assert_awaited_once()
        text = mock_reply.await_args.args[1]
        assert "tmux runtime control" in text
        assert (
            "Each bound topic or supported group main chat controls one live tmux window"
            in text
        )
        assert "Codex" in text
        assert "queue mode" in text
        assert "steer" in text
        assert "until you use /bind or /resume" in text
        assert "address the bot" not in text
        assert "Shared group topics and no-topics main chats stay silent" in text
        assert "raw tmux terminal control" in text
        assert "explicit `/resume <thread-name|id>`" in text
        assert "`/exit`" in text
        assert "Claude Code Monitor" not in text

    @pytest.mark.asyncio
    async def test_start_command_describes_claude_degraded_resume_path(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "claude"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await bot_mod.start_command(update, context)

        mock_reply.assert_awaited_once()
        text = mock_reply.await_args.args[1]
        assert "Claude Code" in text
        assert "not available from an unbound topic" in text
        assert "reversible workspace path" in text

    @pytest.mark.asyncio
    async def test_start_command_describes_fast_agent_degraded_resume_path(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "fast-agent"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await bot_mod.start_command(update, context)

        mock_reply.assert_awaited_once()
        text = mock_reply.await_args.args[1]
        assert "fast-agent" in text
        assert "not available from an unbound topic" in text
        assert "workspace `.fast-agent` root" in text

    @pytest.mark.asyncio
    async def test_usage_command_points_codex_users_to_status(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            window = MagicMock()
            window.window_id = "@7"
            window.cwd = "/tmp"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_window_state.return_value = SimpleNamespace(
                runtime_kind="codex"
            )

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
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            window = MagicMock()
            window.window_id = "@7"
            window.cwd = "/tmp"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
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
    async def test_bind_command_with_codex_token_binds_external_read_only(self):
        update = _make_topic_update()
        update.message.text = "/bind thread-1"
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._resolve_resume_command_target",
                new_callable=AsyncMock,
                return_value=(
                    bot_mod._ResumeCommandTarget(
                        runtime_kind="codex",
                        thread_id="thread-1",
                        summary="Thread One",
                        cwd="/tmp/project",
                        file_path="/tmp/rollout-thread-1.jsonl",
                    ),
                    None,
                ),
            ),
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock) as mock_clear,
            patch(
                "ccbot.bot._sync_topic_title", new_callable=AsyncMock, return_value=True
            ),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.bind_command(update, context)

        mock_clear.assert_awaited_once_with(1, 42, context.bot, context.user_data)
        mock_sm.set_group_chat_id.assert_called_once_with(1, 42, 100)
        mock_sm.allow_implicit_bind.assert_called_once_with(1, 42)
        mock_sm.bind_external_surface.assert_called_once_with(
            1,
            runtime_kind="codex",
            source_thread_id="thread-1",
            summary="Thread One",
            cwd="/tmp/project",
            file_path="/tmp/rollout-thread-1.jsonl",
            read_only=True,
            surface_key="t:42",
        )
        mock_sm.start_topic_bind_flow.assert_not_called()
        mock_reply.assert_awaited_once()
        assert "read-only" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_text_handler_manual_policy_blocks_implicit_bind(self):
        update = _make_topic_update(chat_type="private")
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
        assert "/bind" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_text_handler_does_not_restart_bind_flow_while_picker_is_active(self):
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
            mock_sm.get_topic_binding_state.return_value = (
                bot_mod.BINDING_STATE_BIND_FLOW
            )
            mock_sm.get_topic_policy.return_value = TOPIC_POLICY_IMPLICIT_BIND_ALLOWED

            await bot_mod.text_handler(update, context)

        mock_tmux.list_windows.assert_not_called()
        mock_reply.assert_awaited_once_with(
            update.message, bot_mod.BIND_FLOW_ACTIVE_MESSAGE
        )

    @pytest.mark.asyncio
    async def test_text_handler_external_binding_returns_read_only_warning(self):
        update = _make_topic_update()
        update.message.text = "ping"
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "external:codex:thread-1"
            mock_sm.is_external_binding_window_id.return_value = True
            mock_sm.send_to_window = AsyncMock(
                return_value=(
                    False,
                    "Topic is bound to an external persisted thread in read-only mode. Attach a live tmux window via /bind or /resume to inject input.",
                )
            )
            mock_sm.get_external_topic_binding.return_value = {
                "runtime_kind": "codex",
                "source_thread_id": "thread-1",
                "read_only": True,
            }

            await bot_mod.text_handler(update, context)

        mock_sm.send_to_window.assert_awaited_once_with(
            "external:codex:thread-1", "ping"
        )
        mock_tmux.find_window_by_id.assert_not_called()
        mock_reply.assert_awaited_once()
        assert "read-only" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_text_handler_group_unbound_ordinary_text_is_silent(self):
        update = _make_topic_update(text="hello")
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._start_bind_flow", new_callable=AsyncMock
            ) as mock_start_bind,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.get_topic_binding_state.return_value = BINDING_STATE_NONE
            mock_sm.get_topic_policy.return_value = TOPIC_POLICY_IMPLICIT_BIND_ALLOWED

            await bot_mod.text_handler(update, context)

        mock_start_bind.assert_not_awaited()
        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_text_handler_group_unbound_at_mention_is_silent(
        self,
    ):
        update = _make_topic_update(text="@ccbot hello")
        update.message.entities = [
            SimpleNamespace(type=MessageEntity.MENTION, offset=0, length=6)
        ]
        context = _make_context(bot_username="ccbot")

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._start_bind_flow", new_callable=AsyncMock
            ) as mock_start_bind,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.get_topic_binding_state.return_value = BINDING_STATE_NONE
            mock_sm.get_topic_policy.return_value = TOPIC_POLICY_MANUAL_BIND_REQUIRED

            await bot_mod.text_handler(update, context)

        mock_start_bind.assert_not_awaited()
        assert bot_mod.PENDING_SURFACE_SLOTS_KEY not in context.user_data
        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_text_handler_group_topic_reuses_existing_surface_binding_for_peer_user(
        self,
    ):
        update = _make_topic_update(user_id=2, text="hello")
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
            patch(
                "ccbot.bot._start_bind_flow", new_callable=AsyncMock
            ) as mock_start_bind,
            patch(
                "ccbot.bot._clear_shared_group_peer_flow_state"
            ) as mock_clear_peer_state,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.surface_bindings = {1: {"t:42": "@7"}}
            mock_sm.resolve_chat_id.return_value = 100
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@7")
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.text_handler(update, context)

        mock_start_bind.assert_not_awaited()
        mock_clear_peer_state.assert_called_once()
        assert mock_clear_peer_state.call_args.args[1] == 2
        mock_sm.send_to_window.assert_awaited_once_with("@7", "hello")
        mock_reply.assert_not_awaited()

    def test_shared_group_binding_does_not_cross_telegram_chats(self):
        surface = bot_mod.ControlSurface(
            kind="group_topic",
            chat_id=100,
            thread_id=42,
            legacy_scope_id=42,
            surface_key="t:42",
            label="topic",
            is_shared_group=True,
            supports_bind_flow=True,
        )

        with patch("ccbot.bot.session_manager") as mock_sm:
            mock_sm.surface_bindings = {1: {"t:42": "@7"}}
            mock_sm.resolve_chat_id.return_value = 200

            binding = bot_mod._get_shared_group_binding_for_surface(2, surface)

        assert binding is None

    @pytest.mark.asyncio
    async def test_text_handler_no_topics_main_chat_unbound_ordinary_text_is_silent(
        self,
    ):
        update = _make_main_chat_update(text="hello")
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._start_bind_flow", new_callable=AsyncMock
            ) as mock_start_bind,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.get_topic_binding_state.return_value = BINDING_STATE_NONE
            mock_sm.get_topic_policy.return_value = TOPIC_POLICY_IMPLICIT_BIND_ALLOWED

            await bot_mod.text_handler(update, context)

        mock_start_bind.assert_not_awaited()
        mock_reply.assert_not_awaited()
        mock_sm.send_to_window.assert_not_called()

    @pytest.mark.asyncio
    async def test_bind_command_group_topic_reports_existing_peer_surface_binding(self):
        update = _make_topic_update(user_id=2, text="/bind")
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._start_bind_flow", new_callable=AsyncMock
            ) as mock_start_bind,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.surface_bindings = {1: {"t:42": "@7"}}
            mock_sm.resolve_chat_id.return_value = 100
            mock_sm.get_display_name.return_value = "project"

            await bot_mod.bind_command(update, context)

        mock_start_bind.assert_not_awaited()
        mock_reply.assert_awaited_once()
        assert "already bound to 'project'" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_bind_command_no_topics_main_chat_starts_bind_flow(self):
        update = _make_main_chat_update(text="/bind")
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.get_topic_bind_flow_credentials.return_value = (1, "nonce123")
            mock_tmux.list_windows = AsyncMock(return_value=[])

            await bot_mod.bind_command(update, context)

        mock_sm.allow_implicit_bind.assert_called_once_with(1, -100200)
        mock_sm.start_topic_bind_flow.assert_called_once_with(1, -100200)
        mock_reply.assert_awaited_once()
        assert "topic" not in mock_reply.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_resume_command_no_topics_main_chat_no_longer_hard_rejects(self):
        update = _make_main_chat_update(text="/resume planning-thread")
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock),
            patch(
                "ccbot.bot._register_bound_window", new_callable=AsyncMock
            ) as mock_register,
            patch(
                "ccbot.bot._maybe_autosend_pending_after_activation",
                new_callable=AsyncMock,
            ),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.codex_thread_catalog = MagicMock()
            mock_sm.codex_thread_catalog.resolve_resume_target.return_value = (
                SimpleNamespace(
                    status="selected",
                    selected=SimpleNamespace(
                        thread_id="thread-1",
                        summary="Planning thread",
                        cwd="/tmp/project",
                    ),
                )
            )
            mock_sm.get_runtime_capability.return_value = SimpleNamespace(
                display_name="Codex"
            )
            mock_tmux.create_or_reuse_window = AsyncMock(
                return_value=(
                    True,
                    "Reused window 'project' at /tmp/project",
                    "project",
                    "@7",
                    True,
                )
            )

            await bot_mod.resume_command(update, context)

        mock_register.assert_awaited_once_with(
            context,
            update.effective_user,
            -100200,
            window_id="@7",
            window_name="project",
            selected_path="/tmp/project",
            runtime_kind="codex",
            surface=bot_mod.ControlSurface(
                kind="group_main_chat",
                chat_id=-100200,
                thread_id=None,
                legacy_scope_id=-100200,
                surface_key="c:-100200",
                label="chat",
                is_shared_group=True,
                supports_bind_flow=True,
            ),
            resume_session_id="thread-1",
            sync_topic_title=False,
        )
        mock_reply.assert_awaited_once()
        assert "only works in a topic" not in mock_reply.await_args.args[1].lower()

    @pytest.mark.asyncio
    async def test_bind_command_read_only_binding_does_not_autosend_pending(self):
        update = _make_main_chat_update(text="/bind thread-1")
        context = _make_context()
        context.user_data[bot_mod.PENDING_SURFACE_SLOTS_KEY] = {
            "c:-100200": {
                "text": "queued hello",
                "revision": 1,
                "status": "pending",
                "consumed_by_activation_id": None,
            }
        }

        with (
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch(
                "ccbot.bot._resolve_resume_command_target",
                new_callable=AsyncMock,
                return_value=(
                    bot_mod._ResumeCommandTarget(
                        runtime_kind="codex",
                        thread_id="thread-1",
                        summary="Thread One",
                        cwd="/tmp/project",
                        file_path="/tmp/rollout-thread-1.jsonl",
                    ),
                    None,
                ),
            ),
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
            patch("ccbot.bot.session_manager") as mock_sm,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.bind_command(update, context)

        mock_sm.send_to_window.assert_not_called()
        assert (
            context.user_data[bot_mod.PENDING_SURFACE_SLOTS_KEY]["c:-100200"]["status"]
            == "pending"
        )

    @pytest.mark.asyncio
    async def test_pending_autosend_happens_once_per_writable_activation(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            f"{bot_mod.CB_WIN_BIND}0",
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
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
            bot_mod.UNBOUND_WINDOWS_KEY: ["@7"],
            bot_mod.PENDING_SURFACE_STATE_KEY: {
                "kind": "group_topic",
                "chat_id": 100,
                "thread_id": 42,
                "legacy_scope_id": 42,
                "surface_key": "t:42",
                "label": "topic",
                "is_shared_group": True,
                "supports_bind_flow": True,
            },
            bot_mod.PENDING_SURFACE_SLOTS_KEY: {
                "t:42": {
                    "text": "queued hello",
                    "revision": 1,
                    "status": "pending",
                    "consumed_by_activation_id": None,
                }
            },
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(
                    window_id="@7",
                    window_name="project",
                    cwd="/tmp/project",
                    pane_current_command="codex",
                )
            )
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.callback_handler(update, context)
            assert mock_sm.send_to_window.await_count == 1

            mock_sm.send_to_window.reset_mock()
            await bot_mod.callback_handler(update, context)

        mock_sm.send_to_window.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unbind_command_on_bound_topic_sets_manual_bind_required(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock) as mock_clear,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"

            await bot_mod.unbind_command(update, context)

        mock_sm.unbind_surface.assert_called_once_with(1, surface_key="t:42")
        mock_sm.require_manual_bind.assert_called_once_with(1, 42)
        mock_clear.assert_awaited_once_with(1, 42, context.bot, context.user_data)
        mock_reply.assert_awaited_once()
        assert "/resume <thread-name|id>" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_unbind_command_peer_user_removes_shared_surface_binding(self):
        update = _make_topic_update(user_id=2)
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock) as mock_clear,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.surface_bindings = {1: {"t:42": "@7"}}
            mock_sm.resolve_chat_id.return_value = 100
            mock_sm.get_display_name.return_value = "project"

            await bot_mod.unbind_command(update, context)

        mock_sm.unbind_surface.assert_called_once_with(1, surface_key="t:42")
        mock_sm.require_manual_bind.assert_any_call(2, 42)
        mock_sm.require_manual_bind.assert_any_call(1, 42)
        assert mock_sm.require_manual_bind.call_count == 2
        mock_clear.assert_any_await(2, 42, context.bot, context.user_data)
        mock_clear.assert_any_await(1, 42, context.bot, None)
        mock_reply.assert_awaited_once()
        assert "unbound from window 'project'" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_unbind_command_without_binding_keeps_manual_bind_required(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "fast-agent"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock) as mock_clear,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.unbind_command(update, context)

        mock_sm.require_manual_bind.assert_called_once_with(1, 42)
        mock_clear.assert_awaited_once_with(1, 42, context.bot, context.user_data)
        mock_reply.assert_awaited_once()
        assert "manually unbound" in mock_reply.await_args.args[1]
        assert "workspace `.fast-agent` root" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_resume_command_uses_codex_resolution_and_binds_topic(self):
        update = _make_topic_update()
        update.message.text = "/resume planning-thread"
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "codex"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.clear_topic_state", new_callable=AsyncMock),
            patch(
                "ccbot.bot._register_bound_window", new_callable=AsyncMock
            ) as mock_register,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.codex_thread_catalog = MagicMock()
            mock_sm.codex_thread_catalog.resolve_resume_target.return_value = (
                SimpleNamespace(
                    status="selected",
                    selected=SimpleNamespace(
                        thread_id="thread-1",
                        summary="Planning thread",
                        cwd="/tmp/project",
                    ),
                )
            )
            mock_sm.get_runtime_capability.return_value = SimpleNamespace(
                display_name="Codex"
            )
            mock_tmux.create_or_reuse_window = AsyncMock(
                return_value=(
                    True,
                    "Reused window 'project' at /tmp/project",
                    "project",
                    "@7",
                    True,
                )
            )

            await bot_mod.resume_command(update, context)

        mock_tmux.create_or_reuse_window.assert_awaited_once_with(
            "/tmp/project",
            start_claude=True,
            resume_session_id="thread-1",
            runtime_kind="codex",
            reuse_existing=True,
        )
        mock_register.assert_awaited_once_with(
            context,
            update.effective_user,
            42,
            window_id="@7",
            window_name="project",
            selected_path="/tmp/project",
            runtime_kind="codex",
            surface=bot_mod.ControlSurface(
                kind="group_topic",
                chat_id=100,
                thread_id=42,
                legacy_scope_id=42,
                surface_key="t:42",
                label="topic",
                is_shared_group=True,
                supports_bind_flow=True,
            ),
            resume_session_id="thread-1",
            sync_topic_title=True,
        )
        mock_sm.set_group_chat_id.assert_called_once_with(1, 42, 100)
        mock_sm.allow_implicit_bind.assert_called_once_with(1, 42)
        reply_text = mock_reply.await_args.args[1]
        assert "Reused Codex window for 'Planning thread'" in reply_text
        assert "resumed Codex thread" in reply_text

    @pytest.mark.asyncio
    async def test_resume_command_reports_fast_agent_degraded_mode(self):
        update = _make_topic_update()
        update.message.text = "/resume abc123"
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "fast-agent"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.resume_command(update, context)

        mock_reply.assert_awaited_once()
        assert "workspace `.fast-agent` root" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_resume_command_reports_claude_degraded_mode(self):
        update = _make_topic_update()
        update.message.text = "/resume session-123"
        context = _make_context()

        with (
            patch.object(bot_mod.config, "claude_command", "claude"),
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.resume_command(update, context)

        mock_reply.assert_awaited_once()
        assert "reversible workspace path" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_rename_command_updates_window_topic_and_supported_identity(self):
        update = _make_topic_update()
        update.message.text = "/rename Daily planner"
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._get_window_runtime_kind", return_value="fast-agent"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_runtime_capability.return_value = SimpleNamespace(
                rename_identity_mode="title_only",
                display_name="fast-agent",
            )
            mock_sm.rename_runtime_identity_for_window = AsyncMock(
                return_value=(True, "fast-agent session title metadata updated")
            )
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@7")
            )
            mock_tmux.rename_window_with_suffixes = AsyncMock(
                return_value=(
                    True,
                    "Renamed window to 'Daily planner'",
                    "Daily planner",
                )
            )

            await bot_mod.rename_command(update, context)

        mock_tmux.rename_window_with_suffixes.assert_awaited_once_with(
            "@7", "Daily planner"
        )
        mock_sm.update_display_name.assert_called_once_with("@7", "Daily planner")
        mock_sm.rename_runtime_identity_for_window.assert_awaited_once_with(
            "@7",
            "Daily planner",
        )
        mock_reply.assert_awaited_once()
        reply_text = mock_reply.await_args.args[1]
        assert "Renamed window to 'Daily planner'" in reply_text
        assert "Telegram topic title synced to 'Daily planner'" in reply_text
        assert "Persisted fast-agent title metadata updated" in reply_text
        assert "Persisted conversation id stayed the same" in reply_text

    @pytest.mark.asyncio
    async def test_rename_command_reports_unsupported_identity_mode_clearly(self):
        update = _make_topic_update()
        update.message.text = "/rename core"
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._get_window_runtime_kind", return_value="codex"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_runtime_capability.return_value = SimpleNamespace(
                rename_identity_mode="unsupported_degraded",
                display_name="Codex",
            )
            mock_sm.rename_runtime_identity_for_window = AsyncMock(
                return_value=(False, "persisted identity unchanged")
            )
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@7")
            )
            mock_tmux.rename_window_with_suffixes = AsyncMock(
                return_value=(True, "Renamed window to 'core'", "core")
            )

            await bot_mod.rename_command(update, context)

        mock_reply.assert_awaited_once()
        reply_text = mock_reply.await_args.args[1]
        assert "Renamed window to 'core'" in reply_text
        assert "Persisted runtime identity was not changed" in reply_text
        assert "Telegram topic title synced to 'core'" in reply_text

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
    async def test_bind_flow_hides_codex_subagent_windows_from_picker(self):
        update = _make_topic_update()
        context = _make_context()
        surface = bot_mod.ControlSurface(
            kind="group_topic",
            chat_id=100,
            thread_id=42,
            legacy_scope_id=42,
            surface_key="t:42",
            label="topic",
            is_shared_group=True,
            supports_bind_flow=True,
        )
        helper_window = SimpleNamespace(
            window_id="@45",
            window_name="comfy-agent-spec",
            cwd="/home/tools/server/comfy",
        )
        normal_window = SimpleNamespace(
            window_id="@0",
            window_name="comfy-agent",
            cwd="/home/tools/server/comfy",
        )

        with (
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_tmux.list_windows = AsyncMock(
                return_value=[helper_window, normal_window]
            )
            mock_sm.iter_thread_bindings.return_value = []
            mock_sm.get_topic_bind_flow_credentials.return_value = (2, "nonce123")
            mock_sm.window_states = {
                "@45": SimpleNamespace(
                    runtime_kind="codex",
                    thread_id="019dddf6-7efa-7d13-9a64-08c9dc9ac1d2",
                ),
                "@0": SimpleNamespace(
                    runtime_kind="codex",
                    thread_id="019dbd8b-c0eb-7ee2-bf5b-aa8befdc30bf",
                ),
            }
            mock_sm.codex_thread_catalog.is_helper_thread_fast.side_effect = (
                lambda thread_id: thread_id
                == "019dddf6-7efa-7d13-9a64-08c9dc9ac1d2"
            )

            await bot_mod._start_bind_flow(
                update,
                context,
                update.effective_user,
                surface,
                explicit=True,
            )

        assert context.user_data[bot_mod.UNBOUND_WINDOWS_KEY] == ["@0"]
        reply_text = mock_reply.await_args.args[1]
        assert "comfy-agent" in reply_text
        assert "comfy-agent-spec" not in reply_text

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

    @pytest.mark.asyncio
    async def test_window_picker_bind_registers_existing_window_before_binding(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            f"{bot_mod.CB_WIN_BIND}0",
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
            "_pending_thread_id": 42,
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
            bot_mod.UNBOUND_WINDOWS_KEY: ["@7"],
        }
        window = SimpleNamespace(
            window_id="@7",
            window_name="codex",
            cwd="/tmp/project",
            pane_current_command="codex",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._register_bound_window", new_callable=AsyncMock
            ) as mock_register,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
            patch.object(bot_mod.config, "claude_command", "codex --no-alt-screen"),
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            await bot_mod.callback_handler(update, context)

        mock_register.assert_awaited_once_with(
            context,
            update.effective_user,
            42,
            window_id="@7",
            window_name="codex",
            selected_path="/tmp/project",
            runtime_kind="codex",
            surface=bot_mod.ControlSurface(
                kind="group_topic",
                chat_id=100,
                thread_id=42,
                legacy_scope_id=42,
                surface_key="t:42",
                label="topic",
                is_shared_group=True,
                supports_bind_flow=True,
            ),
            sync_topic_title=True,
        )
        mock_sm.bind_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_picker_bind_shell_only_window_fails_closed(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            f"{bot_mod.CB_WIN_BIND}0",
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
            "_pending_thread_id": 42,
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
            bot_mod.UNBOUND_WINDOWS_KEY: ["@7"],
        }
        window = SimpleNamespace(
            window_id="@7",
            window_name="node",
            cwd="/tmp/project",
            pane_current_command="node",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._register_bound_window", new_callable=AsyncMock
            ) as mock_register,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
            patch.object(bot_mod.config, "claude_command", "codex --no-alt-screen"),
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            await bot_mod.callback_handler(update, context)

        mock_register.assert_not_awaited()
        mock_sm.bind_thread.assert_not_called()
        update.callback_query.answer.assert_awaited_once_with(
            "Window 'node' is not running a known runtime",
            show_alert=True,
        )

    @pytest.mark.asyncio
    async def test_window_picker_bind_uses_registered_runtime_for_shell_pane(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            f"{bot_mod.CB_WIN_BIND}0",
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
            "_pending_thread_id": 42,
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
            bot_mod.UNBOUND_WINDOWS_KEY: ["@7"],
        }
        window = SimpleNamespace(
            window_id="@7",
            window_name="node",
            cwd="/tmp/project",
            pane_current_command="node",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._register_bound_window", new_callable=AsyncMock
            ) as mock_register,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
            patch.object(bot_mod.config, "claude_command", "codex --no-alt-screen"),
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            mock_sm.window_states = {
                "@7": SimpleNamespace(
                    runtime_kind="codex",
                    cwd="/tmp/project",
                    thread_id="thread-1",
                )
            }
            mock_sm.codex_thread_catalog = None
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            await bot_mod.callback_handler(update, context)

        mock_register.assert_awaited_once_with(
            context,
            update.effective_user,
            42,
            window_id="@7",
            window_name="node",
            selected_path="/tmp/project",
            runtime_kind="codex",
            surface=bot_mod.ControlSurface(
                kind="group_topic",
                chat_id=100,
                thread_id=42,
                legacy_scope_id=42,
                surface_key="t:42",
                label="topic",
                is_shared_group=True,
                supports_bind_flow=True,
            ),
            sync_topic_title=True,
        )
        mock_sm.bind_thread.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_picker_rejects_stale_codex_subagent_selection(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            f"{bot_mod.CB_WIN_BIND}0",
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
            "_pending_thread_id": 42,
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
            bot_mod.UNBOUND_WINDOWS_KEY: ["@45"],
        }
        window = SimpleNamespace(
            window_id="@45",
            window_name="comfy-agent-spec",
            cwd="/tmp/project",
            pane_current_command="node",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._is_non_bindable_codex_helper_window",
                return_value=True,
            ),
            patch(
                "ccbot.bot._register_bound_window", new_callable=AsyncMock
            ) as mock_register,
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            await bot_mod.callback_handler(update, context)

        mock_register.assert_not_awaited()
        update.callback_query.answer.assert_awaited_once()
        assert "subagent/helper" in update.callback_query.answer.await_args.args[0]

    @pytest.mark.asyncio
    async def test_window_picker_bind_existing_window_without_cwd_fails_closed(self):
        update = MagicMock()
        update.effective_user = MagicMock(id=1)
        update.effective_chat = MagicMock(type="supergroup", id=100)
        update.callback_query = MagicMock()
        update.callback_query.data = append_bind_flow_token(
            f"{bot_mod.CB_WIN_BIND}0",
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
            "_pending_thread_id": 42,
            bot_mod.STATE_KEY: bot_mod.STATE_SELECTING_WINDOW,
            bot_mod.UNBOUND_WINDOWS_KEY: ["@7"],
        }
        window = SimpleNamespace(
            window_id="@7",
            window_name="node",
            cwd="",
            pane_current_command="node",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._register_bound_window", new_callable=AsyncMock
            ) as mock_register,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)

            await bot_mod.callback_handler(update, context)

        mock_register.assert_not_awaited()
        mock_sm.bind_thread.assert_not_called()
        update.callback_query.answer.assert_awaited_once_with(
            "Window 'node' has no detectable workspace path",
            show_alert=True,
        )


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
            mock_sm.get_runtime_capability.return_value = SimpleNamespace(
                rename_identity_mode="title_only",
                display_name="fast-agent",
            )
            mock_sm.rename_runtime_identity_for_window = AsyncMock(
                return_value=(True, "fast-agent session title metadata updated")
            )
            mock_tmux.rename_window_with_suffixes = AsyncMock(
                return_value=(
                    True,
                    "Renamed window to 'new-topic-name'",
                    "new-topic-name",
                )
            )

            await bot_mod.topic_edited_handler(update, context)

            mock_tmux.rename_window_with_suffixes.assert_awaited_once_with(
                "@7",
                "new-topic-name",
            )
            mock_sm.update_display_name.assert_called_once_with("@7", "new-topic-name")
            mock_sm.rename_runtime_identity_for_window.assert_awaited_once_with(
                "@7",
                "new-topic-name",
            )


class TestMediaForwarding:
    @pytest.mark.asyncio
    async def test_photo_forwarding_downloads_and_sends_attachment_path(self, tmp_path):
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
    async def test_unbound_photo_ingress_is_silent(self):
        update = _make_topic_update()
        context = _make_context()
        photo = MagicMock(file_unique_id="photo-hires")
        photo.get_file = AsyncMock()
        update.message.photo = [photo]

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.photo_handler(update, context)

        photo.get_file.assert_not_called()
        mock_sm.set_group_chat_id.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        mock_tmux.find_window_by_id.assert_not_called()
        mock_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_document_forwarding_downloads_and_sends_attachment_path(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()
        update.message.caption = "use this archive"

        document = MagicMock(file_unique_id="doc-unique", file_name="../archive.tar.gz")
        document.get_file = AsyncMock()
        document_file = MagicMock()
        document_file.download_to_drive = AsyncMock()
        document.get_file.return_value = document_file
        update.message.document = document

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._DOCUMENTS_DIR", tmp_path),
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

            await bot_mod.document_handler(update, context)

            expected_path = tmp_path / "1700000000_doc-unique_archive.tar.gz"
            document_file.download_to_drive.assert_called_once_with(expected_path)
            mock_sm.send_to_window.assert_called_once_with(
                "@7",
                f"use this archive\n\n(document attached: {expected_path})",
            )

    @pytest.mark.asyncio
    async def test_audio_forwarding_saves_artifact_without_openai_transcription(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()
        audio = MagicMock(
            file_unique_id="audio-unique",
            file_name="../meeting.mp3",
            mime_type="audio/mpeg",
            duration=37,
            file_size=12345,
        )
        audio.get_file = AsyncMock()
        audio_file = MagicMock(file_path="voice/meeting.mp3")
        audio_file.download_to_drive = AsyncMock(
            side_effect=lambda path: Path(path).write_bytes(b"mp3")
        )
        audio.get_file.return_value = audio_file
        update.message.audio = audio

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._MEDIA_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
            patch("ccbot.bot.transcribe_voice", new_callable=AsyncMock) as mock_tx,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.audio_handler(update, context)

            expected_path = tmp_path / "1700000000_audio-unique_meeting.mp3"
            audio_file.download_to_drive.assert_called_once_with(expected_path)
            mock_tx.assert_not_called()
            mock_sm.send_to_window.assert_called_once()
            window_id, text_to_send = mock_sm.send_to_window.call_args.args
            assert window_id == "@7"
            assert "Audio artifact received." in text_to_send
            assert f"Audio artifact: {expected_path}" in text_to_send
            assert "Audio metadata: mime=audio/mpeg, duration=37, size=12345" in text_to_send
            assert "Transcript: unavailable" in text_to_send

    @pytest.mark.asyncio
    async def test_unbound_audio_ingress_is_silent(self):
        update = _make_topic_update()
        context = _make_context()
        audio = MagicMock(file_unique_id="audio-unique", mime_type="audio/mpeg")
        audio.get_file = AsyncMock()
        update.message.audio = audio

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.audio_handler(update, context)

        audio.get_file.assert_not_called()
        mock_sm.set_group_chat_id.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        mock_tmux.find_window_by_id.assert_not_called()
        mock_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_audio_oversize_fails_closed_without_download(self):
        update = _make_topic_update()
        context = _make_context()
        audio = MagicMock(
            file_unique_id="audio-huge",
            file_name="huge.mp3",
            mime_type="audio/mpeg",
            file_size=bot_mod._DEFAULT_MAX_AUDIO_BYTES + 1,
        )
        audio.get_file = AsyncMock()
        update.message.audio = audio

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.audio_handler(update, context)

        audio.get_file.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        assert "too large" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_audio_oversize_above_telegram_download_cap_fails_before_get_file(
        self, monkeypatch
    ):
        update = _make_topic_update()
        context = _make_context()
        monkeypatch.delenv("CCBOT_MAX_AUDIO_BYTES", raising=False)
        monkeypatch.setenv(
            "CCBOT_MAX_TELEGRAM_DOWNLOAD_BYTES",
            str(bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES),
        )
        audio = MagicMock(
            file_unique_id="audio-bot-limit",
            file_name="too-large.mp3",
            mime_type="audio/mpeg",
            file_size=bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES + 1,
        )
        audio.get_file = AsyncMock()
        update.message.audio = audio

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.audio_handler(update, context)

        audio.get_file.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        warning = mock_reply.await_args.args[1]
        assert "too large for Telegram bot download" in warning
        assert str(bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES) in warning

    @pytest.mark.asyncio
    async def test_audio_get_file_too_big_error_returns_clear_warning(
        self, monkeypatch
    ):
        update = _make_topic_update()
        context = _make_context()
        monkeypatch.delenv("CCBOT_MAX_AUDIO_BYTES", raising=False)
        monkeypatch.setenv(
            "CCBOT_MAX_TELEGRAM_DOWNLOAD_BYTES",
            str(bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES),
        )
        audio = MagicMock(
            file_unique_id="audio-runtime-limit",
            file_name="runtime-limit.mp3",
            mime_type="audio/mpeg",
            file_size=None,
        )
        audio.get_file = AsyncMock(side_effect=BadRequest("File is too big"))
        update.message.audio = audio

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.audio_handler(update, context)

        audio.get_file.assert_awaited_once()
        mock_sm.send_to_window.assert_not_called()
        warning = mock_reply.await_args.args[1]
        assert "too large for Telegram bot download" in warning
        assert "Could not download" not in warning

    @pytest.mark.asyncio
    async def test_video_forwarding_saves_artifact_and_thumbnail_preview(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()
        thumbnail = MagicMock(file_unique_id="thumb-unique")
        video = MagicMock(
            file_unique_id="video-unique",
            file_name="../clip.mp4",
            mime_type="video/mp4",
            duration=8,
            file_size=45678,
            thumbnail=thumbnail,
        )
        video.get_file = AsyncMock()
        video_file = MagicMock(file_path="videos/clip.mp4")
        video_file.download_to_drive = AsyncMock(
            side_effect=lambda path: Path(path).write_bytes(b"mp4")
        )
        video.get_file.return_value = video_file
        update.message.video = video

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._MEDIA_DIR", tmp_path / "media"),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch(
                "ccbot.bot._download_attachment_image_as_png",
                new_callable=AsyncMock,
                return_value=tmp_path / "images" / "clip_preview.png",
            ) as mock_preview,
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.video_handler(update, context)

            expected_path = tmp_path / "media" / "1700000000_video-unique_clip.mp4"
            video_file.download_to_drive.assert_called_once_with(expected_path)
            mock_preview.assert_awaited_once_with(
                thumbnail,
                "1700000000_video-unique_clip_thumb-unique_video_thumbnail",
            )
            text_to_send = mock_sm.send_to_window.call_args.args[1]
            assert "Video artifact received." in text_to_send
            assert f"Video artifact: {expected_path}" in text_to_send
            assert "Video thumbnail: (image attached:" in text_to_send
            assert "Video metadata: mime=video/mp4, duration=8, size=45678" in text_to_send
            assert "Transcript: not attempted in MVP" in text_to_send

    @pytest.mark.asyncio
    async def test_video_without_preview_still_forwards_artifact_path(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()
        video = MagicMock(
            file_unique_id="video-unique",
            file_name="clip.mp4",
            mime_type="video/mp4",
            duration=8,
            file_size=45678,
            thumbnail=None,
        )
        video.get_file = AsyncMock()
        video_file = MagicMock(file_path="videos/clip.mp4")
        video_file.download_to_drive = AsyncMock(
            side_effect=lambda path: Path(path).write_bytes(b"mp4")
        )
        video.get_file.return_value = video_file
        update.message.video = video

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._MEDIA_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.shutil.which", return_value=None),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.video_handler(update, context)

            expected_path = tmp_path / "1700000000_video-unique_clip.mp4"
            video_file.download_to_drive.assert_called_once_with(expected_path)
            text_to_send = mock_sm.send_to_window.call_args.args[1]
            assert f"Video artifact: {expected_path}" in text_to_send
            assert "Preview unavailable" in text_to_send
            assert "Transcript: not attempted in MVP" in text_to_send

    @pytest.mark.asyncio
    async def test_unbound_video_ingress_is_silent(self):
        update = _make_topic_update()
        context = _make_context()
        video = MagicMock(file_unique_id="video-unique", mime_type="video/mp4")
        video.get_file = AsyncMock()
        update.message.video = video

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.video_handler(update, context)

        video.get_file.assert_not_called()
        mock_sm.set_group_chat_id.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        mock_tmux.find_window_by_id.assert_not_called()
        mock_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_video_oversize_above_telegram_download_cap_fails_before_get_file(
        self, monkeypatch
    ):
        update = _make_topic_update()
        context = _make_context()
        monkeypatch.delenv("CCBOT_MAX_VIDEO_BYTES", raising=False)
        monkeypatch.setenv(
            "CCBOT_MAX_TELEGRAM_DOWNLOAD_BYTES",
            str(bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES),
        )
        video = MagicMock(
            file_unique_id="video-bot-limit",
            file_name="too-large.mp4",
            mime_type="video/mp4",
            file_size=bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES + 1,
        )
        video.get_file = AsyncMock()
        update.message.video = video

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.video_handler(update, context)

        video.get_file.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        warning = mock_reply.await_args.args[1]
        assert "too large for Telegram bot download" in warning
        assert str(bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES) in warning

    @pytest.mark.asyncio
    async def test_video_download_too_big_error_returns_clear_warning(
        self, tmp_path, monkeypatch
    ):
        update = _make_topic_update()
        context = _make_context()
        monkeypatch.delenv("CCBOT_MAX_VIDEO_BYTES", raising=False)
        monkeypatch.setenv(
            "CCBOT_MAX_TELEGRAM_DOWNLOAD_BYTES",
            str(bot_mod._DEFAULT_MAX_TELEGRAM_DOWNLOAD_BYTES),
        )
        video = MagicMock(
            file_unique_id="video-runtime-limit",
            file_name="runtime-limit.mp4",
            mime_type="video/mp4",
            duration=8,
            file_size=None,
            thumbnail=None,
        )
        video.get_file = AsyncMock()
        video_file = MagicMock(file_path="videos/runtime-limit.mp4")
        video_file.download_to_drive = AsyncMock(
            side_effect=BadRequest("File is too big")
        )
        video.get_file.return_value = video_file
        update.message.video = video

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._MEDIA_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.video_handler(update, context)

        video.get_file.assert_awaited_once()
        video_file.download_to_drive.assert_awaited_once()
        mock_sm.send_to_window.assert_not_called()
        warning = mock_reply.await_args.args[1]
        assert "too large for Telegram bot download" in warning
        assert "Could not download" not in warning

    @pytest.mark.asyncio
    async def test_static_sticker_forwarding_normalizes_to_image_attachment(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()

        sticker = MagicMock(
            file_unique_id="sticker-static",
            is_animated=False,
            is_video=False,
            emoji="🙂",
        )
        sticker.get_file = AsyncMock()
        sticker_file = MagicMock()
        sticker_file.download_to_drive = AsyncMock(
            side_effect=lambda path: _write_test_webp(path)
        )
        sticker.get_file.return_value = sticker_file
        update.message.sticker = sticker

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

            await bot_mod.sticker_handler(update, context)

            expected_source = tmp_path / "1700000000_sticker-static.webp"
            expected_path = tmp_path / "1700000000_sticker-static.png"
            sticker_file.download_to_drive.assert_called_once_with(expected_source)
            assert expected_path.exists()
            mock_sm.send_to_window.assert_called_once_with(
                "@7",
                f"Sticker emoji: 🙂\nSticker image: (image attached: {expected_path})",
            )

    @pytest.mark.asyncio
    async def test_unbound_sticker_ingress_is_silent(self):
        update = _make_topic_update()
        context = _make_context()
        sticker = MagicMock(
            file_unique_id="sticker-static",
            is_animated=False,
            is_video=False,
            emoji="🙂",
        )
        sticker.get_file = AsyncMock()
        update.message.sticker = sticker

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
            patch(
                "ccbot.bot._sticker_artifacts",
                new_callable=AsyncMock,
            ) as mock_artifacts,
        ):
            mock_sm.get_window_for_thread.return_value = None

            await bot_mod.sticker_handler(update, context)

        sticker.get_file.assert_not_called()
        mock_artifacts.assert_not_awaited()
        mock_sm.set_group_chat_id.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        mock_tmux.find_window_by_id.assert_not_called()
        mock_reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_animated_tgs_sticker_forwards_thumbnail_and_original_artifact(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()

        thumbnail = MagicMock(file_unique_id="thumb-static")
        thumbnail.get_file = AsyncMock()
        thumbnail_file = MagicMock()
        thumbnail_file.download_to_drive = AsyncMock(
            side_effect=lambda path: _write_test_webp(path)
        )
        thumbnail.get_file.return_value = thumbnail_file
        original_file = MagicMock(file_path="stickers/monkey.tgs")
        original_file.download_to_drive = AsyncMock(
            side_effect=lambda path: path.write_bytes(b"tgs")
        )
        sticker = MagicMock(
            file_unique_id="sticker-animated",
            is_animated=True,
            is_video=False,
            emoji=None,
            thumbnail=thumbnail,
        )
        sticker.get_file = AsyncMock()
        sticker.get_file.return_value = original_file
        update.message.sticker = sticker

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

            await bot_mod.sticker_handler(update, context)

            expected_source = (
                tmp_path / "1700000000_sticker-animated_thumb-static_thumbnail.webp"
            )
            expected_path = (
                tmp_path / "1700000000_sticker-animated_thumb-static_thumbnail.png"
            )
            thumbnail_file.download_to_drive.assert_called_once_with(expected_source)
            expected_original = tmp_path / "1700000000_sticker-animated_original.tgs"
            original_file.download_to_drive.assert_called_once_with(expected_original)
            assert expected_path.exists()
            mock_sm.send_to_window.assert_called_once_with(
                "@7",
                "\n".join(
                    [
                        f"Sticker thumbnail: (image attached: {expected_path})",
                        f"Sticker animation artifact: {expected_original}",
                        "Sticker animation GIF: not generated for .tgs stickers",
                    ]
                ),
            )

    @pytest.mark.asyncio
    async def test_video_sticker_forwards_thumbnail_and_original_without_ffmpeg(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()

        thumbnail = MagicMock(file_unique_id="thumb-static")
        thumbnail.get_file = AsyncMock()
        thumbnail_file = MagicMock()
        thumbnail_file.download_to_drive = AsyncMock(
            side_effect=lambda path: _write_test_webp(path)
        )
        thumbnail.get_file.return_value = thumbnail_file
        original_file = MagicMock(file_path="stickers/monkey.webm")
        original_file.download_to_drive = AsyncMock(
            side_effect=lambda path: path.write_bytes(b"webm")
        )
        sticker = MagicMock(
            file_unique_id="sticker-video",
            is_animated=False,
            is_video=True,
            emoji="🐵",
            thumbnail=thumbnail,
        )
        sticker.get_file = AsyncMock(return_value=original_file)
        update.message.sticker = sticker

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._IMAGES_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.shutil.which", return_value=None),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.sticker_handler(update, context)

            expected_thumbnail = (
                tmp_path / "1700000000_sticker-video_thumb-static_thumbnail.png"
            )
            expected_original = tmp_path / "1700000000_sticker-video_original.webm"
            original_file.download_to_drive.assert_called_once_with(expected_original)
            mock_sm.send_to_window.assert_called_once_with(
                "@7",
                "\n".join(
                    [
                        "Sticker emoji: 🐵",
                        f"Sticker thumbnail: (image attached: {expected_thumbnail})",
                        f"Sticker animation artifact: {expected_original}",
                        "Sticker animation GIF: unavailable (ffmpeg not found)",
                    ]
                ),
            )

    @pytest.mark.asyncio
    async def test_video_sticker_gif_conversion_failure_is_non_fatal(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()

        thumbnail = MagicMock(file_unique_id="thumb-static")
        thumbnail.get_file = AsyncMock()
        thumbnail_file = MagicMock()
        thumbnail_file.download_to_drive = AsyncMock(
            side_effect=lambda path: _write_test_webp(path)
        )
        thumbnail.get_file.return_value = thumbnail_file
        original_file = MagicMock(file_path="stickers/monkey.webm")
        original_file.download_to_drive = AsyncMock(
            side_effect=lambda path: path.write_bytes(b"webm")
        )
        sticker = MagicMock(
            file_unique_id="sticker-video",
            is_animated=False,
            is_video=True,
            emoji=None,
            thumbnail=thumbnail,
        )
        sticker.get_file = AsyncMock(return_value=original_file)
        update.message.sticker = sticker

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._IMAGES_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch(
                "ccbot.bot.subprocess.run",
                side_effect=bot_mod.subprocess.CalledProcessError(1, "ffmpeg"),
            ),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.sticker_handler(update, context)

            text_to_send = mock_sm.send_to_window.await_args.args[1]
            assert "Sticker thumbnail: (image attached:" in text_to_send
            assert "Sticker animation artifact:" in text_to_send
            assert "Sticker animation GIF: unavailable (ffmpeg failed:" in text_to_send

    @pytest.mark.asyncio
    async def test_video_sticker_gif_conversion_success_is_reported(
        self, tmp_path
    ):
        update = _make_topic_update()
        context = _make_context()

        thumbnail = MagicMock(file_unique_id="thumb-static")
        thumbnail.get_file = AsyncMock()
        thumbnail_file = MagicMock()
        thumbnail_file.download_to_drive = AsyncMock(
            side_effect=lambda path: _write_test_webp(path)
        )
        thumbnail.get_file.return_value = thumbnail_file
        original_file = MagicMock(file_path="stickers/monkey.webm")
        original_file.download_to_drive = AsyncMock(
            side_effect=lambda path: path.write_bytes(b"webm")
        )
        sticker = MagicMock(
            file_unique_id="sticker-video",
            is_animated=False,
            is_video=True,
            emoji=None,
            thumbnail=thumbnail,
        )
        sticker.get_file = AsyncMock(return_value=original_file)
        update.message.sticker = sticker

        def _write_gif(args, **_kwargs):
            Path(args[-1]).write_bytes(b"GIF89a")

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._IMAGES_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("ccbot.bot.subprocess.run", side_effect=_write_gif),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.sticker_handler(update, context)

            expected_gif = tmp_path / "1700000000_sticker-video_original.gif"
            text_to_send = mock_sm.send_to_window.await_args.args[1]
            assert f"Sticker animation GIF: {expected_gif}" in text_to_send
            assert "unavailable" not in text_to_send

    @pytest.mark.asyncio
    async def test_animated_sticker_without_thumbnail_fails_closed(self, tmp_path):
        update = _make_topic_update()
        context = _make_context()
        sticker = MagicMock(
            file_unique_id="sticker-animated",
            is_animated=True,
            is_video=False,
            thumbnail=None,
        )
        sticker.get_file = AsyncMock()
        update.message.sticker = sticker

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._IMAGES_DIR", tmp_path),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            await bot_mod.sticker_handler(update, context)

        sticker.get_file.assert_not_called()
        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_called_once()
        await_args = mock_reply.await_args
        assert await_args is not None
        assert "no thumbnail" in await_args.args[1]

    @pytest.mark.asyncio
    async def test_sticker_forwarding_surfaces_blocked_prompt(self, tmp_path):
        update = _make_topic_update()
        context = _make_context()
        sticker = MagicMock(
            file_unique_id="sticker-static",
            is_animated=False,
            is_video=False,
            emoji=None,
        )
        sticker.get_file = AsyncMock()
        sticker_file = MagicMock()
        sticker_file.download_to_drive = AsyncMock(
            side_effect=lambda path: _write_test_webp(path)
        )
        sticker.get_file.return_value = sticker_file
        update.message.sticker = sticker

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot._IMAGES_DIR", tmp_path),
            patch("ccbot.bot.time.time", return_value=1700000000),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.clear_status_msg_info"),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock),
            patch(
                "ccbot.bot._surface_blocked_prompt_state",
                new_callable=AsyncMock,
            ) as mock_blocked,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(
                return_value=(False, bot_mod.BLOCKED_PROMPT_SEND_MESSAGE)
            )

            await bot_mod.sticker_handler(update, context)

        mock_blocked.assert_awaited_once_with(
            context.bot,
            1,
            "@7",
            42,
            reply_message=update.message,
        )

    @pytest.mark.asyncio
    async def test_unsupported_content_message_mentions_documents_and_stickers(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            await bot_mod.unsupported_content_handler(update, context)

        mock_reply.assert_called_once()
        await_args = mock_reply.await_args
        assert await_args is not None
        message = await_args.args[1]
        assert "document" in message
        assert "sticker" in message
        assert "audio" in message
        assert "video messages are supported" in message
        assert "Video notes, animations" in message
        assert "Stickers, video" not in message

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
            patch(
                "ccbot.bot.transcribe_voice",
                new_callable=AsyncMock,
                return_value="Hello from voice",
            ),
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
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@7")
            )
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
            patch(
                "ccbot.bot.handle_interactive_ui", new_callable=AsyncMock
            ) as mock_handle_ui,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@7")
            )
            mock_tmux.capture_pane = AsyncMock(
                return_value="OpenAI Codex\n› ping\n■ Approval required\n"
            )

            await bot_mod.text_handler(update, context)

        mock_handle_ui.assert_awaited_once_with(context.bot, 1, "@7", 42)
        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_awaited_once()
        assert (
            "Terminal prompt is waiting for a decision" in mock_reply.await_args.args[1]
        )

    @pytest.mark.asyncio
    async def test_text_handler_fails_closed_when_omx_question_active(self):
        update = _make_topic_update()
        update.message.text = "continue"
        context = _make_context()
        active_record = object()
        window = MagicMock()
        window.window_id = "@7"
        window.cwd = "/tmp/project"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.enqueue_status_update", new_callable=AsyncMock),
            patch("ccbot.bot.find_active_omx_question", return_value=active_record),
            patch(
                "ccbot.bot.handle_omx_question_ui",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_omx_question,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_tmux.capture_pane = AsyncMock(return_value="OpenAI Codex\n› ready\n")

            await bot_mod.text_handler(update, context)

        mock_omx_question.assert_awaited_once_with(
            context.bot,
            1,
            "@7",
            42,
            record=active_record,
        )
        mock_sm.send_to_window.assert_not_called()
        mock_reply.assert_awaited_once()
        assert "OMX question is waiting for an answer" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_usage_command_is_runtime_gated_for_codex(self):
        update = _make_topic_update()
        context = _make_context()

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            window = MagicMock()
            window.window_id = "@7"
            window.cwd = "/tmp"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_sm.resolve_window_for_surface.return_value = "@7"
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_window_state.return_value = SimpleNamespace(
                runtime_kind="codex"
            )

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
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            window = MagicMock()
            window.window_id = "@7"
            window.cwd = "/tmp"
            mock_tmux.find_window_by_id = AsyncMock(return_value=window)
            mock_sm.resolve_window_for_surface.return_value = "@7"
            mock_sm.resolve_window_for_thread.return_value = "@7"
            mock_sm.get_window_state.return_value = SimpleNamespace(
                runtime_kind="claude"
            )
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
        update.callback_query.message = MagicMock(
            message_thread_id=42, chat=update.effective_chat
        )
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
        assert (
            bot_mod.infer_runtime_kind_from_command("/usr/local/bin/codex --json")
            == "codex"
        )
        assert bot_mod.infer_runtime_kind_from_command("uvx codex --help") == "codex"
        assert (
            bot_mod.infer_runtime_kind_from_command(
                "claude --dangerously-skip-permissions"
            )
            == "claude"
        )

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
                return_value=(
                    True,
                    "Created window 'proj' at /tmp/project",
                    "proj",
                    "@7",
                )
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
            patch(
                "ccbot.bot.build_thread_picker", return_value=("picker", MagicMock())
            ),
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
            patch(
                "ccbot.bot._create_and_bind_window", new_callable=AsyncMock
            ) as mock_create,
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
            patch(
                "ccbot.bot._create_and_bind_window", new_callable=AsyncMock
            ) as mock_create,
        ):
            mock_sm.validate_topic_bind_flow_callback.return_value = True
            await bot_mod.callback_handler(update, context)

        mock_create.assert_awaited_once()
        assert mock_create.await_args.kwargs == {
            "pending_surface": bot_mod.ControlSurface(
                kind="group_topic",
                chat_id=100,
                thread_id=42,
                legacy_scope_id=42,
                surface_key="t:42",
                label="topic",
                is_shared_group=True,
                supports_bind_flow=True,
            )
        }


class TestTelegramDelivery:
    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_commentary_to_latest_visible_artifact(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="Inspecting the repository layout",
            is_complete=True,
            content_type="commentary",
            role="assistant",
            event_kind="commentary",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.mark_runtime_presence_active") as mock_presence,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_commentary_update", new_callable=AsyncMock
            ) as mock_commentary,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_commentary.assert_awaited_once()
        mock_content.assert_not_awaited()
        mock_presence.assert_called_once_with(1, 42, "@7")

    def test_compact_policy_does_not_clip_commentary_text(self):
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="x" * 1200,
            is_complete=True,
            content_type="commentary",
            role="assistant",
            event_kind="commentary",
            runtime_kind="codex",
        )

        projected = apply_telegram_delivery_policy(msg, mode="compact")

        assert projected.semantic_kind == "commentary"
        assert projected.text == "x" * 1200
        assert projected.status_message_eligible is False

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_plan_update_to_dedicated_artifact(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="• Updated Plan\n  ▶ Implement delivery",
            is_complete=True,
            content_type="plan_update",
            role="assistant",
            event_kind="plan_update",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_commentary_update", new_callable=AsyncMock
            ) as mock_commentary,
            patch("ccbot.bot.enqueue_plan_update", new_callable=AsyncMock) as mock_plan,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_commentary.assert_not_awaited()
        mock_plan.assert_awaited_once()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_orchestration_to_latest_visible_artifact_in_commentary_lane(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="• Waiting for Mill [explorer]",
            is_complete=True,
            content_type="orchestration",
            role="assistant",
            event_kind="orchestration",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_commentary_update", new_callable=AsyncMock
            ) as mock_commentary,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_commentary.assert_awaited_once()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_does_not_preclose_surface_before_final_answer(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="Final answer",
            is_complete=True,
            content_type="text",
            role="assistant",
            event_kind="message",
            runtime_kind="codex",
        )

        call_order: list[str] = []

        async def _record_content(*args, **kwargs):
            call_order.append("content")

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", side_effect=_record_content
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        assert mock_content.call_count == 1
        assert call_order == ["content"]

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_suppresses_internal_skill_user_echo(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="<skill>\n<name>parallel</name>\n</skill>",
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_not_called()
        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_keeps_literal_agents_instructions_echo_visible(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="# AGENTS.md instructions for /home/tools/server/comfy\n\n<INSTRUCTIONS>\nkeep calm\n</INSTRUCTIONS>",
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_called_once_with(1, 42)
        mock_status.assert_not_awaited()
        mock_content.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "# Repository Guidelines\n\nKeep the deploy path explicit.",
            "Please review this pasted policy.\n<INSTRUCTIONS>\nkeep calm\n</INSTRUCTIONS>",
        ],
    )
    async def test_handle_new_message_compact_mode_keeps_literal_instruction_like_user_text_visible(
        self, text: str
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text=text,
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_called_once_with(1, 42)
        mock_status.assert_not_awaited()
        mock_content.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_keeps_plain_user_echo_visible(self):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="ping",
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_called_once_with(1, 42)
        mock_status.assert_not_awaited()
        mock_content.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_suppresses_turn_aborted_user_echo(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>",
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_not_called()
        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_suppresses_placeholder_reasoning(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="[reasoning]",
            is_complete=True,
            content_type="reasoning",
            role="assistant",
            event_kind="reasoning",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_thinking_to_status_only(self):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="Checking the workspace layout and comparing outputs.",
            is_complete=True,
            content_type="thinking",
            role="assistant",
            event_kind="reasoning",
            runtime_kind="claude",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_awaited_once()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_command_execution_to_status(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="/bin/bash -lc 'rg -n foo'\ncompleted\noutput 37 line(s)",
            is_complete=True,
            content_type="command_execution",
            role="assistant",
            event_kind="command_execution",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_awaited_once()
        status_text = mock_status.await_args.args[3]
        assert status_text.startswith("⌘ Command")
        assert "rg -n foo" in status_text
        assert "output 37 line(s)" in status_text
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_local_command_to_status(self):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="  ⎿  Output 37 lines",
            is_complete=True,
            content_type="local_command",
            role="assistant",
            event_kind="command_execution",
            runtime_kind="claude",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_awaited_once()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_tool_use_to_status_only(self):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text='exec_command({"cmd":"hostname"})',
            is_complete=True,
            content_type="tool_use",
            tool_use_id="toolu_1",
            role="assistant",
            event_kind="tool_call",
            runtime_kind="codex",
            tool_name="exec_command",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_awaited_once()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_tool_result_to_status_only(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="Tool output: 179 line(s)",
            is_complete=True,
            content_type="tool_result",
            tool_use_id="toolu_1",
            role="assistant",
            event_kind="tool_output",
            runtime_kind="codex",
            tool_name="exec_command",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_awaited_once()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_delivers_generated_image_success_as_final_text(
        self,
    ):
        bot = AsyncMock()
        generated_text = (
            "Generated Image:\n"
            "  └ Create an improved sticker-style monkey thumbnail.\n"
            "  └ Saved to: file:///home/tools/imm/.codex/generated_images/run/ig.png"
        )
        msg = NormalizedEvent(
            thread_id="thread-1",
            text=generated_text,
            is_complete=True,
            content_type="tool_result",
            tool_use_id="toolu_image",
            role="assistant",
            event_kind="tool_output",
            runtime_kind="codex",
            tool_name="image_gen.imagegen",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
            patch("ccbot.bot.get_message_queue", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_content.assert_awaited_once()
        kwargs = mock_content.await_args.kwargs
        assert kwargs["content_type"] == "text"
        assert kwargs["semantic_kind"] == "assistant_final"
        assert kwargs["text"] == generated_text

    @pytest.mark.asyncio
    async def test_handle_new_message_compact_mode_routes_orchestration_to_latest_visible_artifact(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="• Spawned Mill [explorer] (gpt-5.4 medium)\n  └ Review this implementation plan",
            is_complete=True,
            content_type="orchestration",
            role="assistant",
            event_kind="orchestration",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_commentary_update", new_callable=AsyncMock
            ) as mock_commentary,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_commentary.assert_awaited_once()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_routes_incomplete_progress_to_status(self):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="Inspecting the repository layout",
            is_complete=False,
            content_type="commentary",
            role="assistant",
            event_kind="commentary",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "verbose"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_awaited_once()
        assert "Commentary" in mock_status.await_args.args[3]
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_keeps_complete_tool_use_as_content(self):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="Read src/app.py",
            is_complete=True,
            content_type="tool_use",
            tool_use_id="toolu_1",
            role="assistant",
            event_kind="tool_call",
            runtime_kind="claude",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "verbose"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_content.assert_awaited_once()
        assert mock_content.await_args.kwargs["tool_use_id"] == "toolu_1"

    @pytest.mark.asyncio
    async def test_handle_new_message_suppresses_codex_termination_summary_direct_delivery(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text=(
                "Token usage: total=12 input=10 output=2\n"
                "To continue this session, run codex resume comfy"
            ),
            is_complete=True,
            content_type="text",
            role="assistant",
            event_kind="assistant_message",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_skips_lifecycle_only_events(self):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="started",
            is_complete=True,
            content_type="lifecycle",
            role="assistant",
            event_kind="lifecycle",
            runtime_kind="codex",
            dispatch_to_telegram=False,
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_reopens_turn_on_lifecycle_turn_started_when_lane_closed(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="turn_started",
            is_complete=True,
            content_type="lifecycle",
            role="assistant",
            event_kind="lifecycle",
            runtime_kind="codex",
            dispatch_to_telegram=False,
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=4),
            patch("ccbot.bot.is_pre_final_visible_lane_closed", return_value=True),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=5
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_called_once_with(1, 42)
        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_does_not_reopen_turn_on_lifecycle_turn_started_when_lane_open(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="turn_started",
            is_complete=True,
            content_type="lifecycle",
            role="assistant",
            event_kind="lifecycle",
            runtime_kind="codex",
            dispatch_to_telegram=False,
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=4),
            patch("ccbot.bot.is_pre_final_visible_lane_closed", return_value=False),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=5
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_not_called()
        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_reopens_pre_final_lane_for_ordinary_user_echo(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="please continue with the next step",
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
            dispatch_to_telegram=False,
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.flush_terminal_artifacts_before_new_turn",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_flush_terminal,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_flush_terminal.assert_awaited_once_with(bot, 1, 42)
        mock_open_turn.assert_called_once_with(1, 42)
        mock_status.assert_not_awaited()
        mock_content.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_new_message_drops_post_final_commentary_when_lane_closed(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text="still waiting for worker result",
            is_complete=True,
            content_type="commentary",
            role="assistant",
            event_kind="commentary",
            runtime_kind="codex",
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=5),
            patch("ccbot.bot.is_pre_final_visible_lane_closed", return_value=True),
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_commentary_update", new_callable=AsyncMock
            ) as mock_commentary,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
            patch("ccbot.bot.get_interactive_msg_id", return_value=None),
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
            mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

            await bot_mod.handle_new_message(msg, bot)

        mock_status.assert_not_awaited()
        mock_commentary.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_new_message_does_not_reopen_pre_final_lane_for_subagent_notification(
        self,
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text='<subagent_notification>\n{"agent_path":"agent-1","status":{"completed":"done"}}\n</subagent_notification>',
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
            dispatch_to_telegram=False,
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_not_called()
        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "text",
        [
            "<system-reminder>secret instructions</system-reminder>",
            "<bash-stdout>line 1</bash-stdout>",
            "<bash-stderr>line 1</bash-stderr>",
            "<local-command-caveat>caveat</local-command-caveat>",
            "<command-name>/status</command-name>",
            "<bash-input>ls -la</bash-input>",
        ],
    )
    async def test_handle_new_message_does_not_reopen_pre_final_lane_for_hidden_internal_non_turn_payloads(
        self, text: str
    ):
        bot = AsyncMock()
        msg = NormalizedEvent(
            thread_id="thread-1",
            text=text,
            is_complete=True,
            content_type="text",
            role="user",
            event_kind="user_message",
            runtime_kind="codex",
            dispatch_to_telegram=False,
        )

        with (
            patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.current_turn_generation", return_value=0),
            patch(
                "ccbot.bot.open_new_turn_generation", return_value=1
            ) as mock_open_turn,
            patch(
                "ccbot.bot.enqueue_status_update", new_callable=AsyncMock
            ) as mock_status,
            patch(
                "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
            ) as mock_content,
        ):
            mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])

            await bot_mod.handle_new_message(msg, bot)

        mock_open_turn.assert_not_called()
        mock_status.assert_not_awaited()
        mock_content.assert_not_awaited()

    def test_compact_policy_keeps_code_fences_balanced_when_truncating_status(self):
        event = NormalizedEvent(
            thread_id="thread-1",
            text="```sh\n" + "\n".join(f"line {i}" for i in range(40)) + "\n```",
            is_complete=True,
            content_type="command_execution",
            role="assistant",
            event_kind="command_execution",
        )

        projected = apply_telegram_delivery_policy(event, mode="compact")

        assert projected.status_message_eligible is True
        assert projected.text.count("```") % 2 == 0
        assert "\n```\n\npreview " in projected.text
        assert len(projected.text) <= 560

    def test_compact_policy_clips_oversized_first_code_line_within_budget(self):
        event = NormalizedEvent(
            thread_id="thread-1",
            text="```sh\n"
            + "python3 - <<'PY' "
            + ("x" * 500)
            + "\nprint('done')\n```\ncompleted",
            is_complete=True,
            content_type="command_execution",
            role="assistant",
            event_kind="command_execution",
        )

        projected = apply_telegram_delivery_policy(event, mode="compact")

        assert projected.status_message_eligible is True
        assert projected.text.count("```") % 2 == 0
        assert len(projected.text) <= 560
        assert "\n```\n\npreview " in projected.text

    def test_compact_policy_keeps_balanced_code_fence_with_prefix_line(self):
        event = NormalizedEvent(
            thread_id="thread-1",
            text="completed\n```sh\n"
            + "\n".join(f"line {i}" for i in range(40))
            + "\n```\nextra footer line",
            is_complete=False,
            content_type="tool_result",
            role="assistant",
            event_kind="tool_output",
        )

        projected = apply_telegram_delivery_policy(event, mode="compact")

        assert projected.status_message_eligible is True
        assert projected.text.startswith("completed\n```sh\n")
        assert projected.text.count("```") % 2 == 0
        assert "\n```\n\npreview " in projected.text
        assert len(projected.text) <= 560

    def test_compact_policy_overflow_fallback_never_leaves_unbalanced_fence(self):
        event = NormalizedEvent(
            thread_id="thread-1",
            text=(
                "completed "
                + ("very long prefix " * 20)
                + "\n```sh\n"
                + "\n".join(f"line {i}" for i in range(20))
                + "\n```\n"
                + ("very long suffix " * 20)
            ),
            is_complete=False,
            content_type="tool_result",
            role="assistant",
            event_kind="tool_output",
        )

        projected = apply_telegram_delivery_policy(event, mode="compact")

        assert projected.status_message_eligible is True
        assert len(projected.text) <= 560
        assert projected.text.count("```") in {0, 2}

    def test_compact_policy_promotes_generated_image_success_to_final_text(self):
        event = NormalizedEvent(
            thread_id="thread-1",
            text=(
                "```text\n"
                "Generated Image:\n"
                "  └ Create an improved sticker-style monkey thumbnail.\n"
                "  └ Saved to: file:///home/tools/imm/.codex/generated_images/run/ig.png\n"
                "```"
            ),
            is_complete=True,
            content_type="tool_result",
            role="assistant",
            event_kind="tool_output",
            tool_name="imagegen",
        )

        projected = apply_telegram_delivery_policy(event, mode="compact")

        assert projected.content_type == "text"
        assert projected.semantic_kind == "assistant_final"
        assert projected.delivery_class == "history"
        assert projected.dispatch_to_telegram is True
        assert projected.status_message_eligible is False

    def test_compact_policy_forces_dispatch_for_ordinary_user_echo(self):
        event = NormalizedEvent(
            thread_id="thread-1",
            text="show latest status",
            is_complete=False,
            content_type="text",
            role="user",
            event_kind="user_message",
            dispatch_to_telegram=False,
            include_in_history=False,
            status_message_eligible=True,
        )

        projected = apply_telegram_delivery_policy(event, mode="compact")

        assert projected.dispatch_to_telegram is True
        assert projected.include_in_history is True
        assert projected.status_message_eligible is False
        assert projected.is_complete is True


def test_compact_policy_status_preview_shows_ten_code_lines_after_budget_increase():
    from ccbot.runtime_types import NormalizedEvent
    from ccbot.telegram_delivery_policy import apply_telegram_delivery_policy

    event = NormalizedEvent(
        thread_id="thread-1",
        text="```sh\n" + "\n".join(f"line {i}" for i in range(20)) + "\n```",
        is_complete=True,
        content_type="command_execution",
        role="assistant",
        event_kind="command_execution",
    )

    projected = apply_telegram_delivery_policy(event, mode="compact")

    assert projected.status_message_eligible is True
    assert "line 9" in projected.text
    assert "line 10" not in projected.text
    assert "preview 10/20 lines" in projected.text
    assert len(projected.text) <= 560


@pytest.mark.asyncio
async def test_handle_new_message_waits_for_assistant_final_queue_before_next_turn_can_advance():
    bot = AsyncMock()
    msg = NormalizedEvent(
        thread_id="thread-1",
        text="First final answer",
        is_complete=True,
        content_type="text",
        role="assistant",
        event_kind="message",
        runtime_kind="codex",
    )
    joined = False

    class QueueProbe:
        async def join(self):
            nonlocal joined
            joined = True

    with (
        patch.object(bot_mod.config, "telegram_delivery_mode", "compact"),
        patch("ccbot.bot.session_manager") as mock_sm,
        patch(
            "ccbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
        patch("ccbot.bot.get_message_queue", return_value=QueueProbe()),
        patch("ccbot.bot.get_interactive_msg_id", return_value=None),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(1, "@7", 42)])
        mock_sm.resolve_session_for_window = AsyncMock(return_value=None)

        await bot_mod.handle_new_message(msg, bot)

    mock_content.assert_awaited_once()
    assert joined is True
