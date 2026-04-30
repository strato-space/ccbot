"""Runtime-neutral core types for tmux-driven agent control.

These dataclasses implement the ontology in ``doc/runtime-ontology.md``.
They deliberately keep binding, live process, thread, rollout source, and
normalized event concerns separate, while exposing compatibility properties
for the current Claude-oriented code paths.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .state_schema import DEFAULT_RUNTIME_KIND, normalize_runtime_kind

LIFECYCLE_SEMANTIC_KIND = "lifecycle"
USER_ECHO_SEMANTIC_KIND = "user_echo"
COMMENTARY_SEMANTIC_KIND = "commentary"
ORCHESTRATION_SEMANTIC_KIND = "orchestration"
PLAN_UPDATE_SEMANTIC_KIND = "plan_update"
WARNING_SEMANTIC_KIND = "warning"
REASONING_SEMANTIC_KIND = "reasoning"
TOOL_START_SEMANTIC_KIND = "tool_start"
TOOL_PROGRESS_SEMANTIC_KIND = "tool_progress"
TOOL_RESULT_SEMANTIC_KIND = "tool_result"
COMMAND_EXECUTION_SEMANTIC_KIND = "command_execution"
FILE_CHANGE_SEMANTIC_KIND = "file_change"
ASSISTANT_FINAL_SEMANTIC_KIND = "assistant_final"

DELIVERY_CLASS_HISTORY = "history"
DELIVERY_CLASS_PROGRESS = "progress"
DELIVERY_CLASS_LIFECYCLE = "lifecycle"


def infer_semantic_kind(
    *,
    role: str,
    content_type: str,
    event_kind: str,
) -> str:
    """Infer the runtime-neutral semantic kind for a normalized event."""
    if event_kind == "lifecycle" or content_type == "lifecycle":
        return LIFECYCLE_SEMANTIC_KIND
    if role == "user":
        return USER_ECHO_SEMANTIC_KIND
    if content_type == "warning" or event_kind == "warning":
        return WARNING_SEMANTIC_KIND
    if content_type == "orchestration" or event_kind == "orchestration":
        return ORCHESTRATION_SEMANTIC_KIND
    if content_type == "plan_update" or event_kind == "plan_update":
        return PLAN_UPDATE_SEMANTIC_KIND
    if content_type == "commentary" or event_kind == "commentary":
        return COMMENTARY_SEMANTIC_KIND
    if content_type in {"thinking", "reasoning"} or event_kind == "reasoning":
        return REASONING_SEMANTIC_KIND
    if content_type == "tool_use" or event_kind == "tool_call":
        return TOOL_START_SEMANTIC_KIND
    if content_type == "tool_progress" or event_kind == "tool_progress":
        return TOOL_PROGRESS_SEMANTIC_KIND
    if content_type == "tool_result" or event_kind == "tool_output":
        return TOOL_RESULT_SEMANTIC_KIND
    if content_type in {"command_execution", "local_command"} or event_kind == "command_execution":
        return COMMAND_EXECUTION_SEMANTIC_KIND
    if content_type == "file_change" or event_kind == "file_change":
        return FILE_CHANGE_SEMANTIC_KIND
    return ASSISTANT_FINAL_SEMANTIC_KIND


def infer_delivery_class(semantic_kind: str) -> str:
    """Classify a normalized event for Telegram delivery and history policy."""
    if semantic_kind == LIFECYCLE_SEMANTIC_KIND:
        return DELIVERY_CLASS_LIFECYCLE
    if semantic_kind in {
        COMMENTARY_SEMANTIC_KIND,
        REASONING_SEMANTIC_KIND,
        TOOL_START_SEMANTIC_KIND,
        TOOL_PROGRESS_SEMANTIC_KIND,
    }:
        return DELIVERY_CLASS_PROGRESS
    return DELIVERY_CLASS_HISTORY


def is_terminal_turn_artifact(semantic_kind: str) -> bool:
    """Return True when the semantic kind closes the visible turn surface."""
    return semantic_kind == ASSISTANT_FINAL_SEMANTIC_KIND


def is_pre_final_visible_semantic_kind(semantic_kind: str) -> bool:
    """Return True for visible assistant-side artifacts that must precede final."""
    return semantic_kind not in {
        USER_ECHO_SEMANTIC_KIND,
        WARNING_SEMANTIC_KIND,
        ASSISTANT_FINAL_SEMANTIC_KIND,
        LIFECYCLE_SEMANTIC_KIND,
    }


@dataclass(frozen=True)
class TopicBinding:
    """Persisted association from a Telegram topic to a delivery source."""

    user_id: int
    thread_id: int | None
    window_id: str
    window_name: str = ""
    runtime_kind: str = "claude"
    binding_scope: str = "tmux"
    source_thread_id: str = ""
    read_only: bool = False


@dataclass
class LiveProcessDescriptor:
    """State describing the live process hosted by a tmux window."""

    thread_id: str = ""
    cwd: str = ""
    window_name: str = ""
    runtime_kind: str = "claude"
    registered_at: float = 0.0

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for the persisted thread identifier."""
        return self.thread_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.thread_id = value

    def to_dict(self) -> dict[str, Any]:
        """Serialize in the current on-disk shape until T4 migration lands."""
        data: dict[str, Any] = {
            "session_id": self.thread_id,
            "cwd": self.cwd,
            "runtime_kind": self.runtime_kind,
        }
        if self.window_name:
            data["window_name"] = self.window_name
        if self.registered_at:
            data["registered_at"] = self.registered_at
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveProcessDescriptor":
        return cls(
            thread_id=data.get("thread_id") or data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            runtime_kind=data.get("runtime_kind", "claude"),
            registered_at=float(data.get("registered_at", 0.0) or 0.0),
        )


