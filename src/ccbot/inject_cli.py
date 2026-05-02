"""CLI entrypoint for supervisor-safe runtime input injection."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from .input_driver import RuntimeInputDriver
from .runtime_types import runtime_capability_registry
from .session import (
    BLOCKED_PROMPT_SEND_MESSAGE,
    CODEX_RUNTIME_NOT_ACTIVE_MESSAGE,
    SessionManager,
    _codex_has_live_input_plane,
)
from .state_schema import normalize_runtime_kind
from .terminal_parser import classify_input_surface
from .tmux_manager import TmuxManager, TmuxWindow, tmux_manager

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TARGET_NOT_FOUND = 2
EXIT_UNSAFE_TO_SEND = 3
EXIT_ACK_UNCONFIRMED = 4

_ACK_UNCONFIRMED_MARKERS = (
    "did not persist a new turn",
    "draft may still be waiting in the terminal composer",
)
_UNSAFE_MARKERS = (
    "blocked",
    "busy",
    "still working",
    "not active",
    "read-only",
    "requires an idle/input-ready pane",
)


@dataclass(frozen=True)
class SendResult:
    success: bool
    message: str
    exit_code: int = EXIT_OK
    warning: str = ""


@dataclass(frozen=True)
class TmuxTargetSpec:
    """Small parser for supervisor-friendly tmux target strings."""

    raw: str
    session: str
    window: str
    pane: str | None = None

    @classmethod
    def parse(cls, target: str) -> "TmuxTargetSpec":
        raw = target.strip()
        if not raw:
            raise ValueError("tmux target must not be empty")
        if raw.startswith("@"):
            raise ValueError("--target expects session:window[.pane]; use --window for @id")
        session, sep, rest = raw.partition(":")
        if not sep or not session or not rest:
            raise ValueError("--target expects session:window[.pane]")
        window, pane_sep, pane = rest.rpartition(".")
        if pane_sep and window and pane:
            return cls(raw=raw, session=session, window=window, pane=pane)
        return cls(raw=raw, session=session, window=rest, pane=None)


class TmuxTargetAdapter:
    """RuntimeInputDriver-compatible adapter for arbitrary tmux targets."""

    def __init__(self, target: str, tmux: TmuxManager | None = None) -> None:
        self.spec = TmuxTargetSpec.parse(target)
        self._tmux = tmux or tmux_manager

    @property
    def target(self) -> str:
        return self.spec.raw

    async def describe(self) -> TmuxWindow | None:
        return await self._tmux.describe_target(self.target)

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        if window_id != self.target:
            return None
        return await self.describe()

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        return await self._tmux.capture_target(window_id, with_ansi=with_ansi)

    async def send_literal_text(self, window_id: str, text: str) -> bool:
        return await self._tmux.send_literal_text_to_target(window_id, text)

    async def send_pasted_text(self, window_id: str, text: str) -> bool:
        return await self._tmux.send_pasted_text_to_target(window_id, text)

    async def send_submit_key(self, window_id: str) -> bool:
        return await self._tmux.send_submit_key_to_target(window_id)

    async def send_key(self, window_id: str, key: str) -> bool:
        return await self._tmux.send_key_to_target(window_id, key)

    async def send_enter(self, window_id: str) -> bool:
        return await self._tmux.send_enter_to_target(window_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccbot inject",
        description="Send text to a tmux-hosted ccbot runtime without starting the Telegram bot.",
    )
    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--window",
        help="Tracked ccbot tmux window id, for example @0 or @45.",
    )
    target_group.add_argument(
        "--target",
        help="Arbitrary tmux target, for example session:window or session:window.pane.",
    )
    payload_group = parser.add_mutually_exclusive_group()
    payload_group.add_argument("--text", help="Literal text to send.")
    payload_group.add_argument(
        "--stdin",
        action="store_true",
        help="Read the full payload from stdin.",
    )
    parser.add_argument(
        "--runtime",
        default=None,
        help="Runtime kind for --target injection, for example codex or claude.",
    )
    parser.add_argument(
        "--require-idle",
        action="store_true",
        help="Fail closed if the target surface is currently busy.",
    )
    return parser


def _exit_code_for_failure(message: str) -> int:
    lower = message.casefold()
    if any(marker in lower for marker in _ACK_UNCONFIRMED_MARKERS):
        return EXIT_ACK_UNCONFIRMED
    if "not found" in lower or "no such" in lower:
        return EXIT_TARGET_NOT_FOUND
    if any(marker in lower for marker in _UNSAFE_MARKERS):
        return EXIT_UNSAFE_TO_SEND
    return EXIT_ERROR


def _read_payload(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.text is None and not args.stdin:
        return None, "one of --text or --stdin is required"
    text = sys.stdin.read() if args.stdin else args.text
    if text is None or text == "":
        return None, "input text is empty"
    return text, None


async def send_to_tmux_target(
    target: str,
    text: str,
    *,
    runtime_kind: str | None = None,
    require_idle: bool = False,
) -> SendResult:
    """Send text to an arbitrary tmux target through RuntimeInputDriver."""
    try:
        adapter = TmuxTargetAdapter(target)
    except ValueError as exc:
        return SendResult(False, str(exc), EXIT_ERROR)

    window = await adapter.describe()
    if not window:
        return SendResult(False, f"tmux target not found: {target}", EXIT_TARGET_NOT_FOUND)

    normalized_runtime = normalize_runtime_kind(runtime_kind) if runtime_kind else (
        runtime_capability_registry.infer_runtime_kind_from_command(
            window.pane_current_command
        )
    )
    capability = runtime_capability_registry.get(normalized_runtime)
    pane_text = await adapter.capture_pane(window.window_id)
    surface = classify_input_surface(pane_text or "")

    lower_pane_text = (pane_text or "").casefold()
    looks_busy = "working" in lower_pane_text and "esc to interrupt" in lower_pane_text
    if require_idle and (surface.kind == "busy" or looks_busy):
        return SendResult(
            False,
            f"target is busy; refusing to send to {target}",
            EXIT_UNSAFE_TO_SEND,
        )

    if (
        capability.blocked_input_policy == "fail_closed_on_visible_prompt"
        and surface.kind == "blocked_prompt"
    ):
        return SendResult(False, BLOCKED_PROMPT_SEND_MESSAGE, EXIT_UNSAFE_TO_SEND)

    if normalized_runtime == "codex" and not _codex_has_live_input_plane(
        pane_command=window.pane_current_command,
        pane_text=pane_text,
    ):
        return SendResult(False, CODEX_RUNTIME_NOT_ACTIVE_MESSAGE, EXIT_UNSAFE_TO_SEND)

    driver = RuntimeInputDriver(adapter, submit_delay=0)
    trimmed = text.lstrip()
    if trimmed.startswith("/"):
        success, message = await driver.send_raw_slash_command(
            window.window_id,
            text,
            runtime_kind=normalized_runtime,
        )
    else:
        success, message = await driver.send_text(
            window.window_id,
            text,
            runtime_kind=normalized_runtime,
        )
    if not success:
        return SendResult(False, message, _exit_code_for_failure(message))

    warning = ""
    if normalized_runtime == "codex" and "\n" in text:
        warning = (
            "sent via tmux target without ccbot rollout ACK evidence; "
            "supervisor should monitor the runtime for repair if no turn starts"
        )
    return SendResult(True, f"Sent to {target}", EXIT_OK, warning=warning)


async def _send_async(args: argparse.Namespace) -> SendResult:
    text, error = _read_payload(args)
    if error:
        return SendResult(False, error, EXIT_ERROR)
    assert text is not None

    if args.window:
        manager = SessionManager()
        success, message = await manager.send_to_window(args.window, text)
        return SendResult(success, message, EXIT_OK if success else _exit_code_for_failure(message))

    return await send_to_tmux_target(
        args.target,
        text,
        runtime_kind=args.runtime,
        require_idle=args.require_idle,
    )


def inject_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = asyncio.run(_send_async(args))

    if result.success:
        print(f"ok: {result.message}")
        if result.warning:
            print(f"warning: {result.warning}", file=sys.stderr)
        return result.exit_code

    print(f"error: {result.message}", file=sys.stderr)
    return result.exit_code
