"""Launcher-side process registration helpers.

Codex windows must be registered by the bot as soon as the tmux window exists,
before any persisted thread id appears in Codex storage. Claude compatibility
continues to rely on the legacy hook path.
"""

from __future__ import annotations

from .runtime_types import runtime_capability_registry


def infer_runtime_kind_from_command(command: str) -> str:
    """Infer the runtime kind launched in a new tmux window."""
    return runtime_capability_registry.infer_runtime_kind_from_command(command)
