"""Launcher-side process registration helpers.

Claude remains a first-class runtime adapter through its SessionStart hook and
transcript replay evidence. Codex windows are registered by the bot as soon as
the tmux window exists, before any persisted thread id appears in Codex
storage.
"""

from __future__ import annotations

from .runtime_types import runtime_capability_registry


def infer_runtime_kind_from_command(command: str) -> str:
    """Infer the runtime kind launched in a new tmux window."""
    return runtime_capability_registry.infer_runtime_kind_from_command(command)
