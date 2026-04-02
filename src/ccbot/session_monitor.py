"""Rollout monitoring service for normalized runtime events.

Runs an async polling loop that:
  1. Loads the current binding map to know which thread ids are active.
  2. Detects binding changes (new/changed/deleted windows) and cleans up.
  3. Reads new JSONL lines from each rollout source using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NormalizedEvent objects.

Optimizations: mtime cache skips unchanged files; byte offset avoids re-reading.

Legacy Claude-shaped names remain as compatibility aliases for the wider codebase.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

import aiofiles

from .config import config
from .codex_threads import normalize_cwd
from .codex_rollout import CodexRolloutNormalizer
from .monitor_state import MonitorState, TrackedSession
from .runtime_types import NormalizedEvent, RolloutSource
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
        self, session: TrackedSession, file_path: Path
    ) -> list[dict]:
        """Read new lines from a session file using byte offset for efficiency.

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
                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s "
                        "(offset %d > size %d). Resetting.",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0

                # Seek to last read position for incremental reading
                await f.seek(session.last_byte_offset)

                # Detect corrupted offset: if we're mid-line (not at '{'),
                # scan forward to the next line start. This can happen if
                # the state file was manually edited or corrupted.
                if session.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s (mid-line), "
                            "scanning to next line",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await f.readline()  # Skip rest of partial line
                        session.last_byte_offset = await f.tell()
                        return []
                    await f.seek(session.last_byte_offset)  # Reset for normal read

                # Read only new lines from the offset.
                # Track safe_offset: advance past valid lines and past
                # malformed complete lines, but stop on a trailing partial
                # write so the next poll can finish the line.
                safe_offset = session.last_byte_offset
                async for line in f:
                    line_end = await f.tell()
                    data = TranscriptParser.parse_line(line)
                    if data:
                        new_entries.append(data)
                        safe_offset = line_end
                    elif line.strip():
                        if line.endswith("\n"):
                            logger.warning(
                                "Corrupted JSONL line in session %s, skipping",
                                session.session_id,
                            )
                            safe_offset = line_end
                            continue
                        logger.warning(
                            "Partial JSONL line in session %s, will retry next cycle",
                            session.session_id,
                        )
                        break
                    else:
                        # Empty line — safe to skip
                        safe_offset = line_end

                session.last_byte_offset = safe_offset

        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
        return new_entries

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

        # Scan projects to get available rollout sources
        rollout_sources = await self.scan_rollout_sources()

        # Only process sources that are bound through the current topic/window map
        for rollout_source in rollout_sources:
            if rollout_source.thread_id not in active_thread_ids:
                continue
            try:
                tracked = self.state.get_session(rollout_source.thread_id)

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
                    self.state.update_session(tracked)
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
                    # File hasn't changed, skip reading
                    continue

                # File changed, read new content from last offset
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
                    parsed_entries = TranscriptParser.parse_codex_rollout_entries(
                        new_entries,
                        thread_id=rollout_source.thread_id,
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
                    if not entry.text and not entry.image_data and entry.event_kind != "lifecycle":
                        continue
                    # Skip user messages unless show_user_messages is enabled
                    if entry.role == "user" and not config.show_user_messages:
                        continue
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
                        )
                    )

                self.state.update_session(tracked)

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
        live_window_ids = {window.window_id for window in await tmux_manager.list_windows()}
        window_to_session: dict[str, str] = {}

        for binding in session_manager.iter_topic_bindings():
            if binding.window_id not in live_window_ids:
                continue
            resolved = await session_manager.resolve_thread_for_window(binding.window_id)
            if resolved is not None and resolved.thread_id:
                window_to_session[binding.window_id] = resolved.thread_id

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
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
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
            logger.info(
                "Window '%s' deleted, removing tracked thread %s",
                window_id,
                old_session_id,
            )
            sessions_to_remove.add(old_session_id)

        # Perform cleanup
        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
            self.state.save_if_dirty()

        # Update last known map
        self._last_binding_map = current_map

        return current_map

    async def _monitor_loop(self) -> None:
        """Background loop for checking session updates.

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
                    if msg.event_kind == "lifecycle":
                        logger.debug(
                            "Lifecycle marker thread=%s: %s",
                            msg.thread_id,
                            msg.tool_name or msg.content_type,
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
