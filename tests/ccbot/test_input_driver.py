from types import SimpleNamespace

import pytest

from ccbot.input_driver import RuntimeInputDriver


def test_runtime_input_driver_exposes_registry_helpers() -> None:
    driver = RuntimeInputDriver()

    assert driver.supports_message_routing_mode("claude", "queue")
    assert driver.supports_message_routing_mode("fast-agent", "steer")
    assert driver.supports_interactive_control("codex")
    assert driver.blocked_input_policy("fast-agent") == "fail_closed_on_visible_prompt"


class _FakeTmux:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.paste_result = True
        self.submit_result = True

    async def find_window_by_id(self, window_id: str):
        return SimpleNamespace(window_id=window_id)

    async def send_literal_text(self, window_id: str, text: str) -> bool:
        self.calls.append(("literal", window_id, text))
        return True

    async def send_pasted_text(self, window_id: str, text: str) -> bool:
        self.calls.append(("paste", window_id, text))
        return self.paste_result

    async def send_submit_key(self, window_id: str) -> bool:
        self.calls.append(("submit", window_id, None))
        return self.submit_result

    async def send_enter(self, window_id: str) -> bool:
        self.calls.append(("enter", window_id, None))
        return True


@pytest.mark.asyncio
async def test_multiline_codex_text_uses_bracketed_paste_and_submit_key() -> None:
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)
    text = "ну а выводы то какие будут сделаны?\n\n$autopilot"

    success, message = await driver.send_text("@1", text, runtime_kind="codex")

    assert success is True
    assert message == "Sent text to @1"
    assert fake_tmux.calls == [
        ("paste", "@1", text),
        ("submit", "@1", None),
    ]


@pytest.mark.asyncio
async def test_single_line_codex_text_still_uses_literal_path() -> None:
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_text("@1", "ping", runtime_kind="codex")

    assert success is True
    assert message == "Sent text to @1"
    assert fake_tmux.calls == [
        ("literal", "@1", "ping"),
        ("submit", "@1", None),
    ]


@pytest.mark.asyncio
async def test_multiline_paste_failure_fails_before_submit() -> None:
    fake_tmux = _FakeTmux()
    fake_tmux.paste_result = False
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_text(
        "@1",
        "line one\nline two",
        runtime_kind="codex",
    )

    assert success is False
    assert message == "Failed to paste multiline text"
    assert fake_tmux.calls == [
        ("paste", "@1", "line one\nline two"),
    ]


@pytest.mark.asyncio
async def test_submit_failure_is_reported_after_successful_paste() -> None:
    fake_tmux = _FakeTmux()
    fake_tmux.submit_result = False
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_text(
        "@1",
        "line one\nline two",
        runtime_kind="codex",
    )

    assert success is False
    assert message == "Failed to submit text"
    assert fake_tmux.calls == [
        ("paste", "@1", "line one\nline two"),
        ("submit", "@1", None),
    ]
