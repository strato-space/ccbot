from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.restore import (
    RestoreIntent,
    RestoreIntentError,
    bind_restored_surface,
    parse_restore_intent,
    validate_existing_runtime_window_for_restore,
)
from ccbot.runtime_types import LiveProcessDescriptor
from ccbot.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


def test_parse_restore_intent_requires_full_surface_identity_and_group_chat() -> None:
    intent = parse_restore_intent(
        {
            "CCBOT_RESTORE_ENABLED": "1",
            "CCBOT_RESTORE_WINDOW": "comfy-agent",
            "CCBOT_RESTORE_CWD": "/home/tools/server/comfy",
            "CCBOT_RESTORE_RUNTIME_ID": "thread-1",
            "CCBOT_RESTORE_USER_ID": "100",
            "CCBOT_RESTORE_SURFACE_KEY": "t:42",
            "CCBOT_RESTORE_SHARED_GROUP": "true",
            "CCBOT_RESTORE_CHAT_ID": "-1004242",
            "CCBOT_RESTORE_COMMAND": "omx --madmax",
        }
    )

    assert intent == RestoreIntent(
        window_name="comfy-agent",
        cwd="/home/tools/server/comfy",
        runtime_id="thread-1",
        user_id=100,
        surface_key="t:42",
        group_chat_id=-1004242,
        launcher_command="omx --madmax",
        runtime_kind="codex",
        shared_group=True,
    )

    with pytest.raises(RestoreIntentError, match="user_id"):
        parse_restore_intent(
            {
                "CCBOT_RESTORE_ENABLED": "1",
                "CCBOT_RESTORE_WINDOW": "comfy-agent",
                "CCBOT_RESTORE_CWD": "/home/tools/server/comfy",
                "CCBOT_RESTORE_RUNTIME_ID": "thread-1",
                "CCBOT_RESTORE_SURFACE_KEY": "t:42",
            }
        )

    with pytest.raises(RestoreIntentError, match="chat_id"):
        parse_restore_intent(
            {
                "CCBOT_RESTORE_ENABLED": "1",
                "CCBOT_RESTORE_WINDOW": "comfy-agent",
                "CCBOT_RESTORE_CWD": "/home/tools/server/comfy",
                "CCBOT_RESTORE_RUNTIME_ID": "thread-1",
                "CCBOT_RESTORE_USER_ID": "100",
                "CCBOT_RESTORE_SURFACE_KEY": "t:42",
                "CCBOT_RESTORE_SHARED_GROUP": "true",
            }
        )


def test_parse_restore_intent_is_absent_when_no_restore_env() -> None:
    assert parse_restore_intent({}) is None


def test_existing_runtime_reuse_requires_matching_identity_and_not_helper(
    mgr: SessionManager,
) -> None:
    intent = RestoreIntent(
        window_name="comfy-agent",
        cwd="/home/tools/server/comfy",
        runtime_id="thread-1",
        user_id=100,
        surface_key="t:42",
        group_chat_id=-1004242,
        launcher_command="omx --madmax",
        runtime_kind="codex",
        shared_group=True,
    )
    mgr.window_states["@1"] = LiveProcessDescriptor(
        thread_id="other-thread",
        cwd="/home/tools/server/comfy",
        runtime_kind="codex",
    )

    result = validate_existing_runtime_window_for_restore(mgr, "@1", intent)
    assert result.ok is False
    assert "identity" in result.reason

    mgr.window_states["@1"].thread_id = "thread-1"
    result = validate_existing_runtime_window_for_restore(mgr, "@1", intent)
    assert result.ok is True

    mgr.codex_thread_catalog = SimpleNamespace(is_helper_thread_fast=lambda _tid: True)
    result = validate_existing_runtime_window_for_restore(mgr, "@1", intent)
    assert result.ok is False
    assert "helper" in result.reason


def test_bind_restored_surface_records_group_route_and_clears_external(
    mgr: SessionManager,
) -> None:
    intent = RestoreIntent(
        window_name="comfy-agent",
        cwd="/home/tools/server/comfy",
        runtime_id="thread-1",
        user_id=100,
        surface_key="t:42",
        group_chat_id=-1004242,
        launcher_command="omx --madmax",
        runtime_kind="codex",
        shared_group=True,
    )
    mgr.bind_external_surface(
        100,
        runtime_kind="codex",
        source_thread_id="thread-1",
        surface_key="t:42",
        read_only=True,
    )

    bind_restored_surface(mgr, intent, window_id="@9")

    assert mgr.window_states["@9"].thread_id == "thread-1"
    assert mgr.window_states["@9"].runtime_kind == "codex"
    assert mgr.window_states["@9"].cwd == "/home/tools/server/comfy"
    assert mgr.surface_bindings[100]["t:42"] == "@9"
    assert mgr.external_surface_bindings.get(100, {}) == {}
    assert mgr.resolve_chat_id(100, 42) == -1004242


@pytest.mark.asyncio
async def test_startup_restore_reuses_registered_node_runtime_without_injecting(
    mgr: SessionManager,
) -> None:
    intent_env = {
        "CCBOT_RESTORE_ENABLED": "1",
        "CCBOT_RESTORE_WINDOW": "comfy-agent",
        "CCBOT_RESTORE_CWD": "/home/tools/server/comfy",
        "CCBOT_RESTORE_RUNTIME_ID": "thread-1",
        "CCBOT_RESTORE_USER_ID": "100",
        "CCBOT_RESTORE_SURFACE_KEY": "t:42",
        "CCBOT_RESTORE_SHARED_GROUP": "true",
        "CCBOT_RESTORE_CHAT_ID": "-1004242",
        "CCBOT_RESTORE_COMMAND": "omx --madmax",
    }
    mgr.window_states["@1"] = LiveProcessDescriptor(
        thread_id="thread-1",
        cwd="/home/tools/server/comfy",
        runtime_kind="codex",
    )
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(
        return_value=SimpleNamespace(
            window_id="@1",
            window_name="comfy-agent",
            cwd="/home/tools/server/comfy",
            pane_current_command="node",
        )
    )
    tmux.create_or_reuse_window = AsyncMock()

    from ccbot.restore import restore_configured_startup_target

    result = await restore_configured_startup_target(mgr, tmux, environ=intent_env)

    assert result.status == "already_restored"
    assert mgr.surface_bindings[100]["t:42"] == "@1"
    tmux.create_or_reuse_window.assert_not_called()
