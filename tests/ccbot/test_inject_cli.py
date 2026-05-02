import builtins
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_send_help_keeps_telegram_delivery_alias(monkeypatch, capsys) -> None:
    import ccbot.main as main_module

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "ccbot.bot":
            raise AssertionError("ccbot send --help should not start the bot")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(sys, "argv", ["ccbot", "send", "--help"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "ccbot send" in out
    assert "Telegram chat" in out
    assert "--target" not in out


def test_inject_help_does_not_start_bot(monkeypatch, capsys) -> None:
    import ccbot.main as main_module

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "ccbot.bot":
            raise AssertionError("ccbot inject --help should not start the bot")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(sys, "argv", ["ccbot", "inject", "--help"])

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "ccbot inject" in out
    assert "--target" in out


def test_inject_requires_exactly_one_payload_source(capsys) -> None:
    from ccbot.inject_cli import inject_main

    code = inject_main(["--window", "@1"])

    assert code != 0
    assert "one of --text or --stdin is required" in capsys.readouterr().err


def test_inject_window_text_uses_session_manager(monkeypatch, capsys) -> None:
    from ccbot import inject_cli

    manager = SimpleNamespace(
        send_to_window=AsyncMock(return_value=(True, "Sent to @1")),
    )
    monkeypatch.setattr(inject_cli, "SessionManager", lambda: manager)

    code = inject_cli.inject_main(["--window", "@1", "--text", "hello"])

    assert code == 0
    assert "ok: Sent to @1" in capsys.readouterr().out
    manager.send_to_window.assert_awaited_once_with("@1", "hello")


def test_inject_window_stdin_uses_session_manager(monkeypatch) -> None:
    from ccbot import inject_cli

    manager = SimpleNamespace(
        send_to_window=AsyncMock(return_value=(True, "Sent to @1")),
    )
    monkeypatch.setattr(inject_cli, "SessionManager", lambda: manager)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: "multi\nline"))

    code = inject_cli.inject_main(["--window", "@1", "--stdin"])

    assert code == 0
    manager.send_to_window.assert_awaited_once_with("@1", "multi\nline")


def test_inject_window_busy_failure_returns_nonzero(monkeypatch, capsys) -> None:
    from ccbot import inject_cli

    manager = SimpleNamespace(
        send_to_window=AsyncMock(return_value=(False, "Codex is still working")),
    )
    monkeypatch.setattr(inject_cli, "SessionManager", lambda: manager)

    code = inject_cli.inject_main(["--window", "@1", "--text", "hello"])

    assert code == inject_cli.EXIT_UNSAFE_TO_SEND
    assert "Codex is still working" in capsys.readouterr().err


def test_codex_multiline_ack_warning_maps_to_special_exit(monkeypatch, capsys) -> None:
    from ccbot import inject_cli

    manager = SimpleNamespace(
        send_to_window=AsyncMock(
            return_value=(
                False,
                "Codex did not persist a new turn after multiline submit; "
                "the draft may still be waiting in the terminal composer",
            )
        ),
    )
    monkeypatch.setattr(inject_cli, "SessionManager", lambda: manager)

    code = inject_cli.inject_main(["--window", "@1", "--text", "a\nb"])

    assert code == inject_cli.EXIT_ACK_UNCONFIRMED
    assert "draft may still be waiting" in capsys.readouterr().err


def test_tmux_target_spec_accepts_session_window_and_pane() -> None:
    from ccbot.inject_cli import TmuxTargetSpec

    assert TmuxTargetSpec.parse("imm_arena_bot:imm") == TmuxTargetSpec(
        raw="imm_arena_bot:imm",
        session="imm_arena_bot",
        window="imm",
        pane=None,
    )
    assert TmuxTargetSpec.parse("imm_arena_bot:imm.2") == TmuxTargetSpec(
        raw="imm_arena_bot:imm.2",
        session="imm_arena_bot",
        window="imm",
        pane="2",
    )


@pytest.mark.asyncio
async def test_arbitrary_target_codex_multiline_uses_runtime_driver_enter_path(
    monkeypatch,
) -> None:
    from ccbot import inject_cli

    calls: list[tuple[str, str, str | None]] = []

    class FakeAdapter:
        async def describe(self):
            return SimpleNamespace(window_id="imm_arena_bot:imm", pane_current_command="node")

        async def capture_pane(self, window_id: str):
            return "› ready\n"

        async def find_window_by_id(self, window_id: str):
            return SimpleNamespace(window_id=window_id)

        async def send_literal_text(self, window_id: str, text: str) -> bool:
            calls.append(("literal", window_id, text))
            return True

        async def send_pasted_text(self, window_id: str, text: str) -> bool:
            calls.append(("paste", window_id, text))
            return True

        async def send_submit_key(self, window_id: str) -> bool:
            calls.append(("submit", window_id, None))
            return True

        async def send_enter(self, window_id: str) -> bool:
            calls.append(("enter", window_id, None))
            return True

    monkeypatch.setattr(inject_cli, "TmuxTargetAdapter", lambda target: FakeAdapter())
    monkeypatch.setattr(inject_cli.asyncio, "sleep", AsyncMock())

    result = await inject_cli.send_to_tmux_target(
        "imm_arena_bot:imm",
        "multi\nline",
        runtime_kind="codex",
        require_idle=True,
    )

    assert result.success is True
    assert calls == [
        ("paste", "imm_arena_bot:imm", "multi\nline"),
        ("enter", "imm_arena_bot:imm", None),
    ]


@pytest.mark.asyncio
async def test_arbitrary_target_require_idle_fails_closed_when_busy(monkeypatch) -> None:
    from ccbot import inject_cli

    class FakeAdapter:
        async def describe(self):
            return SimpleNamespace(window_id="imm_arena_bot:imm", pane_current_command="node")

        async def capture_pane(self, window_id: str):
            return "· Working (12s • esc to interrupt)\n"

    monkeypatch.setattr(inject_cli, "TmuxTargetAdapter", lambda target: FakeAdapter())

    result = await inject_cli.send_to_tmux_target(
        "imm_arena_bot:imm",
        "hello",
        runtime_kind="codex",
        require_idle=True,
    )

    assert result.success is False
    assert result.exit_code == inject_cli.EXIT_UNSAFE_TO_SEND
    assert "busy" in result.message
