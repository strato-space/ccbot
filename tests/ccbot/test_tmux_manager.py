import pytest

from ccbot.tmux_manager import TmuxManager


class _FakePane:
    def __init__(self) -> None:
        self.commands: list[tuple[str, bool]] = []

    def send_keys(self, text: str, enter: bool = True, literal: bool = True) -> None:
        self.commands.append((text, enter))


class _FakeWindow:
    def __init__(self, pane: _FakePane) -> None:
        self.window_id = "@9"
        self.active_pane = pane
        self.options: list[tuple[str, str]] = []

    def set_window_option(self, name: str, value: str) -> None:
        self.options.append((name, value))


class _FakeSession:
    def __init__(self, window: _FakeWindow) -> None:
        self._window = window

    def new_window(self, window_name: str, start_directory: str) -> _FakeWindow:
        return self._window


@pytest.mark.asyncio
async def test_create_window_explicitly_cd_into_requested_directory(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pane = _FakePane()
    window = _FakeWindow(pane)
    session = _FakeSession(window)
    manager = TmuxManager(session_name="ccbot-test")

    async def _no_conflict(_name: str):
        return None

    monkeypatch.setattr(manager, "find_window_by_name", _no_conflict)
    monkeypatch.setattr(manager, "get_or_create_session", lambda: session)
    monkeypatch.setattr("ccbot.tmux_manager.config.claude_command", "codex")

    ok, message, window_name, window_id = await manager.create_window(
        str(workspace),
        resume_session_id="thread-123",
    )

    assert ok is True
    assert window_name == "workspace"
    assert window_id == "@9"
    assert ("allow-rename", "off") in window.options
    assert pane.commands == [
        (f"cd {workspace} && codex resume thread-123", True)
    ]


@pytest.mark.asyncio
async def test_create_window_keeps_legacy_resume_flag_for_claude(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pane = _FakePane()
    window = _FakeWindow(pane)
    session = _FakeSession(window)
    manager = TmuxManager(session_name="ccbot-test")

    async def _no_conflict(_name: str):
        return None

    monkeypatch.setattr(manager, "find_window_by_name", _no_conflict)
    monkeypatch.setattr(manager, "get_or_create_session", lambda: session)
    monkeypatch.setattr("ccbot.tmux_manager.config.claude_command", "claude")

    ok, _message, _window_name, _window_id = await manager.create_window(
        str(workspace),
        resume_session_id="session-456",
    )

    assert ok is True
    assert pane.commands == [
        (f"cd {workspace} && claude --resume session-456", True)
    ]
