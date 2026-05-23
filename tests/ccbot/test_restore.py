from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.restore import (
    RestoreClassification,
    RestoreIntent,
    RestoreIntentError,
    RestorePaneKind,
    StartupRestoreResult,
    bind_restored_surface,
    classify_restore_pane,
    inspect_configured_startup_target,
    is_startup_restore_retryable,
    parse_restore_intent,
    restore_configured_startup_target,
    validate_restore_env_contract,
    build_restore_launch_command,
    validate_existing_runtime_window_for_restore,
)
from ccbot.runtime_types import LiveProcessDescriptor
from ccbot.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


def _intent_env(**overrides: str) -> dict[str, str]:
    env = {
        "CCBOT_RESTORE_ENABLED": "1",
        "CCBOT_RESTORE_WINDOW": "comfy-agent",
        "CCBOT_RESTORE_CWD": "/home/tools/mediagen-comfy",
        "CCBOT_RESTORE_RUNTIME_ID": "thread-1",
        "CCBOT_RESTORE_USER_ID": "100",
        "CCBOT_RESTORE_SURFACE_KEY": "t:42",
        "CCBOT_RESTORE_SHARED_GROUP": "true",
        "CCBOT_RESTORE_CHAT_ID": "-1004242",
        "CCBOT_RESTORE_COMMAND": "omx --madmax",
        "CODEX_HOME": "/tmp/codex-home",
        "OMX_AUTO_UPDATE": "0",
    }
    env.update(overrides)
    return env


def _intent(**overrides) -> RestoreIntent:
    payload = dict(
        window_name="comfy-agent",
        cwd="/home/tools/mediagen-comfy",
        runtime_id="thread-1",
        user_id=100,
        surface_key="t:42",
        group_chat_id=-1004242,
        launcher_command="omx --madmax",
        runtime_kind="codex",
        shared_group=True,
    )
    payload.update(overrides)
    return RestoreIntent(**payload)



def test_startup_restore_retryable_only_for_transient_reboot_surfaces() -> None:
    assert is_startup_restore_retryable(
        StartupRestoreResult(
            "failed",
            classification=RestoreClassification.FULL_LOSS_MISSING_TMUX_WINDOW,
        )
    )
    assert is_startup_restore_retryable(
        StartupRestoreResult(
            "failed",
            classification=RestoreClassification.EXISTING_SHELL_OR_EMPTY_WINDOW,
        )
    )
    assert not is_startup_restore_retryable(
        StartupRestoreResult(
            "failed",
            classification=RestoreClassification.EXISTING_IDENTITY_MISMATCH,
        )
    )
    assert not is_startup_restore_retryable(
        StartupRestoreResult(
            "already_restored",
            classification=RestoreClassification.EXISTING_VALID_RUNTIME,
        )
    )

def test_parse_restore_intent_requires_full_surface_identity_and_group_chat() -> None:
    intent = parse_restore_intent(
        _intent_env()
    )

    assert intent == _intent()

    with pytest.raises(RestoreIntentError, match="user_id"):
        parse_restore_intent(
            {
                "CCBOT_RESTORE_ENABLED": "1",
                "CCBOT_RESTORE_WINDOW": "comfy-agent",
                "CCBOT_RESTORE_CWD": "/home/tools/mediagen-comfy",
                "CCBOT_RESTORE_RUNTIME_ID": "thread-1",
                "CCBOT_RESTORE_SURFACE_KEY": "t:42",
            }
        )

    with pytest.raises(RestoreIntentError, match="chat_id"):
        parse_restore_intent(
            {
                "CCBOT_RESTORE_ENABLED": "1",
                "CCBOT_RESTORE_WINDOW": "comfy-agent",
                "CCBOT_RESTORE_CWD": "/home/tools/mediagen-comfy",
                "CCBOT_RESTORE_RUNTIME_ID": "thread-1",
                "CCBOT_RESTORE_USER_ID": "100",
                "CCBOT_RESTORE_SURFACE_KEY": "t:42",
                "CCBOT_RESTORE_SHARED_GROUP": "true",
            }
        )


def test_parse_restore_intent_accepts_chat_qualified_topic_key() -> None:
    intent = parse_restore_intent(
        _intent_env(
            CCBOT_RESTORE_SURFACE_KEY="t:-1004242:42",
            CCBOT_RESTORE_CHAT_ID="",
        )
    )

    assert intent is not None
    assert intent.surface_key == "t:-1004242:42"
    assert intent.group_chat_id == -1004242

    with pytest.raises(RestoreIntentError, match="chat_id does not match"):
        parse_restore_intent(
            _intent_env(
                CCBOT_RESTORE_SURFACE_KEY="t:-1004242:42",
                CCBOT_RESTORE_CHAT_ID="-1009999",
            )
        )


