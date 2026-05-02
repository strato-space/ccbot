"""CLI for ontology-aware runtime input injection.

This command is intentionally separate from ``ccbot send``:

- ``ccbot send`` delivers text/files back to Telegram.
- ``ccbot runtime-input`` injects text into a live tmux-backed runtime input
  plane, using the same SessionManager/RuntimeInputDriver guardrails as
  Telegram-originated input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class RuntimeInputCliError(ValueError):
    """Raised when a runtime-input target or payload is not safely resolvable."""


@dataclass(frozen=True)
class RuntimeInputTarget:
    """Resolved runtime input target."""

    window_id: str
    reason: str
    user_id: int | None = None
    surface_key: str | None = None


def _env_default(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value
    return None


def _build_parser(prog: str = "ccbot runtime-input") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Inject text into a live ccbot runtime input plane. This is not "
            "Telegram result delivery; use `ccbot send` for Telegram output."
        ),
        epilog=(
            "Targeting is fail-closed. Pass --window-id for an explicit tmux "
            "window or resolve through persisted control-surface state with "
            "--user-id plus --surface-key/--thread-id/--chat-id. Multiline "
            "Codex input uses the existing paste-buffer + bare-Enter ACK path."
        ),
    )
    parser.add_argument("message", nargs="?", help="Text to inject into the runtime")
    parser.add_argument("--message", dest="message_flag", help="Text to inject")
    parser.add_argument("--message-file", help="Read runtime input text from a file")
    parser.add_argument(
        "--ccbot-dir",
        default=_env_default("CCBOT_DIR"),
        help="ccbot instance directory; defaults to CCBOT_DIR",
    )
    parser.add_argument(
        "--window-id",
        default=_env_default("CCBOT_RUNTIME_WINDOW_ID"),
        help="Explicit tmux window id, e.g. @1",
    )
    parser.add_argument(
        "--user-id",
        default=_env_default("CCBOT_RUNTIME_USER_ID"),
        help="Telegram user id owning the persisted control surface",
    )
    parser.add_argument(
        "--surface-key",
        default=_env_default("CCBOT_RUNTIME_SURFACE_KEY"),
        help="Persisted control-surface key, e.g. t:42 or c:-100123",
    )
    parser.add_argument(
        "--thread-id",
        "--message-thread-id",
        dest="message_thread_id",
        default=_env_default("CCBOT_RUNTIME_THREAD_ID", "TELEGRAM_THREAD_ID"),
        help="Telegram forum topic id to resolve as a t:<id> control surface",
    )
    parser.add_argument(
        "--chat-id",
        default=_env_default("CCBOT_RUNTIME_CHAT_ID", "TELEGRAM_CHAT_ID"),
        help="Telegram no-topics chat id to resolve as a c:<id> control surface",
    )
    parser.add_argument("--json", action="store_true", help="Print full result JSON")
    return parser


def _read_message_from_args(args: argparse.Namespace) -> str | None:
    if args.message_file:
        return Path(args.message_file).read_text(encoding="utf-8")
    if args.message_flag is not None:
        return args.message_flag
    if args.message is not None:
        return args.message
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data:
            return data
    return None


def _parse_int_arg(name: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeInputCliError(f"{name} must be an integer") from exc


def _resolve_surface_key(
    manager: Any,
    *,
    surface_key: str | None,
    thread_id: int | None,
    chat_id: int | None,
) -> str | None:
    if surface_key:
        if thread_id is not None or chat_id is not None:
            raise RuntimeInputCliError(
                "Use either --surface-key or --thread-id/--chat-id, not both"
            )
        raw = str(surface_key).strip()
        normalized = manager._normalize_surface_key(raw)
        if normalized is None:
            raise RuntimeInputCliError(f"invalid surface key: {surface_key!r}")
        return normalized
    if thread_id is not None and chat_id is not None:
        raise RuntimeInputCliError("Use either --thread-id or --chat-id, not both")
    if thread_id is not None:
        return manager.make_surface_key(thread_id=thread_id)
    if chat_id is not None:
        return manager.make_surface_key(chat_id=chat_id)
    return None


def _iter_window_candidates(
    manager: Any,
    *,
    user_id: int | None,
    surface_key: str | None,
) -> list[RuntimeInputTarget]:
    candidates: list[RuntimeInputTarget] = []
    for candidate_user_id, bindings in sorted(manager.surface_bindings.items()):
        if user_id is not None and candidate_user_id != user_id:
            continue
        for candidate_surface_key, _bound_window_id in sorted(bindings.items()):
            if surface_key is not None and candidate_surface_key != surface_key:
                continue
            window_id = manager.resolve_window_for_surface(
                candidate_user_id,
                surface_key=candidate_surface_key,
            )
            if not window_id:
                continue
            candidates.append(
                RuntimeInputTarget(
                    window_id=window_id,
                    reason="state_surface_binding",
                    user_id=candidate_user_id,
                    surface_key=candidate_surface_key,
                )
            )
    return candidates


def resolve_runtime_input_target(
    manager: Any,
    *,
    window_id: str | None = None,
    user_id: str | int | None = None,
    surface_key: str | None = None,
    thread_id: str | int | None = None,
    chat_id: str | int | None = None,
) -> RuntimeInputTarget:
    """Resolve a runtime input target from explicit or persisted state selectors."""
    parsed_user_id = _parse_int_arg("--user-id", user_id)
    parsed_thread_id = _parse_int_arg("--thread-id", thread_id)
    parsed_chat_id = _parse_int_arg("--chat-id", chat_id)

    resolved_surface_key = _resolve_surface_key(
        manager,
        surface_key=surface_key,
        thread_id=parsed_thread_id,
        chat_id=parsed_chat_id,
    )

    if window_id:
        if parsed_user_id is not None or resolved_surface_key is not None:
            raise RuntimeInputCliError(
                "--window-id cannot be combined with persisted surface selectors"
            )
        return RuntimeInputTarget(
            window_id=str(window_id),
            reason="explicit_window_id",
        )

    candidates = _iter_window_candidates(
        manager,
        user_id=parsed_user_id,
        surface_key=resolved_surface_key,
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        hint = (
            "pass --window-id or a bound --user-id/--surface-key/--thread-id/--chat-id"
        )
        raise RuntimeInputCliError(f"Cannot resolve a live runtime input plane; {hint}")
    raise RuntimeInputCliError(
        "Cannot resolve a unique runtime input plane; pass --window-id or a more "
        "specific --user-id/--surface-key"
    )


async def _send_runtime_input(
    manager: Any,
    *,
    target: RuntimeInputTarget,
    message: str,
) -> dict[str, Any]:
    success, detail = await manager.send_to_window(target.window_id, message)
    result: dict[str, Any] = {
        "status": "success" if success else "error",
        "message": detail,
        "window_id": target.window_id,
        "target_reason": target.reason,
    }
    if target.user_id is not None:
        result["user_id"] = target.user_id
    if target.surface_key is not None:
        result["surface_key"] = target.surface_key
    return result


def runtime_input_main(
    argv: list[str] | None = None,
    *,
    prog: str = "ccbot runtime-input",
) -> int:
    parser = _build_parser(prog=prog)
    args = parser.parse_args(argv)
    if args.ccbot_dir:
        os.environ["CCBOT_DIR"] = str(args.ccbot_dir)

    try:
        message = _read_message_from_args(args)
    except OSError as exc:
        parser.exit(1, f"Error reading message file: {exc}\n")
    if message is None:
        parser.exit(2, "Message text, --message-file, or stdin is required\n")

    try:
        # Import after parsing so --help works without TELEGRAM_* env.
        from .session import session_manager

        target = resolve_runtime_input_target(
            session_manager,
            window_id=args.window_id,
            user_id=args.user_id,
            surface_key=args.surface_key,
            thread_id=args.message_thread_id,
            chat_id=args.chat_id,
        )
        result = asyncio.run(
            _send_runtime_input(
                session_manager,
                target=target,
                message=message,
            )
        )
    except ValueError as exc:
        parser.exit(1, f"Error: {exc}\n")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif result.get("status") == "success":
        print(result.get("message") or "Runtime input sent")
    else:
        print(result.get("message") or "Runtime input failed", file=sys.stderr)

    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(runtime_input_main())
