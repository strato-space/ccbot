"""Canonical Telegram control-surface identity helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TOPIC_SURFACE_PREFIX = "t:"
CHAT_SURFACE_PREFIX = "c:"
SurfaceKind = Literal["topic", "chat"]


@dataclass(frozen=True)
class ControlSurfaceIdentity:
    """Product identity for one Telegram control surface.

    ``surface_key`` remains the compact product key used in persisted state.
    The full product control-surface identity is ``(user_id, surface_key)`` for
    owner-scoped maps, while title metadata uses the chat-qualified
    ``title_surface_key`` when Telegram chat coordinates are known.
    """

    kind: SurfaceKind
    user_id: int | None = None
    chat_id: int | None = None
    thread_id: int | None = None

    @classmethod
    def for_topic(
        cls,
        *,
        thread_id: int,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> "ControlSurfaceIdentity":
        return cls(
            kind="topic",
            user_id=int(user_id) if user_id is not None else None,
            chat_id=int(chat_id) if chat_id is not None else None,
            thread_id=int(thread_id),
        )

    @classmethod
    def for_chat(
        cls,
        *,
        chat_id: int,
        user_id: int | None = None,
    ) -> "ControlSurfaceIdentity":
        return cls(
            kind="chat",
            user_id=int(user_id) if user_id is not None else None,
            chat_id=int(chat_id),
            thread_id=None,
        )

    @classmethod
    def from_coordinates(
        cls,
        *,
        surface_key: str | None = None,
        thread_id: int | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> "ControlSurfaceIdentity":
        if thread_id is not None:
            return cls.for_topic(thread_id=thread_id, chat_id=chat_id, user_id=user_id)
        if chat_id is not None:
            return cls.for_chat(chat_id=chat_id, user_id=user_id)
        if surface_key is not None:
            return cls.from_surface_key(surface_key, user_id=user_id)
        raise ValueError("control surface requires surface_key, thread_id, or chat_id")

    @classmethod
    def from_surface_key(
        cls,
        surface_key: str,
        *,
        user_id: int | None = None,
    ) -> "ControlSurfaceIdentity":
        raw = str(surface_key or "").strip()
        if raw.startswith(TOPIC_SURFACE_PREFIX):
            payload = raw[len(TOPIC_SURFACE_PREFIX) :]
            parts = payload.split(":")
            try:
                if len(parts) == 1:
                    return cls.for_topic(thread_id=int(parts[0]), user_id=user_id)
                if len(parts) == 2:
                    return cls.for_topic(
                        chat_id=int(parts[0]),
                        thread_id=int(parts[1]),
                        user_id=user_id,
                    )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid topic surface key: {surface_key!r}") from exc
        if raw.startswith(CHAT_SURFACE_PREFIX):
            try:
                return cls.for_chat(chat_id=int(raw[len(CHAT_SURFACE_PREFIX) :]), user_id=user_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid chat surface key: {surface_key!r}") from exc
        raise ValueError(f"invalid surface key: {surface_key!r}")

    @property
    def surface_key(self) -> str:
        if self.kind == "topic":
            if self.thread_id is None:
                raise ValueError("topic surface requires thread_id")
            if self.chat_id is not None:
                return f"{TOPIC_SURFACE_PREFIX}{int(self.chat_id)}:{int(self.thread_id)}"
            return f"{TOPIC_SURFACE_PREFIX}{int(self.thread_id)}"
        if self.chat_id is None:
            raise ValueError("chat surface requires chat_id")
        return f"{CHAT_SURFACE_PREFIX}{int(self.chat_id)}"

    @property
    def title_surface_key(self) -> str:
        return self.surface_key

    @property
    def legacy_topic_surface_key(self) -> str | None:
        if self.kind != "topic" or self.thread_id is None:
            return None
        return f"{TOPIC_SURFACE_PREFIX}{int(self.thread_id)}"

    def as_parse_tuple(self) -> tuple[SurfaceKind, int | None, int | None]:
        return self.kind, self.chat_id, self.thread_id
