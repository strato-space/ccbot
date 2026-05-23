"""Startup restore intent helpers for tmux-hosted runtime recovery.

The helpers in this module keep restore declarations separate from canonical
binding truth.  Service-local environment variables may declare an intended
restore target, but live binding state is written only after identity and
surface checks pass.
"""

from __future__ import annotations

import logging
import os
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .config import config
from .launcher_registration import infer_runtime_kind_from_command
from .runtime_types import runtime_capability_registry

logger = logging.getLogger(__name__)

_RESTORE_PREFIX = "CCBOT_RESTORE_"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_SHELL_COMMAND_NAMES = {"bash", "dash", "fish", "sh", "zsh"}
_SECRET_ENV_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY")


class RestoreClassification:
    """Semantic startup-restore states used by tests and diagnostics."""

    NO_RESTORE_INTENT = "no_restore_intent"
    INVALID_RESTORE_INTENT = "invalid_restore_intent"
    FULL_LOSS_MISSING_TMUX_WINDOW = "full_loss_missing_tmux_window"
    EXISTING_VALID_RUNTIME = "existing_valid_runtime"
    EXISTING_SHELL_OR_EMPTY_WINDOW = "existing_shell_or_empty_window"
    EXISTING_HELPER_OR_HUD_PANE = "existing_helper_or_hud_pane"
    EXISTING_IDENTITY_MISMATCH = "existing_identity_mismatch"
    EXISTING_CWD_MISMATCH = "existing_cwd_mismatch"
    EXISTING_RUNTIME_KIND_MISMATCH = "existing_runtime_kind_mismatch"
    EXTERNAL_OR_READ_ONLY_BINDING = "external_or_read_only_binding"
    UNSAFE_AMBIGUOUS = "unsafe_ambiguous"
    ENV_CONTRACT_FAILED = "env_contract_failed"


class RestorePaneKind:
    """Pane classifications for restore topology inspection."""

    WORK_RUNTIME_CANDIDATE = "work_runtime_candidate"
    SHELL_OR_EMPTY = "shell_or_empty"
    OMX_HELPER = "omx_helper"
    UNKNOWN = "unknown"


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
    live_proof: "LiveRuntimeProof | None" = None


@dataclass(frozen=True)
class EnvRestoreFacts:
    """Safe-to-log restore environment facts.

    Values such as Telegram tokens are intentionally omitted.  ``CODEX_HOME``
    is a path needed for replay lookup, and ``OMX_AUTO_UPDATE`` is a startup
    safety gate for non-interactive restore.
    """

    codex_home: str = ""
    codex_home_present: bool = False
    omx_auto_update: str = ""
    omx_auto_update_disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RestorePaneSnapshot:
    """Read-only pane topology captured during startup inventory."""

    window_id: str
    pane_id: str
    cwd: str = ""
    command: str = ""
    active: bool = False
    title: str = ""
    classification: str = RestorePaneKind.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LiveRuntimeProof:
    """Proof that a live tmux pane/window is attached to a runtime identity."""

    window_id: str
    pane_id: str = ""
    process_id: str = ""
    runtime_kind: str = ""
    runtime_id: str = ""
    proof_source: str = ""
    codex_home: str = ""
    replay_path: str = ""
    cwd: str = ""
    window_name: str = ""
    observed_at: float = 0.0
    descriptor_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResumeTargetProof:
    """Proof that a persisted runtime identity exists and is resumable."""

    runtime_kind: str
    runtime_id: str
    proof_source: str
    codex_home: str = ""
    replay_path: str = ""
    cwd: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StartupRestoreInventory:
    """Read-only startup restore inspection result."""

    classification: str
    intent: RestoreIntent | None = None
    message: str = ""
    window_id: str = ""
    window_name: str = ""
    cwd: str = ""
    pane_command: str = ""
    pane_id: str = ""
    panes: tuple[RestorePaneSnapshot, ...] = ()
    env: EnvRestoreFacts = field(default_factory=EnvRestoreFacts)
    live_proof: LiveRuntimeProof | None = None
    resume_proof: ResumeTargetProof | None = None
    binding_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        intent = data.get("intent")
        if isinstance(intent, dict):
            data["intent"] = {
                key: value
                for key, value in intent.items()
                if not _looks_sensitive_key(key)
            }
        return data


