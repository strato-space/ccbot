"""Schema helpers for versioned persisted bot state.

The migration work keeps legacy Claude-era files readable while writing the new
versioned envelope that Codex work will rely on.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 2
DEFAULT_RUNTIME_KIND = "claude"
LEGACY_BACKUP_SUFFIX = ".v1.bak"
SESSION_MAP_ENTRIES_KEY = "entries"
SCHEMA_VERSION_KEY = "schema_version"
RUNTIME_KIND_KEY = "runtime_kind"


def legacy_backup_path(path: Path) -> Path:
    """Return the sidecar backup path used during one-time migration."""
    return path.with_name(path.name + LEGACY_BACKUP_SUFFIX)


def ensure_legacy_backup(path: Path) -> Path | None:
    """Create a reversible backup of a legacy state file if needed."""
    if not path.exists():
        return None

    backup_path = legacy_backup_path(path)
    if backup_path.exists():
        return backup_path

    shutil.copy2(path, backup_path)
    return backup_path


def normalize_runtime_kind(runtime_kind: str | None) -> str:
    """Normalize empty runtime labels to the default legacy kind."""
    return runtime_kind or DEFAULT_RUNTIME_KIND


def infer_runtime_kind(runtime_kinds: Iterable[str]) -> str:
    """Collapse a collection of runtime kinds to a stable envelope label."""
    kinds = {kind for kind in runtime_kinds if kind}
    if not kinds:
        return DEFAULT_RUNTIME_KIND
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def build_session_map_payload(
    entries: dict[str, dict[str, Any]],
    *,
    runtime_kind: str,
) -> dict[str, Any]:
    """Wrap session_map entries in a versioned envelope."""
    normalized_entries: dict[str, dict[str, Any]] = {}
    for key, value in entries.items():
        normalized_entry = dict(value)
        normalized_entry["runtime_kind"] = normalize_runtime_kind(
            normalized_entry.get("runtime_kind", runtime_kind)
        )
        normalized_entries[key] = normalized_entry
    return {
        SCHEMA_VERSION_KEY: SCHEMA_VERSION,
        RUNTIME_KIND_KEY: normalize_runtime_kind(runtime_kind),
        SESSION_MAP_ENTRIES_KEY: normalized_entries,
    }


def split_session_map_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], bool]:
    """Extract session_map entries from either the legacy or versioned shape."""
    entries = payload.get(SESSION_MAP_ENTRIES_KEY)
    if isinstance(entries, dict):
        normalized_entries = {
            key: value for key, value in entries.items() if isinstance(value, dict)
        }
        metadata = {
            key: payload[key]
            for key in (SCHEMA_VERSION_KEY, RUNTIME_KIND_KEY)
            if key in payload
        }
        return normalized_entries, metadata, True

    normalized_entries = {
        key: value for key, value in payload.items() if isinstance(value, dict)
    }
    return normalized_entries, {}, False
