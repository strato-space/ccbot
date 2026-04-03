"""Fast-agent session catalog adapter.

The adapter keeps tmux as the live operator surface and treats fast-agent
session metadata as persisted identity plus replay evidence. It discovers
sessions from the confirmed ``.fast-agent/sessions`` tree, exposes title-only
rename semantics, and keeps persisted session-id rename explicitly unsupported.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runtime_types import ThreadLocator

_SESSION_ID_RE = re.compile(
    r"^(?:[A-Za-z0-9]{6}|\d{10}-[A-Za-z0-9]{6})$"
)


def _resolve_environment_root(cwd: str | Path) -> Path:
    base = Path(cwd).expanduser().resolve()
    override = os.getenv("ENVIRONMENT_DIR")
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute():
            root = (base / root).resolve()
        else:
            root = root.resolve()
    else:
        root = base / ".fast-agent"
    return root


def _parse_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _count_json_message_entries(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return 0

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return sum(1 for line in raw.splitlines() if line.strip())

    if isinstance(parsed, dict):
        messages = parsed.get("messages")
        if isinstance(messages, list):
            return len(messages)
        rows = parsed.get("rows")
        if isinstance(rows, list):
            return len(rows)
        return 1

    if isinstance(parsed, list):
        return len(parsed)

    return 0


def _extract_title(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    title = metadata.get("title") or metadata.get("label") or metadata.get(
        "first_user_preview"
    )
    if title is None:
        return ""
    return " ".join(str(title).split())


@dataclass(frozen=True)
class FastAgentSessionCandidate:
    """A deterministic, file-backed fast-agent session candidate."""

    session_id: str
    session_title: str
    cwd: str
    session_dir: Path
    session_file: Path
    history_files: tuple[Path, ...]
    replay_file: Path
    message_count: int
    updated_at: str = ""
    mtime: float = 0.0
    preview: str = ""

    @property
    def thread_id(self) -> str:
        return self.session_id

    @property
    def thread_name(self) -> str:
        return self.session_title

    @property
    def normalized_cwd(self) -> str:
        return str(Path(self.cwd).expanduser().resolve(strict=False))

    @property
    def ordering_timestamp(self) -> float:
        if self.updated_at:
            return _parse_timestamp(self.updated_at)
        return self.mtime

    @property
    def summary(self) -> str:
        title = self.session_title.strip() or self.preview.strip()
        return title or self.session_id

    def to_locator(self) -> ThreadLocator:
        return ThreadLocator(
            thread_id=self.session_id,
            summary=self.summary,
            message_count=self.message_count,
            file_path=str(self.replay_file),
            cwd=self.cwd,
        )


@dataclass(frozen=True)
class FastAgentSessionResolution:
    """Result returned by explicit or cwd-based fast-agent resolution."""

    status: str
    selected: FastAgentSessionCandidate | None
    candidates: tuple[FastAgentSessionCandidate, ...] = ()
    reason: str = ""

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def is_selected(self) -> bool:
        return self.selected is not None


class FastAgentSessionCatalog:
    """Adapter that enumerates and resolves persisted fast-agent sessions."""

    def __init__(self, environment_root: Path | None = None) -> None:
        self.environment_root = (
            environment_root.expanduser().resolve()
            if environment_root is not None
            else None
        )
        self._cached_roots: dict[str, tuple[FastAgentSessionCandidate, ...]] = {}

    def refresh(self) -> None:
        self._cached_roots.clear()

    def _session_root(self, cwd: str | Path) -> Path:
        root = self.environment_root or _resolve_environment_root(cwd)
        return root / "sessions"

    def _load_candidate(self, session_json: Path, cwd: str) -> FastAgentSessionCandidate | None:
        payload = _load_json(session_json)
        if not payload:
            return None

        session_dir = session_json.parent
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        session_id = str(
            payload.get("name")
            or metadata.get("session_id")
            or metadata.get("acp_session_id")
            or session_dir.name
            or ""
        ).strip()
        if not session_id:
            return None

        session_title = _extract_title(metadata)
        preview = str(metadata.get("first_user_preview") or "").strip()

        history_names = payload.get("history_files")
        if not isinstance(history_names, list) or not history_names:
            history_names = sorted(path.name for path in session_dir.glob("history_*.json"))

        history_files = tuple(
            candidate_path
            for name in history_names
            if isinstance(name, str)
            for candidate_path in [session_dir / name]
            if candidate_path.is_file()
        )
        replay_file = session_dir / "acp_log.jsonl"
        if not replay_file.exists():
            replay_file = history_files[-1] if history_files else session_json

        message_count = sum(_count_json_message_entries(path) for path in history_files)
        if message_count == 0 and replay_file.is_file():
            message_count = _count_json_message_entries(replay_file)

        timestamps = [session_json.stat().st_mtime]
        for candidate_path in (*history_files, replay_file):
            try:
                timestamps.append(candidate_path.stat().st_mtime)
            except OSError:
                continue

        updated_at = str(
            payload.get("last_activity")
            or payload.get("created_at")
            or metadata.get("updated_at")
            or ""
        ).strip()
        if not updated_at:
            updated_at = ""

        return FastAgentSessionCandidate(
            session_id=session_id,
            session_title=session_title or session_id,
            cwd=str(Path(cwd).expanduser().resolve(strict=False)),
            session_dir=session_dir,
            session_file=session_json,
            history_files=history_files,
            replay_file=replay_file,
            message_count=message_count,
            updated_at=updated_at,
            mtime=max(timestamps),
            preview=preview,
        )

    def _load_candidates_for_root(self, cwd: str | Path) -> tuple[FastAgentSessionCandidate, ...]:
        root = self._session_root(cwd)
        if not root.exists():
            return ()

        session_json_files = sorted(
            (
                path
                for path in root.rglob("session.json")
                if path.is_file()
            ),
            key=lambda path: str(path),
        )

        candidates: list[FastAgentSessionCandidate] = []
        normalized_cwd = str(Path(cwd).expanduser().resolve(strict=False))
        for session_json in session_json_files:
            candidate = self._load_candidate(session_json, normalized_cwd)
            if candidate is None:
                continue
            candidates.append(candidate)

        candidates.sort(
            key=lambda candidate: (
                0 if candidate.updated_at else 1,
                -_parse_timestamp(candidate.updated_at)
                if candidate.updated_at
                else -candidate.mtime,
                candidate.summary.casefold(),
                candidate.session_id,
            )
        )
        return tuple(candidates)

    def candidates_for_cwd(self, cwd: str) -> tuple[FastAgentSessionCandidate, ...]:
        normalized_cwd = str(Path(cwd).expanduser().resolve(strict=False))
        cached = self._cached_roots.get(normalized_cwd)
        if cached is not None:
            return cached
        candidates = self._load_candidates_for_root(cwd)
        self._cached_roots[normalized_cwd] = candidates
        return candidates

    def list_candidates_for_directory(self, cwd: str) -> list[FastAgentSessionCandidate]:
        return list(self.candidates_for_cwd(cwd))

    def list_candidates_for_name(
        self,
        session_title: str,
        *,
        cwd: str,
    ) -> list[FastAgentSessionCandidate]:
        normalized = session_title.strip().casefold()
        if not normalized:
            return []
        return [
            candidate
            for candidate in self.candidates_for_cwd(cwd)
            if candidate.session_title.strip().casefold() == normalized
        ]

    def get_candidate(self, session_id: str, *, cwd: str) -> FastAgentSessionCandidate | None:
        if not session_id:
            return None
        for candidate in self.candidates_for_cwd(cwd):
            if candidate.session_id == session_id:
                return candidate
        return None

    def _resolve_explicit_token(
        self,
        token: str,
        *,
        cwd: str,
        operation: str,
    ) -> FastAgentSessionResolution:
        normalized_token = token.strip()
        if not normalized_token:
            return FastAgentSessionResolution(
                status="not_found",
                selected=None,
                reason=f"{operation}_token_missing",
            )

        by_id = [candidate for candidate in self.candidates_for_cwd(cwd) if candidate.session_id == normalized_token]
        by_title = self.list_candidates_for_name(normalized_token, cwd=cwd)

        if by_id and by_title:
            same_candidate = (
                len(by_id) == 1
                and len(by_title) == 1
                and by_id[0].session_id == by_title[0].session_id
            )
            if same_candidate:
                return FastAgentSessionResolution(
                    status="selected",
                    selected=by_id[0],
                    candidates=(by_id[0],),
                    reason=f"{operation}_explicit_session_id",
                )
            combined = tuple(
                {
                    candidate.session_id: candidate
                    for candidate in (*by_id, *by_title)
                }.values()
            )
            return FastAgentSessionResolution(
                status="ambiguous",
                selected=None,
                candidates=combined,
                reason=f"{operation}_token_id_title_collision",
            )

        if by_id:
            if len(by_id) == 1:
                return FastAgentSessionResolution(
                    status="selected",
                    selected=by_id[0],
                    candidates=(by_id[0],),
                    reason=f"{operation}_explicit_session_id",
                )
            return FastAgentSessionResolution(
                status="ambiguous",
                selected=None,
                candidates=tuple(by_id),
                reason=f"{operation}_explicit_session_id_ambiguous",
            )

        if by_title:
            if len(by_title) == 1:
                return FastAgentSessionResolution(
                    status="selected",
                    selected=by_title[0],
                    candidates=(by_title[0],),
                    reason=f"{operation}_explicit_session_title",
                )
            return FastAgentSessionResolution(
                status="ambiguous",
                selected=None,
                candidates=tuple(by_title),
                reason=f"{operation}_explicit_session_title_ambiguous",
            )

        if _SESSION_ID_RE.fullmatch(normalized_token):
            return FastAgentSessionResolution(
                status="not_found",
                selected=None,
                reason=f"{operation}_explicit_session_id_not_found",
            )
        return FastAgentSessionResolution(
            status="not_found",
            selected=None,
            reason=f"{operation}_explicit_session_title_not_found",
        )

    def resolve_for_registration(
        self,
        *,
        registered_session_id: str | None = None,
        cwd: str | None = None,
        registered_at: float = 0.0,
    ) -> FastAgentSessionResolution:
        """Resolve a fast-agent session for an explicitly registered live process."""
        if cwd is None:
            return FastAgentSessionResolution(
                status="not_found",
                selected=None,
                reason="launcher_registration_missing_cwd",
            )

        if registered_session_id:
            explicit = self._resolve_explicit_token(
                registered_session_id,
                cwd=cwd,
                operation="launcher_registration",
            )
            if explicit.status != "not_found":
                return explicit

        candidates = list(self.candidates_for_cwd(cwd))
        if registered_at > 0:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.ordering_timestamp >= registered_at - 5.0
            ]
        if len(candidates) == 1:
            return FastAgentSessionResolution(
                status="selected",
                selected=candidates[0],
                candidates=tuple(candidates),
                reason="explicit_launcher_registration",
            )
        if len(candidates) > 1:
            return FastAgentSessionResolution(
                status="ambiguous",
                selected=None,
                candidates=tuple(candidates),
                reason="explicit_launcher_registration_ambiguous",
            )
        return FastAgentSessionResolution(
            status="not_found",
            selected=None,
            reason="explicit_launcher_registration_not_found",
        )

    def resolve_resume_target(
        self,
        token: str | None,
        *,
        cwd: str | None = None,
        registered_session_id: str | None = None,
    ) -> FastAgentSessionResolution:
        """Resolve a fast-agent `/resume` token deterministically."""
        if token:
            if cwd is None:
                return FastAgentSessionResolution(
                    status="not_found",
                    selected=None,
                    reason="resume_missing_cwd",
                )
            return self._resolve_explicit_token(token, cwd=cwd, operation="resume")
        if registered_session_id or cwd:
            return self.resolve_for_registration(
                registered_session_id=registered_session_id,
                cwd=cwd,
            )
        return FastAgentSessionResolution(
            status="not_found",
            selected=None,
            reason="resume_token_missing",
        )

    def resolve_title_rename_target(
        self,
        token: str | None,
        *,
        cwd: str | None = None,
    ) -> FastAgentSessionResolution:
        """Resolve a fast-agent title rename target.

        fast-agent supports title metadata updates, but not direct session-id
        renames. If the token resolves to a persisted session id, callers should
        surface that as unsupported instead of pretending the id changed.
        """
        if not token or cwd is None:
            return FastAgentSessionResolution(
                status="not_found",
                selected=None,
                reason="rename_token_missing",
            )

        session_id_match = self.get_candidate(token, cwd=cwd)
        if session_id_match is not None:
            return FastAgentSessionResolution(
                status="unsupported",
                selected=session_id_match,
                candidates=(session_id_match,),
                reason="session_id_rename_unsupported",
            )

        by_title = self.list_candidates_for_name(token, cwd=cwd)
        if len(by_title) == 1:
            return FastAgentSessionResolution(
                status="selected",
                selected=by_title[0],
                candidates=(by_title[0],),
                reason="title_rename_supported",
            )
        if len(by_title) > 1:
            return FastAgentSessionResolution(
                status="ambiguous",
                selected=None,
                candidates=tuple(by_title),
                reason="title_rename_ambiguous",
            )
        return FastAgentSessionResolution(
            status="not_found",
            selected=None,
            reason="title_rename_not_found",
        )

    def resolve_session_id_rename_target(
        self,
        session_id: str | None,
        *,
        cwd: str | None = None,
    ) -> FastAgentSessionResolution:
        """Resolve the explicitly unsupported fast-agent session-id rename path."""
        if not session_id or cwd is None:
            return FastAgentSessionResolution(
                status="not_found",
                selected=None,
                reason="rename_token_missing",
            )

        session = self.get_candidate(session_id, cwd=cwd)
        if session is None:
            return FastAgentSessionResolution(
                status="not_found",
                selected=None,
                reason="session_id_rename_not_found",
            )
        return FastAgentSessionResolution(
            status="unsupported",
            selected=session,
            candidates=(session,),
            reason="session_id_rename_unsupported",
        )