def test_parse_restore_intent_is_absent_when_no_restore_env() -> None:
    assert parse_restore_intent({}) is None
    assert parse_restore_intent({"CCBOT_RESTORE_ENABLED": "0"}) is None


def test_restore_env_contract_requires_codex_home_and_disabled_omx_update() -> None:
    intent = _intent()

    missing_codex = validate_restore_env_contract(intent, {"OMX_AUTO_UPDATE": "0"})
    assert missing_codex.ok is False
    assert "CODEX_HOME" in missing_codex.reason

    update_prompt_allowed = validate_restore_env_contract(
        intent,
        {"CODEX_HOME": "/tmp/codex-home", "OMX_AUTO_UPDATE": "1"},
    )
    assert update_prompt_allowed.ok is False
    assert "OMX_AUTO_UPDATE=0" in update_prompt_allowed.reason

    ok = validate_restore_env_contract(intent, _intent_env())
    assert ok.ok is True


def test_restore_launch_command_carries_controller_restore_env() -> None:
    command = build_restore_launch_command(_intent(), _intent_env())

    assert command.startswith("CODEX_HOME=/tmp/codex-home OMX_AUTO_UPDATE=0 ")
    assert command.endswith("omx --madmax")


@pytest.mark.asyncio
async def test_inventory_json_preserves_surface_key_without_secret_env(
    mgr: SessionManager,
) -> None:
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(return_value=None)

    inventory = await inspect_configured_startup_target(
        mgr,
        tmux,
        environ=_intent_env(TELEGRAM_BOT_TOKEN="secret-token"),
    )
    data = inventory.to_dict()

    assert data["intent"]["surface_key"] == "t:42"
    assert "TELEGRAM_BOT_TOKEN" not in repr(data)
    assert data["env"] == {
        "codex_home": "/tmp/codex-home",
        "codex_home_present": True,
        "omx_auto_update": "0",
        "omx_auto_update_disabled": True,
    }


def test_existing_runtime_reuse_requires_matching_identity_and_not_helper(
    mgr: SessionManager,
) -> None:
    intent = _intent()
    mgr.window_states["@1"] = LiveProcessDescriptor(
        thread_id="other-thread",
        cwd="/home/tools/mediagen-comfy",
        runtime_kind="codex",
    )

    result = validate_existing_runtime_window_for_restore(mgr, "@1", intent)
    assert result.ok is False
    assert "identity" in result.reason

    mgr.window_states["@1"].thread_id = "thread-1"
    result = validate_existing_runtime_window_for_restore(mgr, "@1", intent)
    assert result.ok is True

    mgr.window_states["@1"].cwd = "/home/tools/server/comfy"
    result = validate_existing_runtime_window_for_restore(mgr, "@1", intent)
    assert result.ok is False
    assert "cwd mismatch" in result.reason

    mgr.window_states["@1"].cwd = "/home/tools/mediagen-comfy"
    mgr.codex_thread_catalog = SimpleNamespace(is_helper_thread_fast=lambda _tid: True)
    result = validate_existing_runtime_window_for_restore(mgr, "@1", intent)
    assert result.ok is False
    assert "helper" in result.reason


def test_bind_restored_surface_records_group_route_and_clears_external(
    mgr: SessionManager,
) -> None:
    intent = _intent()
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
    assert mgr.window_states["@9"].cwd == "/home/tools/mediagen-comfy"
    assert mgr.surface_bindings[100]["t:42"] == "@9"
    assert mgr.external_surface_bindings.get(100, {}) == {}
    assert mgr.resolve_chat_id(100, 42) == -1004242


def test_bind_restored_surface_clears_stale_duplicate_runtime_claim(
    mgr: SessionManager,
) -> None:
    intent = _intent()
    mgr.register_live_process(
        "@6",
        "/home/tools/mediagen-comfy",
        window_name="mediagen-comfy",
        runtime_kind="codex",
        thread_id="thread-1",
    )
    mgr.bind_surface(100, "@6", surface_key="t:8227", window_name="mediagen-comfy")

    bind_restored_surface(mgr, intent, window_id="@9")

    assert mgr.window_states["@9"].thread_id == "thread-1"
    assert mgr.window_states["@6"].thread_id == ""
    assert mgr.surface_bindings[100]["t:42"] == "@9"
    assert mgr.surface_bindings[100]["t:8227"] == "@6"


