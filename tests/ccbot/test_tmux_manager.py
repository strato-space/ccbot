import pytest

from ccbot.tmux_manager import TmuxManager, TmuxWindow


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


@pytest.mark.asyncio
async def test_create_window_uses_registry_resume_flag_for_fast_agent(
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

    ok, _message, _window_name, _window_id = await manager.create_window(
        str(workspace),
        resume_session_id="session-789",
        runtime_kind="fast-agent",
    )

    assert ok is True
    assert pane.commands == [
        (f"cd {workspace} && fast-agent --resume session-789", True)
    ]


@pytest.mark.asyncio
async def test_create_or_reuse_window_reuses_exact_match_and_launches_codex_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = TmuxWindow(
        window_id="@7",
        window_name="workspace",
        cwd=str(workspace),
        pane_current_command="bash",
    )
    manager = TmuxManager(session_name="ccbot-test")
    sent: list[tuple[str, str]] = []

    async def _find_window_by_name(window_name: str):
        return existing if window_name == "workspace" else None

    async def _send_literal_text(window_id: str, text: str) -> bool:
        sent.append((window_id, text))
        return True

    async def _send_enter(window_id: str) -> bool:
        sent.append((window_id, "<enter>"))
        return True

    monkeypatch.setattr(manager, "find_window_by_name", _find_window_by_name)
    monkeypatch.setattr(manager, "send_literal_text", _send_literal_text)
    monkeypatch.setattr(manager, "send_enter", _send_enter)

    ok, message, window_name, window_id, reused = await manager.create_or_reuse_window(
        str(workspace),
        window_name="workspace",
        resume_session_id="thread-123",
        runtime_kind="codex",
    )

    assert ok is True
    assert reused is True
    assert window_name == "workspace"
    assert window_id == "@7"
    assert "Reused window 'workspace'" in message
    assert sent == [
        ("@7", f"cd {workspace} && codex resume thread-123"),
        ("@7", "<enter>"),
    ]


@pytest.mark.asyncio
async def test_create_or_reuse_window_fails_closed_on_runtime_or_cwd_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = TmuxManager(session_name="ccbot-test")

    async def _runtime_mismatch(window_name: str):
        return TmuxWindow(
            window_id="@8",
            window_name=window_name,
            cwd=str(workspace),
            pane_current_command="claude --resume session-456",
        )

    monkeypatch.setattr(manager, "find_window_by_name", _runtime_mismatch)

    ok, message, window_name, window_id, reused = await manager.create_or_reuse_window(
        str(workspace),
        window_name="workspace",
        resume_session_id="thread-123",
        runtime_kind="codex",
    )

    assert ok is False
    assert "running claude, not codex" in message
    assert window_name == ""
    assert window_id == ""
    assert reused is False
