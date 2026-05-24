from __future__ import annotations

import pytest

from ccbot.control_surface import ControlSurfaceIdentity


def test_control_surface_identity_topic_keys_are_chat_qualified_when_known() -> None:
    identity = ControlSurfaceIdentity.for_topic(
        user_id=100,
        chat_id=-100200300,
        thread_id=42,
    )

    assert identity.surface_key == "t:-100200300:42"
    assert identity.title_surface_key == "t:-100200300:42"
    assert identity.legacy_topic_surface_key == "t:42"
    assert identity.as_parse_tuple() == ("topic", -100200300, 42)


def test_control_surface_identity_keeps_legacy_topic_compatibility() -> None:
    identity = ControlSurfaceIdentity.from_surface_key("t:42", user_id=100)

    assert identity.user_id == 100
    assert identity.chat_id is None
    assert identity.thread_id == 42
    assert identity.surface_key == "t:42"
    assert identity.title_surface_key == "t:42"


def test_control_surface_identity_chat_surface_for_no_topics_main_chat() -> None:
    identity = ControlSurfaceIdentity.for_chat(chat_id=-100200300, user_id=100)

    assert identity.surface_key == "c:-100200300"
    assert identity.title_surface_key == "c:-100200300"
    assert identity.legacy_topic_surface_key is None
    assert identity.as_parse_tuple() == ("chat", -100200300, None)


def test_control_surface_identity_rejects_invalid_surface_key() -> None:
    with pytest.raises(ValueError):
        ControlSurfaceIdentity.from_surface_key("not-a-surface")
