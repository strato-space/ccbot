"""Core runtime state hub for bindings, live processes, and thread locators.

This module still interoperates with the current Claude-shaped storage, but it
now exposes runtime-neutral nouns so later Codex work can distinguish:

- topic binding: Telegram topic -> tmux window
- live process descriptor: current process metadata for a window
- thread locator: persisted conversation identity and rollout path

Legacy method names remain as compatibility wrappers while the rest of the bot
is migrated task by task.
"""

import asyncio
import json
import logging
import re
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
from .input_driver import runtime_input_driver
from .runtime_types import InputAction, LiveProcessDescriptor, ThreadLocator, TopicBinding
from .state_schema import (
    build_session_map_payload,
    ensure_legacy_backup,
    infer_runtime_kind,
    split_session_map_payload,
)
from .terminal_parser import classify_input_surface
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

WindowState = LiveProcessDescriptor
ClaudeSession = ThreadLocator


@dataclass
class SessionManager:
    """Manages persisted bindings, process descriptors, and thread locators.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> LiveProcessDescriptor
    user_window_offsets: user_id -> {window_id -> byte_offset}
    thread_bindings: user_id -> {thread_id -> window_id}
    window_display_names: window_id -> window_name (for display)
    group_chat_ids: "user_id:thread_id" -> group chat_id (for supergroup routing)
    """

    window_states: dict[str, LiveProcessDescriptor] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
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

    def __post_init__(self) -> None:
        if self.codex_thread_catalog is None:
            self.codex_thread_catalog = CodexThreadCatalog()
        self._load_state()

    def _save_state(self) -> None:
        state: dict[str, Any] = {
            "schema_version": config.state_schema_version,
            "runtime_kind": infer_runtime_kind(
                window_state.runtime_kind for window_state in self.window_states.values()
            ),
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "thread_bindings": {
                str(uid): {str(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in self.thread_bindings.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
        }
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    def _is_window_id(self, key: str) -> bool:
        """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    def _load_state(self) -> None:
        """Load state synchronously during initialization.

        Detects old-format state (window_name keys without '@' prefix) and
        marks for migration on next startup re-resolution.
        """
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                migrate_legacy = "schema_version" not in state
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
                self.window_display_names = state.get("window_display_names", {})
                self.group_chat_ids = {
                    k: int(v) for k, v in state.get("group_chat_ids", {}).items()
                }

                # Detect old format: keys that don't look like window IDs
                needs_migration = False
                for k in self.window_states:
                    if not self._is_window_id(k):
                        needs_migration = True
                        break
                if not needs_migration:
                    for bindings in self.thread_bindings.values():
                        for wid in bindings.values():
                            if not self._is_window_id(wid):
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
                self.window_display_names = {}
                self.group_chat_ids = {}
                migrate_legacy = False
            else:
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
                        else:
                            logger.info(
                                "Dropping stale thread binding: user=%d, thread=%d, wid=%s",
                                uid,
                                tid,
                                val,
                            )
                            changed = True
                else:
                    # Old format: val is window_name
                    new_id = live_by_name.get(val)
                    if new_id:
                        logger.info("Migrating thread binding %s -> %s", val, new_id)
                        new_bindings[tid] = new_id
                        self.window_display_names[new_id] = val
                        changed = True
                    else:
                        logger.info(
                            "Dropping old-format thread binding: user=%d, thread=%d, name=%s",
                            uid,
                            tid,
                            val,
                        )
                        changed = True
            self.thread_bindings[uid] = new_bindings

        # Remove empty user entries
        empty_users = [uid for uid, b in self.thread_bindings.items() if not b]
        for uid in empty_users:
            del self.thread_bindings[uid]

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
                else:
                    new_id = live_by_name.get(key)
                    if new_id:
                        new_offsets[new_id] = offset
                        changed = True
                    else:
                        changed = True
            self.user_window_offsets[uid] = new_offsets

        if changed:
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
        mapping exists, otherwise falls back to user_id (for private chats).

        Every outbound Telegram API call (send_message, edit_message_text,
        delete_message, send_chat_action, edit_forum_topic, etc.) MUST use
        this method instead of raw user_id. Using user_id directly breaks
        supergroup forum topic routing.
        """
        if thread_id is not None:
            key = f"{user_id}:{thread_id}"
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

    def clear_window_session(self, window_id: str) -> None:
        """Clear the persisted thread association for a window."""
        state = self.get_process_descriptor(window_id)
        state.thread_id = ""
        self._save_state()
        logger.info("Cleared session for window_id %s", window_id)

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

        Returns Codex candidates plus any legacy Claude threads for the same cwd.

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

    async def resolve_thread_candidate(
        self, window_id: str
    ) -> CodexThreadResolution | None:
        """Resolve a live window to a Codex thread candidate without mutating state."""
        state = self.get_process_descriptor(window_id)
        if not state.cwd:
            return None

        if state.runtime_kind == "codex" and self.codex_thread_catalog is not None:
            self.codex_thread_catalog.refresh()
            return self.codex_thread_catalog.resolve_for_registration(
                registered_thread_id=state.thread_id or None,
                cwd=state.cwd,
                registered_at=state.registered_at,
            )

        if state.thread_id and state.cwd:
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

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        """Bind a Telegram topic thread to a tmux window.

        Args:
            user_id: Telegram user ID
            thread_id: Telegram topic thread ID
            window_id: Tmux window ID (e.g. '@0')
            window_name: Display name for the window (optional)
        """
        if user_id not in self.thread_bindings:
            self.thread_bindings[user_id] = {}
        self.thread_bindings[user_id][thread_id] = window_id
        if window_name:
            self.window_display_names[window_id] = window_name
        self._save_state()
        display = window_name or self.get_display_name(window_id)
        logger.info(
            "Bound thread %d -> window_id %s (%s) for user %d",
            thread_id,
            window_id,
            display,
            user_id,
        )

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        """Remove a thread binding. Returns the previously bound window_id, or None."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings or thread_id not in bindings:
            return None
        window_id = bindings.pop(thread_id)
        if not bindings:
            del self.thread_bindings[user_id]
        self._save_state()
        logger.info(
            "Unbound thread %d (was %s) for user %d",
            thread_id,
            window_id,
            user_id,
        )
        return window_id

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        """Look up the window_id bound to a thread."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings:
            return None
        return bindings.get(thread_id)

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
    ) -> str | None:
        """Resolve the tmux window_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id)

    def get_topic_binding(
        self, user_id: int, thread_id: int
    ) -> TopicBinding | None:
        """Resolve a persisted topic binding object."""
        window_id = self.get_window_for_thread(user_id, thread_id)
        if not window_id:
            return None
        return TopicBinding(
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            window_name=self.get_display_name(window_id),
            runtime_kind=self.get_process_descriptor(window_id).runtime_kind,
        )

    def iter_topic_bindings(self) -> Iterator[TopicBinding]:
        """Iterate persisted topic bindings as structured runtime-neutral objects."""
        for user_id, bindings in self.thread_bindings.items():
            for thread_id, window_id in bindings.items():
                yield TopicBinding(
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=window_id,
                    window_name=self.get_display_name(window_id),
                    runtime_kind=self.get_process_descriptor(window_id).runtime_kind,
                )

    def iter_thread_bindings(self) -> Iterator[tuple[int, int, str]]:
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

        pane_text = await tmux_manager.capture_pane(window.window_id)
        if pane_text:
            surface = classify_input_surface(pane_text)
            if surface.kind == "blocked_prompt":
                return False, "Input blocked by a visible prompt in the terminal"

        runtime_kind = (
            self.window_states[window_id].runtime_kind
            if window_id in self.window_states
            else config.default_runtime_kind
        )
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
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"

        runtime_kind = (
            self.window_states[window_id].runtime_kind
            if window_id in self.window_states
            else config.default_runtime_kind
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
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"
        return await runtime_input_driver.send_dispatch(window.window_id, action)

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
            }
            for e in parsed_entries
            if getattr(e, "event_kind", "message") != "lifecycle"
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()
