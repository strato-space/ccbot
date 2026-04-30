"""Replay-evidence monitor for normalized runtime events.

Runs an async polling loop that:
  1. Loads the current binding map to know which thread ids are active.
  2. Detects binding changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each tracked replay source using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NormalizedEvent objects.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

The monitor observes the live semantic stream of each runtime by tailing the
runtime's replay evidence artifact. For Claude this is the transcript tail,
for Codex it is rollout JSONL, and for fast-agent it is the ACP-equivalent
side-channel mirror. Legacy Claude-shaped names remain as compatibility
aliases for the wider codebase.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiofiles

from .config import config
from .codex_threads import normalize_cwd
from .codex_rollout import CodexRolloutNormalizer, CodexRolloutState
from .monitor_state import MonitorState, TrackedSession
from .runtime_types import NormalizedEvent, RolloutSource, USER_ECHO_SEMANTIC_KIND
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import read_cwd_from_jsonl

logger = logging.getLogger(__name__)

SessionInfo = RolloutSource
NewMessage = NormalizedEvent


class SessionMonitor:
    """Monitors runtime rollout sources for new normalized events."""

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        self.projects_path = (
            projects_path if projects_path is not None else config.claude_projects_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NormalizedEvent], Awaitable[None]] | None = (
            None
        )
        # Per-thread pending tool_use state carried across poll cycles
        self._pending_tools: dict[str, dict[str, Any]] = {}  # thread_id -> pending
        # Track last known binding map for detecting changes
        # Keys may be window_id (@12) or window_name (old format) during transition
        self._last_binding_map: dict[str, str] = {}  # window_key -> thread_id
        # Active runtime-resolved replay sources for the current binding map.
        self._active_rollout_sources: dict[str, RolloutSource] = {}
        # Per-thread Codex rollout synthesis state carried across poll cycles.
        self._codex_rollout_states: dict[str, CodexRolloutState] = {}
        # In-memory mtime cache for quick file change detection (not persisted)
        self._file_mtimes: dict[str, float] = {}  # thread_id -> last_seen_mtime

    def set_message_callback(
        self, callback: Callable[[NormalizedEvent], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    async def _get_active_cwds(self) -> set[str]:
        """Get normalized cwds of all active tmux windows."""
        cwds = set()
        windows = await tmux_manager.list_windows()
        for w in windows:
            cwds.add(normalize_cwd(w.cwd))
        return cwds

    async def scan_rollout_sources(self) -> list[RolloutSource]:
        """Scan active projects and return readable rollout sources."""
        active_cwds = await self._get_active_cwds()
        if not active_cwds:
            return []

        rollout_sources: list[RolloutSource] = []

        if not self.projects_path.exists():
            return rollout_sources

        for project_dir in self.projects_path.iterdir():
            if not project_dir.is_dir():
                continue

            index_file = project_dir / "sessions-index.json"
            original_path = ""
            indexed_ids: set[str] = set()

            if index_file.exists():
                try:
                    async with aiofiles.open(index_file, "r") as f:
                        content = await f.read()
                    index_data = json.loads(content)
                    entries = index_data.get("entries", [])
                    original_path = index_data.get("originalPath", "")

                    for entry in entries:
                        thread_id = entry.get("sessionId", "")
                        full_path = entry.get("fullPath", "")
                        project_path = entry.get("projectPath", original_path)

                        if not thread_id or not full_path:
                            continue

                        norm_pp = normalize_cwd(project_path)
                        if norm_pp not in active_cwds:
                            continue

                        indexed_ids.add(thread_id)
                        file_path = Path(full_path)
                        if file_path.exists():
                            rollout_sources.append(
                                RolloutSource(
                                    thread_id=thread_id,
                                    file_path=file_path,
                                )
                            )

                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(f"Error reading index {index_file}: {e}")

            # Pick up un-indexed .jsonl files
            try:
                for jsonl_file in project_dir.glob("*.jsonl"):
                    thread_id = jsonl_file.stem
                    if thread_id in indexed_ids:
                        continue

                    # Determine project_path for this file
                    file_project_path = original_path
                    if not file_project_path:
                        file_project_path = await asyncio.to_thread(
                            read_cwd_from_jsonl, jsonl_file
                        )
                    if not file_project_path:
                        dir_name = project_dir.name
                        if dir_name.startswith("-"):
                            file_project_path = dir_name.replace("-", "/")

                    norm_fp = normalize_cwd(file_project_path)

                    if norm_fp not in active_cwds:
                        continue

                    rollout_sources.append(
                        RolloutSource(
                            thread_id=thread_id,
                            file_path=jsonl_file,
                        )
                    )
            except OSError as e:
                logger.debug(f"Error scanning jsonl files in {project_dir}: {e}")

        return rollout_sources

    async def scan_projects(self) -> list[SessionInfo]:
        """Backward-compatible wrapper for legacy call sites."""
        return await self.scan_rollout_sources()

    async def _read_new_lines(
        self, tracked_source: TrackedSession, file_path: Path
    ) -> list[dict]:
        """Read new lines from replay evidence using byte offset for efficiency.

        Detects file truncation (e.g. after /clear) and resets offset.
        Recovers from corrupted offsets (mid-line) by scanning to next line.
        """
        new_entries = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                # Get file size to detect truncation
                await f.seek(0, 2)  # Seek to end
                file_size = await f.tell()

                # Detect file truncation: if offset is beyond file size, reset
                if tracked_source.last_byte_offset > file_size:
                    logger.info(
                        "Replay evidence truncated for thread %s "
                        "(offset %d > size %d). Resetting.",
                        tracked_source.thread_id,
                        tracked_source.last_byte_offset,
                        file_size,
                    )
                    tracked_source.last_byte_offset = 0
                    self._codex_rollout_states.pop(tracked_source.thread_id, None)

                # Seek to last read position for incremental reading
                await f.seek(tracked_source.last_byte_offset)

                # Detect corrupted offset: if we're mid-line (not at '{'),
                # scan forward to the next line start. This can happen if
                # the state file was manually edited or corrupted.
                if tracked_source.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in thread %s replay evidence "
                            "(mid-line), "
                            "scanning to next line",
                            tracked_source.last_byte_offset,
                            tracked_source.thread_id,
                        )
                        await f.readline()  # Skip rest of partial line
                        tracked_source.last_byte_offset = await f.tell()
                        return []
                    await f.seek(tracked_source.last_byte_offset)  # Reset for normal read

                # Read only new lines from the offset.
                # Track safe_offset: advance past valid lines and past
                # malformed complete lines, but stop on a trailing partial
                # write so the next poll can finish the line.
                safe_offset = tracked_source.last_byte_offset
                async for line in f:
                    line_end = await f.tell()
                    data = TranscriptParser.parse_line(line)
                    if data:
                        new_entries.append(data)
                        safe_offset = line_end
                    elif line.strip():
                        if line.endswith("\n"):
                            logger.warning(
                                "Corrupted JSONL line in thread %s replay evidence, skipping",
                                tracked_source.thread_id,
                            )
                            safe_offset = line_end
                            continue
                        logger.warning(
                            "Partial JSONL line in thread %s replay evidence, will retry next cycle",
                            tracked_source.thread_id,
                        )
                        break
                    else:
                        # Empty line — safe to skip
                        safe_offset = line_end

                tracked_source.last_byte_offset = safe_offset

        except OSError as e:
            logger.error("Error reading replay evidence %s: %s", file_path, e)
        return new_entries

    async def _hydrate_codex_rollout_state(
        self,
        tracked_source: TrackedSession,
        file_path: Path,
        *,
        up_to_offset: int | None = None,
    ) -> CodexRolloutState:
        """Rebuild cross-poll Codex synthesis state up to the tracked offset."""
        state = CodexRolloutState()
        target_offset = tracked_source.last_byte_offset if up_to_offset is None else up_to_offset
        if target_offset <= 0:
            return state

        def _read_prefix() -> list[dict]:
            entries: list[dict] = []
            with file_path.open("r", encoding="utf-8") as handle:
                while handle.tell() < target_offset:
                    line = handle.readline()
                    if not line:
                        break
                    if handle.tell() > target_offset and not line.endswith("\n"):
                        break
                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
            return entries

        try:
            prefix_entries = await asyncio.to_thread(_read_prefix)
        except OSError as e:
            logger.debug(
                "Failed to hydrate Codex rollout state for %s: %s",
                tracked_source.thread_id,
                e,
            )
            return state

        if prefix_entries:
            TranscriptParser.parse_codex_rollout_entries(
                prefix_entries,
                thread_id=tracked_source.thread_id,
                state=state,
            )
            # Historical replay rebuilds long-lived synthesis state, not
            # in-flight duplicate buffers. Otherwise unmatched historical
            # `event_msg` copies can flush again after restart/state eviction.
            state.pending_event_messages.clear()
            # Preserve duplicate-suppression buffers only for the active turn.
            # This lets the later canonical user copy for the same turn collapse
            # correctly after restart, while still preventing historical
            # unmatched event_msg fallbacks from re-flushing.
            active_turn_key = state.current_turn_key or f"surrogate:{state.turn_generation}"
            state.recent_user_event_messages = {
                signature: emitted_at
                for signature, emitted_at in state.recent_user_event_messages.items()
                if signature[0] == active_turn_key
            }
            state.canonical_message_signatures = {
                signature
                for signature in state.canonical_message_signatures
                if signature[0] == active_turn_key
            }
        return state

    async def _codex_rollout_state_for_thread(
        self,
        tracked_source: TrackedSession,
        rollout_source: RolloutSource,
        *,
        up_to_offset: int | None = None,
    ) -> CodexRolloutState:
        """Return the incremental Codex rollout state for a thread."""
        existing = self._codex_rollout_states.get(tracked_source.thread_id)
        if existing is not None:
            return existing
        state = await self._hydrate_codex_rollout_state(
            tracked_source,
            rollout_source.file_path,
            up_to_offset=up_to_offset,
        )
        self._codex_rollout_states[tracked_source.thread_id] = state
        return state

    async def check_for_updates(
        self, active_thread_ids: set[str]
    ) -> list[NormalizedEvent]:
        """Check all active threads for new normalized rollout events.

        Reads from last byte offset. Emits both intermediate
        (stop_reason=null) and complete messages.

        Args:
            active_thread_ids: Set of persisted thread ids currently in the binding map
        """
        new_messages = []

        rollout_sources: list[RolloutSource] = [
            rollout_source
            for thread_id, rollout_source in self._active_rollout_sources.items()
            if thread_id in active_thread_ids
        ]
        known_thread_ids = {rollout_source.thread_id for rollout_source in rollout_sources}
        missing_thread_ids = active_thread_ids - known_thread_ids

        # Legacy fallback: old tests and Claude-only flows may still patch the
        # project scanner directly. Runtime-aware sources win when available.
        if missing_thread_ids:
            for rollout_source in await self.scan_rollout_sources():
                if rollout_source.thread_id not in missing_thread_ids:
                    continue
                rollout_sources.append(rollout_source)
                known_thread_ids.add(rollout_source.thread_id)
                missing_thread_ids.discard(rollout_source.thread_id)

        # Only process sources that are bound through the current topic/window map
        for rollout_source in rollout_sources:
            if rollout_source.thread_id not in active_thread_ids:
                continue
            try:
                tracked = self.state.get_tracked_source(rollout_source.thread_id)

                if tracked is None:
                    # For new threads, initialize offset to end of file
                    # to avoid re-processing old messages
                    try:
                        file_size = rollout_source.file_path.stat().st_size
                        current_mtime = rollout_source.file_path.stat().st_mtime
                    except OSError:
                        file_size = 0
                        current_mtime = 0.0
                    tracked = TrackedSession(
                        session_id=rollout_source.thread_id,
                        file_path=str(rollout_source.file_path),
                        last_byte_offset=file_size,
                    )
                    self.state.update_tracked_source(tracked)
                    self._file_mtimes[rollout_source.thread_id] = current_mtime
                    logger.info(
                        "Started tracking rollout source for thread: %s",
                        rollout_source.thread_id,
                    )
                    continue

                # Check mtime + file size to see if file has changed
                try:
                    st = rollout_source.file_path.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                except OSError:
                    continue

                last_mtime = self._file_mtimes.get(rollout_source.thread_id, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    if rollout_source.runtime_kind == "codex":
                        codex_state = self._codex_rollout_states.get(
                            rollout_source.thread_id
                        )
                        if (
                            codex_state is not None
                            and codex_state.pending_event_messages
                        ):
                            parsed_entries = TranscriptParser.parse_codex_rollout_entries(
                                [],
                                thread_id=rollout_source.thread_id,
                                state=codex_state,
                            )
                            for entry in parsed_entries:
                                if (
                                    not entry.text
                                    and not entry.image_data
                                    and entry.delivery_class != "lifecycle"
                                ):
                                    continue
                                if (
                                    entry.role == "user"
                                    and not config.show_user_messages
                                    and entry.dispatch_to_telegram
                                ):
                                    entry.dispatch_to_telegram = False
                                new_messages.append(
                                    NormalizedEvent(
                                        thread_id=rollout_source.thread_id,
                                        text=entry.text,
                                        is_complete=entry.is_complete,
                                        content_type=entry.content_type,
                                        tool_use_id=entry.tool_use_id,
                                        role=entry.role,
                                        tool_name=entry.tool_name,
                                        image_data=entry.image_data,
                                        timestamp=entry.timestamp,
                                        runtime_kind=entry.runtime_kind,
                                        event_kind=entry.event_kind,
                                        semantic_kind=entry.semantic_kind,
                                        delivery_class=entry.delivery_class,
                                        include_in_history=entry.include_in_history,
                                        dispatch_to_telegram=entry.dispatch_to_telegram,
                                        status_message_eligible=entry.status_message_eligible,
                                    )
                                )
                    # File hasn't changed, skip reading
                    continue

                # File changed, read new content from last offset
                previous_offset = tracked.last_byte_offset
                new_entries = await self._read_new_lines(tracked, rollout_source.file_path)
                self._file_mtimes[rollout_source.thread_id] = current_mtime

                if new_entries:
                    logger.debug(
                        "Read %d new entries for thread %s",
                        len(new_entries),
                        rollout_source.thread_id,
                    )

                # Parse new entries using the runtime-aware normalizer.
                if new_entries and all(
                    CodexRolloutNormalizer.is_codex_rollout_record(entry)
                    for entry in new_entries
                    if isinstance(entry, dict)
                ):
                    codex_state = await self._codex_rollout_state_for_thread(
                        tracked,
                        rollout_source,
                        up_to_offset=previous_offset,
                    )
                    parsed_entries = TranscriptParser.parse_codex_rollout_entries(
                        new_entries,
                        thread_id=rollout_source.thread_id,
                        state=codex_state,
                    )
                    self._pending_tools.pop(rollout_source.thread_id, None)
                else:
                    carry = self._pending_tools.get(rollout_source.thread_id, {})
                    parsed_entries, remaining = TranscriptParser.parse_entries(
                        new_entries,
                        pending_tools=carry,
                    )
                    if remaining:
                        self._pending_tools[rollout_source.thread_id] = remaining
                    else:
                        self._pending_tools.pop(rollout_source.thread_id, None)

                for entry in parsed_entries:
                    if (
                        not entry.text
                        and not entry.image_data
                        and entry.delivery_class != "lifecycle"
                    ):
                        continue
                    if (
                        entry.role == "user"
                        and not config.show_user_messages
                        and entry.dispatch_to_telegram
                    ):
                        entry.dispatch_to_telegram = False
                    new_messages.append(
                        NormalizedEvent(
                            thread_id=rollout_source.thread_id,
                            text=entry.text,
                            is_complete=entry.is_complete,
                            content_type=entry.content_type,
                            tool_use_id=entry.tool_use_id,
                            role=entry.role,
                            tool_name=entry.tool_name,
                            image_data=entry.image_data,
                            timestamp=entry.timestamp,
                            runtime_kind=entry.runtime_kind,
                            event_kind=entry.event_kind,
                            semantic_kind=entry.semantic_kind,
                            delivery_class=entry.delivery_class,
                            include_in_history=entry.include_in_history,
                            dispatch_to_telegram=entry.dispatch_to_telegram,
                            status_message_eligible=entry.status_message_eligible,
                        )
                    )

                self.state.update_tracked_source(tracked)

            except OSError as e:
                logger.debug(
                    "Error processing rollout source for thread %s: %s",
                    rollout_source.thread_id,
                    e,
                )

        self.state.save_if_dirty()
        return new_messages

    async def _load_current_session_map(self) -> dict[str, str]:
        """Build the current live binding map from topic bindings plus resolution.

        The source of truth is the persisted topic/window binding and live
        process registration state, not the legacy Claude hook map alone.
        """
        from .session import session_manager

        await session_manager.load_session_map()
        session_manager.cleanup_helper_window_bindings()
        live_window_ids = {
            window.window_id for window in await tmux_manager.list_windows()
        }
        window_to_session: dict[str, str] = {}
        active_rollout_sources: dict[str, RolloutSource] = {}

        for binding in session_manager.iter_topic_bindings():
            tmux_window_missing = (
                binding.binding_scope == "tmux"
                and binding.window_id not in live_window_ids
            )
            resolved = await session_manager.resolve_thread_for_window(binding.window_id)
            if tmux_window_missing and (
                resolved is None
                or not resolved.thread_id
                or not resolved.file_path
                or not Path(resolved.file_path).exists()
            ):
                continue
            if resolved is not None and resolved.thread_id:
                window_to_session[binding.window_id] = resolved.thread_id
                if resolved.file_path:
                    active_rollout_sources[resolved.thread_id] = RolloutSource(
                        thread_id=resolved.thread_id,
                        file_path=Path(resolved.file_path),
                        runtime_kind=resolved.runtime_kind,
                        cwd=resolved.cwd,
                    )

        self._active_rollout_sources = active_rollout_sources

        return window_to_session

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up tracked threads that are no longer in the binding map."""
        current_map = await self._load_current_session_map()
        active_thread_ids = set(current_map.values())

        stale_sessions = []
        for session_id in self.state.tracked_sessions.keys():
            if session_id not in active_thread_ids:
                stale_sessions.append(session_id)

        if stale_sessions:
            logger.info(
                f"[Startup cleanup] Removing {len(stale_sessions)} stale sessions"
            )
            for session_id in stale_sessions:
                self.state.remove_tracked_source(session_id)
                self._file_mtimes.pop(session_id, None)
                self._codex_rollout_states.pop(session_id, None)
            self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, str]:
        """Detect binding map changes and cleanup replaced/removed thread trackers.

        Returns current binding map for further processing.
        """
        current_map = await self._load_current_session_map()

        sessions_to_remove: set[str] = set()

        # Check for thread changes in live windows.
        for window_id, old_session_id in self._last_binding_map.items():
            new_session_id = current_map.get(window_id)
            if new_session_id and new_session_id != old_session_id:
                logger.info(
                    "Window '%s' thread changed: %s -> %s",
                    window_id,
                    old_session_id,
                    new_session_id,
                )
                sessions_to_remove.add(old_session_id)

        # Check for deleted windows (window in old map but not in current)
        old_windows = set(self._last_binding_map.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows

        for window_id in deleted_windows:
            old_session_id = self._last_binding_map[window_id]
            if old_session_id in current_map.values():
                logger.info(
                    "Window '%s' disappeared but thread %s is still reachable via another binding",
                    window_id,
                    old_session_id,
                )
                continue
            logger.info(
                "Window '%s' deleted, removing tracked thread %s",
                window_id,
                old_session_id,
            )
            sessions_to_remove.add(old_session_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_tracked_source(session_id)
                self._file_mtimes.pop(session_id, None)
                self._codex_rollout_states.pop(session_id, None)
            self.state.save_if_dirty()

        # Update last known map
        self._last_binding_map = current_map

        return current_map

    async def _monitor_loop(self) -> None:
        """Background loop for checking replay-evidence updates.

        Uses simple async polling with aiofiles for non-blocking I/O.
        """
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        # Deferred import to avoid circular dependency (cached once)
        from .session import session_manager

        # Clean up all stale sessions on startup
        await self._cleanup_all_stale_sessions()
        # Initialize last known binding map
        self._last_binding_map = await self._load_current_session_map()

        while self._running:
            try:
                # Load hook-based window -> thread updates
                await session_manager.load_session_map()

                # Detect binding changes and cleanup replaced/removed thread trackers
                current_map = await self._detect_and_cleanup_changes()
                active_thread_ids = set(current_map.values())

                # Check for new messages (all I/O is async)
                new_messages = await self.check_for_updates(active_thread_ids)

                for msg in new_messages:
                    if (
                        not msg.dispatch_to_telegram
                        and msg.semantic_kind != USER_ECHO_SEMANTIC_KIND
                    ):
                        logger.debug(
                            "Lifecycle marker thread=%s: %s",
                            msg.thread_id,
                            msg.tool_name or msg.semantic_kind,
                        )
                        continue
                    status = "complete" if msg.is_complete else "streaming"
                    preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
                    logger.info("[%s] thread=%s: %s", status, msg.thread_id, preview)
                    if self._message_callback:
                        try:
                            await self._message_callback(msg)
                        except Exception as e:
                            logger.error(f"Message callback error: {e}")

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(self.poll_interval)

        logger.info("Session monitor stopped")

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state.save()
        logger.info("Session monitor stopped and state saved")