@dataclass
class ThreadLocator:
    """Resolved persisted conversation identity plus its replay evidence path."""

    thread_id: str
    summary: str
    message_count: int
    file_path: str
    runtime_kind: str = "claude"
    cwd: str = ""

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for historical Claude call sites."""
        return self.thread_id

    @property
    def replay_path(self) -> str:
        """Runtime-neutral alias for the persisted replay evidence path."""
        return self.file_path


@dataclass
class RolloutSource:
    """Readable replay source associated with a persisted thread.

    In the current Codex implementation, tailing this append-only artifact
    yields both the observed live semantic stream and the persisted replay
    evidence. The concepts remain distinct even when the artifact is shared.
    """

    thread_id: str
    file_path: Path
    runtime_kind: str = "claude"
    source_kind: str = "jsonl"
    cwd: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.file_path, Path):
            self.file_path = Path(self.file_path)

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for monitor code that still says session."""
        return self.thread_id

    @property
    def replay_path(self) -> Path:
        """Runtime-neutral alias for the persisted replay evidence path."""
        return self.file_path


@dataclass
class NormalizedEvent:
    """Runtime-neutral event emitted from a rollout source."""

    thread_id: str = ""
    text: str = ""
    is_complete: bool = True
    content_type: str = "text"
    tool_use_id: str | None = None
    role: str = "assistant"
    tool_name: str | None = None
    image_data: list[tuple[str, bytes]] | None = None
    timestamp: str | None = None
    runtime_kind: str = "claude"
    event_kind: str = "message"
    semantic_kind: str = ""
    delivery_class: str = ""
    include_in_history: bool | None = None
    dispatch_to_telegram: bool | None = None
    status_message_eligible: bool | None = None

    def __post_init__(self) -> None:
        if not self.semantic_kind:
            self.semantic_kind = infer_semantic_kind(
                role=self.role,
                content_type=self.content_type,
                event_kind=self.event_kind,
            )
        if not self.delivery_class:
            self.delivery_class = infer_delivery_class(self.semantic_kind)
        if self.include_in_history is None:
            self.include_in_history = self.semantic_kind not in {
                LIFECYCLE_SEMANTIC_KIND,
                TOOL_PROGRESS_SEMANTIC_KIND,
            }
        if self.dispatch_to_telegram is None:
            self.dispatch_to_telegram = self.delivery_class != DELIVERY_CLASS_LIFECYCLE
        if self.status_message_eligible is None:
            self.status_message_eligible = self.delivery_class == DELIVERY_CLASS_PROGRESS

    @property
    def session_id(self) -> str:
        """Backward-compatible alias for notification code."""
        return self.thread_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.thread_id = value