def test_bind_restored_no_topics_surface_records_threadless_group_route(
    mgr: SessionManager,
) -> None:
    intent = _intent(surface_key="c:-100999", group_chat_id=-100999)

    bind_restored_surface(mgr, intent, window_id="@9")

    assert mgr.surface_bindings[100]["c:-100999"] == "@9"
    assert mgr.resolve_chat_id(100, None) == -100999


def test_classify_restore_pane_keeps_omx_helpers_out_of_work_runtime() -> None:
    assert (
        classify_restore_pane(
            SimpleNamespace(
                pane_current_command="node",
                pane_title="omx hud --watch",
            )
        )
        == RestorePaneKind.OMX_HELPER
    )
    assert (
        classify_restore_pane(SimpleNamespace(pane_current_command="bash", pane_title=""))
        == RestorePaneKind.SHELL_OR_EMPTY
    )
    assert (
        classify_restore_pane(
            SimpleNamespace(pane_current_command="codex", pane_title="")
        )
        == RestorePaneKind.WORK_RUNTIME_CANDIDATE
    )


@pytest.mark.asyncio
async def test_dry_run_inventory_does_not_create_or_bind(mgr: SessionManager) -> None:
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(return_value=None)
    tmux.create_or_reuse_window = AsyncMock()

    result = await restore_configured_startup_target(
        mgr,
        tmux,
        environ=_intent_env(),
        dry_run=True,
    )

    assert result.status == "dry_run"
    assert result.classification == RestoreClassification.UNSAFE_AMBIGUOUS
    assert "missing resume target proof" in result.message
    assert mgr.surface_bindings == {}
    tmux.create_or_reuse_window.assert_not_called()


@pytest.mark.asyncio
async def test_replay_resume_target_does_not_authorize_unrelated_live_pane(
    mgr: SessionManager,
) -> None:
    mgr.window_states["@1"] = LiveProcessDescriptor(
        thread_id="other-thread",
        cwd="/home/tools/mediagen-comfy",
        runtime_kind="codex",
    )
    mgr.codex_thread_catalog = SimpleNamespace(
        get_candidate_fast=lambda _thread_id: SimpleNamespace(
            normalized_cwd="/home/tools/mediagen-comfy",
            rollout_file="/tmp/codex-home/sessions/rollout-thread-1.jsonl",
            to_locator=lambda: SimpleNamespace(
                file_path="/tmp/codex-home/sessions/rollout-thread-1.jsonl",
                cwd="/home/tools/mediagen-comfy",
            ),
        )
    )
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(
        return_value=SimpleNamespace(
            window_id="@1",
            window_name="comfy-agent",
            cwd="/home/tools/mediagen-comfy",
            pane_current_command="codex",
            pane_id="%1",
        )
    )
    tmux.create_or_reuse_window = AsyncMock()

    result = await restore_configured_startup_target(mgr, tmux, environ=_intent_env())

    assert result.status == "failed"
    assert result.classification == RestoreClassification.EXISTING_IDENTITY_MISMATCH
    assert mgr.surface_bindings == {}
    tmux.create_or_reuse_window.assert_not_called()


@pytest.mark.asyncio
async def test_matching_helper_thread_descriptor_does_not_restore_writable_binding(
    mgr: SessionManager,
) -> None:
    mgr.window_states["@1"] = LiveProcessDescriptor(
        thread_id="thread-1",
        cwd="/home/tools/mediagen-comfy",
        runtime_kind="codex",
        window_name="comfy-agent",
    )
    mgr.codex_thread_catalog = SimpleNamespace(is_helper_thread_fast=lambda _tid: True)
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(
        return_value=SimpleNamespace(
            window_id="@1",
            window_name="comfy-agent",
            cwd="/home/tools/mediagen-comfy",
            pane_current_command="codex",
            pane_id="%1",
        )
    )
    tmux.create_or_reuse_window = AsyncMock()

    result = await restore_configured_startup_target(mgr, tmux, environ=_intent_env())

    assert result.status == "failed"
    assert result.classification == RestoreClassification.EXISTING_IDENTITY_MISMATCH
    assert mgr.surface_bindings == {}
    tmux.create_or_reuse_window.assert_not_called()


@pytest.mark.asyncio
async def test_shared_topic_same_thread_wrong_chat_id_fails_closed(
    mgr: SessionManager,
) -> None:
    mgr.set_group_chat_id(100, 42, -1001111)
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock()

    inventory = await inspect_configured_startup_target(
        mgr,
        tmux,
        environ=_intent_env(CCBOT_RESTORE_CHAT_ID="-1002222"),
    )

    assert inventory.classification == RestoreClassification.UNSAFE_AMBIGUOUS
    assert "chat_id mismatch" in inventory.message
    tmux.find_window_by_name.assert_not_called()


