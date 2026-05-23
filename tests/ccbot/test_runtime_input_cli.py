import asyncio
from types import SimpleNamespace

from ccbot import runtime_input_cli as cli
from ccbot.runtime_types import LiveProcessDescriptor
from ccbot.session import SessionManager


def _manager_without_io(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


def test_runtime_input_help_is_not_telegram_delivery_help():
    help_text = cli._build_parser().format_help()

    assert "usage: ccbot runtime-input " in help_text
    assert "Inject text into a live ccbot runtime input plane" in help_text
    assert "use `ccbot send` for Telegram output" in help_text
    assert "file-base64" not in help_text


def test_inject_alias_help_uses_alias_command_name():
    help_text = cli._build_parser(prog="ccbot inject").format_help()

    assert "usage: ccbot inject " in help_text
    assert "usage: ccbot runtime-input" not in help_text


def test_runtime_input_resolves_bound_topic_surface(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    manager.bind_surface(12345, "@7", thread_id=42, window_name="codex")
    manager.window_states["@7"] = LiveProcessDescriptor(
        thread_id="thread-1",
        runtime_kind="codex",
    )

    target = cli.resolve_runtime_input_target(
        manager,
        user_id="12345",
        thread_id="42",
    )

    assert target.window_id == "@7"
    assert target.user_id == 12345
    assert target.surface_key == "t:42"
    assert target.reason == "state_surface_binding"


def test_runtime_input_ambiguous_surface_fails_closed(monkeypatch):
    manager = _manager_without_io(monkeypatch)
    manager.bind_surface(111, "@1", thread_id=42, window_name="one")
    manager.bind_surface(222, "@2", thread_id=42, window_name="two")
    manager.window_states["@1"] = LiveProcessDescriptor(runtime_kind="codex")
    manager.window_states["@2"] = LiveProcessDescriptor(runtime_kind="codex")

    try:
        cli.resolve_runtime_input_target(manager, thread_id="42")
    except cli.RuntimeInputCliError as exc:
        assert "Cannot resolve a unique runtime input plane" in str(exc)
    else:
        raise AssertionError("ambiguous runtime input target must fail closed")


def test_runtime_input_rejects_window_id_with_surface_selector(monkeypatch):
    manager = _manager_without_io(monkeypatch)

    try:
        cli.resolve_runtime_input_target(
            manager,
            window_id="@7",
            user_id="12345",
            thread_id="42",
        )
    except cli.RuntimeInputCliError as exc:
        assert "--window-id cannot be combined" in str(exc)
    else:
        raise AssertionError("mixed explicit and state targets must fail closed")


def test_runtime_input_rejects_mixed_surface_selectors(monkeypatch):
    manager = _manager_without_io(monkeypatch)

    try:
        cli.resolve_runtime_input_target(
            manager,
            surface_key="t:42",
            thread_id="43",
        )
    except cli.RuntimeInputCliError as exc:
        assert "Use either --surface-key or --thread-id/--chat-id" in str(exc)
    else:
        raise AssertionError("mixed surface selectors must fail closed")


def test_runtime_input_execute_uses_session_manager_send_to_window_guardrails():
    calls = []

    async def _send_to_window(window_id: str, text: str):
        calls.append((window_id, text))
        return True, f"Sent to {window_id}"

    manager = SimpleNamespace(send_to_window=_send_to_window)
    target = cli.RuntimeInputTarget(
        window_id="@7",
        reason="state_surface_binding",
        user_id=12345,
        surface_key="t:42",
    )

    result = asyncio.run(
        cli._send_runtime_input(
            manager,
            target=target,
            message="hello",
        )
    )

    assert calls == [("@7", "hello")]
    assert result == {
        "status": "success",
        "message": "Sent to @7",
        "window_id": "@7",
        "target_reason": "state_surface_binding",
        "user_id": 12345,
        "surface_key": "t:42",
    }
