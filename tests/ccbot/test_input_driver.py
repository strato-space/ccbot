"""Tests for the runtime input driver."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ccbot.input_driver import RuntimeInputDriver


@pytest.fixture
def tmux_stub():
    tmux = MagicMock()
    tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@1"))
    tmux.send_literal_text = AsyncMock(return_value=True)
    tmux.send_enter = AsyncMock(return_value=True)
    tmux.send_key = AsyncMock(return_value=True)
    return tmux


class TestRuntimeInputDriver:
    @pytest.mark.asyncio
    async def test_codex_submit_text_adds_enter_after_delay(self, tmux_stub):
        driver = RuntimeInputDriver(tmux=tmux_stub, submit_delay=0.25)

        with patch("ccbot.input_driver.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            ok, message = await driver.send_text(
                "@1", "hello", runtime_kind="codex", submit=True
            )

        assert ok is True
        assert message == "Sent text to @1"
        tmux_stub.send_literal_text.assert_awaited_once_with("@1", "hello")
        tmux_stub.send_enter.assert_awaited_once_with("@1")
        mock_sleep.assert_awaited_once_with(0.25)

    @pytest.mark.asyncio
    async def test_codex_shell_command_splits_prefix_and_body(self, tmux_stub):
        driver = RuntimeInputDriver(
            tmux=tmux_stub,
            submit_delay=0.25,
            shell_transition_delay=0.8,
        )

        with patch("ccbot.input_driver.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            ok, message = await driver.send_text(
                "@1", "!ls -la", runtime_kind="codex", submit=True
            )

        assert ok is True
        assert message == "Sent text to @1"
        assert tmux_stub.send_literal_text.await_args_list == [
            call("@1", "!"),
            call("@1", "ls -la"),
        ]
        tmux_stub.send_enter.assert_awaited_once_with("@1")
        assert mock_sleep.await_args_list == [call(0.8), call(0.25)]

    @pytest.mark.asyncio
    async def test_multiline_text_is_pasted_literally(self, tmux_stub):
        driver = RuntimeInputDriver(tmux=tmux_stub, submit_delay=0.25)

        with patch("ccbot.input_driver.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            ok, message = await driver.send_text(
                "@1", "line1\nline2", runtime_kind="codex", submit=True
            )

        assert ok is True
        assert message == "Sent text to @1"
        tmux_stub.send_literal_text.assert_awaited_once_with("@1", "line1\nline2")
        tmux_stub.send_enter.assert_awaited_once_with("@1")
        mock_sleep.assert_awaited_once_with(0.25)

    @pytest.mark.asyncio
    async def test_special_key_dispatch_uses_tmux_key_names(self, tmux_stub):
        driver = RuntimeInputDriver(tmux=tmux_stub)

        ok, message = await driver.send_special_key(
            "@1", "esc", runtime_kind="codex"
        )

        assert ok is True
        assert message == "Sent Escape"
        tmux_stub.send_key.assert_awaited_once_with("@1", "Escape")
        tmux_stub.send_enter.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsupported_special_key_is_rejected(self, tmux_stub):
        driver = RuntimeInputDriver(tmux=tmux_stub)

        ok, message = await driver.send_special_key(
            "@1", "F13", runtime_kind="codex"
        )

        assert ok is False
        assert message == "Unsupported control key: F13"
        tmux_stub.send_key.assert_not_awaited()
        tmux_stub.send_enter.assert_not_awaited()
