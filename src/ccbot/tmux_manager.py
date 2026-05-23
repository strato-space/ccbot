"""Tmux session/window management via libtmux.

Wraps libtmux to provide async-friendly operations on a single tmux session:
  - list_windows / find_window_by_name: discover live windows.
  - capture_pane: read terminal content (plain or with ANSI colors).
  - send_keys / send_literal_text / send_key / send_enter: forward user input
    or control keys to a window.
  - create_window / kill_window: lifecycle management.

All blocking libtmux calls are wrapped in asyncio.to_thread().

Key class: TmuxManager (singleton instantiated as `tmux_manager`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path

import libtmux

from .config import SENSITIVE_ENV_VARS, config
from .launcher_registration import infer_runtime_kind_from_command
from .runtime_types import runtime_capability_registry
from .state_schema import split_session_map_payload

logger = logging.getLogger(__name__)

RUNTIME_ENV_UNSET_VARS = frozenset(SENSITIVE_ENV_VARS) | {
    "CCBOT_COMMAND",
    "CCBOT_DIR",
    "CCBOT_RESTORE_CHAT_ID",
    "CCBOT_RESTORE_COMMAND",
    "CCBOT_RESTORE_CWD",
    "CCBOT_RESTORE_ENABLED",
    "CCBOT_RESTORE_RETRY_INTERVAL_SECONDS",
    "CCBOT_RESTORE_RETRY_TIMEOUT_SECONDS",
    "CCBOT_RESTORE_RUNTIME_ID",
    "CCBOT_RESTORE_RUNTIME_KIND",
    "CCBOT_RESTORE_SHARED_GROUP",
    "CCBOT_RESTORE_SURFACE_KEY",
    "CCBOT_RESTORE_USER_ID",
    "CCBOT_RESTORE_WINDOW",
    "CCBOT_TELEGRAM_PROXY",
    "CLAUDE_COMMAND",
    "TMUX_SESSION_NAME",
}


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str  # Current working directory
    pane_current_command: str = ""  # Process running in active pane
    pane_id: str = ""  # Active pane id (e.g. "%0")
    pane_pid: str = ""  # Active pane root process id
    pane_ids: tuple[str, ...] = ()  # All pane ids in this window


@dataclass(frozen=True)
class TmuxPane:
    """Information about one pane inside a tmux window."""

    window_id: str
    pane_id: str
    cwd: str
    pane_current_command: str = ""
    pane_active: bool = False
    pane_title: str = ""


@dataclass(frozen=True)
class _RegisteredWindowIdentity:
    thread_id: str
    runtime_kind: str


class TmuxManager:
    """Manages tmux windows for Claude Code sessions."""

    def __init__(self, session_name: str | None = None):
        """Initialize tmux manager.

        Args:
            session_name: Name of the tmux session to use (default from config)
        """
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None

    @property
    def server(self) -> libtmux.Server:
        """Get or create tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            return session

        # Create new session with main window named specifically
        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        # Rename the default window to the main window name
        if session.windows:
            session.windows[0].rename_window(config.tmux_main_window_name)
        self._scrub_session_env(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Remove sensitive env vars from the tmux session environment.

        Prevents new windows (and their child processes like Claude Code)
        from inheriting secrets such as TELEGRAM_BOT_TOKEN.
        """
        for var in RUNTIME_ENV_UNSET_VARS:
            try:
                session.unset_environment(var)
            except Exception:
                pass  # var not set in session env — nothing to remove

    @staticmethod
    def _runtime_env_scrub_prefix() -> str:
        """Return an env(1) prefix that removes controller-only variables."""
        return " ".join(
            [
                "env",
                *(f"-u {shlex.quote(var)}" for var in sorted(RUNTIME_ENV_UNSET_VARS)),
            ]
        )

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the session with their working directories.

        Returns:
            List of TmuxWindow with window info and cwd
        """

        def _sync_list_windows() -> list[TmuxWindow]:
            windows = []
            session = self.get_session()

            if not session:
                return windows

            for window in session.windows:
                name = window.window_name or ""
                # Skip the main window (placeholder window)
                if name == config.tmux_main_window_name:
                    continue

                try:
                    # Get all panes, plus active-pane convenience fields.
                    all_panes = tuple(window.panes or ())
                    pane_ids = tuple(
                        str(getattr(candidate, "pane_id", "") or "")
                        for candidate in all_panes
                        if getattr(candidate, "pane_id", None)
                    )
                    pane = window.active_pane
                    if pane:
                        cwd = pane.pane_current_path or ""
                        pane_cmd = pane.pane_current_command or ""
                        pane_id = pane.pane_id or ""
                        pane_pid = str(getattr(pane, "pane_pid", "") or "")
                    else:
                        cwd = ""
                        pane_cmd = ""
                        pane_id = ""
                        pane_pid = ""

                    windows.append(
                        TmuxWindow(
                            window_id=window.window_id or "",
                            window_name=name,
                            cwd=cwd,
                            pane_current_command=pane_cmd,
                            pane_id=pane_id,
                            pane_pid=pane_pid,
                            pane_ids=pane_ids,
                        )
                    )
                except Exception as e:
                    logger.debug(f"Error getting window info: {e}")

            return windows

        return await asyncio.to_thread(_sync_list_windows)

    async def list_panes(self, window_id: str) -> list[TmuxPane]:
        """List panes for a tmux window.

        This is a read-only topology view. It lets control-plane features map a
        temporary tmux split pane back to the parent bound window without making
        that pane an independently bindable delivery source.
        """

        def _sync_list_panes() -> list[TmuxPane]:
            session = self.get_session()
            if not session:
                return []
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return []
                panes: list[TmuxPane] = []
                active_id = getattr(window.active_pane, "pane_id", "") or ""
                for pane in window.panes or ():
                    pane_id = getattr(pane, "pane_id", "") or ""
                    if not pane_id:
                        continue
                    panes.append(
                        TmuxPane(
                            window_id=window.window_id or window_id,
                            pane_id=pane_id,
                            cwd=getattr(pane, "pane_current_path", "") or "",
                            pane_current_command=getattr(
                                pane,
                                "pane_current_command",
                                "",
                            )
                            or "",
                            pane_active=pane_id == active_id,
                            pane_title=getattr(pane, "pane_title", "") or "",
                        )
                    )
                return panes
            except Exception as e:
                logger.debug("Error listing panes for %s: %s", window_id, e)
                return []

        return await asyncio.to_thread(_sync_list_panes)

    async def _resolve_unique_window_name(
        self,
        desired_name: str,
        *,
        exclude_window_id: str | None = None,
    ) -> tuple[str, bool]:
        """Resolve a deterministic tmux window name with collision suffixes.

        Returns ``(final_name, collision_suffix_applied)``.
        """
        base_name = desired_name.strip()
        if not base_name:
            base_name = config.tmux_main_window_name

        windows = await self.list_windows()
        existing_names = {
            window.window_name
            for window in windows
            if window.window_name and window.window_id != exclude_window_id
        }
        if base_name not in existing_names:
            return base_name, False

        counter = 2
        while f"{base_name}-{counter}" in existing_names:
            counter += 1
        return f"{base_name}-{counter}", True

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its name.

        Args:
            window_name: The window name to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_name == window_name:
                return window
        logger.debug("Window not found by name: %s", window_name)
        return None

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window ID (e.g. '@0', '@12').

        Args:
            window_id: The tmux window ID to match

        Returns:
            TmuxWindow if found, None otherwise
        """
        windows = await self.list_windows()
        for window in windows:
            if window.window_id == window_id:
                return window
        logger.debug("Window not found by id: %s", window_id)
        return None

    async def capture_pane(self, window_id: str, with_ansi: bool = False) -> str | None:
        """Capture the visible text content of a window's active pane.

        Args:
            window_id: The window ID to capture
            with_ansi: If True, capture with ANSI color codes

        Returns:
            The captured text, or None on failure.
        """
        if with_ansi:
            # Use async subprocess to call tmux capture-pane -e for ANSI colors
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux",
                    "capture-pane",
                    "-e",
                    "-p",
                    "-t",
                    window_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return stdout.decode("utf-8")
                logger.error(
                    f"Failed to capture pane {window_id}: {stderr.decode('utf-8')}"
                )
                return None
            except Exception as e:
                logger.error(f"Unexpected error capturing pane {window_id}: {e}")
                return None

        # Original implementation for plain text - wrap in thread
        def _sync_capture() -> str | None:
            session = self.get_session()
            if not session:
                return None
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return None
                pane = window.active_pane
                if not pane:
                    return None
                lines = pane.capture_pane()
                return "\n".join(lines) if isinstance(lines, list) else str(lines)
            except Exception as e:
                logger.error(f"Failed to capture pane {window_id}: {e}")
                return None

        return await asyncio.to_thread(_sync_capture)

    async def _send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Send keys to a specific window."""

        def _sync_send_keys() -> bool:
            session = self.get_session()
            if not session:
                logger.error("No tmux session found")
                return False

            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    logger.error(f"Window {window_id} not found")
                    return False

                pane = window.active_pane
                if not pane:
                    logger.error(f"No active pane in window {window_id}")
                    return False

                pane.send_keys(text, enter=enter, literal=literal)
                return True

            except Exception as e:
                logger.error(f"Failed to send keys to window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_send_keys)

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        """Compatibility wrapper around the raw tmux send-keys primitive."""
        return await self._send_keys(window_id, text, enter=enter, literal=literal)

    async def send_literal_text(self, window_id: str, text: str) -> bool:
        """Send literal text to a pane without submitting it."""
        return await self._send_keys(window_id, text, enter=False, literal=True)

    async def send_pasted_text(self, window_id: str, text: str) -> bool:
        """Paste text through tmux's bracketed-paste aware buffer path.

        This is used for multiline runtime input. Unlike ``send-keys -l``,
        ``paste-buffer -p`` lets alternate-screen TUIs that support bracketed
        paste treat the payload as one paste event rather than as a fast stream
        of character and Enter key events.
        """
        if not text:
            return False

        buffer_name = f"ccbot-{uuid.uuid4().hex}"

        async def _run_tmux(*args: str, stdin: bytes | None = None) -> bool:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "tmux",
                    *args,
                    stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate(stdin)
                if proc.returncode == 0:
                    return True
                logger.error(
                    "tmux %s failed for window %s: %s",
                    " ".join(args[:1]),
                    window_id,
                    stderr.decode("utf-8", errors="replace").strip(),
                )
                return False
            except Exception as e:
                logger.error(
                    "Failed to run tmux for pasted text to %s: %s", window_id, e
                )
                return False

        loaded = await _run_tmux(
            "load-buffer",
            "-b",
            buffer_name,
            "-",
            stdin=text.encode("utf-8"),
        )
        if not loaded:
            return False

        pasted = await _run_tmux(
            "paste-buffer",
            "-p",
            "-d",
            "-b",
            buffer_name,
            "-t",
            window_id,
        )
        if not pasted:
            await _run_tmux("delete-buffer", "-b", buffer_name)
            return False
        return True

    async def send_submit_key(self, window_id: str) -> bool:
        """Send the canonical carriage-return submit key for text input."""
        return await self._send_keys(window_id, "C-m", enter=False, literal=False)

    async def send_key(self, window_id: str, key: str) -> bool:
        """Send a named tmux key such as Escape, Up, Tab, or C-c."""
        return await self._send_keys(window_id, key, enter=False, literal=False)

    async def send_enter(self, window_id: str) -> bool:
        """Send a bare Enter key to the active pane."""
        return await self._send_keys(window_id, "", enter=True, literal=False)

    async def rename_window(self, window_id: str, new_name: str) -> bool:
        """Rename a tmux window by its ID."""

        def _sync_rename() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.rename_window(new_name)
                logger.info("Renamed window %s to '%s'", window_id, new_name)
                return True
            except Exception as e:
                logger.error(f"Failed to rename window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_rename)

    async def rename_window_with_suffixes(
        self,
        window_id: str,
        desired_name: str,
    ) -> tuple[bool, str, str]:
        """Rename a window with deterministic collision suffixes.

        Returns ``(success, message, final_name)``.
        """
        window = await self.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)", ""

        final_name, collision_suffix_applied = await self._resolve_unique_window_name(
            desired_name,
            exclude_window_id=window_id,
        )
        current_name = window.window_name or ""
        if current_name == final_name:
            return True, f"Window already named '{final_name}'", final_name

        renamed = await self.rename_window(window_id, final_name)
        if not renamed:
            return False, f"Failed to rename window '{current_name or window_id}'", ""

        if collision_suffix_applied and final_name != desired_name.strip():
            return (
                True,
                f"Renamed window '{current_name or window_id}' to '{final_name}' "
                f"(collision with '{desired_name.strip()}')",
                final_name,
            )
        return True, f"Renamed window to '{final_name}'", final_name

    def _build_runtime_launch_command(
        self,
        path: Path,
        *,
        start_runtime: bool,
        resume_session_id: str | None = None,
        runtime_kind: str | None = None,
        launch_command: str | None = None,
    ) -> str:
        """Build the exact shell command used to start or resume a runtime."""
        if not start_runtime:
            return ""
        configured_runtime_kind = infer_runtime_kind_from_command(config.ccbot_command)
        source_command = launch_command or config.ccbot_command
        inferred_runtime_kind = runtime_kind or infer_runtime_kind_from_command(
            source_command
        )
        cmd = runtime_capability_registry.build_launch_command(
            inferred_runtime_kind,
            base_command=(
                launch_command
                or (
                    config.ccbot_command
                    if runtime_kind is None
                    or inferred_runtime_kind == configured_runtime_kind
                    else None
                )
            ),
            resume_session_id=resume_session_id,
        )
        return (
            f"cd {shlex.quote(str(path))} && {self._runtime_env_scrub_prefix()} {cmd}"
        )

    def _registered_window_identity(
        self,
        window: TmuxWindow,
        *,
        expected_cwd: Path,
    ) -> _RegisteredWindowIdentity | None:
        """Return a verified hook-registered live identity for a tmux window.

        The tmux window id is the only non-ambiguous key for a live process.
        Legacy name-keyed entries are intentionally not accepted here because
        this identity gates /resume reuse and must fail closed rather than bind
        a requested thread to a same-name, unrelated live runtime.
        """
        if not config.session_map_file.exists():
            return None
        try:
            raw = json.loads(config.session_map_file.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        entries, _, _ = split_session_map_payload(raw)
        info = entries.get(f"{self.session_name}:{window.window_id}")
        if not isinstance(info, dict):
            return None

        thread_id = str(info.get("thread_id") or info.get("session_id") or "").strip()
        if not thread_id:
            return None

        registered_cwd = str(info.get("cwd") or "").strip()
        if registered_cwd:
            try:
                resolved_cwd = str(
                    Path(registered_cwd).expanduser().resolve(strict=False)
                )
            except (OSError, RuntimeError, ValueError):
                resolved_cwd = str(Path(registered_cwd).expanduser())
            if resolved_cwd != str(expected_cwd):
                return None

        registered_window_name = str(info.get("window_name") or "").strip()
        if (
            registered_window_name
            and window.window_name
            and registered_window_name != window.window_name
        ):
            return None

        return _RegisteredWindowIdentity(
            thread_id=thread_id,
            runtime_kind=str(info.get("runtime_kind") or "").strip(),
        )

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by its ID."""

        def _sync_kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                window = session.windows.get(window_id=window_id)
                if not window:
                    return False
                window.kill()
                logger.info("Killed window %s", window_id)
                return True
            except Exception as e:
                logger.error(f"Failed to kill window {window_id}: {e}")
                return False

        return await asyncio.to_thread(_sync_kill)

    async def create_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
        runtime_kind: str | None = None,
        launch_command: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally start the configured runtime.

        Args:
            work_dir: Working directory for the new window
            window_name: Optional window name (defaults to directory name)
            start_claude: Whether to start the configured runtime command
            resume_session_id: If set, resume the persisted thread/session id
            runtime_kind: Optional explicit runtime kind for the launch
            launch_command: Optional explicit runtime launch command override

        Returns:
            Tuple of (success, message, window_name, window_id)
        """
        # Validate directory first
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", ""
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", ""

        # Create window name, adding suffix if name already exists
        final_window_name = window_name if window_name else path.name

        # Check for existing window name
        base_name = final_window_name
        counter = 2
        while await self.find_window_by_name(final_window_name):
            final_window_name = f"{base_name}-{counter}"
            counter += 1

        # Create window in thread
        def _create_and_start() -> tuple[bool, str, str, str]:
            session = self.get_or_create_session()
            try:
                # Create new window
                window = session.new_window(
                    window_name=final_window_name,
                    start_directory=str(path),
                )

                wid = window.window_id or ""

                # Prevent Claude Code from overriding window name
                window.set_window_option("allow-rename", "off")

                # Start the configured runtime command if requested.
                # Explicitly `cd` into the selected directory because shell init
                # files may override tmux's start_directory on some hosts.
                if start_claude:
                    pane = window.active_pane
                    if pane:
                        launch_cmd = self._build_runtime_launch_command(
                            path,
                            start_runtime=start_claude,
                            resume_session_id=resume_session_id,
                            runtime_kind=runtime_kind,
                            launch_command=launch_command,
                        )
                        pane.send_keys(launch_cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_window_name,
                    wid,
                    path,
                )
                return (
                    True,
                    f"Created window '{final_window_name}' at {path}",
                    final_window_name,
                    wid,
                )

            except Exception as e:
                logger.error(f"Failed to create window: {e}")
                return False, f"Failed to create window: {e}", "", ""

        return await asyncio.to_thread(_create_and_start)

    async def create_or_reuse_window(
        self,
        work_dir: str,
        window_name: str | None = None,
        start_claude: bool = True,
        resume_session_id: str | None = None,
        runtime_kind: str | None = None,
        launch_command: str | None = None,
        reuse_existing: bool = True,
    ) -> tuple[bool, str, str, str, bool]:
        """Create a new tmux window or reuse an exact live match.

        Reuse is fail-closed: the live window must match by exact name and
        directory, and any active runtime command must match the requested
        runtime kind when one is supplied.
        """
        path = Path(work_dir).expanduser().resolve()
        if not path.exists():
            return False, f"Directory does not exist: {work_dir}", "", "", False
        if not path.is_dir():
            return False, f"Not a directory: {work_dir}", "", "", False

        final_window_name = window_name if window_name else path.name

        if reuse_existing:
            existing = await self.find_window_by_name(final_window_name)
            if existing:
                try:
                    existing_cwd = str(
                        Path(existing.cwd).expanduser().resolve(strict=False)
                    )
                except (OSError, RuntimeError, ValueError):
                    existing_cwd = (
                        str(Path(existing.cwd).expanduser()) if existing.cwd else ""
                    )
                if existing_cwd and existing_cwd != str(path):
                    return (
                        False,
                        (
                            f"Existing window '{final_window_name}' is bound to "
                            f"{existing_cwd}, not {path}"
                        ),
                        "",
                        "",
                        False,
                    )

                active_runtime = (
                    runtime_capability_registry.known_runtime_kind_from_command(
                        existing.pane_current_command
                    )
                )
                requested_runtime = runtime_kind or infer_runtime_kind_from_command(
                    launch_command or config.ccbot_command
                )
                registered_identity = self._registered_window_identity(
                    existing,
                    expected_cwd=path,
                )
                if active_runtime is None and registered_identity:
                    active_runtime = (
                        registered_identity.runtime_kind or requested_runtime
                    )
                if active_runtime and active_runtime != requested_runtime:
                    return (
                        False,
                        (
                            f"Existing window '{final_window_name}' is running "
                            f"{active_runtime}, not {requested_runtime}"
                        ),
                        "",
                        "",
                        False,
                    )

                if start_claude and resume_session_id and active_runtime:
                    if registered_identity is None:
                        return (
                            False,
                            (
                                f"Existing window '{final_window_name}' is running "
                                f"{active_runtime} but has no verified live "
                                "runtime identity for resume"
                            ),
                            "",
                            "",
                            False,
                        )
                    if registered_identity.thread_id != resume_session_id:
                        return (
                            False,
                            (
                                f"Existing window '{final_window_name}' is running "
                                f"{active_runtime} thread "
                                f"{registered_identity.thread_id}, not "
                                f"{resume_session_id}"
                            ),
                            "",
                            "",
                            False,
                        )

                if start_claude and active_runtime is None:
                    launch_cmd = self._build_runtime_launch_command(
                        path,
                        start_runtime=start_claude,
                        resume_session_id=resume_session_id,
                        runtime_kind=runtime_kind,
                        launch_command=launch_command,
                    )
                    if not await self.send_literal_text(existing.window_id, launch_cmd):
                        return (
                            False,
                            f"Failed to inject resume command into '{final_window_name}'",
                            "",
                            "",
                            False,
                        )
                    if not await self.send_enter(existing.window_id):
                        return (
                            False,
                            f"Failed to submit resume command into '{final_window_name}'",
                            "",
                            "",
                            False,
                        )
                    return (
                        True,
                        f"Reused window '{final_window_name}' at {path} and launched runtime",
                        existing.window_name,
                        existing.window_id,
                        True,
                    )

                return (
                    True,
                    f"Reused window '{final_window_name}' at {path}",
                    existing.window_name,
                    existing.window_id,
                    True,
                )

        success, message, created_name, created_id = await self.create_window(
            str(path),
            window_name=window_name,
            start_claude=start_claude,
            resume_session_id=resume_session_id,
            runtime_kind=runtime_kind,
            launch_command=launch_command,
        )
        return success, message, created_name, created_id, False


# Global instance with default session name
tmux_manager = TmuxManager()
