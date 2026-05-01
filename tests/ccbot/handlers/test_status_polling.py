"""Tests for status_polling — Settings UI detection via the poller path.

Simulates the user workflow: /model is sent to Claude Code, the Settings
model picker renders in the terminal, and the status poller detects it
on its next 1s tick.
"""

import asyncio

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.status_polling import update_status_message
from ccbot.handlers import status_polling as status_polling_mod


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from ccbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.fixture(autouse=True)
def _clear_runtime_presence():
    status_polling_mod._runtime_presence.clear()
    status_polling_mod._last_pane_text.clear()
    yield
    status_polling_mod._runtime_presence.clear()
    status_polling_mod._last_pane_text.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestStatusPollerSettingsDetection:
    """Simulate the status poller detecting a Settings UI in the terminal.

    This is the actual code path for /model: no JSONL tool_use entry exists,
    so the status poller (update_status_message) is the only detector.
    """

    @pytest.mark.asyncio
    async def test_settings_ui_detected_and_snapshot_sent(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Poller captures blocked prompt surface and delegates to prompt handler."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_handle_ui.return_value = True

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_called_once_with(mock_bot, 1, window_id, 42)

    @pytest.mark.asyncio
    async def test_normal_pane_no_interactive_ui(self, mock_bot: AsyncMock):
        """Normal pane text → no handle_interactive_ui call, just status check."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        normal_pane = (
            "some output\n"
            "✻ Reading file\n"
            "──────────────────────────────────────\n"
            "❯ \n"
            "──────────────────────────────────────\n"
            "  [Opus 4.6] Context: 50%\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.handle_interactive_ui",
                new_callable=AsyncMock,
            ) as mock_handle_ui,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_status,
            patch(
                "ccbot.handlers.status_polling.current_turn_generation",
                return_value=7,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=normal_pane)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            mock_handle_ui.assert_not_called()
            mock_status.assert_awaited_once()
            assert mock_status.await_args.kwargs["turn_generation"] == 7

    @pytest.mark.asyncio
    async def test_omx_question_does_not_skip_terminal_safety_checks(
        self, mock_bot: AsyncMock
    ):
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        mock_window.pane_current_command = "node"
        pane_text = "Working\n────────────────\ngpt-5.5 high"

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_pending_input_update",
                new_callable=AsyncMock,
            ) as mock_pending,
            patch(
                "ccbot.handlers.status_polling._maybe_enqueue_runtime_exit_warning",
                new_callable=AsyncMock,
            ) as mock_runtime_warning,
            patch(
                "ccbot.handlers.status_polling.handle_omx_question_ui",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_omx_question,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_status,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        mock_tmux.capture_pane.assert_awaited_once_with(window_id)
        mock_pending.assert_awaited_once()
        mock_runtime_warning.assert_awaited_once()
        mock_omx_question.assert_awaited_once_with(mock_bot, 1, window_id, 42)
        mock_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_settings_ui_end_to_end_sends_read_only_prompt_snapshot(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """Full end-to-end: poller → blocked prompt detection → Telegram snapshot.

        Uses real handle_interactive_ui (not mocked) to verify the full path.
        """
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

            # Verify bot.send_message was called with a read-only snapshot
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert call_kwargs["chat_id"] == 100
            assert call_kwargs["message_thread_id"] == 42
            keyboard = call_kwargs["reply_markup"]
            assert keyboard is None
            # Verify the message text contains model picker content
            assert "Select model" in call_kwargs["text"]
            assert "Remote controls are disabled for this prompt" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_codex_exec_approval_end_to_end_sends_keyboard(self, mock_bot: AsyncMock):
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id
        approval_pane = (
            "\n"
            "  Would you like to run the following command?\n"
            "\n"
            "  Reason: because the model asked to do it\n"
            "\n"
            "  $ echo hello world\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. Yes, and don't ask again for commands that start with `echo hello world` (p)\n"
            "  3. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )

        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux_poll,
            patch("ccbot.handlers.interactive_ui.tmux_manager") as mock_tmux_ui,
            patch("ccbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux_poll.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_poll.capture_pane = AsyncMock(return_value=approval_pane)
            mock_tmux_ui.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux_ui.capture_pane = AsyncMock(return_value=approval_pane)
            mock_sm.resolve_chat_id.return_value = 100

            await update_status_message(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        call_kwargs = mock_bot.send_message.call_args.kwargs
        assert call_kwargs["reply_markup"] is not None
        assert "Would you like to run the following command?" in call_kwargs["text"]

    @pytest.mark.asyncio
    async def test_missing_window_clear_status_enqueues_clear_with_current_generation(
        self, mock_bot: AsyncMock
    ):
        with (
            patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
            patch(
                "ccbot.handlers.status_polling.enqueue_status_update",
                new_callable=AsyncMock,
            ) as mock_status,
            patch(
                "ccbot.handlers.status_polling.current_turn_generation",
                return_value=11,
            ),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=None)

            await update_status_message(
                mock_bot, user_id=1, window_id="@5", thread_id=42
            )

            mock_status.assert_awaited_once()
            assert mock_status.await_args.kwargs["turn_generation"] == 11


@pytest.mark.asyncio
async def test_status_poll_loop_unbinds_stale_main_chat_surface_by_chat_id(
    mock_bot: AsyncMock,
):
    with (
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch(
            "ccbot.handlers.status_polling._enqueue_discontinuity_warning",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.clear_topic_state",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "ccbot.handlers.status_polling.asyncio.sleep",
            side_effect=asyncio.CancelledError,
        ),
    ):
        mock_sm.iter_topic_bindings.return_value = [
            SimpleNamespace(user_id=1, thread_id=None, window_id="@7")
        ]
        mock_sm.is_external_binding_window_id.return_value = False
        mock_sm.resolve_chat_id.return_value = -100200300
        mock_sm.resolve_thread_for_window = AsyncMock(return_value=None)
        mock_sm.get_surface_coordinates_for_window.return_value = (None, -100200300, None)
        mock_tmux.find_window_by_id = AsyncMock(return_value=None)

        with pytest.raises(asyncio.CancelledError):
            await status_polling_mod.status_poll_loop(mock_bot)

    mock_sm.unbind_surface.assert_called_once_with(1, chat_id=-100200300)
    mock_clear.assert_awaited_once_with(1, None, mock_bot)


@pytest.mark.asyncio
async def test_update_status_message_emits_runtime_exit_warning_with_images_first(
    mock_bot: AsyncMock,
):
    status_polling_mod._runtime_presence[(1, "@5")] = True
    mock_window = MagicMock()
    mock_window.window_id = "@5"
    mock_window.pane_current_command = "bash"

    with (
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.enqueue_pending_input_update",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.enqueue_status_update",
            new_callable=AsyncMock,
        ) as mock_status,
        patch(
            "ccbot.handlers.status_polling.build_discontinuity_image_data",
            new_callable=AsyncMock,
            return_value=[("discontinuity-screenshot.png", b"png-bytes")],
        ),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(
            return_value="codex resume thread-1\nuser@host:/tmp$ "
        )
        mock_sm.get_process_descriptor.return_value = SimpleNamespace(runtime_kind="codex")
        mock_sm.resolve_thread_for_window = AsyncMock(return_value=None)

        await update_status_message(mock_bot, user_id=1, window_id="@5", thread_id=42)

    mock_content.assert_awaited_once()
    kwargs = mock_content.await_args.kwargs
    assert kwargs["semantic_kind"] == "warning"
    assert kwargs["warning_key"] == "runtime-discontinuity:exit:@5"
    assert kwargs["image_data"] == [("discontinuity-screenshot.png", b"png-bytes")]
    assert "codex resume thread-1" in kwargs["text"]
    mock_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_status_message_detects_runtime_exit_even_when_pane_command_is_bash(
    mock_bot: AsyncMock,
):
    status_polling_mod._runtime_presence[(1, "@5")] = True
    mock_window = MagicMock()
    mock_window.window_id = "@5"
    mock_window.pane_current_command = "bash"
    pane_text = (
        "iqdoctor@str:/tools/ccbot$ codex --no-alt-screen\n"
        "╭──────────────────────────────────────────────╮\n"
        "│ >_ OpenAI Codex (v0.118.0)                   │\n"
        "╰──────────────────────────────────────────────╯\n"
        "Token usage: total=10 input=8 output=2\n"
        "To continue this session, run codex resume thread-1\n"
        "iqdoctor@str:/tools/ccbot$\n"
    )

    with (
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.enqueue_pending_input_update",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.enqueue_status_update",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.build_discontinuity_image_data",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
        mock_sm.get_process_descriptor.return_value = SimpleNamespace(runtime_kind="codex")
        mock_sm.resolve_thread_for_window = AsyncMock(return_value=None)

        await update_status_message(mock_bot, user_id=1, window_id="@5", thread_id=42)

    mock_content.assert_awaited_once()
    kwargs = mock_content.await_args.kwargs
    assert kwargs["warning_key"] == "runtime-discontinuity:exit:@5"
    assert "codex resume thread-1" in kwargs["text"]




@pytest.mark.asyncio
async def test_update_status_message_emits_runtime_exit_warning_for_shell_generic_prompt_glyph(
    mock_bot: AsyncMock,
):
    status_polling_mod._runtime_presence[(1, "@5")] = True
    mock_window = MagicMock()
    mock_window.window_id = "@5"
    mock_window.pane_current_command = "zsh"

    with (
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.enqueue_pending_input_update",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.enqueue_status_update",
            new_callable=AsyncMock,
        ) as mock_status,
        patch(
            "ccbot.handlers.status_polling.build_discontinuity_image_data",
            new_callable=AsyncMock,
            return_value=[("discontinuity-screenshot.png", b"png-bytes")],
        ),
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value="❯ ")
        mock_sm.get_process_descriptor.return_value = SimpleNamespace(runtime_kind="codex")
        mock_sm.resolve_thread_for_window = AsyncMock(return_value=None)

        await update_status_message(mock_bot, user_id=1, window_id="@5", thread_id=42)

    mock_content.assert_awaited_once()
    kwargs = mock_content.await_args.kwargs
    assert kwargs["semantic_kind"] == "warning"
    assert kwargs["warning_key"] == "runtime-discontinuity:exit:@5"
    assert kwargs["image_data"] == [("discontinuity-screenshot.png", b"png-bytes")]
    mock_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_status_message_does_not_emit_exit_warning_for_active_codex_footer_without_banner(
    mock_bot: AsyncMock,
):
    status_polling_mod._runtime_presence[(1, "@5")] = True
    mock_window = MagicMock()
    mock_window.window_id = "@5"
    mock_window.pane_current_command = "node"
    pane_text = (
        "• Waited for background terminal · cd /home/tools/ComfyUI_next && ./.venv/bin/python compare.py\n\n"
        "• Face-model pack уже скачался; теперь сам расчёт similarity по embeddings должен завершиться быстро.\n\n"
        "› Improve documentation in @filename\n"
        "· gpt-5.4 high · 21% left · /home/tools/server/comfy\n"
        "──────────────────────────────────────\n"
        "❯ \n"
        "──────────────────────────────────────\n"
        "  23% context left\n"
    )

    with (
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.enqueue_pending_input_update",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.enqueue_status_update",
            new_callable=AsyncMock,
        ) as mock_status,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
        mock_sm.get_process_descriptor.return_value = SimpleNamespace(runtime_kind="codex")

        await update_status_message(mock_bot, user_id=1, window_id="@5", thread_id=42)

    mock_content.assert_not_awaited()
    mock_status.assert_awaited_once()
    assert mock_status.await_args.args[3] == "gpt-5.4 high · 21% left · /home/tools/server/comfy"


@pytest.mark.asyncio
async def test_update_status_message_does_not_emit_exit_warning_for_unknown_node_codex_footer(
    mock_bot: AsyncMock,
):
    status_polling_mod._runtime_presence[(1, "@5")] = True
    mock_window = MagicMock()
    mock_window.window_id = "@5"
    mock_window.pane_current_command = "node"
    pane_text = (
        "• Waiting for background terminal (\n"
        "\n"
        "… +23 lines (ctrl + t to view transcript)\n"
        "\n"
        "73                     2\n"
        "tab to queue message                                    37% context left\n"
    )

    with (
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.enqueue_pending_input_update",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.enqueue_status_update",
            new_callable=AsyncMock,
        ) as mock_status,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
        mock_sm.get_process_descriptor.return_value = SimpleNamespace(runtime_kind="codex")

        await update_status_message(mock_bot, user_id=1, window_id="@5", thread_id=42)

    mock_content.assert_not_awaited()
    mock_status.assert_not_awaited()

@pytest.mark.asyncio
async def test_update_status_message_usage_limit_banner_enqueues_durable_warning(
    mock_bot: AsyncMock,
):
    mock_window = MagicMock()
    mock_window.window_id = "@5"
    mock_window.pane_current_command = "codex"
    pane_text = (
        "Running UserPromptSubmit hook: Applying OMX prompt routing\n\n"
        "UserPromptSubmit hook (completed)\n\n"
        "■ You've hit your usage limit. Upgrade to Pro, visit usage to purchase more credits or try again at Apr 11th, 2026 10:11 PM.\n\n"
        "› Write tests for @filename\n"
    )

    with (
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.enqueue_pending_input_update",
            new_callable=AsyncMock,
        ),
        patch(
            "ccbot.handlers.status_polling.handle_interactive_ui",
            new_callable=AsyncMock,
        ) as mock_ui,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
        mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
        mock_sm.get_process_descriptor.return_value = SimpleNamespace(runtime_kind="codex")
        mock_sm.is_external_binding_window_id.return_value = False

        await update_status_message(mock_bot, user_id=1, window_id="@5", thread_id=42)

    mock_ui.assert_not_awaited()
    mock_content.assert_awaited_once()
    kwargs = mock_content.await_args.kwargs
    assert kwargs["semantic_kind"] == "warning"
    assert kwargs["warning_key"] == "usage-limit:@5"
    assert "You've hit your usage limit" in kwargs["text"]


@pytest.mark.asyncio
async def test_transition_missing_window_binding_chat_surface_rebinds_same_chat_to_external(
    mock_bot: AsyncMock,
):
    with (
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.clear_topic_state",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.build_discontinuity_image_data",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        mock_sm.resolve_thread_for_window = AsyncMock(
            return_value=SimpleNamespace(
                thread_id="thread-chat-1",
                runtime_kind="codex",
                summary="Recovered chat thread",
                cwd="/tmp/project",
                file_path=__file__,
            )
        )
        mock_sm.bind_external_surface.return_value = "external:codex:thread-chat-1"

        await status_polling_mod._transition_missing_window_binding(
            mock_bot,
            user_id=1,
            thread_id=None,
            window_id="@14",
            surface_key="c:-5081683643",
            chat_id=-5081683643,
        )

    mock_sm.bind_external_surface.assert_called_once()
    kwargs = mock_sm.bind_external_surface.call_args.kwargs
    assert kwargs["surface_key"] == "c:-5081683643"
    assert kwargs["chat_id"] == -5081683643
    mock_clear.assert_awaited_once_with(1, None, mock_bot)
    mock_content.assert_awaited_once()
    assert mock_content.await_args.kwargs["window_id"] == "external:codex:thread-chat-1"


@pytest.mark.asyncio
async def test_transition_missing_window_binding_rebinds_external_when_replay_survives(
    mock_bot: AsyncMock,
):
    with (
        patch("ccbot.handlers.status_polling.session_manager") as mock_sm,
        patch(
            "ccbot.handlers.status_polling.clear_topic_state",
            new_callable=AsyncMock,
        ) as mock_clear,
        patch(
            "ccbot.handlers.status_polling.enqueue_content_message",
            new_callable=AsyncMock,
        ) as mock_content,
        patch(
            "ccbot.handlers.status_polling.build_discontinuity_image_data",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        mock_sm.resolve_thread_for_window = AsyncMock(
            return_value=SimpleNamespace(
                thread_id="thread-1",
                runtime_kind="codex",
                summary="Recovered thread",
                cwd="/tmp/project",
                file_path=__file__,
            )
        )
        mock_sm.bind_external_surface.return_value = "external:codex:thread-1"

        await status_polling_mod._transition_missing_window_binding(
            mock_bot,
            user_id=1,
            thread_id=42,
            window_id="@7",
        )

    mock_sm.bind_external_surface.assert_called_once()
    mock_clear.assert_awaited_once_with(1, 42, mock_bot)
    mock_content.assert_awaited_once()
    kwargs = mock_content.await_args.kwargs
    assert kwargs["window_id"] == "external:codex:thread-1"
    assert "read-only mode" in kwargs["text"] or "persisted replay evidence" in kwargs["text"]
