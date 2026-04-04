"""Codex thread catalog adapter.

This module resolves persisted Codex threads as file-backed candidates without
collapsing them into a live tmux window or process. The catalog is built from a
session index plus the rollout files stored under ``~/.codex/sessions``.

The adapter keeps three concepts separate:

* index entries: named sessions listed by ``session_index.jsonl``
* rollout candidates: index entries that also have a readable rollout file
* resolution results: explicit selection, cwd-based selection, or ambiguity
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from .runtime_types import ThreadLocator
from .transcript_parser import TranscriptParser
from .utils import read_cwd_from_jsonl

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}"
)


def normalize_cwd(cwd: str) -> str:
    """Normalize a cwd for exact comparison."""
    if not cwd:
        return ""
    try:
        return str(Path(cwd).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        try:
            return str(Path(cwd).expanduser())
        except (OSError, RuntimeError, ValueError):
            return cwd


def _parse_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _extract_thread_id_from_filename(path: Path) -> str:
    match = _UUID_RE.search(path.name)
    return match.group(0) if match else ""


def _load_jsonl_or_json(path: Path) -> list[dict[str, Any]]:
    """Load a JSONL file or a fixture-style JSON wrapper with rows."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        return rows

    if isinstance(parsed, dict):
        if isinstance(parsed.get("rows"), list):
            rows = []
            for item in parsed["rows"]:
                if isinstance(item, dict):
                    row = item.get("row") if isinstance(item.get("row"), dict) else item
                    if isinstance(row, dict):
                        rows.append(row)
            return rows
        return [parsed]

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]

    return []


@dataclass(frozen=True)
class CodexThreadIndexEntry:
    """A persisted session index entry."""

    thread_id: str
    thread_name: str
    updated_at: str = ""
    raw: dict[str, Any] | None = None

    @property
    def normalized_thread_name(self) -> str:
        return self.thread_name.strip()


@dataclass(frozen=True)
class CodexThreadCandidate:
    """A deterministic, file-backed Codex thread candidate."""

    thread_id: str
    thread_name: str
    cwd: str
    rollout_file: Path
    message_count: int
    updated_at: str = ""
    mtime: float = 0.0
    preview: str = ""

    @property
    def normalized_cwd(self) -> str:
        return normalize_cwd(self.cwd)

    @property
    def ordering_timestamp(self) -> float:
        if self.updated_at:
            return _parse_timestamp(self.updated_at)
        return self.mtime

    @property
    def summary(self) -> str:
        title = self.thread_name.strip() or self.preview.strip()
        return title or self.thread_id

    def to_locator(self) -> ThreadLocator:
        return ThreadLocator(
            thread_id=self.thread_id,
            summary=self.summary,
            message_count=self.message_count,
            file_path=str(self.rollout_file),
            runtime_kind="codex",
            cwd=self.cwd,
        )


@dataclass(frozen=True)
class CodexThreadResolution:
    """Result returned by explicit or cwd-based thread resolution."""

    status: str
    selected: CodexThreadCandidate | None
    candidates: tuple[CodexThreadCandidate, ...] = ()
    reason: str = ""

    @property
    def is_ambiguous(self) -> bool:
        return self.status == "ambiguous"

    @property
    def is_selected(self) -> bool:
        return self.selected is not None


@dataclass(frozen=True)
class CodexThreadLookup:
    """Internal rollout snapshot for a thread id."""

    thread_id: str
    file_path: Path
    cwd: str
    message_count: int
    mtime: float = 0.0
    preview: str = ""

    @property
    def normalized_cwd(self) -> str:
        return normalize_cwd(self.cwd)

    def to_candidate(self, entry: CodexThreadIndexEntry | None = None) -> CodexThreadCandidate:
        return CodexThreadCandidate(
            thread_id=self.thread_id,
            thread_name=entry.normalized_thread_name if entry else "",
            cwd=self.cwd,
            rollout_file=self.file_path,
            message_count=self.message_count,
            updated_at=entry.updated_at if entry else "",
            mtime=self.mtime,
            preview=self.preview,
        )


