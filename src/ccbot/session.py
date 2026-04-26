"""Core runtime state hub for bindings, live processes, and thread locators.

This module still interoperates with the current Claude-shaped storage, but it
now exposes runtime-neutral nouns so later Codex work can distinguish:

- topic binding: Telegram topic -> tmux window
- live process descriptor: current process metadata for a window
- thread locator: persisted conversation identity and replay evidence path

Legacy method names remain as compatibility wrappers while the rest of the bot
is migrated task by task.
"""

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import aiofiles

from .config import config
from .codex_rollout import CodexRolloutNormalizer
from .codex_threads import (
    CodexThreadCandidate,
    CodexThreadCatalog,
    CodexThreadResolution,
)
from .fast_agent_sessions import (
    FastAgentSessionCatalog,
    FastAgentSessionResolution,
)
from .input_driver import runtime_input_driver
from .runtime_types import (
    InputAction,
    LiveProcessDescriptor,
    RuntimeCapability,
    ThreadLocator,
    TopicBinding,
    runtime_capability_registry,
)
from .state_schema import (
    BINDING_STATE_BIND_FLOW,
    BINDING_STATE_BOUND,
    BINDING_STATE_NONE,
    EXTERNAL_TOPIC_BINDINGS_KEY,
    TOPIC_BIND_FLOW_NONCES_KEY,
    TOPIC_BIND_FLOW_VERSIONS_KEY,
    TOPIC_BINDING_STATES_KEY,
    TOPIC_POLICIES_KEY,
    TOPIC_POLICY_IMPLICIT_BIND_ALLOWED,
    TOPIC_POLICY_MANUAL_BIND_REQUIRED,
    build_session_map_payload,
    ensure_legacy_backup,
    infer_runtime_kind,
    normalize_bind_flow_nonce,
    normalize_bind_flow_version,
    normalize_binding_state,
    normalize_topic_policy,
    split_session_map_payload,
)
from .terminal_parser import classify_input_surface
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

BLOCKED_PROMPT_SEND_MESSAGE = "Input blocked by a visible prompt in the terminal"

WindowState = LiveProcessDescriptor
ClaudeSession = ThreadLocator

EXTERNAL_BINDING_PREFIX = "external"
EXTERNAL_BINDING_WINDOW_PREFIX = f"{EXTERNAL_BINDING_PREFIX}:"
EXTERNAL_BINDING_READ_ONLY_MESSAGE = (
    "Topic is bound to an external persisted thread in read-only mode. "
    "Attach a live tmux window via /bind or /resume to inject input."
)

SURFACE_BINDINGS_KEY = "surface_bindings"
EXTERNAL_SURFACE_BINDINGS_KEY = "external_surface_bindings"
SURFACE_POLICIES_KEY = "surface_policies"
SURFACE_BINDING_STATES_KEY = "surface_binding_states"
SURFACE_BIND_FLOW_VERSIONS_KEY = "surface_bind_flow_versions"
SURFACE_BIND_FLOW_NONCES_KEY = "surface_bind_flow_nonces"
SURFACE_PENDING_SLOTS_KEY = "surface_pending_slots"
TOPIC_SURFACE_PREFIX = "t:"
CHAT_SURFACE_PREFIX = "c:"
PENDING_SLOT_STATUS_PENDING = "pending"
PENDING_SLOT_STATUS_CONSUMED = "consumed"
SURFACE_PENDING_STATUS_PENDING = PENDING_SLOT_STATUS_PENDING
SURFACE_PENDING_STATUS_CONSUMED = PENDING_SLOT_STATUS_CONSUMED


