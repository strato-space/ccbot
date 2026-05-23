"""Tests for terminal-surface polling helpers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers.status_polling import _build_pending_input_text, update_status_message


def test_build_pending_input_text_formats_preview_and_footer() -> None:
    pane = (
        "Queued follow-up messages\n"
        "◻ update docs\n"
        "◻ continue infra\n"
        "◻ git commit push\n"
        "◻ review changes\n"
        "shift+← edit last queued message\n"
        "──────────────────────────────\n"
        "❯\n"
    )

    text = _build_pending_input_text(pane)

    assert text is not None
    assert "Queued follow-up messages" in text
    assert "↳ update docs" in text
    assert "↳ git commit push" in text
    assert "preview 3/4 messages" in text
    assert "edit last queued message" in text


def test_build_pending_input_text_includes_all_pending_sections() -> None:
    pane = (
        "Messages to be submitted after next tool call\n"
        "• continue infra\n"
        "Messages to be submitted at end of turn\n"
        "• send report\n"
        "Queued follow-up messages\n"
        "◻ update docs\n"
        "shift+← edit last queued message\n"
        "──────────────────────────────\n"
        "❯\n"
    )

    text = _build_pending_input_text(pane)

    assert text is not None
    assert "Pending input" in text
    assert "Messages to be submitted after next tool call" in text
    assert "Messages to be submitted at end of turn" in text
    assert "Queued follow-up messages" in text
    assert "↳ continue infra" in text
    assert "↳ send report" in text
    assert "↳ update docs" in text


def test_build_pending_input_text_returns_none_for_edit_hint_without_messages() -> None:
    pane = (
        "Queued follow-up messages\n"
        "shift+← edit last queued message\n"
        "──────────────────────────────\n"
        "❯\n"
    )

    assert _build_pending_input_text(pane) is None


def test_build_pending_input_text_clips_long_edit_hint() -> None:
    pane = (
        "Queued follow-up messages\n"
        "◻ update docs\n"
        "shift+← edit last queued message "
        + ("x" * 220)
        + "\n"
        "──────────────────────────────\n"
        "❯\n"
    )

    text = _build_pending_input_text(pane)

    assert text is not None
    assert "edit last queued message" in text
    hint_line = text.splitlines()[-1]
    assert hint_line.endswith("…")
    assert len(hint_line) <= 120


@pytest.mark.asyncio
async def test_update_status_message_enqueues_pending_input_preview() -> None:
    pane = (
        "Queued follow-up messages\n"
        "◻ update docs\n"
        "shift+← edit last queued message\n"
        "──────────────────────────────\n"
        "❯\n"
    )

    with (
        patch("ccbot.handlers.status_polling.tmux_manager") as mock_tmux,
        patch("ccbot.handlers.status_polling.enqueue_pending_input_update", new_callable=AsyncMock) as mock_pending,
        patch("ccbot.handlers.status_polling.enqueue_status_update", new_callable=AsyncMock) as mock_status,
    ):
        mock_tmux.find_window_by_id = AsyncMock(return_value=SimpleNamespace(window_id="@7"))
        mock_tmux.capture_pane = AsyncMock(return_value=pane)

        await update_status_message(
            AsyncMock(),
            user_id=1,
            window_id="@7",
            thread_id=42,
            skip_status=True,
        )

    mock_pending.assert_awaited_once()
    pending_text = mock_pending.await_args.args[3]
    assert pending_text is not None
    assert "Queued follow-up messages" in pending_text
    mock_status.assert_not_awaited()