class CodexThreadCatalog:
    """Adapter that enumerates and resolves persisted Codex threads."""

    def __init__(
        self,
        codex_home: Path | None = None,
        session_index_path: Path | None = None,
        sessions_root: Path | None = None,
    ) -> None:
        self.codex_home = codex_home or Path.home() / ".codex"
        self.session_index_path = session_index_path or (self.codex_home / "session_index.jsonl")
        self.sessions_root = sessions_root or (self.codex_home / "sessions")

    def refresh(self) -> None:
        """Invalidate cached directory scans so delayed writes become visible."""
        for key in (
            "index_entries",
            "rollout_lookup",
            "candidates",
            "index_only_entries",
        ):
            self.__dict__.pop(key, None)

    @cached_property
    def index_entries(self) -> tuple[CodexThreadIndexEntry, ...]:
        """Load session_index entries in file order with last-wins deduping."""
        if not self.session_index_path.exists():
            return ()

        entries: dict[str, CodexThreadIndexEntry] = {}
        for row in _load_jsonl_or_json(self.session_index_path):
            thread_id = str(row.get("id") or row.get("session_id") or "").strip()
            if not thread_id:
                continue
            thread_name = str(row.get("thread_name") or row.get("name") or "").strip()
            entries[thread_id] = CodexThreadIndexEntry(
                thread_id=thread_id,
                thread_name=thread_name,
                updated_at=str(row.get("updated_at") or row.get("updatedAt") or ""),
                raw=row,
            )
        return tuple(entries.values())

    def _read_rollout_lookup(self) -> dict[str, CodexThreadLookup]:
        """Index rollout files by thread id, returning deterministic candidates."""
        if not self.sessions_root.exists():
            return {}

        lookups: dict[str, CodexThreadLookup] = {}
        rollout_paths = sorted(
            (
                path
                for path in self.sessions_root.rglob("*")
                if path.is_file() and path.suffix in {".jsonl", ".json"}
            ),
            key=lambda path: str(path),
        )
        for path in rollout_paths:
            lookup = self._load_rollout_lookup(path)
            if lookup is None:
                continue
            existing = lookups.get(lookup.thread_id)
            if existing is None:
                lookups[lookup.thread_id] = lookup
                continue
            # Deterministic tie-breaker: prefer the newer file, then lexicographic path.
            current_key = (existing.mtime, str(existing.file_path))
            new_key = (lookup.mtime, str(lookup.file_path))
            if new_key >= current_key:
                lookups[lookup.thread_id] = lookup
        return lookups

    def _load_rollout_lookup(self, path: Path) -> CodexThreadLookup | None:
        """Load the first session_meta record from a rollout file."""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                message_count = 0
                thread_id = ""
                cwd = ""
                preview = ""
                for line in stream:
                    if not line.strip():
                        continue
                    message_count += 1
                    data = TranscriptParser.parse_line(line)
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") == "session_meta":
                        payload = data.get("payload") or {}
                        if not thread_id:
                            thread_id = str(
                                payload.get("id")
                                or payload.get("session_id")
                                or payload.get("sessionId")
                                or ""
                            ).strip()
                        if not cwd:
                            cwd = str(payload.get("cwd") or "").strip()
                    elif not preview and TranscriptParser.is_user_message(data):
                        parsed = TranscriptParser.parse_message(data)
                        if parsed and parsed.text.strip():
                            preview = parsed.text.strip()
                    if thread_id and cwd:
                        # Keep counting to maintain a stable message_count.
                        continue
                if not thread_id:
                    thread_id = _extract_thread_id_from_filename(path)
                if not thread_id:
                    return None
                if not cwd:
                    cwd = read_cwd_from_jsonl(path)
                return CodexThreadLookup(
                    thread_id=thread_id,
                    file_path=path,
                    cwd=cwd,
                    message_count=message_count,
                    mtime=path.stat().st_mtime,
                    preview=preview,
                )
        except OSError:
            return None

    def _load_rollout_identity(self, path: Path) -> CodexThreadLookup | None:
        """Load only the identifying session metadata from a rollout file."""
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                thread_id = ""
                cwd = ""
                for line in stream:
                    if not line.strip():
                        continue
                    data = TranscriptParser.parse_line(line)
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") != "session_meta":
                        continue
                    payload = data.get("payload") or {}
                    thread_id = str(
                        payload.get("id")
                        or payload.get("session_id")
                        or payload.get("sessionId")
                        or ""
                    ).strip()
                    cwd = str(payload.get("cwd") or "").strip()
                    break
                if not thread_id:
                    thread_id = _extract_thread_id_from_filename(path)
                if not thread_id:
                    return None
                if not cwd:
                    cwd = read_cwd_from_jsonl(path)
                return CodexThreadLookup(
                    thread_id=thread_id,
                    file_path=path,
                    cwd=cwd,
                    message_count=0,
                    mtime=path.stat().st_mtime,
                    preview="",
                )
        except OSError:
            return None

    @cached_property
    def rollout_lookup(self) -> dict[str, CodexThreadLookup]:
        return self._read_rollout_lookup()

    @cached_property
    def candidates(self) -> tuple[CodexThreadCandidate, ...]:
        """Return available thread candidates with known rollout files."""
        index_by_id = {entry.thread_id: entry for entry in self.index_entries}
        candidates: list[CodexThreadCandidate] = []
        for thread_id, lookup in self.rollout_lookup.items():
            entry = index_by_id.get(thread_id)
            candidates.append(
                CodexThreadCandidate(
                    thread_id=thread_id,
                    thread_name=entry.normalized_thread_name if entry else "",
                    cwd=lookup.cwd,
                    rollout_file=lookup.file_path,
                    message_count=lookup.message_count,
                    updated_at=entry.updated_at if entry else "",
                    mtime=lookup.mtime,
                    preview=lookup.preview,
                )
            )
        candidates.sort(
            key=lambda candidate: (
                0 if candidate.updated_at else 1,
                -_parse_timestamp(candidate.updated_at)
                if candidate.updated_at
                else -candidate.mtime,
                candidate.summary.casefold(),
                candidate.thread_id,
            )
        )
        return tuple(candidates)

    @cached_property
    def index_only_entries(self) -> tuple[CodexThreadIndexEntry, ...]:
        """Persisted session index rows that do not have a readable rollout file."""
        available_ids = {candidate.thread_id for candidate in self.candidates}
        return tuple(
            entry for entry in self.index_entries if entry.thread_id not in available_ids
        )

    def list_candidates_for_cwd(self, cwd: str) -> list[CodexThreadCandidate]:
        """List available rollout-backed candidates whose cwd matches exactly."""
        normalized = normalize_cwd(cwd)
        if not normalized:
            return []
        return [
            candidate
            for candidate in self.candidates
            if candidate.normalized_cwd == normalized
        ]

    def list_candidates_for_name(self, thread_name: str) -> list[CodexThreadCandidate]:
        """List available rollout-backed candidates whose name matches exactly."""
        normalized = thread_name.strip().casefold()
        if not normalized:
            return []
        return [
            candidate
            for candidate in self.candidates
            if candidate.thread_name.strip().casefold() == normalized
        ]

    def get_candidate(self, thread_id: str) -> CodexThreadCandidate | None:
        """Return a rollout-backed candidate by thread id."""
        if not thread_id:
            return None
        for candidate in self.candidates:
            if candidate.thread_id == thread_id:
                return candidate
        return None

    def get_candidate_fast(self, thread_id: str) -> CodexThreadCandidate | None:
        """Resolve a candidate by thread id without scanning the full catalog."""
        if not thread_id:
            return None
        index_by_id = {entry.thread_id: entry for entry in self.index_entries}
        for path in sorted(
            self.sessions_root.rglob(f"*{thread_id}*.jsonl"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        ):
            lookup = self._load_rollout_identity(path)
            if lookup is None or lookup.thread_id != thread_id:
                continue
            full_lookup = self._load_rollout_lookup(path)
            if full_lookup is None:
                continue
            return full_lookup.to_candidate(index_by_id.get(thread_id))
        return None

    def _resolve_explicit_token(
        self,
        token: str,
        *,
        operation: str,
    ) -> CodexThreadResolution:
        """Resolve an explicit resume/rename token by exact id or exact name."""
        normalized_token = token.strip()
        if not normalized_token:
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason=f"{operation}_token_missing",
            )

        by_id = [candidate for candidate in self.candidates if candidate.thread_id == normalized_token]
        by_name = self.list_candidates_for_name(normalized_token)

        if by_id and by_name:
            same_candidate = (
                len(by_id) == 1
                and len(by_name) == 1
                and by_id[0].thread_id == by_name[0].thread_id
            )
            if same_candidate:
                return CodexThreadResolution(
                    status="selected",
                    selected=by_id[0],
                    candidates=(by_id[0],),
                    reason=f"{operation}_explicit_thread_id",
                )
            combined = tuple(
                {
                    candidate.thread_id: candidate
                    for candidate in (*by_id, *by_name)
                }.values()
            )
            return CodexThreadResolution(
                status="ambiguous",
                selected=None,
                candidates=combined,
                reason=f"{operation}_token_id_name_collision",
            )

        if by_id:
            if len(by_id) == 1:
                return CodexThreadResolution(
                    status="selected",
                    selected=by_id[0],
                    candidates=(by_id[0],),
                    reason=f"{operation}_explicit_thread_id",
                )
            return CodexThreadResolution(
                status="ambiguous",
                selected=None,
                candidates=tuple(by_id),
                reason=f"{operation}_explicit_thread_id_ambiguous",
            )

        if by_name:
            if len(by_name) == 1:
                return CodexThreadResolution(
                    status="selected",
                    selected=by_name[0],
                    candidates=(by_name[0],),
                    reason=f"{operation}_explicit_thread_name",
                )
            return CodexThreadResolution(
                status="ambiguous",
                selected=None,
                candidates=tuple(by_name),
                reason=f"{operation}_explicit_thread_name_ambiguous",
            )

        if _UUID_RE.fullmatch(normalized_token):
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason=f"{operation}_explicit_thread_id_not_found",
            )
        return CodexThreadResolution(
            status="not_found",
            selected=None,
            reason=f"{operation}_explicit_thread_name_not_found",
        )

    def resolve_recent_for_registration(
        self,
        *,
        cwd: str,
        registered_at: float,
    ) -> CodexThreadResolution:
        """Resolve a just-registered process by scanning only recent rollout files."""
        normalized_cwd = normalize_cwd(cwd)
        if not normalized_cwd:
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason="launcher_registration_missing_cwd",
            )

        recent_paths = sorted(
            (
                path
                for path in self.sessions_root.rglob("rollout-*.jsonl")
                if path.is_file() and path.stat().st_mtime >= max(0.0, registered_at - 5.0)
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        index_by_id = {entry.thread_id: entry for entry in self.index_entries}
        candidates: list[CodexThreadCandidate] = []
        for path in recent_paths:
            lookup = self._load_rollout_identity(path)
            if lookup is None or lookup.normalized_cwd != normalized_cwd:
                continue
            full_lookup = self._load_rollout_lookup(path)
            if full_lookup is None:
                continue
            candidates.append(full_lookup.to_candidate(index_by_id.get(full_lookup.thread_id)))

        if len(candidates) == 1:
            return CodexThreadResolution(
                status="selected",
                selected=candidates[0],
                candidates=tuple(candidates),
                reason="explicit_launcher_registration_recent_rollout",
            )
        if len(candidates) > 1:
            return CodexThreadResolution(
                status="ambiguous",
                selected=None,
                candidates=tuple(candidates),
                reason="explicit_launcher_registration_recent_rollout_ambiguous",
            )
        return CodexThreadResolution(
            status="not_found",
            selected=None,
            reason="explicit_launcher_registration_recent_rollout_not_found",
        )

    def resolve(
        self,
        *,
        thread_id: str | None = None,
        registered_thread_id: str | None = None,
        cwd: str | None = None,
    ) -> CodexThreadResolution:
        """Resolve a persisted thread with fail-closed precedence rules."""
        if thread_id:
            candidate = self.get_candidate(thread_id)
            if candidate:
                return CodexThreadResolution(
                    status="selected",
                    selected=candidate,
                    candidates=(candidate,),
                    reason="explicit_thread_id",
                )
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason="explicit_thread_id_not_found",
            )

        if registered_thread_id:
            candidate = self.get_candidate(registered_thread_id)
            if candidate:
                return CodexThreadResolution(
                    status="selected",
                    selected=candidate,
                    candidates=(candidate,),
                    reason="explicit_launcher_registration",
                )
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason="launcher_registration_not_found",
            )

        if cwd:
            candidates = self.list_candidates_for_cwd(cwd)
            if len(candidates) == 1:
                return CodexThreadResolution(
                    status="selected",
                    selected=candidates[0],
                    candidates=tuple(candidates),
                    reason="normalized_cwd",
                )
            if len(candidates) > 1:
                return CodexThreadResolution(
                    status="ambiguous",
                    selected=None,
                    candidates=tuple(candidates),
                    reason="normalized_cwd_ambiguous",
                )
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason="normalized_cwd_not_found",
            )

        if self.candidates:
            return CodexThreadResolution(
                status="ambiguous",
                selected=None,
                candidates=self.candidates,
                reason="user_visible_disambiguation_required",
            )
        return CodexThreadResolution(
            status="not_found",
            selected=None,
            reason="no_persisted_threads",
        )

    def resolve_for_registration(
        self,
        *,
        registered_thread_id: str | None = None,
        cwd: str | None = None,
        registered_at: float = 0.0,
    ) -> CodexThreadResolution:
        """Resolve a thread for an explicitly registered live Codex process."""
        if registered_thread_id:
            return self.resolve(thread_id=registered_thread_id, cwd=cwd)

        if not cwd:
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason="launcher_registration_missing_cwd",
            )

        candidates = self.list_candidates_for_cwd(cwd)
        if registered_at > 0:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.ordering_timestamp >= registered_at
            ]
            if len(candidates) == 1:
                return CodexThreadResolution(
                    status="selected",
                    selected=candidates[0],
                    candidates=tuple(candidates),
                    reason="explicit_launcher_registration",
                )
            if len(candidates) > 1:
                return CodexThreadResolution(
                    status="ambiguous",
                    selected=None,
                    candidates=tuple(candidates),
                    reason="explicit_launcher_registration_ambiguous",
                )
            return CodexThreadResolution(
                status="not_found",
                selected=None,
                reason="explicit_launcher_registration_not_found",
            )

        return self.resolve(cwd=cwd)

    def resolve_resume_target(
        self,
        token: str | None,
        *,
        cwd: str | None = None,
        registered_thread_id: str | None = None,
    ) -> CodexThreadResolution:
        """Resolve a Codex `/resume` token deterministically.

        The explicit token must match exactly by thread id or exact thread name.
        Ambiguity is fail-closed and does not fall back to cwd guessing.
        """
        if token:
            return self._resolve_explicit_token(token, operation="resume")
        if registered_thread_id or cwd:
            return self.resolve_for_registration(
                registered_thread_id=registered_thread_id,
                cwd=cwd,
            )
        return CodexThreadResolution(
            status="not_found",
            selected=None,
            reason="resume_token_missing",
        )

    def resolve_rename_target(
        self,
        token: str | None,
        *,
        cwd: str | None = None,
        registered_thread_id: str | None = None,
    ) -> CodexThreadResolution:
        """Resolve a Codex `/rename` target deterministically.

        Rename uses the same explicit token rules as resume. If a runtime cannot
        safely rename persisted identity, callers should treat the returned
        target as tmux-only and document the degraded mode explicitly.
        """
        if token:
            return self._resolve_explicit_token(token, operation="rename")
        if registered_thread_id or cwd:
            return self.resolve_for_registration(
                registered_thread_id=registered_thread_id,
                cwd=cwd,
            )
        return CodexThreadResolution(
            status="not_found",
            selected=None,
            reason="rename_token_missing",
        )

    def exact_locator(self, thread_id: str, cwd: str) -> ThreadLocator | None:
        """Return a locator only when thread id and cwd match exactly."""
        candidate = self.get_candidate(thread_id)
        if candidate is None:
            return None
        if candidate.normalized_cwd != normalize_cwd(cwd):
            return None
        return candidate.to_locator()
