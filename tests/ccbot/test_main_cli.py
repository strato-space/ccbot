import sys
from types import SimpleNamespace

import pytest

from ccbot import main as main_mod


def test_top_level_help_does_not_start_bot(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "ccbot.bot", SimpleNamespace())

    with pytest.raises(SystemExit) as exc:
        main_mod.main(["--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage: ccbot" in output
    assert "send" in output
    assert "runtime-input" in output
    assert "binding-preflight" in output


def test_short_top_level_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["-h"])

    assert exc.value.code == 0
    assert "usage: ccbot" in capsys.readouterr().out


def test_no_args_starts_bot_with_configured_poll_timeout(monkeypatch):
    calls = []

    fake_config_mod = SimpleNamespace(
        config=SimpleNamespace(
            allowed_users={1},
            claude_projects_path="/tmp/claude-projects",
        )
    )
    class _FakeApplication:
        def run_polling(self, **kwargs):
            calls.append(kwargs)

    fake_bot_mod = SimpleNamespace(
        create_bot=lambda: _FakeApplication(),
        telegram_bootstrap_retries=lambda: -1,
        telegram_poll_timeout=lambda: 17,
    )
    monkeypatch.setitem(sys.modules, "ccbot.config", fake_config_mod)
    monkeypatch.setitem(sys.modules, "ccbot.bot", fake_bot_mod)

    main_mod.main([])

    assert calls == [
        {
            "allowed_updates": ["message", "callback_query"],
            "bootstrap_retries": -1,
            "timeout": 17,
        }
    ]


def test_send_help_keeps_delivery_alias(capsys):
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["send", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage: ccbot send " in output
    assert "--file-path" in output
    assert "--file-type" in output


def test_runtime_input_help_stays_separate_from_delivery(capsys):
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["runtime-input", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage: ccbot runtime-input " in output
    assert "use `ccbot send` for Telegram output" in output
    assert "--file-path" not in output


def test_inject_help_uses_alias_name(capsys):
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["inject", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage: ccbot inject " in output
    assert "usage: ccbot runtime-input" not in output


def test_binding_preflight_help_is_read_only(capsys):
    with pytest.raises(SystemExit) as exc:
        main_mod.main(["binding-preflight", "--help"])

    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage: ccbot binding-preflight " in output
    assert "Read-only ccbot binding/workspace preflight" in output
    assert "--expected-cwd" in output
