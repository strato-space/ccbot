"""Launcher-side process registration helpers.

Codex windows must be registered by the bot as soon as the tmux window exists,
before any persisted thread id appears in Codex storage. Claude compatibility
continues to rely on the legacy hook path.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .state_schema import DEFAULT_RUNTIME_KIND


def infer_runtime_kind_from_command(command: str) -> str:
    """Infer the runtime kind launched in a new tmux window."""
    command = (command or "").strip().casefold()
    if not command:
        return DEFAULT_RUNTIME_KIND
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if "=" in token and not token.startswith(("/", "./", "../")):
            name, _, value = token.partition("=")
            if name and value:
                continue
        executable = Path(token).name
        if "codex" in executable:
            return "codex"
    return DEFAULT_RUNTIME_KIND