@dataclass(frozen=True)
class StartupRestoreResult:
    """Outcome of a startup restore attempt."""

    status: str
    message: str = ""
    window_id: str = ""
    classification: str = ""
    inventory: StartupRestoreInventory | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"skipped", "restored", "already_restored", "dry_run"}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.inventory is not None:
            data["inventory"] = self.inventory.to_dict()
        return data


_RETRYABLE_RESTORE_CLASSIFICATIONS = {
    RestoreClassification.FULL_LOSS_MISSING_TMUX_WINDOW,
    RestoreClassification.EXISTING_SHELL_OR_EMPTY_WINDOW,
}


def is_startup_restore_retryable(result: StartupRestoreResult) -> bool:
    """Return True when a later retry may safely recover the restore target.

    Reboots can start the Telegram controller before the tmux runtime has
    finished recreating or resuming its live Codex pane. Those cases are
    transient only for a missing target window or a shell/empty placeholder;
    identity, cwd, helper, env, or ambiguity failures must remain fail-closed.
    """
    return (
        result.status == "failed"
        and result.classification in _RETRYABLE_RESTORE_CLASSIFICATIONS
    )


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in _TRUE_VALUES


def _falsy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in _FALSE_VALUES


def _looks_sensitive_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SECRET_ENV_MARKERS)


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


def _env_restore_facts(environ: Mapping[str, str] | None = None) -> EnvRestoreFacts:
    source: Mapping[str, str] = os.environ if environ is None else environ
    codex_home = str(source.get("CODEX_HOME") or "").strip()
    omx_auto_update = str(source.get("OMX_AUTO_UPDATE") or "").strip()
    return EnvRestoreFacts(
        codex_home=codex_home,
        codex_home_present=bool(codex_home),
        omx_auto_update=omx_auto_update,
        omx_auto_update_disabled=_falsy(omx_auto_update),
    )


def validate_restore_env_contract(
    intent: RestoreIntent,
    environ: Mapping[str, str] | None = None,
) -> RestoreCheckResult:
    """Validate controller env needed before restore can become writable."""
    env = _env_restore_facts(environ)
    if intent.runtime_kind == "codex":
        if not env.codex_home_present:
            return RestoreCheckResult(False, "missing CODEX_HOME for Codex restore")
        if not env.omx_auto_update_disabled:
            return RestoreCheckResult(
                False,
                "OMX_AUTO_UPDATE=0 is required for non-interactive restore",
            )
    return RestoreCheckResult(True)