@pytest.mark.asyncio
async def test_window_change_between_inventory_and_bind_fails_closed(
    mgr: SessionManager,
) -> None:
    mgr.window_states["@1"] = LiveProcessDescriptor(
        thread_id="thread-1",
        cwd="/home/tools/mediagen-comfy",
        runtime_kind="codex",
        window_name="comfy-agent",
    )
    first = SimpleNamespace(
        window_id="@1",
        window_name="comfy-agent",
        cwd="/home/tools/mediagen-comfy",
        pane_current_command="codex",
        pane_id="%1",
    )
    second = SimpleNamespace(
        window_id="@2",
        window_name="comfy-agent",
        cwd="/home/tools/mediagen-comfy",
        pane_current_command="codex",
        pane_id="%2",
    )
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(side_effect=[first, second])
    tmux.create_or_reuse_window = AsyncMock()

    result = await restore_configured_startup_target(mgr, tmux, environ=_intent_env())

    assert result.status == "failed"
    assert "changed" in result.message
    assert mgr.surface_bindings == {}


@pytest.mark.asyncio
async def test_shell_window_without_resume_target_proof_does_not_launch(
    mgr: SessionManager,
) -> None:
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(
        return_value=SimpleNamespace(
            window_id="@1",
            window_name="comfy-agent",
            cwd="/home/tools/mediagen-comfy",
            pane_current_command="bash",
            pane_id="%1",
        )
    )
    tmux.create_or_reuse_window = AsyncMock()

    result = await restore_configured_startup_target(mgr, tmux, environ=_intent_env())

    assert result.status == "failed"
    assert result.classification == RestoreClassification.UNSAFE_AMBIGUOUS
    assert "resume target proof" in result.message
    tmux.create_or_reuse_window.assert_not_called()


@pytest.mark.asyncio
async def test_full_loss_binds_only_after_resume_and_live_proofs(
    mgr: SessionManager,
) -> None:
    class _Candidate:
        normalized_cwd = "/home/tools/mediagen-comfy"
        rollout_file = "/tmp/codex-home/sessions/rollout-thread-1.jsonl"

        def to_locator(self):
            return SimpleNamespace(
                file_path="/tmp/codex-home/sessions/rollout-thread-1.jsonl",
                cwd="/home/tools/mediagen-comfy",
            )

    mgr.codex_thread_catalog = SimpleNamespace(get_candidate_fast=lambda _tid: _Candidate())

    created_window = SimpleNamespace(
        window_id="@9",
        window_name="comfy-agent",
        cwd="/home/tools/mediagen-comfy",
        pane_current_command="codex",
        pane_id="%9",
    )
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(side_effect=[None, created_window])
    tmux.create_or_reuse_window = AsyncMock(
        return_value=(True, "created", "comfy-agent", "@9", False)
    )

    async def wait_for_entry(window_id, **_kwargs):
        assert window_id == "@9"
        mgr.window_states["@9"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/home/tools/mediagen-comfy",
            runtime_kind="codex",
            window_name="comfy-agent",
        )
        return True

    mgr.wait_for_session_map_entry = wait_for_entry  # type: ignore[method-assign]

    result = await restore_configured_startup_target(mgr, tmux, environ=_intent_env())

    assert result.status == "restored"
    assert result.classification == RestoreClassification.FULL_LOSS_MISSING_TMUX_WINDOW
    assert mgr.surface_bindings[100]["t:42"] == "@9"
    assert (
        tmux.create_or_reuse_window.await_args.kwargs["launch_command"]
        == "CODEX_HOME=/tmp/codex-home OMX_AUTO_UPDATE=0 omx --madmax"
    )


@pytest.mark.asyncio
async def test_startup_restore_reuses_registered_node_runtime_without_injecting(
    mgr: SessionManager,
) -> None:
    intent_env = _intent_env()
    mgr.window_states["@1"] = LiveProcessDescriptor(
        thread_id="thread-1",
        cwd="/home/tools/mediagen-comfy",
        runtime_kind="codex",
    )
    tmux = SimpleNamespace()
    tmux.find_window_by_name = AsyncMock(
        return_value=SimpleNamespace(
            window_id="@1",
            window_name="comfy-agent",
            cwd="/home/tools/mediagen-comfy",
            pane_current_command="node",
        )
    )
    tmux.create_or_reuse_window = AsyncMock()

    result = await restore_configured_startup_target(mgr, tmux, environ=intent_env)

    assert result.status == "already_restored"
    assert result.classification == RestoreClassification.EXISTING_VALID_RUNTIME
    assert mgr.surface_bindings[100]["t:42"] == "@1"
    tmux.create_or_reuse_window.assert_not_called()