@dataclass
class SessionManager:
    """Manages persisted bindings, process descriptors, and thread locators.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> LiveProcessDescriptor
    user_window_offsets: user_id -> {window_id -> byte_offset}
    surface_bindings: user_id -> {surface_key -> window_id}
    external_surface_bindings: user_id -> {surface_key -> external-thread metadata}
    surface_policies: user_id -> {surface_key -> topic policy}
    surface_binding_states: user_id -> {surface_key -> binding state}
    surface_bind_flow_versions/nonces: user_id -> {surface_key -> bind-flow credentials}
    surface_pending_slots: user_id -> {surface_key -> pending input metadata}
    thread_bindings/external_topic_bindings/topic_*: compatibility-only topic mirrors
    window_display_names: window_id -> window_name (for display)
    group_chat_ids: "user_id:thread_id" -> group chat_id (for supergroup routing)
    """

    window_states: dict[str, LiveProcessDescriptor] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    surface_bindings: dict[int, dict[str, str]] = field(default_factory=dict)
    external_surface_bindings: dict[int, dict[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    surface_policies: dict[int, dict[str, str]] = field(default_factory=dict)
    surface_binding_states: dict[int, dict[str, str]] = field(default_factory=dict)
    surface_bind_flow_versions: dict[int, dict[str, int]] = field(default_factory=dict)
    surface_bind_flow_nonces: dict[int, dict[str, str]] = field(default_factory=dict)
    surface_pending_slots: dict[int, dict[str, dict[str, Any]]] = field(
        default_factory=dict
    )
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    external_topic_bindings: dict[int, dict[int, dict[str, Any]]] = field(
        default_factory=dict
    )
    topic_policies: dict[int, dict[int, str]] = field(default_factory=dict)
    topic_binding_states: dict[int, dict[int, str]] = field(default_factory=dict)
    topic_bind_flow_versions: dict[int, dict[int, int]] = field(default_factory=dict)
    topic_bind_flow_nonces: dict[int, dict[int, str]] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # "user_id:thread_id" -> group chat_id (for supergroup forum topic routing)
    # IMPORTANT: This mapping is essential for supergroup/forum topic support.
    # Telegram Bot API requires group chat_id (negative number like -100xxx)
    # as the chat_id parameter when sending messages to forum topics.
    # Using user_id as chat_id will fail with "Message thread not found".
    # See: https://core.telegram.org/bots/api#sendmessage
    # History: originally added in 5afc111, erroneously removed in 26cb81f,
    # restored in PR #23.
    group_chat_ids: dict[str, int] = field(default_factory=dict)
    codex_thread_catalog: CodexThreadCatalog | None = field(default=None, repr=False)
    fast_agent_session_catalog: FastAgentSessionCatalog | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if self.codex_thread_catalog is None:
            self.codex_thread_catalog = CodexThreadCatalog()
        if self.fast_agent_session_catalog is None:
            self.fast_agent_session_catalog = FastAgentSessionCatalog()
        self._load_state()

    def _save_state(self) -> None:
        self._sync_legacy_topic_views_from_surface()
        state: dict[str, Any] = {
            "schema_version": config.state_schema_version,
            "runtime_kind": infer_runtime_kind(
                window_state.runtime_kind for window_state in self.window_states.values()
            ),
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            SURFACE_BINDINGS_KEY: {
                str(uid): {surface_key: wid for surface_key, wid in bindings.items()}
                for uid, bindings in self.surface_bindings.items()
            },
            EXTERNAL_SURFACE_BINDINGS_KEY: {
                str(uid): {
                    surface_key: dict(meta)
                    for surface_key, meta in bindings.items()
                    if isinstance(meta, dict)
                }
                for uid, bindings in self.external_surface_bindings.items()
            },
            SURFACE_POLICIES_KEY: {
                str(uid): {surface_key: policy for surface_key, policy in policies.items()}
                for uid, policies in self.surface_policies.items()
            },
            SURFACE_BINDING_STATES_KEY: {
                str(uid): {surface_key: binding_state for surface_key, binding_state in states.items()}
                for uid, states in self.surface_binding_states.items()
            },
            SURFACE_BIND_FLOW_VERSIONS_KEY: {
                str(uid): {surface_key: version for surface_key, version in versions.items()}
                for uid, versions in self.surface_bind_flow_versions.items()
            },
            SURFACE_BIND_FLOW_NONCES_KEY: {
                str(uid): {surface_key: nonce for surface_key, nonce in nonces.items()}
                for uid, nonces in self.surface_bind_flow_nonces.items()
            },
            SURFACE_PENDING_SLOTS_KEY: {
                str(uid): {
                    surface_key: normalized_pending
                    for surface_key, pending in pending_slots.items()
                    if (
                        normalized_pending := self._normalize_pending_slot_record(pending)
                    )
                    is not None
                }
                for uid, pending_slots in self.surface_pending_slots.items()
            },
            "thread_bindings": {
                str(uid): {str(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in self.thread_bindings.items()
            },
            EXTERNAL_TOPIC_BINDINGS_KEY: {
                str(uid): {str(tid): dict(meta) for tid, meta in bindings.items()}
                for uid, bindings in self.external_topic_bindings.items()
            },
            "topic_policies": {
                str(uid): {str(tid): policy for tid, policy in policies.items()}
                for uid, policies in self.topic_policies.items()
            },
            "topic_binding_states": {
                str(uid): {str(tid): state for tid, state in states.items()}
                for uid, states in self.topic_binding_states.items()
            },
            "topic_bind_flow_versions": {
                str(uid): {str(tid): version for tid, version in versions.items()}
                for uid, versions in self.topic_bind_flow_versions.items()
            },
            "topic_bind_flow_nonces": {
                str(uid): {str(tid): nonce for tid, nonce in nonces.items()}
                for uid, nonces in self.topic_bind_flow_nonces.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
        }
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    @staticmethod
    def _is_window_id(key: str) -> bool:
        """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    @staticmethod
    def make_external_binding_window_id(runtime_kind: str, source_thread_id: str) -> str:
        """Build a synthetic window key for external non-tmux binds."""
        safe_runtime = (runtime_kind or "codex").strip() or "codex"
        safe_thread = source_thread_id.strip()
        return f"{EXTERNAL_BINDING_WINDOW_PREFIX}{safe_runtime}:{safe_thread}"

    @staticmethod
    def parse_external_binding_window_id(window_id: str) -> tuple[str, str] | None:
        """Parse a synthetic external binding key into runtime/thread ids."""
        if not window_id.startswith(EXTERNAL_BINDING_WINDOW_PREFIX):
            return None
        payload = window_id[len(EXTERNAL_BINDING_WINDOW_PREFIX) :]
        runtime, sep, thread_id = payload.partition(":")
        if not sep or not runtime.strip() or not thread_id.strip():
            return None
        return runtime.strip(), thread_id.strip()

    @classmethod
    def is_external_binding_window_id(cls, window_id: str) -> bool:
        """Return True when a binding key targets external non-tmux replay."""
        return cls.parse_external_binding_window_id(window_id) is not None

    @classmethod
    def _is_binding_id(cls, key: str) -> bool:
        """Return True for supported persisted topic binding ids."""
        return cls._is_window_id(key) or cls.is_external_binding_window_id(key)

    @staticmethod
    def make_surface_key(
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str:
        """Build the canonical persisted key for a control surface."""
        if thread_id is not None and chat_id is not None:
            raise ValueError("surface key requires exactly one of thread_id or chat_id")
        if thread_id is not None:
            return f"{TOPIC_SURFACE_PREFIX}{int(thread_id)}"
        if chat_id is not None:
            return f"{CHAT_SURFACE_PREFIX}{int(chat_id)}"
        raise ValueError("surface key requires thread_id or chat_id")

    @staticmethod
    def _parse_surface_key(surface_key: str) -> tuple[str, int] | None:
        """Parse a persisted surface key into (kind, numeric id)."""
        if surface_key.startswith(TOPIC_SURFACE_PREFIX):
            payload = surface_key[len(TOPIC_SURFACE_PREFIX) :]
            kind = "topic"
        elif surface_key.startswith(CHAT_SURFACE_PREFIX):
            payload = surface_key[len(CHAT_SURFACE_PREFIX) :]
            kind = "chat"
        else:
            return None
        try:
            return kind, int(payload)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _topic_thread_id_from_surface_key(cls, surface_key: str) -> int | None:
        parsed = cls._parse_surface_key(surface_key)
        if parsed is None or parsed[0] != "topic":
            return None
        return parsed[1]

    def _resolve_surface_key(
        self,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str:
        """Resolve either a direct surface key or thread/chat coordinates."""
        if surface_key is not None:
            parsed = self._parse_surface_key(surface_key)
            if parsed is None:
                raise ValueError(f"invalid surface key: {surface_key!r}")
            return self.make_surface_key(
                thread_id=parsed[1] if parsed[0] == "topic" else None,
                chat_id=parsed[1] if parsed[0] == "chat" else None,
            )
        return self.make_surface_key(thread_id=thread_id, chat_id=chat_id)

    @classmethod
    def _normalize_surface_map(cls, raw: Any) -> dict[int, dict[str, Any]]:
        """Normalize nested user_id -> surface_key payloads from JSON state."""
        normalized: dict[int, dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return normalized
        for uid, payload in raw.items():
            try:
                user_id = int(uid)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            normalized_payload: dict[str, Any] = {}
            for key, value in payload.items():
                normalized_key = cls._normalize_surface_key(key)
                if normalized_key is None:
                    continue
                normalized_payload[normalized_key] = value
            if normalized_payload:
                normalized[user_id] = normalized_payload
        return normalized

    @staticmethod
    def _prune_empty_surface_entry(store: dict[int, dict[str, Any]], user_id: int) -> None:
        if user_id in store and not store[user_id]:
            store.pop(user_id, None)

    @classmethod
    def _normalize_surface_key(cls, surface_key: Any) -> str | None:
        raw_key = str(surface_key or "").strip()
        parsed = cls._parse_surface_key(raw_key)
        if parsed is None:
            return None
        kind, numeric_id = parsed
        if kind == "topic":
            return cls.make_surface_key(thread_id=numeric_id)
        return cls.make_surface_key(chat_id=numeric_id)

    @staticmethod
    def _normalize_pending_slot_record(record: Any) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            return None
        text = str(record.get("text") or "")
        if not text:
            return None
        try:
            revision = max(int(record.get("revision") or 0), 1)
        except (TypeError, ValueError):
            revision = 1
        consumed_by_activation_id = str(record.get("consumed_by_activation_id") or "")
        status = str(record.get("status") or PENDING_SLOT_STATUS_PENDING)
        if status != PENDING_SLOT_STATUS_CONSUMED:
            status = PENDING_SLOT_STATUS_PENDING
            consumed_by_activation_id = ""
        return {
            "text": text,
            "revision": revision,
            "status": status,
            "consumed_by_activation_id": consumed_by_activation_id,
        }

    @classmethod
    def _normalize_surface_pending_slots(
        cls,
        raw: Any,
    ) -> dict[int, dict[str, dict[str, Any]]]:
        normalized: dict[int, dict[str, dict[str, Any]]] = {}
        for user_id, payload in cls._normalize_surface_map(raw).items():
            normalized_payload: dict[str, dict[str, Any]] = {}
            for surface_key, record in payload.items():
                normalized_record = cls._normalize_pending_slot_record(record)
                if normalized_record is None:
                    continue
                normalized_payload[surface_key] = normalized_record
            if normalized_payload:
                normalized[user_id] = normalized_payload
        return normalized

    def _sync_legacy_topic_views_from_surface(self) -> bool:
        """Rebuild legacy topic-keyed mirrors from canonical surface state."""
        thread_bindings: dict[int, dict[int, str]] = {}
        external_topic_bindings: dict[int, dict[int, dict[str, Any]]] = {}
        topic_policies: dict[int, dict[int, str]] = {}
        topic_binding_states: dict[int, dict[int, str]] = {}
        topic_bind_flow_versions: dict[int, dict[int, int]] = {}
        topic_bind_flow_nonces: dict[int, dict[int, str]] = {}

        for user_id, bindings in self.surface_bindings.items():
            for surface_key, window_id in bindings.items():
                thread_id = self._topic_thread_id_from_surface_key(surface_key)
                if thread_id is None:
                    continue
                thread_bindings.setdefault(user_id, {})[thread_id] = window_id

        for user_id, bindings in self.external_surface_bindings.items():
            for surface_key, metadata in bindings.items():
                thread_id = self._topic_thread_id_from_surface_key(surface_key)
                if thread_id is None or not isinstance(metadata, dict):
                    continue
                external_topic_bindings.setdefault(user_id, {})[thread_id] = dict(metadata)

        for user_id, policies in self.surface_policies.items():
            for surface_key, policy in policies.items():
                thread_id = self._topic_thread_id_from_surface_key(surface_key)
                if thread_id is None:
                    continue
                topic_policies.setdefault(user_id, {})[thread_id] = normalize_topic_policy(policy)

        for user_id, states in self.surface_binding_states.items():
            for surface_key, binding_state in states.items():
                thread_id = self._topic_thread_id_from_surface_key(surface_key)
                if thread_id is None:
                    continue
                topic_binding_states.setdefault(user_id, {})[thread_id] = normalize_binding_state(binding_state)

        for user_id, versions in self.surface_bind_flow_versions.items():
            for surface_key, version in versions.items():
                thread_id = self._topic_thread_id_from_surface_key(surface_key)
                if thread_id is None:
                    continue
                topic_bind_flow_versions.setdefault(user_id, {})[thread_id] = normalize_bind_flow_version(version)

        for user_id, nonces in self.surface_bind_flow_nonces.items():
            for surface_key, nonce in nonces.items():
                thread_id = self._topic_thread_id_from_surface_key(surface_key)
                if thread_id is None:
                    continue
                topic_bind_flow_nonces.setdefault(user_id, {})[thread_id] = normalize_bind_flow_nonce(nonce)

        changed = (
            self.thread_bindings != thread_bindings
            or self.external_topic_bindings != external_topic_bindings
            or self.topic_policies != topic_policies
            or self.topic_binding_states != topic_binding_states
            or self.topic_bind_flow_versions != topic_bind_flow_versions
            or self.topic_bind_flow_nonces != topic_bind_flow_nonces
        )

        self.thread_bindings = thread_bindings
        self.external_topic_bindings = external_topic_bindings
        self.topic_policies = topic_policies
        self.topic_binding_states = topic_binding_states
        self.topic_bind_flow_versions = topic_bind_flow_versions
        self.topic_bind_flow_nonces = topic_bind_flow_nonces
        return changed

    def _merge_legacy_topic_state_into_surface(self, *, overwrite: bool = False) -> bool:
        """Merge legacy topic-keyed state into canonical surface maps."""
        changed = False

        for user_id, bindings in self.thread_bindings.items():
            target = self.surface_bindings.setdefault(user_id, {})
            for thread_id, window_id in bindings.items():
                surface_key = self.make_surface_key(thread_id=thread_id)
                if overwrite or surface_key not in target:
                    if target.get(surface_key) != window_id:
                        target[surface_key] = window_id
                        changed = True

        for user_id, bindings in self.external_topic_bindings.items():
            target = self.external_surface_bindings.setdefault(user_id, {})
            for thread_id, metadata in bindings.items():
                surface_key = self.make_surface_key(thread_id=thread_id)
                normalized_metadata = dict(metadata) if isinstance(metadata, dict) else {}
                if overwrite or surface_key not in target:
                    if target.get(surface_key) != normalized_metadata:
                        target[surface_key] = normalized_metadata
                        changed = True

        for user_id, policies in self.topic_policies.items():
            target = self.surface_policies.setdefault(user_id, {})
            for thread_id, policy in policies.items():
                surface_key = self.make_surface_key(thread_id=thread_id)
                normalized = normalize_topic_policy(policy)
                if overwrite or surface_key not in target:
                    if target.get(surface_key) != normalized:
                        target[surface_key] = normalized
                        changed = True

        for user_id, states in self.topic_binding_states.items():
            target = self.surface_binding_states.setdefault(user_id, {})
            for thread_id, binding_state in states.items():
                surface_key = self.make_surface_key(thread_id=thread_id)
                normalized = normalize_binding_state(binding_state)
                if overwrite or surface_key not in target:
                    if target.get(surface_key) != normalized:
                        target[surface_key] = normalized
                        changed = True

        for user_id, versions in self.topic_bind_flow_versions.items():
            target = self.surface_bind_flow_versions.setdefault(user_id, {})
            for thread_id, version in versions.items():
                surface_key = self.make_surface_key(thread_id=thread_id)
                normalized = normalize_bind_flow_version(version)
                if overwrite or surface_key not in target:
                    if target.get(surface_key) != normalized:
                        target[surface_key] = normalized
                        changed = True

        for user_id, nonces in self.topic_bind_flow_nonces.items():
            target = self.surface_bind_flow_nonces.setdefault(user_id, {})
            for thread_id, nonce in nonces.items():
                surface_key = self.make_surface_key(thread_id=thread_id)
                normalized = normalize_bind_flow_nonce(nonce)
                if overwrite or surface_key not in target:
                    if target.get(surface_key) != normalized:
                        target[surface_key] = normalized
                        changed = True

        for user_id, bindings in self.surface_bindings.items():
            policy_map = self.surface_policies.setdefault(user_id, {})
            state_map = self.surface_binding_states.setdefault(user_id, {})
            for surface_key in bindings:
                if surface_key not in policy_map:
                    policy_map[surface_key] = TOPIC_POLICY_IMPLICIT_BIND_ALLOWED
                    changed = True
                if surface_key not in state_map:
                    state_map[surface_key] = BINDING_STATE_BOUND
                    changed = True

        for user_id, states in self.surface_binding_states.items():
            version_map = self.surface_bind_flow_versions.setdefault(user_id, {})
            nonce_map = self.surface_bind_flow_nonces.setdefault(user_id, {})
            for surface_key, binding_state in states.items():
                if normalize_binding_state(binding_state) != BINDING_STATE_BIND_FLOW:
                    continue
                version = normalize_bind_flow_version(version_map.get(surface_key))
                nonce = normalize_bind_flow_nonce(nonce_map.get(surface_key))
                if version <= 0 or not nonce:
                    version_map[surface_key] = version + 1
                    nonce_map[surface_key] = secrets.token_urlsafe(16)
                    changed = True

        if self._sync_legacy_topic_views_from_surface():
            changed = True
        return changed

    def _load_state(self) -> None:
        """Load state synchronously during initialization.

        Detects old-format state (window_name keys without '@' prefix) and
        marks for migration on next startup re-resolution.
        """
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                migrate_legacy = int(state.get("schema_version", 0) or 0) < (
                    config.state_schema_version
                )
                if migrate_legacy:
                    ensure_legacy_backup(config.state_file)
                self.window_states = {
                    k: LiveProcessDescriptor.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
                self.user_window_offsets = {
                    int(uid): offsets
                    for uid, offsets in state.get("user_window_offsets", {}).items()
                }
                self.thread_bindings = {
                    int(uid): {int(tid): wid for tid, wid in bindings.items()}
                    for uid, bindings in state.get("thread_bindings", {}).items()
                }
                self.external_topic_bindings = {
                    int(uid): {
                        int(tid): dict(meta)
                        for tid, meta in bindings.items()
                        if isinstance(meta, dict)
                    }
                    for uid, bindings in state.get(
                        EXTERNAL_TOPIC_BINDINGS_KEY, {}
                    ).items()
                    if isinstance(bindings, dict)
                }
                self.topic_policies = {
                    int(uid): {
                        int(tid): normalize_topic_policy(policy)
                        for tid, policy in policies.items()
                    }
                    for uid, policies in state.get(TOPIC_POLICIES_KEY, {}).items()
                }
                self.topic_binding_states = {
                    int(uid): {
                        int(tid): normalize_binding_state(binding_state)
                        for tid, binding_state in states.items()
                    }
                    for uid, states in state.get(TOPIC_BINDING_STATES_KEY, {}).items()
                }
                self.topic_bind_flow_versions = {
                    int(uid): {
                        int(tid): normalize_bind_flow_version(version)
                        for tid, version in versions.items()
                    }
                    for uid, versions in state.get(
                        TOPIC_BIND_FLOW_VERSIONS_KEY, {}
                    ).items()
                }
                self.topic_bind_flow_nonces = {
                    int(uid): {
                        int(tid): normalize_bind_flow_nonce(nonce)
                        for tid, nonce in nonces.items()
                    }
                    for uid, nonces in state.get(TOPIC_BIND_FLOW_NONCES_KEY, {}).items()
                }
                self.surface_bindings = {
                    user_id: {
                        surface_key: str(window_id)
                        for surface_key, window_id in bindings.items()
                    }
                    for user_id, bindings in self._normalize_surface_map(
                        state.get(SURFACE_BINDINGS_KEY, {})
                    ).items()
                }
                self.external_surface_bindings = {
                    user_id: {
                        surface_key: dict(metadata)
                        for surface_key, metadata in bindings.items()
                        if isinstance(metadata, dict)
                    }
                    for user_id, bindings in self._normalize_surface_map(
                        state.get(EXTERNAL_SURFACE_BINDINGS_KEY, {})
                    ).items()
                }
                self.surface_policies = {
                    user_id: {
                        surface_key: normalize_topic_policy(policy)
                        for surface_key, policy in policies.items()
                    }
                    for user_id, policies in self._normalize_surface_map(
                        state.get(SURFACE_POLICIES_KEY, {})
                    ).items()
                }
                self.surface_binding_states = {
                    user_id: {
                        surface_key: normalize_binding_state(binding_state)
                        for surface_key, binding_state in states.items()
                    }
                    for user_id, states in self._normalize_surface_map(
                        state.get(SURFACE_BINDING_STATES_KEY, {})
                    ).items()
                }
                self.surface_bind_flow_versions = {
                    user_id: {
                        surface_key: normalize_bind_flow_version(version)
                        for surface_key, version in versions.items()
                    }
                    for user_id, versions in self._normalize_surface_map(
                        state.get(SURFACE_BIND_FLOW_VERSIONS_KEY, {})
                    ).items()
                }
                self.surface_bind_flow_nonces = {
                    user_id: {
                        surface_key: normalize_bind_flow_nonce(nonce)
                        for surface_key, nonce in nonces.items()
                    }
                    for user_id, nonces in self._normalize_surface_map(
                        state.get(SURFACE_BIND_FLOW_NONCES_KEY, {})
                    ).items()
                }
                self.surface_pending_slots = self._normalize_surface_pending_slots(
                    state.get(SURFACE_PENDING_SLOTS_KEY, {})
                )
                self.window_display_names = state.get("window_display_names", {})
                self.group_chat_ids = {
                    k: int(v) for k, v in state.get("group_chat_ids", {}).items()
                }

                # Detect old format: keys that don't look like window IDs
                needs_migration = False
                for k in self.window_states:
                    if not self._is_binding_id(k):
                        needs_migration = True
                        break
                if not needs_migration:
                    for bindings in self.thread_bindings.values():
                        for wid in bindings.values():
                            if not self._is_binding_id(wid):
                                needs_migration = True
                                break
                        if needs_migration:
                            break
                if not needs_migration:
                    for bindings in self.surface_bindings.values():
                        for wid in bindings.values():
                            if not self._is_binding_id(wid):
                                needs_migration = True
                                break
                        if needs_migration:
                            break

                if needs_migration:
                    logger.info(
                        "Detected old-format state (window_name keys), "
                        "will re-resolve on startup"
                    )
                    migrate_legacy = True

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to load state: %s", e)
                self.window_states = {}
                self.user_window_offsets = {}
                self.thread_bindings = {}
                self.external_topic_bindings = {}
                self.topic_policies = {}
                self.topic_binding_states = {}
                self.topic_bind_flow_versions = {}
                self.topic_bind_flow_nonces = {}
                self.surface_bindings = {}
                self.external_surface_bindings = {}
                self.surface_policies = {}
                self.surface_binding_states = {}
                self.surface_bind_flow_versions = {}
                self.surface_bind_flow_nonces = {}
                self.surface_pending_slots = {}
                self.window_display_names = {}
                self.group_chat_ids = {}
                migrate_legacy = False
            else:
                if TOPIC_POLICIES_KEY not in state:
                    self.topic_policies = {}
                    migrate_legacy = True
                if TOPIC_BINDING_STATES_KEY not in state:
                    self.topic_binding_states = {}
                    migrate_legacy = True
                if TOPIC_BIND_FLOW_VERSIONS_KEY not in state:
                    self.topic_bind_flow_versions = {}
                    migrate_legacy = True
                if TOPIC_BIND_FLOW_NONCES_KEY not in state:
                    self.topic_bind_flow_nonces = {}
                    migrate_legacy = True
                if EXTERNAL_TOPIC_BINDINGS_KEY not in state:
                    self.external_topic_bindings = {}
                    migrate_legacy = True
                if SURFACE_BINDINGS_KEY not in state:
                    self.surface_bindings = {}
                    migrate_legacy = True
                if EXTERNAL_SURFACE_BINDINGS_KEY not in state:
                    self.external_surface_bindings = {}
                    migrate_legacy = True
                if SURFACE_POLICIES_KEY not in state:
                    self.surface_policies = {}
                    migrate_legacy = True
                if SURFACE_BINDING_STATES_KEY not in state:
                    self.surface_binding_states = {}
                    migrate_legacy = True
                if SURFACE_BIND_FLOW_VERSIONS_KEY not in state:
                    self.surface_bind_flow_versions = {}
                    migrate_legacy = True
                if SURFACE_BIND_FLOW_NONCES_KEY not in state:
                    self.surface_bind_flow_nonces = {}
                    migrate_legacy = True
                if SURFACE_PENDING_SLOTS_KEY not in state:
                    self.surface_pending_slots = {}
                    migrate_legacy = True

                if self.topic_policies:
                    for uid, policies in self.topic_policies.items():
                        for tid, policy in list(policies.items()):
                            policies[tid] = normalize_topic_policy(policy)
                if self.topic_binding_states:
                    for uid, states in self.topic_binding_states.items():
                        for tid, binding_state in list(states.items()):
                            states[tid] = normalize_binding_state(binding_state)
                if self.topic_bind_flow_versions:
                    for uid, versions in self.topic_bind_flow_versions.items():
                        for tid, version in list(versions.items()):
                            versions[tid] = normalize_bind_flow_version(version)
                if self.topic_bind_flow_nonces:
                    for uid, nonces in self.topic_bind_flow_nonces.items():
                        for tid, nonce in list(nonces.items()):
                            nonces[tid] = normalize_bind_flow_nonce(nonce)

                migrated = self._merge_legacy_topic_state_into_surface(overwrite=False)
                if migrated:
                    migrate_legacy = True
                self._sync_legacy_topic_views_from_surface()

                if migrate_legacy:
                    self._save_state()

    def _session_map_entries(self) -> tuple[dict[str, dict[str, Any]], bool, bool]:
        """Load session_map entries from either the legacy or versioned shape.

        Returns (entries, versioned, loaded_ok). Invalid JSON is a load failure
        so callers do not accidentally overwrite a corrupt file.
        """
        if not config.session_map_file.exists():
            return {}, False, False

        try:
            raw = json.loads(config.session_map_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}, False, False

        entries, _, versioned = split_session_map_payload(raw)
        if not versioned:
            ensure_legacy_backup(config.session_map_file)
        return entries, versioned, True

    def _write_session_map_entries(self, entries: dict[str, dict[str, Any]]) -> None:
        """Persist session_map entries in the versioned envelope."""
        payload = build_session_map_payload(
            entries,
            runtime_kind=infer_runtime_kind(
                entry.get("runtime_kind", config.default_runtime_kind)
                for entry in entries.values()
            ),
        )
        atomic_write_json(config.session_map_file, payload)

    async def resolve_stale_ids(self) -> None:
        """Re-resolve persisted window IDs against live tmux windows.

        Called on startup. Handles two cases:
        1. Old-format migration: window_name keys → window_id keys
        2. Stale IDs: window_id no longer exists but display name matches a live window

        Builds {window_name: window_id} from live windows, then remaps or drops entries.
        """
        windows = await tmux_manager.list_windows()
        live_by_name: dict[str, str] = {}  # window_name -> window_id
        live_ids: set[str] = set()
        for w in windows:
            live_by_name[w.window_name] = w.window_id
            live_ids.add(w.window_id)

        changed = False

        # --- Migrate window_states ---
        new_window_states: dict[str, LiveProcessDescriptor] = {}
        for key, ws in self.window_states.items():
            if self._is_window_id(key):
                if key in live_ids:
                    new_window_states[key] = ws
                else:
                    # Stale ID — try re-resolve by display name
                    display = self.window_display_names.get(key, ws.window_name or key)
                    new_id = live_by_name.get(display)
                    if new_id:
                        logger.info(
                            "Re-resolved stale window_id %s -> %s (name=%s)",
                            key,
                            new_id,
                            display,
                        )
                        new_window_states[new_id] = ws
                        ws.window_name = display
                        self.window_display_names[new_id] = display
                        self.window_display_names.pop(key, None)
                        changed = True
                    else:
                        logger.info(
                            "Dropping stale window_state: %s (name=%s)", key, display
                        )
                        changed = True
            elif self.is_external_binding_window_id(key):
                new_window_states[key] = ws
            else:
                # Old format: key is window_name
                new_id = live_by_name.get(key)
                if new_id:
                    logger.info("Migrating window_state key %s -> %s", key, new_id)
                    ws.window_name = key
                    new_window_states[new_id] = ws
                    self.window_display_names[new_id] = key
                    changed = True
                else:
                    logger.info(
                        "Dropping old-format window_state: %s (no live window)", key
                    )
                    changed = True
        self.window_states = new_window_states

        # --- Migrate thread_bindings ---
        dropped_bindings: list[tuple[int, int]] = []
        for uid, bindings in self.thread_bindings.items():
            new_bindings: dict[int, str] = {}
            for tid, val in bindings.items():
                if self._is_window_id(val):
                    if val in live_ids:
                        new_bindings[tid] = val
                    else:
                        display = self.window_display_names.get(val, val)
                        new_id = live_by_name.get(display)
                        if new_id:
                            logger.info(
                                "Re-resolved thread binding %s -> %s (name=%s)",
                                val,
                                new_id,
                                display,
                            )
                            new_bindings[tid] = new_id
                            self.window_display_names[new_id] = display
                            changed = True
                            self.topic_binding_states.setdefault(uid, {})[tid] = (
                                BINDING_STATE_BOUND
                            )
                        else:
                            logger.info(
                                "Dropping stale thread binding: user=%d, thread=%d, wid=%s",
                                uid,
                                tid,
                                val,
                            )
                            dropped_bindings.append((uid, tid))
                            changed = True
                elif self.is_external_binding_window_id(val):
                    new_bindings[tid] = val
                    parsed = self.parse_external_binding_window_id(val)
                    runtime_kind = (
                        (parsed[0] if parsed is not None else config.default_runtime_kind)
                        or config.default_runtime_kind
                    )
                    source_thread_id = parsed[1] if parsed is not None else ""
                    external = self.external_topic_bindings.setdefault(uid, {})
                    meta = external.get(tid)
                    if not isinstance(meta, dict):
                        external[tid] = {
                            "runtime_kind": runtime_kind,
                            "source_thread_id": source_thread_id,
                            "summary": "",
                            "cwd": "",
                            "file_path": "",
                            "read_only": True,
                        }
                        changed = True
                    else:
                        if not str(meta.get("runtime_kind") or "").strip():
                            meta["runtime_kind"] = runtime_kind
                            changed = True
                        if (
                            not str(meta.get("source_thread_id") or "").strip()
                            and source_thread_id
                        ):
                            meta["source_thread_id"] = source_thread_id
                            changed = True
                        if "read_only" not in meta:
                            meta["read_only"] = True
                            changed = True
                    self.topic_binding_states.setdefault(uid, {})[tid] = (
                        BINDING_STATE_BOUND
                    )
                else:
                    # Old format: val is window_name
                    new_id = live_by_name.get(val)
                    if new_id:
                        logger.info("Migrating thread binding %s -> %s", val, new_id)
                        new_bindings[tid] = new_id
                        self.window_display_names[new_id] = val
                        self.topic_binding_states.setdefault(uid, {})[tid] = (
                            BINDING_STATE_BOUND
                        )
                        changed = True
                    else:
                        logger.info(
                            "Dropping old-format thread binding: user=%d, thread=%d, name=%s",
                            uid,
                            tid,
                            val,
                        )
                        dropped_bindings.append((uid, tid))
                        changed = True
            self.thread_bindings[uid] = new_bindings

        # Remove empty user entries
        empty_users = [uid for uid, b in self.thread_bindings.items() if not b]
        for uid in empty_users:
            del self.thread_bindings[uid]

        for uid, tid in dropped_bindings:
            self.topic_binding_states.setdefault(uid, {})[tid] = BINDING_STATE_NONE

        for uid, external in list(self.external_topic_bindings.items()):
            bindings = self.thread_bindings.get(uid, {})
            for tid in list(external.keys()):
                current_binding = bindings.get(tid)
                if current_binding is None or not self.is_external_binding_window_id(
                    current_binding
                ):
                    del external[tid]
                    changed = True
            if not external:
                del self.external_topic_bindings[uid]
                changed = True

        # Ensure every live binding has an explicit policy/state record.
        for uid, bindings in self.thread_bindings.items():
            policy_map = self.topic_policies.setdefault(uid, {})
            state_map = self.topic_binding_states.setdefault(uid, {})
            for tid in bindings:
                policy_map.setdefault(tid, TOPIC_POLICY_IMPLICIT_BIND_ALLOWED)
                state_map[tid] = BINDING_STATE_BOUND

        # --- Migrate user_window_offsets ---
        for uid, offsets in self.user_window_offsets.items():
            new_offsets: dict[str, int] = {}
            for key, offset in offsets.items():
                if self._is_window_id(key):
                    if key in live_ids:
                        new_offsets[key] = offset
                    else:
                        display = self.window_display_names.get(key, key)
                        new_id = live_by_name.get(display)
                        if new_id:
                            new_offsets[new_id] = offset
                            changed = True
                        else:
                            changed = True
                elif self.is_external_binding_window_id(key):
                    new_offsets[key] = offset
                else:
                    new_id = live_by_name.get(key)
                    if new_id:
                        new_offsets[new_id] = offset
                        changed = True
                    else:
                        changed = True
            self.user_window_offsets[uid] = new_offsets

        if changed:
            self._merge_legacy_topic_state_into_surface(overwrite=True)
            self._save_state()
            logger.info("Startup re-resolution complete")

        # Clean up session_map.json: stale window IDs and old-format keys
        await self._cleanup_stale_session_map_entries(live_ids)
        await self._cleanup_old_format_session_map_keys()

    async def _cleanup_old_format_session_map_keys(self) -> None:
        """Preserve legacy session_map bindings while ensuring versioned storage."""
        if not config.session_map_file.exists():
            return
        session_map, versioned, loaded = self._session_map_entries()
        if not loaded:
            return
        if versioned:
            return

        self._write_session_map_entries(session_map)
        logger.info(
            "Migrated legacy session_map to versioned envelope (%d entries preserved)",
            len(session_map),
        )

    async def _cleanup_stale_session_map_entries(self, live_ids: set[str]) -> None:
        """Remove entries for tmux windows that no longer exist.

        When windows are closed externally (outside ccbot), session_map.json
        retains orphan references. This cleanup removes entries whose window_id
        is not in the current set of live tmux windows.
        """
        if not config.session_map_file.exists():
            return
        session_map, versioned, loaded = self._session_map_entries()
        if not loaded:
            return

        prefix = f"{config.tmux_session_name}:"
        stale_keys = [
            key
            for key in session_map
            if key.startswith(prefix)
            and self._is_window_id(key[len(prefix) :])
            and key[len(prefix) :] not in live_ids
        ]
        if not stale_keys and versioned:
            return

        for key in stale_keys:
            del session_map[key]
            logger.info("Removed stale session_map entry: %s", key)

        self._write_session_map_entries(session_map)
        logger.info(
            "Cleaned up %d stale session_map entries (windows no longer in tmux)",
            len(stale_keys),
        )

    # --- Display name management ---

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def update_display_name(self, window_id: str, new_name: str) -> None:
        """Update the display name for a window and persist state."""
        self.window_display_names[window_id] = new_name
        # Also update the live process descriptor if it exists
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self._save_state()
        logger.info("Updated display name: window_id %s -> '%s'", window_id, new_name)

    # --- Group chat ID management (supergroup forum topic routing) ---

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        """Store the group chat_id for a user+thread combination.

        In supergroups with forum topics, messages must be sent to the group's
        chat_id (negative number like -100xxx) rather than the user's personal ID.
        Telegram's Bot API rejects message_thread_id when chat_id is a private
        user ID — the thread only exists within the group context.

        DO NOT REMOVE this method or the group_chat_ids mapping.
        Without it, all outbound messages in forum topics fail with
        "Message thread not found". See commit history: 5afc111 → 26cb81f → PR #23.
        """
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            self._save_state()
            logger.debug(
                "Stored group chat_id: user=%d, thread=%s, chat_id=%d",
                user_id,
                thread_id,
                chat_id,
            )

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        """Resolve the correct chat_id for sending messages.

        Returns the stored group chat_id when a thread_id is present and a
        mapping exists. When thread_id is None, this also checks the chat-wide
        no-topics slot (`user_id:0`) before falling back to user_id.

        Every outbound Telegram API call (send_message, edit_message_text,
        delete_message, send_chat_action, edit_forum_topic, etc.) MUST use
        this method instead of raw user_id. Using user_id directly breaks
        supergroup forum topic routing.
        """
        lookup_thread_id = thread_id if thread_id is not None else 0
        key = f"{user_id}:{lookup_thread_id}"
        group_id = self.group_chat_ids.get(key)
        if group_id is not None:
            return group_id
        return user_id

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Poll session_map.json until an entry for window_id appears.

        Returns True if the entry was found within timeout, False otherwise.
        """
        logger.debug(
            "Waiting for session_map entry: window_id=%s, timeout=%.1f",
            window_id,
            timeout,
        )
        key = f"{config.tmux_session_name}:{window_id}"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            session_map, _, loaded = self._session_map_entries()
            if loaded and session_map:
                info = session_map.get(key, {})
                if info.get("session_id"):
                    # Found — load into window_states immediately
                    logger.debug("session_map entry found for window_id %s", window_id)
                    await self.load_session_map()
                    return True
            await asyncio.sleep(interval)
        logger.warning(
            "Timed out waiting for session_map entry: window_id=%s", window_id
        )
        return False

    async def load_session_map(self) -> None:
        """Read session_map.json and update window_states with new session associations.

        Keys in session_map are formatted as "tmux_session:window_id" (e.g. "ccbot:@12").
        Only entries matching our tmux_session_name are processed.
        Also cleans up window_states entries not in current session_map.
        Updates window_display_names from the "window_name" field in values.
        """
        if not config.session_map_file.exists():
            return
        session_map, versioned, loaded = self._session_map_entries()
        if not loaded:
            return

        prefix = f"{config.tmux_session_name}:"
        valid_wids: set[str] = set()
        changed = False
        legacy_keys_seen = False

        for key, info in session_map.items():
            # Only process entries for our tmux session
            if not key.startswith(prefix):
                continue
            window_id = key[len(prefix) :]
            state = self.get_window_state(window_id)
            new_sid = info.get("session_id", "")
            new_cwd = info.get("cwd", "")
            new_wname = info.get("window_name", "")
            new_runtime_kind = info.get("runtime_kind", state.runtime_kind)
            if self._is_window_id(window_id):
                valid_wids.add(window_id)
            else:
                legacy_keys_seen = True
            if (
                (new_sid and state.thread_id != new_sid)
                or state.cwd != new_cwd
                or state.runtime_kind != new_runtime_kind
            ):
                logger.info(
                    "Session map: window_id %s updated sid=%s, cwd=%s, runtime=%s",
                    window_id,
                    new_sid,
                    new_cwd,
                    new_runtime_kind,
                )
                if new_sid:
                    state.thread_id = new_sid
                state.cwd = new_cwd
                state.runtime_kind = new_runtime_kind
                changed = True
            # Update display name
            if new_wname:
                state.window_name = new_wname
                if self.window_display_names.get(window_id) != new_wname:
                    self.window_display_names[window_id] = new_wname
                    changed = True

        # Clean up window_states entries only once the binding map has fully
        # transitioned to tmux window IDs. Legacy window-name keys are preserved
        # until the codex binding path has been validated.
        if valid_wids and not legacy_keys_seen:
            stale_wids = [w for w in self.window_states if w and w not in valid_wids]
            for wid in stale_wids:
                logger.info("Removing stale window_state: %s", wid)
                del self.window_states[wid]
                changed = True

        if changed or not versioned:
            self._save_state()
        if not versioned:
            self._write_session_map_entries(session_map)

    # --- Window state management ---

    def get_process_descriptor(self, window_id: str) -> LiveProcessDescriptor:
        """Get or create the live process descriptor for a tmux window."""
        if window_id not in self.window_states:
            self.window_states[window_id] = LiveProcessDescriptor()
        return self.window_states[window_id]

    def get_runtime_capability(
        self, runtime_kind: str | None = None
    ) -> RuntimeCapability:
        """Return the capability profile for the requested runtime kind."""
        return runtime_capability_registry.get(
            runtime_kind or config.default_runtime_kind
        )

    def get_window_state(self, window_id: str) -> WindowState:
        """Backward-compatible alias for get_process_descriptor()."""
        return self.get_process_descriptor(window_id)

    def register_live_process(
        self,
        window_id: str,
        cwd: str,
        *,
        window_name: str = "",
        runtime_kind: str | None = None,
        thread_id: str = "",
    ) -> WindowState:
        """Register a live process before its persisted thread is known."""
        state = self.get_process_descriptor(window_id)
        state.cwd = cwd
        if window_name:
            state.window_name = window_name
        if runtime_kind:
            state.runtime_kind = runtime_kind
        state.registered_at = time.time()
        state.thread_id = thread_id
        self._save_state()
        return state

    def clear_window_binding(self, window_id: str) -> None:
        """Clear the persisted identity binding for a live window."""
        state = self.get_process_descriptor(window_id)
        state.thread_id = ""
        self._save_state()
        logger.info("Cleared persisted binding for window_id %s", window_id)

    def clear_window_session(self, window_id: str) -> None:
        """Backward-compatible alias for clear_window_binding()."""
        self.clear_window_binding(window_id)

    @staticmethod
    def _encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Claude Code's project directory naming.

        Replaces all non-alphanumeric characters (except dash) with dashes.
        E.g. /home/user_name/Code/project -> -home-user-name-Code-project
        """
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

    def _build_session_file_path(self, thread_id: str, cwd: str) -> Path | None:
        """Build the direct rollout path for a thread from thread_id and cwd."""
        if not thread_id or not cwd:
            return None
        encoded_cwd = self._encode_cwd(cwd)
        return config.claude_projects_path / encoded_cwd / f"{thread_id}.jsonl"

    async def _get_thread_locator_direct(
        self, thread_id: str, cwd: str
    ) -> ThreadLocator | None:
        """Resolve a legacy Claude transcript directly from thread_id and cwd."""
        file_path = self._build_session_file_path(thread_id, cwd)

        # Fallback: glob search if direct path doesn't exist
        if not file_path or not file_path.exists():
            pattern = f"*/{thread_id}.jsonl"
            matches = list(config.claude_projects_path.glob(pattern))
            if matches:
                file_path = matches[0]
                logger.debug("Found session via glob: %s", file_path)
            else:
                return None

        # Single pass: read file once, extract summary + count messages
        summary = ""
        last_user_msg = ""
        message_count = 0
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                        # Check for summary
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        # Track last user message as fallback
                        elif TranscriptParser.is_user_message(data):
                            parsed = TranscriptParser.parse_message(data)
                            if parsed and parsed.text.strip():
                                last_user_msg = parsed.text.strip()
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return ThreadLocator(
            thread_id=thread_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
            runtime_kind="claude",
            cwd=cwd,
        )

    async def _get_session_direct(
        self, session_id: str, cwd: str
    ) -> ClaudeSession | None:
        """Backward-compatible wrapper for Claude-shaped call sites."""
        return await self._get_thread_locator_direct(session_id, cwd)

    # --- Directory session listing ---

    async def list_threads_for_directory(self, cwd: str) -> list[ThreadLocator]:
        """List persisted threads for a directory.

        Returns Codex candidates, fast-agent sessions, plus any legacy Claude
        threads for the same cwd.

        Mixed-runtime directories must not hide older Claude transcripts simply
        because Codex candidates exist for the same path.
        """
        sessions: list[ThreadLocator] = []
        seen_thread_ids: set[str] = set()

        if self.codex_thread_catalog is not None:
            self.codex_thread_catalog.refresh()
            candidates = await asyncio.to_thread(
                self.codex_thread_catalog.list_candidates_for_cwd, cwd
            )
            for candidate in candidates:
                locator = candidate.to_locator()
                sessions.append(locator)
                seen_thread_ids.add(locator.thread_id)

        if self.fast_agent_session_catalog is not None:
            self.fast_agent_session_catalog.refresh()
            fast_agent_candidates = await asyncio.to_thread(
                self.fast_agent_session_catalog.list_candidates_for_directory,
                cwd,
            )
            for candidate in fast_agent_candidates:
                locator = candidate.to_locator()
                if locator.thread_id in seen_thread_ids:
                    continue
                sessions.append(locator)
                seen_thread_ids.add(locator.thread_id)

        encoded_cwd = self._encode_cwd(cwd)
        project_dir = config.claude_projects_path / encoded_cwd
        if not project_dir.is_dir():
            return sessions

        # Collect JSONL files sorted by mtime (newest first)
        jsonl_files = sorted(
            project_dir.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        # Skip sessions-index and cap at 10
        for f in jsonl_files:
            if f.stem == "sessions-index":
                continue
            if len(sessions) >= 10:
                break
            session_id = f.stem
            if session_id in seen_thread_ids:
                continue
            session = await self._get_thread_locator_direct(session_id, cwd)
            if session and session.message_count > 0:
                sessions.append(session)
                seen_thread_ids.add(session.thread_id)
        return sessions

    async def list_sessions_for_directory(self, cwd: str) -> list[ClaudeSession]:
        """Backward-compatible wrapper for legacy callers."""
        return await self.list_threads_for_directory(cwd)

    # --- Window -> thread resolution ---

    async def resolve_thread_for_window(self, window_id: str) -> ThreadLocator | None:
        """Resolve a tmux window to the persisted thread bound to its process.

        Uses the explicit launcher registration first, then exact cwd-based
        resolution. Ambiguity is fail-closed.
        """
        if self.is_external_binding_window_id(window_id):
            return await self._resolve_external_thread_for_window(window_id)

        state = self.get_process_descriptor(window_id)
        resolution = await self.resolve_thread_candidate(window_id)
        if resolution is None:
            return None

        if resolution.status == "selected" and resolution.selected is not None:
            selected = resolution.selected
            changed = False
            if state.thread_id != selected.thread_id:
                state.thread_id = selected.thread_id
                changed = True
            if selected.cwd and state.cwd != selected.cwd:
                state.cwd = selected.cwd
                changed = True
            if changed:
                self._save_state()
            return selected.to_locator()

        if resolution.status == "ambiguous":
            logger.warning(
                "Ambiguous Codex thread resolution for window_id %s (cwd=%s)",
                window_id,
                state.cwd,
            )
        return None

    def _find_external_binding_record_by_window(
        self,
        window_id: str,
    ) -> tuple[int, str, dict[str, Any]] | None:
        """Return (user_id, surface_key, metadata) for an external binding id."""
        for user_id, bindings in self.surface_bindings.items():
            for surface_key, bound_window_id in bindings.items():
                if bound_window_id != window_id:
                    continue
                metadata = self.get_external_surface_binding(
                    user_id,
                    surface_key=surface_key,
                ) or {}
                return user_id, surface_key, dict(metadata)
        return None

    def get_surface_coordinates_for_window(
        self,
        user_id: int,
        window_id: str,
    ) -> tuple[str | None, int | None, int | None]:
        """Return the bound surface key and coordinates for a user/window pair."""
        bindings = self.surface_bindings.get(user_id) or {}
        for surface_key, bound_window_id in bindings.items():
            if bound_window_id != window_id:
                continue
            parsed = self._parse_surface_key(surface_key)
            if parsed is None:
                return surface_key, None, None
            kind, numeric_id = parsed
            if kind == "chat":
                return surface_key, numeric_id, None
            return surface_key, None, numeric_id
        return None, None, None

    async def _resolve_external_thread_for_window(
        self,
        window_id: str,
    ) -> ThreadLocator | None:
        """Resolve a non-tmux external binding to a persisted thread locator."""
        parsed = self.parse_external_binding_window_id(window_id)
        if parsed is None:
            return None

        parsed_runtime_kind, parsed_thread_id = parsed
        record = self._find_external_binding_record_by_window(window_id)
        metadata = record[2] if record is not None else {}
        runtime_kind = (
            str(metadata.get("runtime_kind") or parsed_runtime_kind).strip()
            or config.default_runtime_kind
        )
        source_thread_id = (
            str(metadata.get("source_thread_id") or parsed_thread_id).strip()
            or parsed_thread_id
        )
        summary = str(metadata.get("summary") or source_thread_id).strip() or source_thread_id
        cwd = str(metadata.get("cwd") or "").strip()
        file_path = str(metadata.get("file_path") or "").strip()
        message_count = 0

        if runtime_kind == "codex" and self.codex_thread_catalog is not None:
            self.codex_thread_catalog.refresh()
            candidate = await asyncio.to_thread(
                self.codex_thread_catalog.get_candidate_fast,
                source_thread_id,
            )
            if candidate is None:
                resolution = await asyncio.to_thread(
                    self.codex_thread_catalog.resolve_resume_target,
                    source_thread_id,
                )
                if resolution.status == "selected" and resolution.selected is not None:
                    candidate = resolution.selected
            if candidate is not None:
                summary = candidate.summary
                cwd = candidate.cwd
                file_path = str(candidate.rollout_file)
                message_count = candidate.message_count

        if not file_path:
            return None

        if record is not None:
            user_id, surface_key, _ = record
            external = self.external_surface_bindings.setdefault(user_id, {})
            current = external.setdefault(surface_key, {})
            changed = False
            for key, value in (
                ("runtime_kind", runtime_kind),
                ("source_thread_id", source_thread_id),
                ("summary", summary),
                ("cwd", cwd),
                ("file_path", file_path),
            ):
                if str(current.get(key) or "") != value:
                    current[key] = value
                    changed = True
            if "read_only" not in current:
                current["read_only"] = True
                changed = True
            if changed:
                self._save_state()

        return ThreadLocator(
            thread_id=source_thread_id,
            summary=summary,
            message_count=message_count,
            file_path=file_path,
            runtime_kind=runtime_kind,
            cwd=cwd,
        )

    async def resolve_thread_candidate(
        self, window_id: str
    ) -> CodexThreadResolution | FastAgentSessionResolution | None:
        """Resolve a live window to a runtime-specific thread candidate."""
        state = self.get_process_descriptor(window_id)
        if not state.cwd:
            return None

        capability = self.get_runtime_capability(state.runtime_kind)

        if (
            capability.replay_evidence_discovery == "rollout_jsonl"
            and self.codex_thread_catalog is not None
        ):
            if state.thread_id:
                candidate = self.codex_thread_catalog.get_candidate_fast(state.thread_id)
                if candidate is not None:
                    return CodexThreadResolution(
                        status="selected",
                        selected=candidate,
                        candidates=(candidate,),
                        reason="explicit_thread_id_fast_path",
                    )
            if state.registered_at > 0:
                recent_resolution = self.codex_thread_catalog.resolve_recent_for_registration(
                    cwd=state.cwd,
                    registered_at=state.registered_at,
                )
                if recent_resolution.status != "not_found":
                    return recent_resolution
            self.codex_thread_catalog.refresh()
            return self.codex_thread_catalog.resolve_for_registration(
                registered_thread_id=state.thread_id or None,
                cwd=state.cwd,
                registered_at=state.registered_at,
            )

        if capability.replay_evidence_discovery == "acp_log_jsonl":
            if self.fast_agent_session_catalog is None:
                return None
            self.fast_agent_session_catalog.refresh()
            return await asyncio.to_thread(
                self.fast_agent_session_catalog.resolve_for_registration,
                registered_session_id=state.thread_id or None,
                cwd=state.cwd,
                registered_at=state.registered_at,
            )

        if (
            capability.replay_evidence_discovery == "transcript_jsonl"
            and state.thread_id
            and state.cwd
        ):
            session = await self._get_thread_locator_direct(state.thread_id, state.cwd)
            if session:
                return CodexThreadResolution(
                    status="selected",
                    selected=CodexThreadCandidate(
                        thread_id=session.thread_id,
                        thread_name=session.summary,
                        cwd=session.cwd,
                        rollout_file=Path(session.file_path),
                        message_count=session.message_count,
                        mtime=0.0,
                        preview=session.summary,
                    ),
                    candidates=(),
                    reason="legacy_direct_lookup",
                )
        return None

    async def resolve_session_for_window(self, window_id: str) -> ClaudeSession | None:
        """Backward-compatible wrapper for legacy callers."""
        return await self.resolve_thread_for_window(window_id)

    # --- User window offset management ---

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        """Update the user's last read offset for a window."""
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self._save_state()

    # --- Thread binding management ---

    def _rotate_surface_bind_flow_credentials(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> tuple[int, str]:
        """Issue fresh bind-flow credentials and persist them for a surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        version_map = self.surface_bind_flow_versions.setdefault(user_id, {})
        nonce_map = self.surface_bind_flow_nonces.setdefault(user_id, {})
        version = normalize_bind_flow_version(version_map.get(resolved_surface_key)) + 1
        nonce = secrets.token_urlsafe(16)
        version_map[resolved_surface_key] = version
        nonce_map[resolved_surface_key] = nonce
        return version, nonce

    def get_surface_bind_flow_version(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> int:
        """Return the current bind-flow version for a surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        versions = self.surface_bind_flow_versions.get(user_id)
        if not versions:
            return 0
        return normalize_bind_flow_version(versions.get(resolved_surface_key))

    def get_surface_bind_flow_nonce(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str:
        """Return the current bind-flow nonce for a surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        nonces = self.surface_bind_flow_nonces.get(user_id)
        if not nonces:
            return ""
        return normalize_bind_flow_nonce(nonces.get(resolved_surface_key))

    def get_surface_bind_flow_credentials(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> tuple[int, str]:
        """Return the current bind-flow version and nonce for a surface."""
        return (
            self.get_surface_bind_flow_version(
                user_id,
                surface_key=surface_key,
                thread_id=thread_id,
                chat_id=chat_id,
            ),
            self.get_surface_bind_flow_nonce(
                user_id,
                surface_key=surface_key,
                thread_id=thread_id,
                chat_id=chat_id,
            ),
        )

    def validate_surface_bind_flow_callback(
        self,
        user_id: int,
        version: int,
        nonce: str,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> bool:
        """Check whether a callback matches the current bind-flow credentials."""
        current_version, current_nonce = self.get_surface_bind_flow_credentials(
            user_id,
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        return (
            version > 0
            and bool(nonce)
            and current_version == version
            and current_nonce == nonce
        )

    def get_window_for_surface(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str | None:
        """Look up the window_id bound to a control surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        bindings = self.surface_bindings.get(user_id)
        if not bindings:
            return None
        return bindings.get(resolved_surface_key)

    def resolve_window_for_surface(
        self,
        user_id: int,
        surface_key: str | None = None,
        *,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str | None:
        """Resolve the tmux window_id for a control surface."""
        return self.get_window_for_surface(
            user_id,
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )

    def get_external_surface_binding(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Return external bind metadata for a control surface, if present."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        bindings = self.external_surface_bindings.get(user_id)
        if not bindings:
            return None
        binding = bindings.get(resolved_surface_key)
        if not isinstance(binding, dict):
            return None
        return dict(binding)

    def bind_surface(
        self,
        user_id: int,
        window_id: str,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
        window_name: str = "",
    ) -> None:
        """Bind a control surface to a live tmux window."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        bindings = self.surface_bindings.setdefault(user_id, {})
        bindings[resolved_surface_key] = window_id
        external = self.external_surface_bindings.get(user_id)
        if external and resolved_surface_key in external:
            del external[resolved_surface_key]
            self._prune_empty_surface_entry(self.external_surface_bindings, user_id)
        states = self.surface_binding_states.setdefault(user_id, {})
        states[resolved_surface_key] = BINDING_STATE_BOUND
        policies = self.surface_policies.setdefault(user_id, {})
        policies.setdefault(resolved_surface_key, TOPIC_POLICY_IMPLICIT_BIND_ALLOWED)
        self._rotate_surface_bind_flow_credentials(user_id, surface_key=resolved_surface_key)
        if window_name:
            self.window_display_names[window_id] = window_name
        self._sync_legacy_topic_views_from_surface()
        self._save_state()
        display = window_name or self.get_display_name(window_id)
        logger.info(
            "Bound surface %s -> window_id %s (%s) for user %d",
            resolved_surface_key,
            window_id,
            display,
            user_id,
        )

    def bind_external_surface(
        self,
        user_id: int,
        *,
        runtime_kind: str,
        source_thread_id: str,
        summary: str = "",
        cwd: str = "",
        file_path: str = "",
        read_only: bool = True,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str:
        """Bind a control surface to an external persisted thread without tmux."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        binding_window_id = self.make_external_binding_window_id(
            runtime_kind,
            source_thread_id,
        )
        self.surface_bindings.setdefault(user_id, {})[resolved_surface_key] = binding_window_id
        self.external_surface_bindings.setdefault(user_id, {})[resolved_surface_key] = {
            "runtime_kind": runtime_kind,
            "source_thread_id": source_thread_id,
            "summary": summary,
            "cwd": cwd,
            "file_path": file_path,
            "read_only": bool(read_only),
        }
        if summary:
            self.window_display_names[binding_window_id] = summary
        self.surface_binding_states.setdefault(user_id, {})[resolved_surface_key] = (
            BINDING_STATE_BOUND
        )
        self.surface_policies.setdefault(user_id, {}).setdefault(
            resolved_surface_key,
            TOPIC_POLICY_IMPLICIT_BIND_ALLOWED,
        )
        self._rotate_surface_bind_flow_credentials(user_id, surface_key=resolved_surface_key)
        self._sync_legacy_topic_views_from_surface()
        self._save_state()
        logger.info(
            "Bound surface %s -> external %s thread=%s (read_only=%s) for user %d",
            resolved_surface_key,
            runtime_kind,
            source_thread_id,
            read_only,
            user_id,
        )
        return binding_window_id

    def unbind_surface(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str | None:
        """Remove a control-surface binding and return the previous window_id."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        self._rotate_surface_bind_flow_credentials(user_id, surface_key=resolved_surface_key)
        bindings = self.surface_bindings.get(user_id)
        if not bindings or resolved_surface_key not in bindings:
            self.surface_binding_states.setdefault(user_id, {})[resolved_surface_key] = (
                BINDING_STATE_NONE
            )
            self._sync_legacy_topic_views_from_surface()
            self._save_state()
            return None
        window_id = bindings.pop(resolved_surface_key)
        self._prune_empty_surface_entry(self.surface_bindings, user_id)
        external = self.external_surface_bindings.get(user_id)
        if external and resolved_surface_key in external:
            del external[resolved_surface_key]
            self._prune_empty_surface_entry(self.external_surface_bindings, user_id)
        self.surface_binding_states.setdefault(user_id, {})[resolved_surface_key] = (
            BINDING_STATE_NONE
        )
        self._sync_legacy_topic_views_from_surface()
        self._save_state()
        logger.info(
            "Unbound surface %s (was %s) for user %d",
            resolved_surface_key,
            window_id,
            user_id,
        )
        return window_id

    def get_surface_policy(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str:
        """Return the persisted policy for a control surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        policies = self.surface_policies.get(user_id)
        if not policies:
            return TOPIC_POLICY_IMPLICIT_BIND_ALLOWED
        return normalize_topic_policy(policies.get(resolved_surface_key))

    def set_surface_policy(
        self,
        user_id: int,
        policy: str,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> None:
        """Persist the control-surface policy without changing the binding."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        normalized = normalize_topic_policy(policy)
        policies = self.surface_policies.setdefault(user_id, {})
        if policies.get(resolved_surface_key) == normalized:
            return
        policies[resolved_surface_key] = normalized
        self._sync_legacy_topic_views_from_surface()
        self._save_state()
        logger.info(
            "Set surface policy for user %d surface %s -> %s",
            user_id,
            resolved_surface_key,
            normalized,
        )

    def get_surface_binding_state(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> str:
        """Return the persisted binding state for a control surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        states = self.surface_binding_states.get(user_id)
        if states and resolved_surface_key in states:
            return normalize_binding_state(states[resolved_surface_key])
        return (
            BINDING_STATE_BOUND
            if self.get_window_for_surface(user_id, surface_key=resolved_surface_key)
            else BINDING_STATE_NONE
        )

    def set_surface_binding_state(
        self,
        user_id: int,
        binding_state: str,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> None:
        """Persist the binding state without changing the control-surface policy."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        normalized = normalize_binding_state(binding_state)
        states = self.surface_binding_states.setdefault(user_id, {})
        if states.get(resolved_surface_key) == normalized:
            return
        states[resolved_surface_key] = normalized
        self._sync_legacy_topic_views_from_surface()
        self._save_state()
        logger.info(
            "Set surface binding state for user %d surface %s -> %s",
            user_id,
            resolved_surface_key,
            normalized,
        )

    def start_surface_bind_flow(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> None:
        """Mark a control surface as being in an active bind flow."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        self.surface_binding_states.setdefault(user_id, {})[resolved_surface_key] = (
            BINDING_STATE_BIND_FLOW
        )
        self._rotate_surface_bind_flow_credentials(user_id, surface_key=resolved_surface_key)
        self._sync_legacy_topic_views_from_surface()
        self._save_state()

    def require_manual_bind_for_surface(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> None:
        """Force a control surface into manual-bind mode without changing its binding."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        self.surface_policies.setdefault(user_id, {})[resolved_surface_key] = (
            TOPIC_POLICY_MANUAL_BIND_REQUIRED
        )
        self.surface_binding_states.setdefault(user_id, {})[resolved_surface_key] = (
            BINDING_STATE_NONE
        )
        self._rotate_surface_bind_flow_credentials(user_id, surface_key=resolved_surface_key)
        self._sync_legacy_topic_views_from_surface()
        self._save_state()

    def allow_implicit_bind_for_surface(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> None:
        """Allow implicit binding again for a control surface."""
        self.set_surface_policy(
            user_id,
            TOPIC_POLICY_IMPLICIT_BIND_ALLOWED,
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )

    def peek_surface_pending_slot(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Return the current pending-slot payload for a surface without consuming it."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        pending_slots = self.surface_pending_slots.get(user_id)
        if not pending_slots:
            return None
        pending = self._normalize_pending_slot_record(
            pending_slots.get(resolved_surface_key)
        )
        if pending is None:
            return None
        return dict(pending)

    def set_surface_pending_slot(
        self,
        user_id: int,
        text: str,
        *,
        revision: int | None = None,
        status: str = PENDING_SLOT_STATUS_PENDING,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist or overwrite the latest pending-slot payload for a surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        current = self.peek_surface_pending_slot(
            user_id,
            surface_key=resolved_surface_key,
        ) or {}
        next_revision = revision
        if next_revision is None:
            next_revision = normalize_bind_flow_version(current.get("revision")) + 1
        record = self._normalize_pending_slot_record(
            {
                "text": text,
                "revision": next_revision,
                "status": status or PENDING_SLOT_STATUS_PENDING,
                "consumed_by_activation_id": "",
            }
        )
        assert record is not None
        self.surface_pending_slots.setdefault(user_id, {})[resolved_surface_key] = record
        self._save_state()
        return dict(record)

    def clear_surface_pending_slot(
        self,
        user_id: int,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Remove any pending-slot payload associated with a surface."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        pending_slots = self.surface_pending_slots.get(user_id)
        if not pending_slots:
            return None
        pending = self._normalize_pending_slot_record(pending_slots.pop(resolved_surface_key))
        self._prune_empty_surface_entry(self.surface_pending_slots, user_id)
        self._save_state()
        return dict(pending) if pending is not None else None

    def consume_surface_pending_slot(
        self,
        user_id: int,
        activation_id: str,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Consume a pending-slot payload exactly once for a writable activation."""
        resolved_surface_key = self._resolve_surface_key(
            surface_key=surface_key,
            thread_id=thread_id,
            chat_id=chat_id,
        )
        pending_slots = self.surface_pending_slots.get(user_id)
        if not pending_slots:
            return None
        pending = self._normalize_pending_slot_record(
            pending_slots.get(resolved_surface_key)
        )
        if pending is None:
            return None
        if pending.get("status") == PENDING_SLOT_STATUS_CONSUMED:
            return None
        consumed = dict(pending)
        consumed["status"] = PENDING_SLOT_STATUS_CONSUMED
        consumed["consumed_by_activation_id"] = activation_id
        pending_slots[resolved_surface_key] = consumed
        self._save_state()
        return dict(consumed)

    def _rotate_topic_bind_flow_credentials(
        self, user_id: int, thread_id: int
    ) -> tuple[int, str]:
        """Issue fresh bind-flow credentials for a legacy topic wrapper."""
        return self._rotate_surface_bind_flow_credentials(user_id, thread_id=thread_id)

    def get_topic_bind_flow_version(self, user_id: int, thread_id: int) -> int:
        """Return the current bind-flow version for a legacy topic wrapper."""
        return self.get_surface_bind_flow_version(user_id, thread_id=thread_id)

    def get_topic_bind_flow_nonce(self, user_id: int, thread_id: int) -> str:
        """Return the current bind-flow nonce for a legacy topic wrapper."""
        return self.get_surface_bind_flow_nonce(user_id, thread_id=thread_id)

    def get_topic_bind_flow_credentials(
        self, user_id: int, thread_id: int
    ) -> tuple[int, str]:
        """Return the current bind-flow version and nonce for a legacy topic wrapper."""
        return self.get_surface_bind_flow_credentials(user_id, thread_id=thread_id)

    def validate_topic_bind_flow_callback(
        self,
        user_id: int,
        thread_id: int,
        version: int,
        nonce: str,
    ) -> bool:
        """Check whether a callback matches the current bind-flow credentials."""
        return self.validate_surface_bind_flow_callback(
            user_id,
            version,
            nonce,
            thread_id=thread_id,
        )

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        """Legacy topic wrapper around bind_surface()."""
        self.bind_surface(
            user_id,
            window_id,
            thread_id=thread_id,
            window_name=window_name,
        )

    def bind_external_thread(
        self,
        user_id: int,
        thread_id: int,
        *,
        runtime_kind: str,
        source_thread_id: str,
        summary: str = "",
        cwd: str = "",
        file_path: str = "",
        read_only: bool = True,
    ) -> str:
        """Legacy topic wrapper around bind_external_surface()."""
        return self.bind_external_surface(
            user_id,
            runtime_kind=runtime_kind,
            source_thread_id=source_thread_id,
            summary=summary,
            cwd=cwd,
            file_path=file_path,
            read_only=read_only,
            thread_id=thread_id,
        )

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        """Legacy topic wrapper around unbind_surface()."""
        return self.unbind_surface(user_id, thread_id=thread_id)

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        """Look up the window_id bound to a topic thread."""
        return self.get_window_for_surface(user_id, thread_id=thread_id)

    def get_external_topic_binding(
        self,
        user_id: int,
        thread_id: int,
    ) -> dict[str, Any] | None:
        """Return external bind metadata for a topic, if present."""
        return self.get_external_surface_binding(user_id, thread_id=thread_id)

    def get_topic_policy(self, user_id: int, thread_id: int) -> str:
        """Return the persisted topic policy for a legacy topic wrapper."""
        return self.get_surface_policy(user_id, thread_id=thread_id)

    def set_topic_policy(self, user_id: int, thread_id: int, policy: str) -> None:
        """Persist the topic policy without changing the binding itself."""
        self.set_surface_policy(user_id, policy, thread_id=thread_id)

    def get_topic_binding_state(self, user_id: int, thread_id: int) -> str:
        """Return the persisted binding state for a legacy topic wrapper."""
        return self.get_surface_binding_state(user_id, thread_id=thread_id)

    def set_topic_binding_state(
        self, user_id: int, thread_id: int, binding_state: str
    ) -> None:
        """Persist the binding state without changing the topic policy."""
        self.set_surface_binding_state(user_id, binding_state, thread_id=thread_id)

    def start_topic_bind_flow(self, user_id: int, thread_id: int) -> None:
        """Mark a topic as being in an active bind flow."""
        self.start_surface_bind_flow(user_id, thread_id=thread_id)

    def require_manual_bind(self, user_id: int, thread_id: int) -> None:
        """Force a topic into manual-bind mode without changing its binding."""
        self.require_manual_bind_for_surface(user_id, thread_id=thread_id)

    def allow_implicit_bind(self, user_id: int, thread_id: int) -> None:
        """Allow implicit binding again for a topic."""
        self.allow_implicit_bind_for_surface(user_id, thread_id=thread_id)

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
        *,
        chat_id: int | None = None,
    ) -> str | None:
        """Resolve the tmux window_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            if chat_id is None:
                return None
            return self.get_window_for_surface(user_id, chat_id=chat_id)
        return self.get_window_for_thread(user_id, thread_id)

    def get_topic_binding(
        self, user_id: int, thread_id: int
    ) -> TopicBinding | None:
        """Resolve a persisted topic binding object."""
        window_id = self.get_window_for_thread(user_id, thread_id)
        if not window_id:
            return None
        if self.is_external_binding_window_id(window_id):
            external = self.get_external_topic_binding(user_id, thread_id) or {}
            runtime_kind = (
                str(external.get("runtime_kind") or config.default_runtime_kind).strip()
                or config.default_runtime_kind
            )
            source_thread_id = str(external.get("source_thread_id") or "").strip()
            return TopicBinding(
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                window_name=self.get_display_name(window_id),
                runtime_kind=runtime_kind,
                binding_scope="external",
                source_thread_id=source_thread_id,
                read_only=bool(external.get("read_only", True)),
            )
        return TopicBinding(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            window_name=self.get_display_name(window_id),
            runtime_kind=self.get_process_descriptor(window_id).runtime_kind,
        )

    def iter_topic_bindings(self) -> Iterator[TopicBinding]:
        """Iterate persisted topic bindings as structured runtime-neutral objects."""
        for user_id, bindings in self.surface_bindings.items():
            for surface_key, window_id in bindings.items():
                thread_id = self._topic_thread_id_from_surface_key(surface_key)
                if self.is_external_binding_window_id(window_id):
                    external = self.get_external_surface_binding(
                        user_id,
                        surface_key=surface_key,
                    ) or {}
                    runtime_kind = (
                        str(
                            external.get("runtime_kind")
                            or config.default_runtime_kind
                        ).strip()
                        or config.default_runtime_kind
                    )
                    source_thread_id = str(external.get("source_thread_id") or "").strip()
                    yield TopicBinding(
                        user_id=user_id,
                        thread_id=thread_id,
                        window_id=window_id,
                        window_name=self.get_display_name(window_id),
                        runtime_kind=runtime_kind,
                        binding_scope="external",
                        source_thread_id=source_thread_id,
                        read_only=bool(external.get("read_only", True)),
                    )
                    continue
                yield TopicBinding(
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    window_name=self.get_display_name(window_id),
                    runtime_kind=self.get_process_descriptor(window_id).runtime_kind,
                )

    def iter_thread_bindings(self) -> Iterator[tuple[int, int | None, str]]:
        """Backward-compatible tuple view over iter_topic_bindings()."""
        for binding in self.iter_topic_bindings():
            yield binding.user_id, binding.thread_id, binding.window_id

    async def find_bindings_for_thread(self, thread_id: str) -> list[TopicBinding]:
        """Find all topic bindings whose live window resolves to thread_id."""
        result: list[TopicBinding] = []
        for binding in self.iter_topic_bindings():
            resolved = await self.resolve_thread_for_window(binding.window_id)
            if resolved and resolved.thread_id == thread_id:
                result.append(binding)
        return result

    async def find_users_for_session(
        self,
        session_id: str,
    ) -> list[tuple[int, str, int]]:
        """Backward-compatible tuple view over find_bindings_for_thread().

        Returns list of (user_id, window_id, thread_id) tuples.
        """
        bindings = await self.find_bindings_for_thread(session_id)
        return [
            (binding.user_id, binding.window_id, binding.thread_id)
            for binding in bindings
        ]

    # --- Tmux helpers ---

    async def send_to_window(self, window_id: str, text: str) -> tuple[bool, str]:
        """Send text to a tmux window by ID."""
        if self.is_external_binding_window_id(window_id):
            return False, EXTERNAL_BINDING_READ_ONLY_MESSAGE
        display = self.get_display_name(window_id)
        logger.debug(
            "send_to_window: window_id=%s (%s), text_len=%d",
            window_id,
            display,
            len(text),
        )
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"

        runtime_kind = (
            self.window_states[window_id].runtime_kind
            if window_id in self.window_states
            else config.default_runtime_kind
        )
        capability = self.get_runtime_capability(runtime_kind)
        pane_text = await tmux_manager.capture_pane(window.window_id)
        if pane_text:
            surface = classify_input_surface(pane_text)
            if (
                capability.blocked_input_policy == "fail_closed_on_visible_prompt"
                and surface.kind == "blocked_prompt"
            ):
                return False, BLOCKED_PROMPT_SEND_MESSAGE

        trimmed = text.lstrip()
        if trimmed.startswith("/"):
            success, message = await runtime_input_driver.send_raw_slash_command(
                window.window_id,
                text,
                runtime_kind=runtime_kind,
            )
        else:
            success, message = await runtime_input_driver.send_text(
                window.window_id,
                text,
                runtime_kind=runtime_kind,
            )
        if success:
            return True, f"Sent to {display}"
        return False, message

    async def send_special_key_to_window(
        self, window_id: str, key: str
    ) -> tuple[bool, str]:
        """Send a control key through the runtime input driver."""
        if self.is_external_binding_window_id(window_id):
            return False, EXTERNAL_BINDING_READ_ONLY_MESSAGE
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"

        runtime_kind = (
            self.window_states[window_id].runtime_kind
            if window_id in self.window_states
            else config.default_runtime_kind
        )
        capability = self.get_runtime_capability(runtime_kind)
        if not capability.interactive_control_supported:
            return (
                False,
                f"Interactive control is not supported for {capability.display_name}",
            )
        return await runtime_input_driver.send_special_key(
            window.window_id,
            key,
            runtime_kind=runtime_kind,
        )

    async def send_input_to_window(
        self, window_id: str, action: InputAction
    ) -> tuple[bool, str]:
        """Send a runtime-neutral input action to the live window."""
        if self.is_external_binding_window_id(window_id):
            return False, EXTERNAL_BINDING_READ_ONLY_MESSAGE
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"
        return await runtime_input_driver.send_dispatch(window.window_id, action)

    async def rename_runtime_identity_for_window(
        self,
        window_id: str,
        new_name: str,
    ) -> tuple[bool, str]:
        """Rename the persisted runtime identity when the runtime supports it."""
        state = self.get_process_descriptor(window_id)
        capability = self.get_runtime_capability(state.runtime_kind)

        if capability.rename_identity_mode != "title_only":
            return False, "persisted identity unchanged"

        if self.fast_agent_session_catalog is None:
            return False, "fast-agent session catalog unavailable"
        if not state.thread_id or not state.cwd:
            return False, "fast-agent session metadata unavailable"

        self.fast_agent_session_catalog.refresh()
        result = await asyncio.to_thread(
            self.fast_agent_session_catalog.rename_title,
            session_id=state.thread_id,
            cwd=state.cwd,
            title=new_name,
        )
        if result.status == "selected":
            return True, "fast-agent session title metadata updated"
        if result.reason == "title_rename_write_failed":
            return False, "fast-agent session title metadata update failed"
        return False, "fast-agent session title metadata not found"

    # --- Message history ---

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get normalized message history for the thread bound to a window.

        Resolves window -> thread, then reads the JSONL rollout.
        Supports byte range filtering via start_byte/end_byte.
        Returns (messages, total_count).
        """
        session = await self.resolve_thread_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        # Read JSONL entries (optionally filtered by byte range)
        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    # Check byte limit before reading
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        if entries and all(
            CodexRolloutNormalizer.is_codex_rollout_record(entry)
            for entry in entries
            if isinstance(entry, dict)
        ):
            parsed_entries = TranscriptParser.parse_codex_rollout_entries(
                entries,
                thread_id=session.thread_id,
            )
        else:
            parsed_entries, _ = TranscriptParser.parse_entries(entries)
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
                "event_kind": getattr(e, "event_kind", "message"),
                "semantic_kind": getattr(e, "semantic_kind", "assistant_final"),
                "delivery_class": getattr(e, "delivery_class", "history"),
            }
            for e in parsed_entries
            if getattr(e, "include_in_history", True)
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()