def build_restore_launch_command(
    intent: RestoreIntent,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return launch command with durable non-interactive restore env prefix."""
    env = _env_restore_facts(environ)
    assignments: list[str] = []
    if intent.runtime_kind == "codex" and env.codex_home:
        assignments.append(f"CODEX_HOME={shlex.quote(env.codex_home)}")
    if env.omx_auto_update:
        assignments.append(f"OMX_AUTO_UPDATE={shlex.quote(env.omx_auto_update)}")
    command = intent.launcher_command.strip()
    if assignments and command:
        return " ".join([*assignments, command])
    return command


def _pane_value(pane: Any, *names: str) -> str:
    for name in names:
        value = getattr(pane, name, None)
        if value is not None:
            text = str(value)
            if text:
                return text
    return ""


def classify_restore_pane(pane: Any) -> str:
    """Classify tmux pane topology without treating it as identity proof."""
    command = _pane_value(pane, "pane_current_command", "command").strip()
    title = _pane_value(pane, "pane_title", "title").strip()
    haystack = f"{command} {title}".casefold()
    if not command:
        return RestorePaneKind.SHELL_OR_EMPTY
    if _is_shell_command(command):
        return RestorePaneKind.SHELL_OR_EMPTY
    if (
        "omx hud" in haystack
        or "omx question" in haystack
        or "omx update" in haystack
        or "oh-my-codex:update" in haystack
        or ("hud" in haystack and "omx" in haystack)
        or ("question" in haystack and "omx" in haystack)
    ):
        return RestorePaneKind.OMX_HELPER
    known = runtime_capability_registry.known_runtime_kind_from_command(command)
    if known is not None:
        return RestorePaneKind.WORK_RUNTIME_CANDIDATE
    return RestorePaneKind.UNKNOWN


def _pane_snapshot(window_id: str, pane: Any) -> RestorePaneSnapshot:
    return RestorePaneSnapshot(
        window_id=window_id,
        pane_id=_pane_value(pane, "pane_id"),
        cwd=_normalize_path(_pane_value(pane, "cwd", "pane_current_path")),
        command=_pane_value(pane, "pane_current_command", "command"),
        active=bool(getattr(pane, "pane_active", getattr(pane, "active", False))),
        title=_pane_value(pane, "pane_title", "title"),
        classification=classify_restore_pane(pane),
    )


def _window_active_pane_snapshot(window: Any) -> RestorePaneSnapshot:
    return RestorePaneSnapshot(
        window_id=str(getattr(window, "window_id", "") or ""),
        pane_id=str(getattr(window, "pane_id", "") or ""),
        cwd=_normalize_path(str(getattr(window, "cwd", "") or "")),
        command=str(getattr(window, "pane_current_command", "") or ""),
        active=True,
        title=str(getattr(window, "pane_title", "") or ""),
        classification=classify_restore_pane(
            type(
                "_RestoreWindowPane",
                (),
                {
                    "pane_current_command": str(
                        getattr(window, "pane_current_command", "") or ""
                    ),
                    "pane_title": str(getattr(window, "pane_title", "") or ""),
                },
            )()
        ),
    )


async def _list_restore_panes(tmux_manager: Any, window: Any) -> tuple[RestorePaneSnapshot, ...]:
    window_id = str(getattr(window, "window_id", "") or "")
    list_panes = getattr(tmux_manager, "list_panes", None)
    if callable(list_panes) and window_id:
        try:
            panes = await list_panes(window_id)
        except Exception as exc:
            logger.warning("Startup restore failed to list panes for %s: %s", window_id, exc)
            panes = []
        snapshots = tuple(_pane_snapshot(window_id, pane) for pane in panes)
        if snapshots:
            return snapshots
    return (_window_active_pane_snapshot(window),)


def _descriptor_fingerprint(session_manager: Any, window_id: str) -> str:
    state = getattr(session_manager, "window_states", {}).get(window_id)
    if state is None:
        return "missing"
    parts = [
        str(getattr(state, "thread_id", "") or ""),
        str(getattr(state, "runtime_kind", "") or ""),
        _normalize_path(str(getattr(state, "cwd", "") or "")),
        str(getattr(state, "window_name", "") or ""),
    ]
    return "|".join(parts)


def _binding_fingerprint(session_manager: Any, intent: RestoreIntent) -> str:
    surface_bindings = getattr(session_manager, "surface_bindings", {})
    external_bindings = getattr(session_manager, "external_surface_bindings", {})
    surface_states = getattr(session_manager, "surface_binding_states", {})
    bound = surface_bindings.get(intent.user_id, {}).get(intent.surface_key, "")
    external = external_bindings.get(intent.user_id, {}).get(intent.surface_key, {})
    state = surface_states.get(intent.user_id, {}).get(intent.surface_key, "")
    return f"{bound}|{external!r}|{state}"


def _validate_group_chat_coordinate(
    session_manager: Any,
    intent: RestoreIntent,
) -> RestoreCheckResult:
    if intent.group_chat_id is None:
        return RestoreCheckResult(True)
    thread_id = _surface_thread_id(intent.surface_key)
    group_chat_ids = getattr(session_manager, "group_chat_ids", {})
    key = f"{intent.user_id}:{thread_id or 0}"
    existing = group_chat_ids.get(key)
    if existing is not None and int(existing) != int(intent.group_chat_id):
        return RestoreCheckResult(False, "telegram routing chat_id mismatch")
    return RestoreCheckResult(True)


def build_live_runtime_proof(
    session_manager: Any,
    window: Any,
    intent: RestoreIntent,
    *,
    pane_id: str = "",
    proof_source: str = "live_descriptor",
    environ: Mapping[str, str] | None = None,
) -> LiveRuntimeProof | None:
    """Build LiveRuntimeProof from exact live descriptor/session metadata."""
    window_id = str(getattr(window, "window_id", "") or "")
    state = getattr(session_manager, "window_states", {}).get(window_id)
    if state is None:
        return None
    runtime_kind = str(getattr(state, "runtime_kind", "") or "")
    runtime_id = str(getattr(state, "thread_id", "") or "").strip()
    if runtime_kind != intent.runtime_kind or runtime_id != intent.runtime_id:
        return None
    helper_check = getattr(session_manager, "_is_codex_helper_window", None)
    if callable(helper_check) and helper_check(window_id):
        return None
    env = _env_restore_facts(environ)
    replay_path = ""
    if intent.runtime_kind == "codex":
        catalog = getattr(session_manager, "codex_thread_catalog", None)
        if catalog is not None:
            try:
                candidate = catalog.get_candidate_fast(intent.runtime_id)
            except Exception as exc:
                logger.warning(
                    "Unable to resolve Codex live proof replay path for %s: %s",
                    intent.runtime_id,
                    exc,
                )
                candidate = None
            if candidate is not None:
                replay_path = str(getattr(candidate, "rollout_file", "") or "")
    return LiveRuntimeProof(
        window_id=window_id,
        pane_id=pane_id or str(getattr(window, "pane_id", "") or ""),
        runtime_kind=runtime_kind,
        runtime_id=runtime_id,
        proof_source=proof_source,
        codex_home=env.codex_home,
        replay_path=replay_path,
        cwd=_normalize_path(str(getattr(state, "cwd", "") or getattr(window, "cwd", "") or "")),
        window_name=str(
            getattr(state, "window_name", "") or getattr(window, "window_name", "") or ""
        ),
        observed_at=time.time(),
        descriptor_fingerprint=_descriptor_fingerprint(session_manager, window_id),
    )


def build_resume_target_proof(
    session_manager: Any,
    intent: RestoreIntent,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResumeTargetProof | None:
    """Build proof that a configured runtime id exists as a resume target."""
    env = _env_restore_facts(environ)
    if intent.runtime_kind != "codex":
        return ResumeTargetProof(
            runtime_kind=intent.runtime_kind,
            runtime_id=intent.runtime_id,
            proof_source="runtime_kind_without_catalog",
            cwd=_normalize_path(intent.cwd),
        )
    if not env.codex_home_present:
        return None
    catalog = getattr(session_manager, "codex_thread_catalog", None)
    if catalog is None:
        return None
    try:
        locator = None
        exact_locator = getattr(catalog, "exact_locator", None)
        if callable(exact_locator):
            locator = exact_locator(intent.runtime_id, intent.cwd)
        if locator is None:
            candidate = catalog.get_candidate_fast(intent.runtime_id)
            if candidate is not None and candidate.normalized_cwd == _normalize_path(intent.cwd):
                locator = candidate.to_locator()
    except Exception as exc:
        logger.warning("Unable to build Codex resume target proof: %s", exc)
        return None
    if locator is None:
        return None
    return ResumeTargetProof(
        runtime_kind="codex",
        runtime_id=intent.runtime_id,
        proof_source="codex_replay_catalog",
        codex_home=env.codex_home,
        replay_path=str(getattr(locator, "file_path", "") or ""),
        cwd=_normalize_path(str(getattr(locator, "cwd", "") or intent.cwd)),
    )


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
    existing_cwd = _normalize_path(str(getattr(state, "cwd", "") or ""))
    if existing_cwd and existing_cwd != _normalize_path(intent.cwd):
        return RestoreCheckResult(False, "target window cwd mismatch")
    helper_check = getattr(session_manager, "_is_codex_helper_window", None)
    if callable(helper_check) and helper_check(window_id):
        return RestoreCheckResult(False, "runtime helper window is not bindable")
    proof = LiveRuntimeProof(
        window_id=window_id,
        runtime_kind=runtime_kind,
        runtime_id=thread_id,
        proof_source="live_descriptor",
        cwd=_normalize_path(str(getattr(state, "cwd", "") or "")),
        window_name=str(getattr(state, "window_name", "") or ""),
        observed_at=time.time(),
        descriptor_fingerprint=_descriptor_fingerprint(session_manager, window_id),
    )
    return RestoreCheckResult(True, live_proof=proof)


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
    clear_duplicates = getattr(
        session_manager,
        "clear_duplicate_thread_claims_for_window",
        None,
    )
    if callable(clear_duplicates):
        clear_duplicates(window_id, reason="startup_restore")


def _classify_existing_window(
    *,
    window: Any,
    intent: RestoreIntent,
    panes: tuple[RestorePaneSnapshot, ...],
    live_proof: LiveRuntimeProof | None,
) -> tuple[str, str]:
    existing_cwd = _normalize_path(str(getattr(window, "cwd", "") or ""))
    if existing_cwd and existing_cwd != _normalize_path(intent.cwd):
        return RestoreClassification.EXISTING_CWD_MISMATCH, "target window cwd mismatch"

    command = str(getattr(window, "pane_current_command", "") or "")
    active_runtime = runtime_capability_registry.known_runtime_kind_from_command(command)
    if active_runtime is not None and active_runtime != intent.runtime_kind:
        return (
            RestoreClassification.EXISTING_RUNTIME_KIND_MISMATCH,
            "target window runtime kind mismatch",
        )
    if live_proof is not None:
        return RestoreClassification.EXISTING_VALID_RUNTIME, (
            "existing runtime identity matched restore target"
        )

    non_helper = [
        pane
        for pane in panes
        if pane.classification not in {RestorePaneKind.OMX_HELPER}
    ]
    if panes and not non_helper:
        return (
            RestoreClassification.EXISTING_HELPER_OR_HUD_PANE,
            "target window contains only OMX helper panes",
        )
    if all(p.classification == RestorePaneKind.SHELL_OR_EMPTY for p in non_helper):
        return (
            RestoreClassification.EXISTING_SHELL_OR_EMPTY_WINDOW,
            "target window is shell/empty and may launch resume",
        )
    if active_runtime is not None:
        return (
            RestoreClassification.EXISTING_IDENTITY_MISMATCH,
            "missing runtime identity metadata",
        )
    return RestoreClassification.UNSAFE_AMBIGUOUS, (
        "target window has ambiguous non-runtime pane state"
    )


async def inspect_configured_startup_target(
    session_manager: Any,
    tmux_manager: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> StartupRestoreInventory:
    """Inspect configured startup restore target without mutating state."""
    try:
        intent = parse_restore_intent(environ)
    except RestoreIntentError as exc:
        return StartupRestoreInventory(
            classification=RestoreClassification.INVALID_RESTORE_INTENT,
            message=str(exc),
            env=_env_restore_facts(environ),
        )
    if intent is None:
        return StartupRestoreInventory(
            classification=RestoreClassification.NO_RESTORE_INTENT,
            message="no restore intent configured",
            env=_env_restore_facts(environ),
        )

    env_check = validate_restore_env_contract(intent, environ)
    if not env_check.ok:
        return StartupRestoreInventory(
            classification=RestoreClassification.ENV_CONTRACT_FAILED,
            intent=intent,
            message=env_check.reason,
            env=_env_restore_facts(environ),
            binding_fingerprint=_binding_fingerprint(session_manager, intent),
        )

    route_check = _validate_group_chat_coordinate(session_manager, intent)
    if not route_check.ok:
        return StartupRestoreInventory(
            classification=RestoreClassification.UNSAFE_AMBIGUOUS,
            intent=intent,
            message=route_check.reason,
            env=_env_restore_facts(environ),
            binding_fingerprint=_binding_fingerprint(session_manager, intent),
        )

    existing = await tmux_manager.find_window_by_name(intent.window_name)
    if existing is None:
        resume_proof = build_resume_target_proof(
            session_manager,
            intent,
            environ=environ,
        )
        classification = RestoreClassification.FULL_LOSS_MISSING_TMUX_WINDOW
        message = (
            "target tmux window missing"
            if resume_proof is not None
            else "missing resume target proof"
        )
        if intent.runtime_kind == "codex" and resume_proof is None:
            classification = RestoreClassification.UNSAFE_AMBIGUOUS
        return StartupRestoreInventory(
            classification=classification,
            intent=intent,
            message=message,
            env=_env_restore_facts(environ),
            resume_proof=resume_proof,
            binding_fingerprint=_binding_fingerprint(session_manager, intent),
        )

    panes = await _list_restore_panes(tmux_manager, existing)
    active_pane_id = str(getattr(existing, "pane_id", "") or "")
    live_proof = build_live_runtime_proof(
        session_manager,
        existing,
        intent,
        pane_id=active_pane_id,
        environ=environ,
    )
    classification, message = _classify_existing_window(
        window=existing,
        intent=intent,
        panes=panes,
        live_proof=live_proof,
    )
    resume_proof: ResumeTargetProof | None = None
    if classification == RestoreClassification.EXISTING_SHELL_OR_EMPTY_WINDOW:
        resume_proof = build_resume_target_proof(
            session_manager,
            intent,
            environ=environ,
        )
        if intent.runtime_kind == "codex" and resume_proof is None:
            classification = RestoreClassification.UNSAFE_AMBIGUOUS
            message = "missing resume target proof"
    return StartupRestoreInventory(
        classification=classification,
        intent=intent,
        message=message,
        window_id=str(getattr(existing, "window_id", "") or ""),
        window_name=str(getattr(existing, "window_name", "") or intent.window_name),
        cwd=_normalize_path(str(getattr(existing, "cwd", "") or "")),
        pane_command=str(getattr(existing, "pane_current_command", "") or ""),
        pane_id=active_pane_id,
        panes=panes,
        env=_env_restore_facts(environ),
        live_proof=live_proof,
        resume_proof=resume_proof,
        binding_fingerprint=_binding_fingerprint(session_manager, intent),
    )


async def _revalidate_before_bind(
    session_manager: Any,
    tmux_manager: Any,
    intent: RestoreIntent,
    inventory: StartupRestoreInventory,
    *,
    window_id: str,
    live_proof: LiveRuntimeProof,
    environ: Mapping[str, str] | None,
) -> RestoreCheckResult:
    if _binding_fingerprint(session_manager, intent) != inventory.binding_fingerprint:
        return RestoreCheckResult(False, "binding state changed during restore")
    existing = await tmux_manager.find_window_by_name(intent.window_name)
    if existing is None or str(getattr(existing, "window_id", "") or "") != window_id:
        return RestoreCheckResult(False, "target window changed during restore")
    refreshed = build_live_runtime_proof(
        session_manager,
        existing,
        intent,
        pane_id=str(getattr(existing, "pane_id", "") or live_proof.pane_id),
        environ=environ,
    )
    if refreshed is None:
        return RestoreCheckResult(False, "runtime identity proof disappeared")
    if refreshed.descriptor_fingerprint != live_proof.descriptor_fingerprint:
        return RestoreCheckResult(False, "runtime descriptor changed during restore")
    return RestoreCheckResult(True, live_proof=refreshed)


async def restore_configured_startup_target(
    session_manager: Any,
    tmux_manager: Any,
    *,
    environ: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> StartupRestoreResult:
    """Restore one configured startup target, if service env declares one."""
    inventory = await inspect_configured_startup_target(
        session_manager,
        tmux_manager,
        environ=environ,
    )
    logger.info(
        "Startup restore inventory: classification=%s window_id=%s message=%s",
        inventory.classification,
        inventory.window_id,
        inventory.message,
    )
    if dry_run:
        return StartupRestoreResult(
            "dry_run",
            inventory.message,
            inventory.window_id,
            inventory.classification,
            inventory,
        )
    if inventory.classification == RestoreClassification.NO_RESTORE_INTENT:
        return StartupRestoreResult(
            "skipped",
            inventory.message,
            classification=inventory.classification,
            inventory=inventory,
        )
    if inventory.intent is None:
        logger.error("Invalid startup restore intent: %s", inventory.message)
        return StartupRestoreResult(
            "failed",
            inventory.message,
            classification=inventory.classification,
            inventory=inventory,
        )

    intent = inventory.intent
    if inventory.classification == RestoreClassification.EXISTING_VALID_RUNTIME:
        assert inventory.live_proof is not None
        recheck = await _revalidate_before_bind(
            session_manager,
            tmux_manager,
            intent,
            inventory,
            window_id=inventory.window_id,
            live_proof=inventory.live_proof,
            environ=environ,
        )
        if not recheck.ok:
            return StartupRestoreResult(
                "failed",
                recheck.reason,
                inventory.window_id,
                inventory.classification,
                inventory,
            )
        bind_restored_surface(session_manager, intent, window_id=inventory.window_id)
        return StartupRestoreResult(
            "already_restored",
            "existing runtime identity matched restore target",
            inventory.window_id,
            inventory.classification,
            inventory,
        )

    if inventory.classification not in {
        RestoreClassification.FULL_LOSS_MISSING_TMUX_WINDOW,
        RestoreClassification.EXISTING_SHELL_OR_EMPTY_WINDOW,
    }:
        return StartupRestoreResult(
            "failed",
            inventory.message,
            inventory.window_id,
            inventory.classification,
            inventory,
        )

    success, message, _window_name, window_id, _reused = await tmux_manager.create_or_reuse_window(
        intent.cwd,
        window_name=intent.window_name,
        start_claude=True,
        resume_session_id=intent.runtime_id,
        runtime_kind=intent.runtime_kind,
        launch_command=build_restore_launch_command(intent, environ),
        reuse_existing=True,
    )
    if not success or not window_id:
        return StartupRestoreResult(
            "failed",
            message,
            classification=inventory.classification,
            inventory=inventory,
        )
    wait_for_entry = getattr(session_manager, "wait_for_session_map_entry", None)
    if callable(wait_for_entry):
        try:
            await wait_for_entry(window_id, timeout=5.0, interval=0.25)
        except TypeError:
            await wait_for_entry(window_id)

    created_window = await tmux_manager.find_window_by_name(intent.window_name)
    if created_window is None or str(getattr(created_window, "window_id", "") or "") != window_id:
        return StartupRestoreResult(
            "failed",
            "target window changed before restore proof",
            window_id,
            inventory.classification,
            inventory,
        )
    live_proof = build_live_runtime_proof(
        session_manager,
        created_window,
        intent,
        pane_id=str(getattr(created_window, "pane_id", "") or ""),
        environ=environ,
    )
    if live_proof is None:
        return StartupRestoreResult(
            "failed",
            "missing live runtime proof after launch",
            window_id,
            inventory.classification,
            inventory,
        )
    bind_restored_surface(session_manager, intent, window_id=window_id)
    return StartupRestoreResult(
        "restored",
        message,
        window_id,
        inventory.classification,
        inventory,
    )
