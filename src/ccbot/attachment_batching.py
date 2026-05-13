"""In-memory Telegram attachment ingress batching primitives.

The module intentionally owns only data/policy decisions. Telegram side effects
(progress messages, downloads, target revalidation, and runtime injection) stay
in ``bot.py`` so the bot's control-surface guardrails remain authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MEDIA_GROUP_IDLE_SECONDS = 1.0
ORPHAN_ATTACHMENT_HOLD_SECONDS = 15.0
TEXT_LEAD_HOLD_SECONDS = 0.75
MAX_ATTACHMENTS_PER_BATCH = 10
MAX_BATCH_DOWNLOADED_BYTES = 100 * 1024 * 1024

BindingKind = Literal["direct", "shared"]
AttachmentKind = Literal["document", "image"]


@dataclass(frozen=True)
class CapturedBindingTarget:
    requesting_user_id: int
    binding_owner_user_id: int
    binding_kind: BindingKind
    surface_key: str
    chat_id: int
    message_thread_id: int | None
    window_id: str
    binding_generation: tuple[int, str]


@dataclass(frozen=True)
class IngressBatchKey:
    requesting_user_id: int
    surface_key: str
    chat_id: int
    message_thread_id: int | None
    media_group_id: str | None
    binding_owner_user_id: int
    binding_kind: BindingKind
    window_id: str

    @classmethod
    def from_target(
        cls,
        target: CapturedBindingTarget,
        *,
        media_group_id: str | None = None,
    ) -> "IngressBatchKey":
        normalized_group = str(media_group_id).strip() if media_group_id else None
        return cls(
            requesting_user_id=target.requesting_user_id,
            surface_key=target.surface_key,
            chat_id=target.chat_id,
            message_thread_id=target.message_thread_id,
            media_group_id=normalized_group or None,
            binding_owner_user_id=target.binding_owner_user_id,
            binding_kind=target.binding_kind,
            window_id=target.window_id,
        )


@dataclass(frozen=True)
class AttachmentTextFragment:
    text: str
    order: int


@dataclass(frozen=True)
class AttachmentBatchItem:
    kind: AttachmentKind
    path: Path
    display_name: str
    downloaded_size: int
    order: int


@dataclass(frozen=True)
class AttachmentBatchFailure:
    kind: AttachmentKind | str
    display_name: str
    reason: str
    order: int
    material: bool = True


@dataclass
class PendingAttachmentBatch:
    key: IngressBatchKey
    target: CapturedBindingTarget
    created_at: float
    last_activity_at: float
    texts: list[AttachmentTextFragment] = field(default_factory=list)
    attachments: list[AttachmentBatchItem] = field(default_factory=list)
    failures: list[AttachmentBatchFailure] = field(default_factory=list)
    latest_message: object | None = None
    progress_message: object | None = None

    @property
    def has_media_group(self) -> bool:
        return self.key.media_group_id is not None

    @property
    def downloaded_bytes(self) -> int:
        return sum(max(0, item.downloaded_size) for item in self.attachments)

    @property
    def attachment_count(self) -> int:
        return len(self.attachments)

    @property
    def text_only(self) -> bool:
        return bool(self.texts) and not self.attachments and not self.failures

    def due_at(self) -> float:
        if self.has_media_group:
            return self.last_activity_at + MEDIA_GROUP_IDLE_SECONDS
        if self.attachments or self.failures:
            if self.has_instruction_text() and self.has_usable_content():
                return self.last_activity_at
            return self.last_activity_at + ORPHAN_ATTACHMENT_HOLD_SECONDS
        return self.last_activity_at + TEXT_LEAD_HOLD_SECONDS

    def has_instruction_text(self) -> bool:
        return any(fragment.text.strip() for fragment in self.texts)

    def has_usable_content(self) -> bool:
        return bool(self.attachments) or bool(self.texts)

    def is_sufficiently_complete(self, now: float) -> bool:
        if self.text_only:
            return now >= self.due_at()
        if self.has_instruction_text():
            return True
        if not self.attachments and not self.failures:
            return False
        return now >= self.due_at()


class AttachmentBatcher:
    """Small deterministic in-memory batching policy object."""

    def __init__(self) -> None:
        self._batches: dict[IngressBatchKey, PendingAttachmentBatch] = {}
        self._order = 0

    def next_order(self) -> int:
        self._order += 1
        return self._order

    def get(self, key: IngressBatchKey) -> PendingAttachmentBatch | None:
        return self._batches.get(key)

    def keys(self) -> list[IngressBatchKey]:
        return list(self._batches)

    def pop(self, key: IngressBatchKey) -> PendingAttachmentBatch | None:
        return self._batches.pop(key, None)

    def clear(self) -> None:
        self._batches.clear()
        self._order = 0

    def _ensure(
        self,
        key: IngressBatchKey,
        target: CapturedBindingTarget,
        *,
        now: float,
        latest_message: object | None = None,
    ) -> PendingAttachmentBatch:
        batch = self._batches.get(key)
        if batch is None:
            batch = PendingAttachmentBatch(
                key=key,
                target=target,
                created_at=now,
                last_activity_at=now,
                latest_message=latest_message,
            )
            self._batches[key] = batch
        else:
            batch.last_activity_at = now
            if latest_message is not None:
                batch.latest_message = latest_message
        return batch

    def add_attachment(
        self,
        key: IngressBatchKey,
        target: CapturedBindingTarget,
        item: AttachmentBatchItem,
        *,
        now: float,
        latest_message: object | None = None,
    ) -> PendingAttachmentBatch:
        batch = self._ensure(key, target, now=now, latest_message=latest_message)
        batch.attachments.append(item)
        return batch

    def add_failure(
        self,
        key: IngressBatchKey,
        target: CapturedBindingTarget,
        failure: AttachmentBatchFailure,
        *,
        now: float,
        latest_message: object | None = None,
    ) -> PendingAttachmentBatch:
        batch = self._ensure(key, target, now=now, latest_message=latest_message)
        batch.failures.append(failure)
        return batch

    def add_text(
        self,
        key: IngressBatchKey,
        target: CapturedBindingTarget,
        fragment: AttachmentTextFragment,
        *,
        now: float,
        latest_message: object | None = None,
    ) -> PendingAttachmentBatch:
        batch = self._ensure(key, target, now=now, latest_message=latest_message)
        if fragment.text.strip():
            batch.texts.append(fragment)
        return batch

    def flush_due_at(self, key: IngressBatchKey) -> float | None:
        batch = self._batches.get(key)
        if batch is None:
            return None
        return batch.due_at()

    def is_due(self, key: IngressBatchKey, *, now: float) -> bool:
        batch = self._batches.get(key)
        if batch is None:
            return False
        return batch.is_sufficiently_complete(now)

    def is_sufficiently_complete(self, key: IngressBatchKey, *, now: float) -> bool:
        batch = self._batches.get(key)
        return False if batch is None else batch.is_sufficiently_complete(now)

    def has_usable_content(self, key: IngressBatchKey) -> bool:
        batch = self._batches.get(key)
        return False if batch is None else batch.has_usable_content()

    def is_full(self, key: IngressBatchKey) -> bool:
        batch = self._batches.get(key)
        if batch is None:
            return False
        return (
            batch.attachment_count >= MAX_ATTACHMENTS_PER_BATCH
            or batch.downloaded_bytes >= MAX_BATCH_DOWNLOADED_BYTES
        )

    def should_start_new_batch_for_attachment(
        self,
        key: IngressBatchKey,
        *,
        downloaded_size: int,
    ) -> bool:
        batch = self._batches.get(key)
        if batch is None:
            return False
        if batch.attachment_count >= MAX_ATTACHMENTS_PER_BATCH:
            return True
        return (
            batch.downloaded_bytes + max(0, downloaded_size)
            > MAX_BATCH_DOWNLOADED_BYTES
        )

    def format_runtime_input(self, key: IngressBatchKey) -> str:
        batch = self._require(key)
        lines: list[str] = []
        text_parts = [
            fragment.text.strip()
            for fragment in sorted(batch.texts, key=lambda f: f.order)
            if fragment.text.strip()
        ]
        if text_parts:
            lines.append("\n\n".join(text_parts))
        if batch.attachments:
            if lines:
                lines.append("")
            lines.append("Attachments:")
            for index, item in enumerate(
                sorted(batch.attachments, key=lambda i: i.order),
                1,
            ):
                lines.append(f"{index}. {item.kind}: {item.path}")
        material_failures = [
            failure
            for failure in sorted(batch.failures, key=lambda f: f.order)
            if failure.material
        ]
        if material_failures:
            if lines:
                lines.append("")
            lines.append("Attachment download failures:")
            for failure in material_failures:
                lines.append(
                    f"- {failure.kind}: {failure.display_name} — {failure.reason}"
                )
        return "\n".join(lines).strip()

    def progress_text(self, key: IngressBatchKey) -> str:
        batch = self._require(key)
        names = [
            item.display_name
            for item in sorted(batch.attachments, key=lambda i: i.order)
        ]
        names.extend(
            failure.display_name
            for failure in sorted(batch.failures, key=lambda f: f.order)
        )
        safe_names = [_safe_display_name(name) for name in names if name]
        if not safe_names:
            return "Получил вложение. Жду ещё файлы или инструкцию до 15 секунд."
        if len(safe_names) == 1:
            return (
                f"Получил файл: {safe_names[0]}. "
                "Жду ещё файлы или инструкцию до 15 секунд."
            )
        return (
            f"Получил файлы: {', '.join(safe_names)}. "
            "Обновил ожидание ещё на 15 секунд."
        )

    def final_ack_text(self, key: IngressBatchKey) -> str:
        batch = self._require(key)
        count = len(batch.attachments)
        if count == 0:
            if batch.failures:
                return "📎 Sent text and attachment failure details to Codex."
            return "📎 Sent text to Codex."
        if count == 1:
            noun = "attachment"
        else:
            noun = "attachments"
        suffix = " with text" if batch.has_instruction_text() else ""
        return f"📎 Sent {count} {noun}{suffix} to Codex."

    def _require(self, key: IngressBatchKey) -> PendingAttachmentBatch:
        batch = self._batches.get(key)
        if batch is None:
            raise KeyError(key)
        return batch


def _safe_display_name(name: str) -> str:
    return Path(str(name)).name or "attachment"
