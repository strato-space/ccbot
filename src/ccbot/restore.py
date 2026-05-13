"""Startup restore intent helpers for tmux-hosted runtime recovery.

The helpers in this module keep restore declarations separate from canonical
binding truth.  Service-local environment variables may declare an intended
restore target, but live binding state is written only after identity and
surface checks pass.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import config
from .launcher_registration import infer_runtime_kind_from_command
from .runtime_types import runtime_capability_registry

logger = logging.getLogger(__name__)

_RESTORE_PREFIX = "CCBOT_RESTORE_"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_SHELL_COMMAND_NAMES = {"bash", "dash", "fish", "sh", "zsh"}


class RestoreIntentError(ValueError):
    """Raised when a configured restore intent is malformed."""


@dataclass(frozen=True)
class RestoreIntent:
    """Service-local declaration of one intended restore target.

    This is intent, not authoritative binding state.
    """

    window_name: str
    cwd: str
    runtime_id: str
    user_id: int
    surface_key: str
    group_chat_id: int | None = None
    launcher_command: str = ""
    runtime_kind: str = "codex"
    shared_group: bool = False


@dataclass(frozen=True)
class RestoreCheckResult:
    """Result for a fail-closed restore guard."""

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class StartupRestoreResult:
    """Outcome of a startup restore attempt."""

    status: str
    message: str = ""
    window_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"skipped", "restored", "already_restored"}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in _TRUE_VALUES


def _has_restore_env(environ: Mapping[str, str]) -> bool:
    return any(key.startswith(_RESTORE_PREFIX) for key in environ)


def _required(environ: Mapping[str, str], key: str, label: str) -> str:
    value = str(environ.get(key) or "").strip()
    if not value:
        raise RestoreIntentError(f"restore intent missing {label}")
    return value


def _parse_int(value: str, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RestoreIntentError(f"restore intent has invalid {label}") from exc


def _normalize_surface_key(surface_key: str) -> str:
    raw = surface_key.strip()
    if not raw.startswith(("t:", "c:")):
        raise RestoreIntentError("restore intent has invalid surface_key")
    prefix, payload = raw.split(":", 1)
    try:
        numeric_id = int(payload)
    except ValueError as exc:
        raise RestoreIntentError("restore intent has invalid surface_key") from exc
    return f"{prefix}:{numeric_id}"


def parse_restore_intent(
    environ: Mapping[str, str] | None = None,
) -> RestoreIntent | None:
    """Parse one service-local startup restore intent from environment data."""
    source: Mapping[str, str] = os.environ if environ is None else environ
    if not _has_restore_env(source):
        return None
    if "CCBOT_RESTORE_ENABLED" in source and not _truthy(source.get("CCBOT_RESTORE_ENABLED")):
        return None

    window_name = _required(source, "CCBOT_RESTORE_WINDOW", "window_name")
    cwd = _required(source, "CCBOT_RESTORE_CWD", "cwd")
    runtime_id = _required(source, "CCBOT_RESTORE_RUNTIME_ID", "runtime_id")
    user_id = _parse_int(_required(source, "CCBOT_RESTORE_USER_ID", "user_id"), "user_id")
    surface_key = _normalize_surface_key(
        _required(source, "CCBOT_RESTORE_SURFACE_KEY", "surface_key")
    )
    shared_group = _truthy(source.get("CCBOT_RESTORE_SHARED_GROUP"))
    group_chat_raw = str(source.get("CCBOT_RESTORE_CHAT_ID") or "").strip()
    group_chat_id = _parse_int(group_chat_raw, "chat_id") if group_chat_raw else None
    if shared_group and group_chat_id is None:
        raise RestoreIntentError("restore intent missing chat_id for shared group surface")

    launcher_command = str(source.get("CCBOT_RESTORE_COMMAND") or config.ccbot_command).strip()
    runtime_kind = infer_runtime_kind_from_command(launcher_command)
    return RestoreIntent(
        window_name=window_name,
        cwd=cwd,
        runtime_id=runtime_id,
        user_id=user_id,
        surface_key=surface_key,
        group_chat_id=group_chat_id,
        launcher_command=launcher_command,
        runtime_kind=runtime_kind,
        shared_group=shared_group,
    )


def _surface_thread_id(surface_key: str) -> int | None:
    if not surface_key.startswith("t:"):
        return None
    return int(surface_key.split(":", 1)[1])


def _normalize_path(path: str) -> str:
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return str(Path(path).expanduser()) if path else ""


def _is_shell_command(command: str) -> bool:
    executable = Path(str(command or "").strip()).name.casefold()
    return executable in _SHELL_COMMAND_NAMES


def validate_existing_runtime_window_for_restore(
    session_manager: Any,
    window_id: str,
    intent: RestoreIntent,
) -> RestoreCheckResult:
    """Return whether an already-running runtime window may be reused."""
    state = getattr(session_manager, "window_states", {}).get(window_id)
    if state is None:
        return RestoreCheckResult(False, "missing runtime identity metadata")
    runtime_kind = str(getattr(state, "runtime_kind", "") or "")
    if runtime_kind != intent.runtime_kind:
        return RestoreCheckResult(False, "runtime kind mismatch")
    thread_id = str(getattr(state, "thread_id", "") or "").strip()
    if thread_id != intent.runtime_id:
        return RestoreCheckResult(False, "runtime conversation identity mismatch")
    helper_check = getattr(session_manager, "_is_codex_helper_window", None)
    if callable(helper_check) and helper_check(window_id):
        return RestoreCheckResult(False, "runtime helper window is not bindable")
    return RestoreCheckResult(True)


def bind_restored_surface(
    session_manager: Any,
    intent: RestoreIntent,
    *,
    window_id: str,
) -> None:
    """Register a restored live process and bind the configured surface."""
    if intent.group_chat_id is not None:
        session_manager.set_group_chat_id(
            intent.user_id,
            _surface_thread_id(intent.surface_key),
            intent.group_chat_id,
        )
    session_manager.register_live_process(
        window_id,
        intent.cwd,
        window_name=intent.window_name,
        runtime_kind=intent.runtime_kind,
        thread_id=intent.runtime_id,
    )
    session_manager.bind_surface(
        intent.user_id,
        window_id,
        surface_key=intent.surface_key,
        window_name=intent.window_name,
    )


async def restore_configured_startup_target(
    session_manager: Any,
    tmux_manager: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> StartupRestoreResult:
    """Restore one configured startup target, if service env declares one."""
    try:
        intent = parse_restore_intent(environ)
    except RestoreIntentError as exc:
        logger.error("Invalid startup restore intent: %s", exc)
        return StartupRestoreResult("failed", str(exc))
    if intent is None:
        return StartupRestoreResult("skipped", "no restore intent configured")

    existing = await tmux_manager.find_window_by_name(intent.window_name)
    if existing is not None:
        existing_cwd = _normalize_path(getattr(existing, "cwd", ""))
        if existing_cwd and existing_cwd != _normalize_path(intent.cwd):
            return StartupRestoreResult("failed", "target window cwd mismatch")
        active_runtime = runtime_capability_registry.known_runtime_kind_from_command(
            getattr(existing, "pane_current_command", "")
        )
        pane_command = getattr(existing, "pane_current_command", "")
        if active_runtime is not None:
            if active_runtime != intent.runtime_kind:
                return StartupRestoreResult("failed", "target window runtime kind mismatch")
            check = validate_existing_runtime_window_for_restore(
                session_manager,
                existing.window_id,
                intent,
            )
            if not check.ok:
                return StartupRestoreResult("failed", check.reason)
            bind_restored_surface(session_manager, intent, window_id=existing.window_id)
            return StartupRestoreResult(
                "already_restored",
                "existing runtime identity matched restore target",
                existing.window_id,
            )
        if not _is_shell_command(pane_command):
            check = validate_existing_runtime_window_for_restore(
                session_manager,
                existing.window_id,
                intent,
            )
            if not check.ok:
                return StartupRestoreResult("failed", check.reason)
            bind_restored_surface(session_manager, intent, window_id=existing.window_id)
            return StartupRestoreResult(
                "already_restored",
                "registered runtime identity matched restore target",
                existing.window_id,
            )

    success, message, _window_name, window_id, _reused = await tmux_manager.create_or_reuse_window(
        intent.cwd,
        window_name=intent.window_name,
        start_claude=True,
        resume_session_id=intent.runtime_id,
        runtime_kind=intent.runtime_kind,
        launch_command=intent.launcher_command,
        reuse_existing=True,
    )
    if not success or not window_id:
        return StartupRestoreResult("failed", message)
    bind_restored_surface(session_manager, intent, window_id=window_id)
    return StartupRestoreResult("restored", message, window_id)