@dataclass(frozen=True)
class InputAction:
    """Runtime-neutral intent for sending input to a live process."""

    action_type: str
    payload: str = ""
    submit: bool = True
    runtime_kind: str = "claude"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeCapability:
    """Capability profile for one supported runtime."""

    runtime_kind: str
    display_name: str
    launch_command_name: str
    resume_style: str
    resume_token: str = "--resume"
    resume_subcommand: str = "resume"
    command_aliases: tuple[str, ...] = ()
    rename_tmux_supported: bool = True
    rename_identity_mode: str = "unsupported"
    live_stream_discovery: str = "unknown"
    replay_evidence_discovery: str = "unknown"
    progress_source: str = "live_stream"
    final_result_source: str = "live_stream"
    prompt_detection: str = "capture_pane"
    blocked_input_policy: str = "fail_closed_on_visible_prompt"
    message_routing_modes: tuple[str, ...] = ("queue", "steer")
    interactive_control_supported: bool = True
    safe_degraded_mode: str = "monitor_only"
    tmux_stdio_cli_first: bool = True
    machine_transport_over_stdio_required: bool = False

    def build_launch_command(
        self,
        base_command: str | None = None,
        *,
        resume_session_id: str | None = None,
    ) -> str:
        """Build a runtime-specific command string for a tmux launch."""
        command = (base_command or self.launch_command_name).strip()
        if not command:
            return ""
        if not resume_session_id or self.resume_style == "none":
            return command
        quoted_resume_id = shlex.quote(resume_session_id)
        if self.resume_style == "flag":
            return f"{command} {self.resume_token} {quoted_resume_id}"
        if self.resume_style == "subcommand":
            return f"{command} {self.resume_subcommand} {quoted_resume_id}"
        if self.resume_style == "inline":
            return f"{command} {quoted_resume_id}"
        return command

    def supports_message_routing_mode(self, mode: str) -> bool:
        """Check whether the runtime advertises a routing mode."""
        return mode in self.message_routing_modes


def _build_default_runtime_capabilities() -> dict[str, RuntimeCapability]:
    """Construct the runtime capability registry used by ccbot."""
    return {
        "claude": RuntimeCapability(
            runtime_kind="claude",
            display_name="Claude Code",
            launch_command_name="claude",
            resume_style="flag",
            command_aliases=("claude-code",),
            rename_tmux_supported=True,
            rename_identity_mode="unsupported",
            live_stream_discovery="transcript_tail",
            replay_evidence_discovery="transcript_jsonl",
            progress_source="replay_evidence",
            final_result_source="replay_evidence",
            prompt_detection="capture_pane",
            blocked_input_policy="fail_closed_on_visible_prompt",
            message_routing_modes=("queue", "steer"),
            interactive_control_supported=True,
            safe_degraded_mode="manual_bind_and_monitor",
            tmux_stdio_cli_first=True,
        ),
        "codex": RuntimeCapability(
            runtime_kind="codex",
            display_name="Codex",
            launch_command_name="codex",
            resume_style="subcommand",
            command_aliases=("codex-acp",),
            rename_tmux_supported=True,
            rename_identity_mode="unsupported_degraded",
            live_stream_discovery="rollout_tail",
            replay_evidence_discovery="rollout_jsonl",
            progress_source="replay_evidence",
            final_result_source="replay_evidence",
            prompt_detection="capture_pane",
            blocked_input_policy="fail_closed_on_visible_prompt",
            message_routing_modes=("queue", "steer"),
            interactive_control_supported=True,
            safe_degraded_mode="tmux_only_monitor_replay",
            tmux_stdio_cli_first=True,
        ),
        "fast-agent": RuntimeCapability(
            runtime_kind="fast-agent",
            display_name="fast-agent",
            launch_command_name="fast-agent",
            resume_style="flag",
            command_aliases=("fast-agent-acp",),
            rename_tmux_supported=True,
            rename_identity_mode="title_only",
            live_stream_discovery="acp_equivalent_sidechannel",
            replay_evidence_discovery="acp_log_jsonl",
            progress_source="live_stream",
            final_result_source="live_stream",
            prompt_detection="capture_pane",
            blocked_input_policy="fail_closed_on_visible_prompt",
            message_routing_modes=("queue", "steer"),
            interactive_control_supported=True,
            safe_degraded_mode="monitor_only_without_sidechannel",
            tmux_stdio_cli_first=True,
        ),
    }


