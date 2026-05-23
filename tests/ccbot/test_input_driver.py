from types import SimpleNamespace

import pytest

import ccbot.input_driver as input_driver_module
from ccbot.input_driver import RuntimeInputDriver


def test_runtime_input_driver_exposes_registry_helpers() -> None:
    driver = RuntimeInputDriver()

    assert driver.supports_message_routing_mode("claude", "queue")
    assert driver.supports_message_routing_mode("fast-agent", "steer")
    assert driver.supports_interactive_control("codex")
    assert driver.blocked_input_policy("fast-agent") == "fail_closed_on_visible_prompt"


def test_codex_multiline_submit_delay_has_readiness_debounce() -> None:
    driver = RuntimeInputDriver(submit_delay=0)

    assert (
        driver._submit_delay_seconds(runtime_kind="codex", multiline=True)  # noqa: SLF001
        == 0.1
    )
    assert driver._submit_delay_seconds(runtime_kind="codex", multiline=False) == 0  # noqa: SLF001
    assert driver._submit_delay_seconds(runtime_kind="claude", multiline=True) == 0  # noqa: SLF001


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

    async def send_key(self, window_id: str, key: str) -> bool:
        self.calls.append(("key", window_id, key))
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
        ("enter", "@1", None),
    ]


@pytest.mark.asyncio
async def test_multiline_codex_send_path_waits_for_composer_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(input_driver_module.asyncio, "sleep", fake_sleep)
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, _ = await driver.send_text(
        "@1",
        "line one\nline two",
        runtime_kind="codex",
    )

    assert success is True
    assert sleeps == [0.1]
    assert fake_tmux.calls == [
        ("paste", "@1", "line one\nline two"),
        ("enter", "@1", None),
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
async def test_codex_queued_text_uses_tab_instead_of_enter() -> None:
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_queued_text("@1", "ping", runtime_kind="codex")

    assert success is True
    assert message == "Queued text to @1"
    assert fake_tmux.calls == [
        ("literal", "@1", "ping"),
        ("key", "@1", "Tab"),
    ]


@pytest.mark.asyncio
async def test_multiline_codex_queued_text_uses_paste_then_tab() -> None:
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_queued_text(
        "@1",
        "line one\nline two",
        runtime_kind="codex",
    )

    assert success is True
    assert message == "Queued text to @1"
    assert fake_tmux.calls == [
        ("paste", "@1", "line one\nline two"),
        ("key", "@1", "Tab"),
    ]


@pytest.mark.asyncio
async def test_shell_command_submit_path_has_defined_multiline_state() -> None:
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(
        fake_tmux,
        submit_delay=0,
        shell_transition_delay=0,
    )

    success, message = await driver.send_text("@1", "!pwd", runtime_kind="codex")

    assert success is True
    assert message == "Sent text to @1"
    assert fake_tmux.calls == [
        ("literal", "@1", "!"),
        ("literal", "@1", "pwd"),
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
async def test_public_multiline_submit_key_uses_codex_enter_path() -> None:
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_multiline_submit_key("@1", runtime_kind="codex")

    assert success is True
    assert message == "Submitted text to @1"
    assert fake_tmux.calls == [("enter", "@1", None)]


@pytest.mark.asyncio
async def test_single_line_submit_failure_is_reported_after_successful_text() -> None:
    fake_tmux = _FakeTmux()
    fake_tmux.submit_result = False
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_text(
        "@1",
        "line one",
        runtime_kind="codex",
    )

    assert success is False
    assert message == "Failed to submit text"
    assert fake_tmux.calls == [
        ("literal", "@1", "line one"),
        ("submit", "@1", None),
    ]


@pytest.mark.asyncio
async def test_multiline_submit_failure_is_reported_after_successful_paste() -> None:
    class FailingEnterTmux(_FakeTmux):
        async def send_enter(self, window_id: str) -> bool:
            self.calls.append(("enter", window_id, None))
            return False

    fake_tmux = FailingEnterTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_text(
        "@1",
        "line one\nline two\n$ralph",
        runtime_kind="codex",
    )

    assert success is False
    assert message == "Failed to submit text"
    assert fake_tmux.calls == [
        ("paste", "@1", "line one\nline two\n$ralph"),
        ("enter", "@1", None),
    ]


@pytest.mark.asyncio
async def test_multiline_non_codex_text_keeps_submit_key_path() -> None:
    fake_tmux = _FakeTmux()
    driver = RuntimeInputDriver(fake_tmux, submit_delay=0)

    success, message = await driver.send_text(
        "@1",
        "line one\nline two",
        runtime_kind="claude",
    )

    assert success is True
    assert message == "Sent text to @1"
    assert fake_tmux.calls == [
        ("paste", "@1", "line one\nline two"),
        ("submit", "@1", None),
    ]


@pytest.mark.asyncio
async def test_codex_multiline_without_enter_fails_closed() -> None:
    class NoEnterTmux(_FakeTmux):
        send_enter = None

    fake_tmux = NoEnterTmux()
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
    ]
