"""Read-only binding/workspace preflight for ccbot runtime targets.

This command intentionally performs no Telegram delivery and no runtime input
injection.  It validates persisted binding facts before operators or external
automation trust a target for follow-up side effects.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class BindingPreflightCliError(ValueError):
    """Raised when preflight arguments are malformed."""


_BLOCKING_TARGET_REASONS = {
    "helper_binding",
    "inactive_binding",
    "missing_runtime_metadata",
}


@dataclass(frozen=True)
class RuntimeBindingFacts:
    """Resolved safe-to-log runtime binding facts."""

    user_id: int | None = None
    surface_key: str | None = None
    window_id: str = ""
    window_name: str = ""
    cwd: str = ""
    normalized_cwd: str = ""
    runtime_kind: str = ""
    target_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeBindingExpected:
    """Expected safe-to-log runtime binding facts."""

    user_id: int | None = None
    surface_key: str | None = None
    window_id: str = ""
    window_name: str = ""
    cwd: str = ""
    normalized_cwd: str = ""
    runtime_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeBindingPreflightResult:
    """Read-only binding preflight result."""

    ok: bool
    classification: str
    resolved: RuntimeBindingFacts | None = None
    expected: RuntimeBindingExpected | None = None
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "classification": self.classification,
            "remediation": self.remediation,
        }
        if self.resolved is not None:
            data["resolved"] = self.resolved.to_dict()
        if self.expected is not None:
            data["expected"] = self.expected.to_dict()
        return data


def _env_default(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value
    return None


def _normalize_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    try:
        return str(Path(cwd).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        try:
            return str(Path(cwd).expanduser())
        except (OSError, RuntimeError, ValueError):
            return cwd


def _build_parser(prog: str = "ccbot binding-preflight") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Read-only ccbot binding/workspace preflight. Validates a persisted "
            "runtime binding target before any Telegram delivery or runtime input."
        ),
        epilog=(
            "This command never calls send_to_window and never sends Telegram "
            "messages. Use it as a safe gate before `ccbot runtime-input` or "
            "workspace-sensitive artifact delivery."
        ),
    )
    parser.add_argument(
        "--ccbot-dir",
        default=_env_default("CCBOT_DIR"),
        help="ccbot instance directory; defaults to CCBOT_DIR",
    )
    parser.add_argument("--window-id", help="Explicit tmux window id, e.g. @1")
    parser.add_argument("--user-id", help="Telegram user id selector")
    parser.add_argument("--surface-key", help="Control-surface key selector, e.g. t:555")
    parser.add_argument("--thread-id", help="Telegram forum topic id selector")
    parser.add_argument("--chat-id", help="Telegram no-topics chat id selector")
    parser.add_argument(
        "--expected-user-id",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_USER_ID"),
        help="Expected Telegram user id",
    )
    parser.add_argument(
        "--expected-surface-key",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_SURFACE_KEY"),
        help="Expected control-surface key, e.g. t:555",
    )
    parser.add_argument(
        "--expected-thread-id",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_THREAD_ID"),
        help="Expected Telegram forum topic id",
    )
    parser.add_argument(
        "--expected-chat-id",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_CHAT_ID"),
        help="Expected Telegram no-topics chat id",
    )
    parser.add_argument(
        "--expected-window-id",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_WINDOW_ID"),
        help="Expected tmux window id, e.g. @1",
    )
    parser.add_argument(
        "--expected-window-name",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_WINDOW_NAME"),
        help="Expected tmux window display name",
    )
    parser.add_argument(
        "--expected-runtime-kind",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_RUNTIME_KIND"),
        help="Expected runtime kind, e.g. codex",
    )
    parser.add_argument(
        "--expected-cwd",
        default=_env_default("CCBOT_PREFLIGHT_EXPECTED_CWD"),
        help="Expected runtime cwd",
    )
    parser.add_argument("--json", action="store_true", help="Print result JSON")
    return parser


def _parse_int_arg(name: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise BindingPreflightCliError(f"{name} must be an integer") from exc


def _resolve_surface_key(
    manager: Any,
    *,
    surface_key: str | None = None,
    thread_id: int | None = None,
    chat_id: int | None = None,
    label: str = "surface",
) -> str | None:
    flag_prefix = f"{label}-" if label else ""
    error_label = f"{label} " if label else ""
    if surface_key:
        if thread_id is not None or chat_id is not None:
            raise BindingPreflightCliError(
                f"Use either --{flag_prefix}surface-key or --{flag_prefix}thread-id/"
                f"--{flag_prefix}chat-id, not both"
            )
        normalized = manager._normalize_surface_key(str(surface_key).strip())
        if normalized is None:
            raise BindingPreflightCliError(f"invalid {error_label}surface key")
        return normalized
    if thread_id is not None and chat_id is not None:
        raise BindingPreflightCliError(
            f"Use either --{flag_prefix}thread-id or --{flag_prefix}chat-id, not both"
        )
    if thread_id is not None:
        return manager.make_surface_key(thread_id=thread_id)
    if chat_id is not None:
        return manager.make_surface_key(chat_id=chat_id)
    return None


def _window_name(manager: Any, window_id: str, descriptor: Any | None) -> str:
    descriptor_name = str(getattr(descriptor, "window_name", "") or "").strip()
    if descriptor_name:
        return descriptor_name
    display_name = str(manager.get_display_name(window_id) or "").strip()
    if display_name and display_name != window_id:
        return display_name
    return ""


def _target_blocker(manager: Any, window_id: str, descriptor: Any | None) -> str:
    helper_check = getattr(manager, "_is_codex_helper_window", None)
    if callable(helper_check) and helper_check(window_id):
        return "helper_binding"

    inactive_check = getattr(manager, "_is_inactive_or_helper_tmux_binding", None)
    if callable(inactive_check) and inactive_check(window_id):
        return "inactive_binding"

    if descriptor is None:
        return "inactive_binding"

    cwd = str(getattr(descriptor, "cwd", "") or "").strip()
    runtime_kind = str(getattr(descriptor, "runtime_kind", "") or "").strip()
    if not cwd or not runtime_kind:
        return "missing_runtime_metadata"

    return ""


def _facts_for_binding(
    manager: Any,
    *,
    user_id: int | None,
    surface_key: str | None,
    window_id: str,
    target_reason: str,
) -> RuntimeBindingFacts:
    descriptor = manager.get_process_descriptor(window_id)
    target_blocker = _target_blocker(manager, window_id, descriptor)
    cwd = str(getattr(descriptor, "cwd", "") or "").strip()
    return RuntimeBindingFacts(
        user_id=user_id,
        surface_key=surface_key,
        window_id=window_id,
        window_name=_window_name(manager, window_id, descriptor),
        cwd=cwd,
        normalized_cwd=_normalize_cwd(cwd),
        runtime_kind=str(getattr(descriptor, "runtime_kind", "") or "").strip(),
        target_reason=target_blocker or target_reason,
    )


def _canonical_binding_surface_key(
    manager: Any,
    *,
    user_id: int,
    surface_key: str,
) -> str:
    """Return a chat-qualified topic key when legacy state has chat coordinates."""
    parse_surface_key = getattr(manager, "_parse_surface_key", None)
    make_surface_key = getattr(manager, "make_surface_key", None)
    stored_topic_chat_id = getattr(manager, "_stored_topic_chat_id", None)
    if (
        not callable(parse_surface_key)
        or not callable(make_surface_key)
        or not callable(stored_topic_chat_id)
    ):
        return surface_key
    parsed = parse_surface_key(surface_key)
    if parsed is None:
        return surface_key
    kind, chat_id, thread_id = parsed
    if kind != "topic" or chat_id is not None or thread_id is None:
        return surface_key
    stored_chat_id = stored_topic_chat_id(user_id, thread_id)
    if not isinstance(stored_chat_id, int):
        return surface_key
    return make_surface_key(thread_id=thread_id, chat_id=stored_chat_id)


def _bound_candidates(
    manager: Any,
    *,
    user_id: int | None,
    surface_key: str | None,
    window_id: str | None,
) -> list[RuntimeBindingFacts]:
    candidates: list[RuntimeBindingFacts] = []
    for candidate_user_id, bindings in sorted(manager.surface_bindings.items()):
        if user_id is not None and candidate_user_id != user_id:
            continue
        for candidate_surface_key, candidate_window_id in sorted(bindings.items()):
            delivery_surface_key = _canonical_binding_surface_key(
                manager,
                user_id=candidate_user_id,
                surface_key=candidate_surface_key,
            )
            if (
                surface_key is not None
                and candidate_surface_key != surface_key
                and delivery_surface_key != surface_key
            ):
                continue
            if window_id is not None and candidate_window_id != window_id:
                continue
            candidates.append(
                _facts_for_binding(
                    manager,
                    user_id=candidate_user_id,
                    surface_key=delivery_surface_key,
                    window_id=candidate_window_id,
                    target_reason="explicit_window_id"
                    if window_id is not None
                    else "state_surface_binding",
                )
            )
    return candidates


def _explicit_window_facts(
    manager: Any,
    *,
    window_id: str,
) -> RuntimeBindingFacts | None:
    if manager.get_process_descriptor(window_id) is None:
        return None
    return _facts_for_binding(
        manager,
        user_id=None,
        surface_key=None,
        window_id=window_id,
        target_reason="explicit_window_id_unbound",
    )


def resolve_runtime_binding_facts(
    manager: Any,
    *,
    window_id: str | None = None,
    user_id: int | None = None,
    surface_key: str | None = None,
) -> RuntimeBindingPreflightResult:
    """Resolve safe persisted binding facts without mutating runtime state."""
    candidates = _bound_candidates(
        manager,
        user_id=user_id,
        surface_key=surface_key,
        window_id=window_id,
    )
    if len(candidates) == 1:
        return RuntimeBindingPreflightResult(
            ok=True,
            classification="resolved",
            resolved=candidates[0],
        )
    if len(candidates) > 1:
        return RuntimeBindingPreflightResult(
            ok=False,
            classification="ambiguous_binding",
            remediation="Pass a more specific --user-id, --surface-key, or --window-id.",
        )
    if window_id:
        if user_id is not None or surface_key is not None:
            return RuntimeBindingPreflightResult(
                ok=False,
                classification="no_binding",
                remediation=(
                    "No binding matches the requested --window-id plus "
                    "--user-id/--surface-key selectors; rebind/resume the "
                    "intended runtime surface."
                ),
            )
        facts = _explicit_window_facts(manager, window_id=window_id)
        if facts is not None:
            return RuntimeBindingPreflightResult(
                ok=True,
                classification="resolved",
                resolved=facts,
            )
    return RuntimeBindingPreflightResult(
        ok=False,
        classification="no_binding",
        remediation="Bind or resume the intended runtime surface, then rerun preflight.",
    )


def _expected_from_args(manager: Any, args: argparse.Namespace) -> RuntimeBindingExpected:
    expected_user_id = _parse_int_arg("--expected-user-id", args.expected_user_id)
    expected_thread_id = _parse_int_arg("--expected-thread-id", args.expected_thread_id)
    expected_chat_id = _parse_int_arg("--expected-chat-id", args.expected_chat_id)
    expected_surface_key = _resolve_surface_key(
        manager,
        surface_key=args.expected_surface_key,
        thread_id=expected_thread_id,
        chat_id=expected_chat_id,
        label="expected",
    )
    expected_cwd = str(args.expected_cwd or "").strip()
    return RuntimeBindingExpected(
        user_id=expected_user_id,
        surface_key=expected_surface_key,
        window_id=str(args.expected_window_id or "").strip(),
        window_name=str(args.expected_window_name or "").strip(),
        cwd=expected_cwd,
        normalized_cwd=_normalize_cwd(expected_cwd),
        runtime_kind=str(args.expected_runtime_kind or "").strip(),
    )


def _remediation(classification: str, expected: RuntimeBindingExpected) -> str:
    canonical_hint = ""
    if expected.cwd:
        canonical_hint = f" expected cwd {expected.cwd}"
    if expected.surface_key:
        canonical_hint += f" expected surface {expected.surface_key}"
    if expected.window_name:
        canonical_hint += f" expected window {expected.window_name}"
    if expected.runtime_kind:
        canonical_hint += f" expected runtime {expected.runtime_kind}"
    suffix = canonical_hint.strip()
    if suffix:
        suffix = f" ({suffix})"
    return (
        f"Binding preflight failed with {classification}; rebind/resume the "
        f"intended runtime surface or fix service restore configuration{suffix}."
    )


def validate_runtime_binding_facts(
    facts: RuntimeBindingFacts,
    expected: RuntimeBindingExpected,
) -> RuntimeBindingPreflightResult:
    """Validate resolved facts against expected safe metadata."""
    if facts.target_reason in _BLOCKING_TARGET_REASONS:
        return RuntimeBindingPreflightResult(
            ok=False,
            classification=facts.target_reason,
            resolved=facts,
            expected=expected,
            remediation=_remediation(facts.target_reason, expected),
        )

    checks: tuple[tuple[str, bool], ...] = (
        (
            "user_mismatch",
            expected.user_id is None or facts.user_id == expected.user_id,
        ),
        (
            "surface_mismatch",
            expected.surface_key is None or facts.surface_key == expected.surface_key,
        ),
        (
            "window_mismatch",
            not expected.window_id or facts.window_id == expected.window_id,
        ),
        (
            "window_name_mismatch",
            not expected.window_name or facts.window_name == expected.window_name,
        ),
        (
            "runtime_kind_mismatch",
            not expected.runtime_kind or facts.runtime_kind == expected.runtime_kind,
        ),
        (
            "cwd_mismatch",
            not expected.normalized_cwd
            or facts.normalized_cwd == expected.normalized_cwd,
        ),
    )
    for classification, passed in checks:
        if not passed:
            return RuntimeBindingPreflightResult(
                ok=False,
                classification=classification,
                resolved=facts,
                expected=expected,
                remediation=_remediation(classification, expected),
            )
    return RuntimeBindingPreflightResult(
        ok=True,
        classification="ok",
        resolved=facts,
        expected=expected,
        remediation="Binding preflight passed.",
    )


def run_binding_preflight(
    manager: Any,
    args: argparse.Namespace,
) -> RuntimeBindingPreflightResult:
    parsed_user_id = _parse_int_arg("--user-id", args.user_id)
    parsed_thread_id = _parse_int_arg("--thread-id", args.thread_id)
    parsed_chat_id = _parse_int_arg("--chat-id", args.chat_id)
    surface_key = _resolve_surface_key(
        manager,
        surface_key=args.surface_key,
        thread_id=parsed_thread_id,
        chat_id=parsed_chat_id,
        label="",
    )
    expected = _expected_from_args(manager, args)
    resolved = resolve_runtime_binding_facts(
        manager,
        window_id=str(args.window_id or "").strip() or None,
        user_id=parsed_user_id,
        surface_key=surface_key,
    )
    if resolved.resolved is None:
        return RuntimeBindingPreflightResult(
            ok=False,
            classification=resolved.classification,
            expected=expected,
            remediation=resolved.remediation,
        )
    return validate_runtime_binding_facts(resolved.resolved, expected)


def _validate_parse_only_args(args: argparse.Namespace) -> None:
    parsed_thread_id = _parse_int_arg("--thread-id", args.thread_id)
    parsed_chat_id = _parse_int_arg("--chat-id", args.chat_id)
    _parse_int_arg("--user-id", args.user_id)
    expected_thread_id = _parse_int_arg("--expected-thread-id", args.expected_thread_id)
    expected_chat_id = _parse_int_arg("--expected-chat-id", args.expected_chat_id)
    _parse_int_arg("--expected-user-id", args.expected_user_id)

    if args.surface_key and (parsed_thread_id is not None or parsed_chat_id is not None):
        raise BindingPreflightCliError(
            "Use either --surface-key or --thread-id/--chat-id, not both"
        )
    if parsed_thread_id is not None and parsed_chat_id is not None:
        raise BindingPreflightCliError("Use either --thread-id or --chat-id, not both")
    if args.expected_surface_key and (
        expected_thread_id is not None or expected_chat_id is not None
    ):
        raise BindingPreflightCliError(
            "Use either --expected-surface-key or --expected-thread-id/"
            "--expected-chat-id, not both"
        )
    if expected_thread_id is not None and expected_chat_id is not None:
        raise BindingPreflightCliError(
            "Use either --expected-thread-id or --expected-chat-id, not both"
        )


def binding_preflight_main(
    argv: list[str] | None = None,
    *,
    prog: str = "ccbot binding-preflight",
) -> int:
    parser = _build_parser(prog=prog)
    args = parser.parse_args(argv)
    if args.ccbot_dir:
        os.environ["CCBOT_DIR"] = str(args.ccbot_dir)

    try:
        _validate_parse_only_args(args)
        from .session import session_manager

        result = run_binding_preflight(session_manager, args)
    except BindingPreflightCliError as exc:
        parser.exit(2, f"Error: {exc}\n")

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif result.ok:
        print("Binding preflight passed")
    else:
        print(result.remediation or result.classification)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(binding_preflight_main())