class RuntimeCapabilityRegistry:
    """Lookup helper for runtime capability profiles."""

    def __init__(
        self,
        capabilities: Mapping[str, RuntimeCapability] | None = None,
        default_runtime_kind: str = DEFAULT_RUNTIME_KIND,
    ) -> None:
        self._capabilities = dict(capabilities or _build_default_runtime_capabilities())
        self.default_runtime_kind = normalize_runtime_kind(default_runtime_kind)
        if self.default_runtime_kind not in self._capabilities:
            self.default_runtime_kind = next(iter(self._capabilities))
        self._command_index = self._build_command_index()

    def _build_command_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for runtime_kind, capability in self._capabilities.items():
            for command_name in (capability.launch_command_name,) + capability.command_aliases:
                normalized = command_name.strip().casefold()
                if normalized:
                    index[normalized] = runtime_kind
        return index

    def get(self, runtime_kind: str | None = None) -> RuntimeCapability:
        """Get the capability profile for a runtime, falling back safely."""
        normalized = normalize_runtime_kind(runtime_kind or self.default_runtime_kind)
        return self._capabilities.get(normalized, self._capabilities[self.default_runtime_kind])

    def items(self) -> tuple[tuple[str, RuntimeCapability], ...]:
        """Return all registered runtime capabilities."""
        return tuple(self._capabilities.items())

    def describe(self, runtime_kind: str | None = None) -> dict[str, Any]:
        """Return a serializable description of a runtime capability."""
        capability = self.get(runtime_kind)
        return {
            "runtime_kind": capability.runtime_kind,
            "display_name": capability.display_name,
            "launch_command_name": capability.launch_command_name,
            "resume_style": capability.resume_style,
            "resume_token": capability.resume_token,
            "resume_subcommand": capability.resume_subcommand,
            "rename_tmux_supported": capability.rename_tmux_supported,
            "rename_identity_mode": capability.rename_identity_mode,
            "live_stream_discovery": capability.live_stream_discovery,
            "replay_evidence_discovery": capability.replay_evidence_discovery,
            "progress_source": capability.progress_source,
            "final_result_source": capability.final_result_source,
            "prompt_detection": capability.prompt_detection,
            "blocked_input_policy": capability.blocked_input_policy,
            "message_routing_modes": list(capability.message_routing_modes),
            "interactive_control_supported": capability.interactive_control_supported,
            "safe_degraded_mode": capability.safe_degraded_mode,
            "tmux_stdio_cli_first": capability.tmux_stdio_cli_first,
            "machine_transport_over_stdio_required": (
                capability.machine_transport_over_stdio_required
            ),
        }

    def known_runtime_kind_from_command(self, command: str) -> str | None:
        """Return the runtime kind named by a command, or None for shell/unknown."""
        command = (command or "").strip().casefold()
        if not command:
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()

        for token in tokens:
            if "=" in token and not token.startswith(("/", "./", "../")):
                name, _, value = token.partition("=")
                if name and value:
                    continue
            executable = Path(token).name.casefold()
            if executable in self._command_index:
                return self._command_index[executable]
        return None

    def infer_runtime_kind_from_command(self, command: str) -> str:
        """Infer a runtime kind from a launcher command string."""
        known_runtime = self.known_runtime_kind_from_command(command)
        if known_runtime:
            return known_runtime
        return self.default_runtime_kind

    def build_launch_command(
        self,
        runtime_kind: str | None = None,
        *,
        base_command: str | None = None,
        resume_session_id: str | None = None,
    ) -> str:
        """Build a runtime-specific launch command."""
        capability = self.get(runtime_kind)
        return capability.build_launch_command(
            base_command or capability.launch_command_name,
            resume_session_id=resume_session_id,
        )

    def supports_message_routing_mode(
        self, runtime_kind: str | None, mode: str
    ) -> bool:
        """Check whether a runtime advertises the requested routing mode."""
        return self.get(runtime_kind).supports_message_routing_mode(mode)

    def supports_interactive_control(self, runtime_kind: str | None) -> bool:
        """Check whether a runtime supports operator-level interactive control."""
        return self.get(runtime_kind).interactive_control_supported


runtime_capability_registry = RuntimeCapabilityRegistry()
