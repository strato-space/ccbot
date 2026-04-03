"""Schema helpers for versioned persisted bot state.

The migration work keeps legacy Claude-era files readable while writing the new
versioned envelope that Codex work will rely on.

Topic control now has two distinct axes:
  - topic_policy: whether implicit bind is allowed or an explicit /bind is
    required
  - binding_state: whether a topic currently has no binding, is in a bind flow,
    or is bound
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 4
DEFAULT_RUNTIME_KIND = "claude"
LEGACY_BACKUP_SUFFIX = ".v1.bak"
SESSION_MAP_ENTRIES_KEY = "entries"
SCHEMA_VERSION_KEY = "schema_version"
RUNTIME_KIND_KEY = "runtime_kind"
TOPIC_POLICIES_KEY = "topic_policies"
TOPIC_BINDING_STATES_KEY = "topic_binding_states"
TOPIC_BIND_FLOW_VERSIONS_KEY = "topic_bind_flow_versions"
TOPIC_BIND_FLOW_NONCES_KEY = "topic_bind_flow_nonces"
TOPIC_POLICY_IMPLICIT_BIND_ALLOWED = "implicit_bind_allowed"
TOPIC_POLICY_MANUAL_BIND_REQUIRED = "manual_bind_required"
BINDING_STATE_NONE = "none"
BINDING_STATE_BIND_FLOW = "bind_flow"
BINDING_STATE_BOUND = "bound"
DEFAULT_TOPIC_BIND_FLOW_VERSION = 0


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


def normalize_topic_policy(topic_policy: str | None) -> str:
    """Normalize topic policy labels to the supported policy vocabulary."""
    if topic_policy == TOPIC_POLICY_MANUAL_BIND_REQUIRED:
        return TOPIC_POLICY_MANUAL_BIND_REQUIRED
    return TOPIC_POLICY_IMPLICIT_BIND_ALLOWED


def normalize_binding_state(binding_state: str | None) -> str:
    """Normalize topic binding state labels to the supported state vocabulary."""
    if binding_state == BINDING_STATE_BIND_FLOW:
        return BINDING_STATE_BIND_FLOW
    if binding_state == BINDING_STATE_BOUND:
        return BINDING_STATE_BOUND
    return BINDING_STATE_NONE


def normalize_bind_flow_version(bind_flow_version: Any) -> int:
    """Normalize bind-flow version counters to a non-negative integer."""
    try:
        version = int(bind_flow_version or 0)
    except (TypeError, ValueError):
        return DEFAULT_TOPIC_BIND_FLOW_VERSION
    return max(version, DEFAULT_TOPIC_BIND_FLOW_VERSION)


def normalize_bind_flow_nonce(bind_flow_nonce: Any) -> str:
    """Normalize bind-flow nonces to a stable string representation."""
    return str(bind_flow_nonce or "")


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
