"""Runtime input driver for tmux-backed Codex and Claude sessions.

This layer owns input semantics that are higher level than raw tmux key
delivery:

- submit timing between text and Enter
- multiline paste delivery
- shell-command transitions for ``!`` prompts
- special key dispatch for Escape, arrows, Tab, Space, and Enter

Unsupported keys are rejected explicitly instead of being treated as success.
That keeps the bot honest when the current terminal surface cannot safely
accept a control action.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .runtime_types import InputAction
from .runtime_types import RuntimeCapability, runtime_capability_registry
from .state_schema import DEFAULT_RUNTIME_KIND, normalize_runtime_kind
from .tmux_manager import TmuxManager, tmux_manager

logger = logging.getLogger(__name__)

_SUPPORTED_SPECIAL_KEYS = {
    "Escape",
    "Enter",
    "Left",
    "Right",
    "Up",
    "Down",
    "Space",
    "Tab",
    "C-c",
}

_SPECIAL_KEY_ALIASES = {
    "esc": "Escape",
    "escape": "Escape",
    "enter": "Enter",
    "return": "Enter",
    "left": "Left",
    "right": "Right",
    "up": "Up",
    "down": "Down",
    "space": "Space",
    "spc": "Space",
    "tab": "Tab",
    "c-c": "C-c",
    "^c": "C-c",
}


@dataclass(frozen=True)
class InputDispatch:
    """Structured input request consumed by the runtime input driver."""

    action: InputAction

    @classmethod
    def text(
        cls,
        text: str,
        *,
        runtime_kind: str = DEFAULT_RUNTIME_KIND,
        submit: bool = True,
    ) -> "InputDispatch":
        action_type = "submit_text" if submit else "paste_text"
        return cls(
            InputAction(
                action_type=action_type,
                payload=text,
                submit=submit,
                runtime_kind=runtime_kind,
            )
        )

    @classmethod
    def raw_slash_command(
        cls,
        text: str,
        *,
        runtime_kind: str = DEFAULT_RUNTIME_KIND,
    ) -> "InputDispatch":
        return cls(
            InputAction(
                action_type="raw_slash_command",
                payload=text,
                submit=True,
                runtime_kind=runtime_kind,
                metadata={"shell_transition": False},
            )
        )

    @classmethod
    def special_key(
        cls,
        key: str,
        *,
        runtime_kind: str = DEFAULT_RUNTIME_KIND,
    ) -> "InputDispatch":
        return cls(
            InputAction(
                action_type="special_key",
                payload=key,
                submit=False,
                runtime_kind=runtime_kind,
            )
        )


class RuntimeInputDriver:
    """Send Codex-oriented input to a tmux pane with explicit semantics."""

    def __init__(
        self,
        tmux: TmuxManager | None = None,
        *,
        submit_delay: float = 0.5,
        shell_transition_delay: float = 1.0,
    ) -> None:
        self._tmux = tmux or tmux_manager
        self._submit_delay = submit_delay
        self._shell_transition_delay = shell_transition_delay

    def get_runtime_capability(self, runtime_kind: str = DEFAULT_RUNTIME_KIND) -> RuntimeCapability:
        """Return the capability profile for a runtime kind."""
        return runtime_capability_registry.get(runtime_kind)

    def supports_message_routing_mode(
        self, runtime_kind: str, routing_mode: str
    ) -> bool:
        """Check whether a runtime supports the requested routing mode."""
        return runtime_capability_registry.supports_message_routing_mode(
            runtime_kind, routing_mode
        )

    def supports_interactive_control(self, runtime_kind: str) -> bool:
        """Check whether a runtime supports operator control through tmux."""
        return runtime_capability_registry.supports_interactive_control(runtime_kind)

    def blocked_input_policy(self, runtime_kind: str) -> str:
        """Return the runtime's blocked-input policy."""
        return self.get_runtime_capability(runtime_kind).blocked_input_policy

    async def send_text(
        self,
        window_id: str,
        text: str,
        *,
        runtime_kind: str = DEFAULT_RUNTIME_KIND,
        submit: bool = True,
    ) -> tuple[bool, str]:
        """Send text input, handling submit timing and shell transitions."""
        return await self.send_dispatch(
            window_id,
            InputDispatch.text(text, runtime_kind=runtime_kind, submit=submit).action,
        )

    async def send_raw_slash_command(
        self,
        window_id: str,
        text: str,
        *,
        runtime_kind: str = DEFAULT_RUNTIME_KIND,
    ) -> tuple[bool, str]:
        """Send a slash command verbatim to the runtime prompt."""
        return await self.send_dispatch(
            window_id,
            InputDispatch.raw_slash_command(text, runtime_kind=runtime_kind).action,
        )

    async def send_special_key(
        self,
        window_id: str,
        key: str,
        *,
        runtime_kind: str = DEFAULT_RUNTIME_KIND,
    ) -> tuple[bool, str]:
        """Send a named control key or reject unsupported controls."""
        return await self.send_dispatch(
            window_id,
            InputDispatch.special_key(key, runtime_kind=runtime_kind).action,
        )

    async def send_dispatch(
        self,
        window_id: str,
        action: InputAction,
    ) -> tuple[bool, str]:
        """Dispatch a runtime-neutral input action to the live pane."""
        window = await self._tmux.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"

        runtime_kind = normalize_runtime_kind(action.runtime_kind)
        action_type = action.action_type
        capability = self.get_runtime_capability(runtime_kind)

        if action_type in {"submit_text", "paste_text", "raw_slash_command"}:
            return await self._send_text(
                window.window_id,
                action.payload,
                runtime_kind=runtime_kind,
                submit=action.submit,
                metadata=action.metadata,
            )

        if action_type == "special_key":
            if not capability.interactive_control_supported:
                return (
                    False,
                    f"Interactive control is not supported for {capability.display_name}",
                )
            return await self._send_special_key(
                window.window_id,
                action.payload,
                runtime_kind=runtime_kind,
            )

        if action_type == "noop":
            return False, "Input action is a no-op"

        return False, f"Unsupported input action: {action_type}"

    async def _send_text(
        self,
        window_id: str,
        text: str,
        *,
        runtime_kind: str,
        submit: bool,
        metadata: dict,
    ) -> tuple[bool, str]:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return False, "No text to send"

        # Codex and Claude both treat a leading ! as a shell command prompt.
        # We split the first character so the terminal can transition into
        # shell mode before the rest of the command arrives.
        if text.startswith("!") and metadata.get("shell_transition", True):
            if not await self._tmux.send_literal_text(window_id, "!"):
                return False, "Failed to send shell-command prefix"
            rest = text[1:]
            if rest:
                await asyncio.sleep(self._shell_transition_delay)
                if not await self._tmux.send_literal_text(window_id, rest):
                    return False, "Failed to send shell-command body"
        else:
            if not await self._tmux.send_literal_text(window_id, text):
                return False, "Failed to send text"

        if not submit:
            return True, f"Sent text to {window_id}"

        await asyncio.sleep(self._submit_delay)
        if not await self._tmux.send_enter(window_id):
            return False, "Failed to submit text"
        return True, f"Sent text to {window_id}"

    async def _send_special_key(
        self,
        window_id: str,
        key: str,
        *,
        runtime_kind: str,
    ) -> tuple[bool, str]:
        canonical_key = _SPECIAL_KEY_ALIASES.get(key.lower(), key)
        if canonical_key not in _SUPPORTED_SPECIAL_KEYS:
            logger.debug(
                "Unsupported special key %r for runtime %s", key, runtime_kind
            )
            return False, f"Unsupported control key: {key}"

        if canonical_key == "Enter":
            success = await self._tmux.send_enter(window_id)
        else:
            success = await self._tmux.send_key(window_id, canonical_key)

        if not success:
            return False, f"Failed to send {canonical_key}"

        return True, f"Sent {canonical_key}"


runtime_input_driver = RuntimeInputDriver()
