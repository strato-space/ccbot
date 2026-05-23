"""Tests for SessionManager pure dict operations."""

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.codex_threads import CodexThreadCatalog
from ccbot.session import (
    CODEX_DELIVERED_NO_ACK_MESSAGE,
    CODEX_DELIVERED_NO_ACK_STATUS,
    FastRuntimeInputProof,
    PendingSurfaceSlot,
    SessionManager,
    _codex_text_matches_expected_exact,
    _stable_text_hash,
)
from ccbot.runtime_types import LiveProcessDescriptor, ThreadLocator
from ccbot.state_schema import (
    BINDING_STATE_BIND_FLOW,
    BINDING_STATE_BOUND,
    BINDING_STATE_NONE,
    TOPIC_POLICY_IMPLICIT_BIND_ALLOWED,
    TOPIC_POLICY_MANUAL_BIND_REQUIRED,
)


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}

    def test_get_topic_binding(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        binding = mgr.get_topic_binding(100, 1)
        assert binding is not None
        assert binding.user_id == 100
        assert binding.thread_id == 1
        assert binding.window_id == "@1"
        assert binding.window_name == "proj"

    def test_get_topic_binding_preserves_runtime_kind(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        mgr.get_window_state("@1").runtime_kind = "codex"
        binding = mgr.get_topic_binding(100, 1)
        assert binding is not None
        assert binding.runtime_kind == "codex"

    def test_iter_topic_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="one")
        mgr.bind_thread(100, 2, "@2", window_name="two")
        result = {
            (b.user_id, b.thread_id, b.window_id) for b in mgr.iter_topic_bindings()
        }
        assert result == {(100, 1, "@1"), (100, 2, "@2")}

    def test_iter_topic_bindings_preserves_surface_coordinates(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_surface(100, "@1", surface_key="t:-100200300:42", window_name="one")

        binding = next(mgr.iter_topic_bindings())

        assert binding.surface_key == "t:-100200300:42"
        assert binding.chat_id == -100200300
        assert binding.thread_id == 42

    def test_iter_topic_bindings_canonicalizes_legacy_topic_when_chat_is_known(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_group_chat_id(100, 42, -100200300)
        mgr.bind_surface(100, "@1", surface_key="t:42", window_name="one")

        binding = next(mgr.iter_topic_bindings())

        assert binding.surface_key == "t:-100200300:42"
        assert binding.chat_id == -100200300
        assert binding.thread_id == 42

    def test_iter_topic_bindings_deduplicates_canonical_and_legacy_topic_mirrors(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_group_chat_id(100, 42, -100200300)
        mgr.bind_surface(100, "@1", surface_key="t:42", window_name="one")
        mgr.bind_surface(
            100,
            "@1",
            surface_key="t:-100200300:42",
            window_name="one",
        )

        bindings = list(mgr.iter_topic_bindings())

        assert len(bindings) == 1
        assert bindings[0].surface_key == "t:-100200300:42"

    def test_iter_topic_bindings_preserves_runtime_kind(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="one")
        mgr.get_window_state("@1").runtime_kind = "codex"
        binding = next(mgr.iter_topic_bindings())
        assert binding.runtime_kind == "codex"

    def test_helper_codex_window_binding_is_pruned_fail_closed(
        self, mgr: SessionManager
    ) -> None:
        mgr.codex_thread_catalog = SimpleNamespace(
            is_helper_thread_fast=lambda thread_id: thread_id == "helper-thread"
        )
        mgr.bind_thread(100, 1, "@45", window_name="comfy-agent-spec")
        mgr.bind_thread(100, 2, "@0", window_name="comfy-agent")
        helper_state = mgr.get_window_state("@45")
        helper_state.runtime_kind = "codex"
        helper_state.thread_id = "helper-thread"
        parent_state = mgr.get_window_state("@0")
        parent_state.runtime_kind = "codex"
        parent_state.thread_id = "parent-thread"
        mgr.user_window_offsets = {100: {"@45": 123, "@0": 456}}

        removed = mgr.cleanup_helper_window_bindings()

        assert [(b.user_id, b.thread_id, b.window_id) for b in removed] == [
            (100, 1, "@45")
        ]
        assert mgr.get_window_for_thread(100, 1) is None
        assert mgr.get_topic_binding(100, 1) is None
        assert mgr.get_window_for_thread(100, 2) == "@0"
        assert {
            (b.user_id, b.thread_id, b.window_id) for b in mgr.iter_topic_bindings()
        } == {(100, 2, "@0")}
        assert mgr.topic_binding_states[100][1] == BINDING_STATE_NONE
        assert mgr.user_window_offsets == {100: {"@0": 456}}

    def test_helper_codex_window_binding_is_hidden_before_cleanup(
        self, mgr: SessionManager
    ) -> None:
        mgr.codex_thread_catalog = SimpleNamespace(
            is_helper_thread_fast=lambda thread_id: thread_id == "helper-thread"
        )
        mgr.bind_thread(100, 1, "@45", window_name="comfy-agent-spec")
        helper_state = mgr.get_window_state("@45")
        helper_state.runtime_kind = "codex"
        helper_state.thread_id = "helper-thread"

        assert mgr.get_window_for_thread(100, 1) is None
        assert mgr.get_topic_binding(100, 1) is None
        assert list(mgr.iter_topic_bindings()) == []

    def test_state_less_tmux_binding_is_pruned_fail_closed(
        self, mgr: SessionManager
    ) -> None:
        mgr.surface_bindings = {100: {"t:1": "@45"}}
        mgr.thread_bindings = {100: {1: "@45"}}
        mgr.surface_binding_states = {100: {"t:1": BINDING_STATE_BOUND}}
        mgr.topic_binding_states = {100: {1: BINDING_STATE_BOUND}}
        mgr.window_states = {}

        removed = mgr.cleanup_helper_window_bindings()

        assert [(b.user_id, b.thread_id, b.window_id) for b in removed] == [
            (100, 1, "@45")
        ]
        assert mgr.get_window_for_thread(100, 1) is None
        assert mgr.get_topic_binding(100, 1) is None
        assert list(mgr.iter_topic_bindings()) == []
        assert mgr.topic_binding_states[100][1] == BINDING_STATE_NONE

    def test_bind_thread_creates_window_descriptor_for_live_tmux_id(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 1, "@45", window_name="manual-window")

        assert "@45" in mgr.window_states
        assert mgr.get_window_for_thread(100, 1) == "@45"

    def test_bind_external_thread_exposes_external_topic_binding(
        self, mgr: SessionManager
    ) -> None:
        binding_window_id = mgr.bind_external_thread(
            100,
            1,
            runtime_kind="codex",
            source_thread_id="thread-1",
            summary="Thread One",
            cwd="/tmp/project",
            file_path="/tmp/rollout-thread-1.jsonl",
            read_only=True,
        )

        assert binding_window_id == "external:codex:thread-1"
        binding = mgr.get_topic_binding(100, 1)
        assert binding is not None
        assert binding.binding_scope == "external"
        assert binding.source_thread_id == "thread-1"
        assert binding.read_only is True
        assert binding.runtime_kind == "codex"
        assert binding.window_name == "Thread One"


class TestSurfaceKeyedBindings:
    def test_make_surface_key_formats_topic_and_chat(self, mgr: SessionManager) -> None:
        assert mgr.make_surface_key(thread_id=42) == "t:42"
        assert mgr.make_surface_key(chat_id=-100123, thread_id=42) == "t:-100123:42"
        assert mgr.make_surface_key(chat_id=-100123) == "c:-100123"

    def test_bind_surface_chat_key_does_not_backfill_legacy_topic_maps(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_surface(100, "@9", surface_key="c:-100123", window_name="main-chat")

        assert mgr.get_window_for_surface(100, surface_key="c:-100123") == "@9"
        assert mgr.get_window_for_thread(100, 42) is None
        assert mgr.thread_bindings == {}

    def test_bind_surface_topic_key_keeps_legacy_topic_wrappers_in_sync(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_surface(100, "@7", surface_key="t:42", window_name="proj")

        assert mgr.get_window_for_surface(100, surface_key="t:42") == "@7"
        assert mgr.get_window_for_thread(100, 42) == "@7"

    def test_chat_qualified_surface_keys_do_not_collide_by_topic_id(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_surface(100, "@7", surface_key="t:-1001:42", window_name="one")
        mgr.bind_surface(100, "@8", surface_key="t:-1002:42", window_name="two")

        assert mgr.get_window_for_surface(100, surface_key="t:-1001:42") == "@7"
        assert mgr.get_window_for_surface(100, surface_key="t:-1002:42") == "@8"

    def test_chat_qualified_lookup_can_read_legacy_topic_key_when_chat_matches(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_group_chat_id(100, 42, -1001)
        mgr.bind_surface(100, "@7", surface_key="t:42", window_name="legacy")

        assert mgr.get_window_for_surface(100, surface_key="t:-1001:42") == "@7"
        assert mgr.get_window_for_surface(100, surface_key="t:-1002:42") is None

    def test_external_surface_binding_round_trips_for_chat_surface(
        self, mgr: SessionManager
    ) -> None:
        binding_window_id = mgr.bind_external_surface(
            100,
            runtime_kind="codex",
            source_thread_id="thread-1",
            summary="Main chat",
            cwd="/tmp/project",
            file_path="/tmp/rollout-thread-1.jsonl",
            read_only=True,
            surface_key="c:-100123",
        )

        assert binding_window_id == "external:codex:thread-1"
        assert (
            mgr.get_window_for_surface(100, surface_key="c:-100123")
            == binding_window_id
        )
        assert mgr.get_external_surface_binding(100, surface_key="c:-100123") == {
            "runtime_kind": "codex",
            "source_thread_id": "thread-1",
            "summary": "Main chat",
            "cwd": "/tmp/project",
            "file_path": "/tmp/rollout-thread-1.jsonl",
            "read_only": True,
        }

    def test_surface_pending_slot_latest_wins_and_consumes_once(
        self, mgr: SessionManager
    ) -> None:
        first = mgr.set_surface_pending_slot(100, "hello", surface_key="t:42")
        second = mgr.set_surface_pending_slot(100, "hello again", surface_key="t:42")

        assert first["revision"] == 1
        assert second["revision"] == 2
        assert isinstance(mgr.surface_pending_slots[100]["t:42"], PendingSurfaceSlot)
        assert mgr.peek_surface_pending_slot(100, surface_key="t:42") == second

        consumed = mgr.consume_surface_pending_slot(
            100,
            "activation-1",
            surface_key="t:42",
        )
        assert consumed is not None
        assert consumed["status"] == "consumed"
        assert consumed["consumed_by_activation_id"] == "activation-1"
        assert (
            mgr.consume_surface_pending_slot(
                100,
                "activation-1",
                surface_key="t:42",
            )
            is None
        )
        assert (
            mgr.consume_surface_pending_slot(
                100,
                "activation-2",
                surface_key="t:42",
            )
            is None
        )

    def test_clear_surface_pending_slot_returns_previous_record(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_surface_pending_slot(100, "queued", surface_key="c:-100123")

        cleared = mgr.clear_surface_pending_slot(100, surface_key="c:-100123")

        assert cleared is not None
        assert cleared["text"] == "queued"
        assert mgr.peek_surface_pending_slot(100, surface_key="c:-100123") is None

    def test_clear_surface_pending_slot_missing_key_is_noop(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_surface_pending_slot(100, "queued", surface_key="t:41")

        cleared = mgr.clear_surface_pending_slot(100, surface_key="t:42")

        assert cleared is None
        assert mgr.peek_surface_pending_slot(100, surface_key="t:41") is not None

    def test_pending_surface_slot_normalizes_storage_records(self) -> None:
        slot = PendingSurfaceSlot.from_record(
            {
                "text": "queued",
                "revision": "not-an-int",
                "status": "unexpected",
                "consumed_by_activation_id": "stale",
            }
        )

        assert slot is not None
        assert slot.to_dict() == {
            "text": "queued",
            "revision": 1,
            "status": "pending",
            "consumed_by_activation_id": "",
        }
        assert slot.consume("activation-1").to_dict() == {
            "text": "queued",
            "revision": 1,
            "status": "consumed",
            "consumed_by_activation_id": "activation-1",
        }


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id supports no-topics main-chat routing via the chat-wide slot."""
        mgr.set_group_chat_id(100, None, -999)
        assert mgr.resolve_chat_id(100, None) == -999
        assert mgr.group_chat_ids.get("100:0") == -999

    def test_make_surface_key_for_topic_and_chat(self, mgr: SessionManager) -> None:
        assert mgr.make_surface_key(thread_id=42) == "t:42"
        assert mgr.make_surface_key(chat_id=-100200300, thread_id=42) == (
            "t:-100200300:42"
        )
        assert mgr.make_surface_key(chat_id=-100200300) == "c:-100200300"


class TestSurfaceBindingsChatMode:
    def test_bind_surface_for_chat_main_thread(self, mgr: SessionManager) -> None:
        mgr.bind_surface(100, "@7", chat_id=-100200300, window_name="main-chat")

        assert mgr.get_window_for_surface(100, chat_id=-100200300) == "@7"
        assert mgr.resolve_window_for_thread(100, None, chat_id=-100200300) == "@7"
        assert mgr.surface_bindings[100]["c:-100200300"] == "@7"

        assert (100, None, "@7") in set(mgr.iter_thread_bindings())
        assert any(
            binding.thread_id is None and binding.window_id == "@7"
            for binding in mgr.iter_topic_bindings()
        )

    def test_get_surface_coordinates_for_chat_window(self, mgr: SessionManager) -> None:
        mgr.bind_surface(100, "@7", chat_id=-100200300, window_name="main-chat")

        surface_key, chat_id, thread_id = mgr.get_surface_coordinates_for_window(
            100, "@7"
        )

        assert surface_key == "c:-100200300"
        assert chat_id == -100200300
        assert thread_id is None

    def test_get_surface_coordinates_canonicalizes_legacy_topic_when_chat_is_known(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_group_chat_id(100, 42, -100200300)
        mgr.bind_surface(100, "@7", surface_key="t:42", window_name="topic")

        surface_key, chat_id, thread_id = mgr.get_surface_coordinates_for_window(
            100,
            "@7",
        )

        assert surface_key == "t:-100200300:42"
        assert chat_id == -100200300
        assert thread_id == 42

    def test_surface_policy_and_binding_state_for_chat(
        self, mgr: SessionManager
    ) -> None:
        mgr.require_manual_bind_for_surface(100, chat_id=-100200300)
        assert (
            mgr.get_surface_policy(100, chat_id=-100200300)
            == TOPIC_POLICY_MANUAL_BIND_REQUIRED
        )
        assert (
            mgr.get_surface_binding_state(100, chat_id=-100200300) == BINDING_STATE_NONE
        )

        mgr.start_surface_bind_flow(100, chat_id=-100200300)
        assert (
            mgr.get_surface_binding_state(100, chat_id=-100200300)
            == BINDING_STATE_BIND_FLOW
        )
        version, nonce = mgr.get_surface_bind_flow_credentials(100, chat_id=-100200300)
        assert mgr.validate_surface_bind_flow_callback(
            100,
            version,
            nonce,
            chat_id=-100200300,
        )

    def test_surface_pending_slot_is_consume_once(self, mgr: SessionManager) -> None:
        pending = mgr.set_surface_pending_slot(100, "hello", chat_id=-100200300)
        assert pending["revision"] == 1
        assert pending["status"] == "pending"
        assert mgr.peek_surface_pending_slot(100, chat_id=-100200300)["text"] == "hello"

        overwritten = mgr.set_surface_pending_slot(100, "updated", chat_id=-100200300)
        assert overwritten["revision"] == 2
        assert (
            mgr.peek_surface_pending_slot(100, chat_id=-100200300)["text"] == "updated"
        )

        consumed = mgr.consume_surface_pending_slot(
            100,
            "activation-1",
            chat_id=-100200300,
        )
        assert consumed is not None
        assert consumed["status"] == "consumed"
        assert consumed["consumed_by_activation_id"] == "activation-1"
        assert (
            mgr.consume_surface_pending_slot(
                100,
                "activation-2",
                chat_id=-100200300,
            )
            is None
        )

        mgr.clear_surface_pending_slot(100, chat_id=-100200300)
        assert mgr.peek_surface_pending_slot(100, chat_id=-100200300) is None


class TestRuntimeCapabilityRegistryIntegration:
    def test_session_manager_exposes_runtime_capabilities(
        self, mgr: SessionManager
    ) -> None:
        claude = mgr.get_runtime_capability("claude")
        codex = mgr.get_runtime_capability("codex")
        fast_agent = mgr.get_runtime_capability("fast-agent")

        assert claude.tmux_stdio_cli_first is True
        assert codex.resume_style == "subcommand"
        assert fast_agent.replay_evidence_discovery == "acp_log_jsonl"
        assert fast_agent.supports_message_routing_mode("steer")


class TestWindowState:
    def test_get_process_descriptor_is_read_only(self, mgr: SessionManager) -> None:
        assert mgr.get_process_descriptor("@missing") is None
        assert "@missing" not in mgr.window_states

    def test_get_or_create_process_descriptor_creates_descriptor(
        self, mgr: SessionManager
    ) -> None:
        state = mgr.get_or_create_process_descriptor("@created")

        assert isinstance(state, LiveProcessDescriptor)
        assert mgr.get_process_descriptor("@created") is state
        assert "@created" in mgr.window_states

    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_binding(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_binding("@1")
        assert mgr.get_window_state("@1").session_id == ""

    def test_clear_window_session_alias(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""

    def test_reconcile_live_tmux_window_adopts_codex_after_cwd_drift(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / ".codex"
        sessions_root = codex_home / "sessions" / "2026" / "05" / "22"
        sessions_root.mkdir(parents=True)
        new_thread_id = "019e4e71-1499-7d11-991b-2de6af8aa0ce"
        rollout = sessions_root / f"rollout-2026-05-22T06-48-14-{new_thread_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-22T06:48:14Z",
                    "type": "session_meta",
                    "payload": {
                        "id": new_thread_id,
                        "cwd": "/tmp/new-project",
                        "originator": "codex_cli",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        manager = SessionManager(
            codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
        )
        manager.window_states["@8"] = LiveProcessDescriptor(
            thread_id="019d6825-88ba-7f10-948e-eaaf162ea2a9",
            cwd="/tmp/old-project",
            runtime_kind="codex",
            window_name="comfy-agent",
        )

        changed = manager.reconcile_live_tmux_window(
            window_id="@8",
            cwd="/tmp/new-project",
            window_name="comfy-agent",
            pane_current_command="node",
        )

        assert changed is True
        state = manager.window_states["@8"]
        assert state.thread_id == new_thread_id
        assert state.cwd == "/tmp/new-project"

    def test_reconcile_fresh_codex_cwd_drift_stays_replay_silent_without_fd_proof(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / ".codex"
        sessions_root = codex_home / "sessions" / "2026" / "05" / "22"
        sessions_root.mkdir(parents=True)
        stale_thread_id = "019e4e71-1499-7d11-991b-2de6af8aa0ce"
        rollout = sessions_root / f"rollout-2026-05-22T06-48-14-{stale_thread_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-22T06:48:14Z",
                    "type": "session_meta",
                    "payload": {
                        "id": stale_thread_id,
                        "cwd": "/tmp/new-project",
                        "originator": "codex_cli",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        manager = SessionManager(
            codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
        )
        manager.window_states["@8"] = LiveProcessDescriptor(
            thread_id="",
            cwd="/tmp/old-project",
            runtime_kind="codex",
            window_name="comfy-agent",
            registered_at=100.0,
            requires_live_proof=True,
        )

        changed = manager.reconcile_live_tmux_window(
            window_id="@8",
            cwd="/tmp/new-project",
            window_name="comfy-agent",
            pane_current_command="node",
        )

        assert changed is True
        state = manager.window_states["@8"]
        assert state.thread_id == ""
        assert state.cwd == "/tmp/new-project"
        assert state.requires_live_proof is True

    def test_reconcile_live_tmux_window_adopts_same_cwd_with_fd_proof(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        new_thread_id = "019e4e71-1499-7d11-991b-2de6af8aa0ce"
        mgr.window_states["@8"] = LiveProcessDescriptor(
            thread_id="019d6825-88ba-7f10-948e-eaaf162ea2a9",
            cwd="/tmp/project",
            runtime_kind="codex",
            window_name="comfy-agent",
        )
        monkeypatch.setattr(
            mgr,
            "_resolve_live_codex_rollout_from_pane_pid",
            lambda *, pane_pid, cwd: SimpleNamespace(
                thread_id=new_thread_id,
                cwd="/tmp/project",
                mtime=100.0,
                ordering_timestamp=100.0,
            ),
        )

        changed = mgr.reconcile_live_tmux_window(
            window_id="@8",
            cwd="/tmp/project",
            window_name="comfy-agent",
            pane_current_command="node",
            pane_pid="1234",
        )

        assert changed is True
        state = mgr.window_states["@8"]
        assert state.thread_id == new_thread_id
        assert state.cwd == "/tmp/project"

    def test_resolve_live_codex_rollout_matches_timestamped_filename(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / ".codex"
        sessions_root = codex_home / "sessions" / "2026" / "05" / "23"
        sessions_root.mkdir(parents=True)
        thread_id = "019e5459-7f95-7cd1-906b-2d04e664796c"
        rollout = sessions_root / f"rollout-2026-05-23T10-20-12-{thread_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-23T10:20:12Z",
                    "type": "session_meta",
                    "payload": {
                        "id": thread_id,
                        "cwd": "/tmp/project",
                        "originator": "codex_cli",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        manager = SessionManager(
            codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
        )
        monkeypatch.setattr(
            manager,
            "_iter_open_rollout_paths_under_pid",
            lambda _root_pid: iter((rollout,)),
        )

        candidate = manager._resolve_live_codex_rollout_from_pane_pid(
            pane_pid="1234",
            cwd="/tmp/project",
        )

        assert candidate is not None
        assert candidate.thread_id == thread_id

    @pytest.mark.asyncio
    async def test_resolve_thread_for_window_requires_live_proof_for_fresh_codex(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / ".codex"
        sessions_root = codex_home / "sessions" / "2026" / "05" / "16"
        sessions_root.mkdir(parents=True)
        stale_thread_id = "019e3169-663d-76f0-aeaf-18c952412efd"
        rollout = sessions_root / f"rollout-2026-05-16T15-30-51-{stale_thread_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-16T15:30:51Z",
                    "type": "session_meta",
                    "payload": {"id": stale_thread_id, "cwd": "/tmp/project"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        manager = SessionManager(
            codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
        )
        manager.window_states["@8"] = LiveProcessDescriptor(
            thread_id=stale_thread_id,
            cwd="/tmp/project",
            runtime_kind="codex",
            window_name="comfy-agent-ops",
            registered_at=100.0,
            requires_live_proof=True,
        )

        locator = await manager.resolve_thread_for_window("@8")

        assert locator is None
        assert manager.window_states["@8"].thread_id == stale_thread_id

    def test_reconcile_live_tmux_window_same_cwd_without_fd_proof_keeps_binding(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old_thread_id = "019d6825-88ba-7f10-948e-eaaf162ea2a9"
        mgr.window_states["@8"] = LiveProcessDescriptor(
            thread_id=old_thread_id,
            cwd="/tmp/project",
            runtime_kind="codex",
            window_name="comfy-agent",
        )
        monkeypatch.setattr(
            mgr,
            "_resolve_live_codex_rollout_from_pane_pid",
            lambda *, pane_pid, cwd: None,
        )

        changed = mgr.reconcile_live_tmux_window(
            window_id="@8",
            cwd="/tmp/project",
            window_name="comfy-agent",
            pane_current_command="node",
            pane_pid="1234",
        )

        assert changed is False
        assert mgr.window_states["@8"].thread_id == old_thread_id


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestCodexHistory:
    @pytest.mark.asyncio
    async def test_get_recent_messages_reads_codex_rollout_events(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / ".codex"
        sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
        sessions_root.mkdir(parents=True)
        thread_id = "019d4e76-7fae-7a90-bc40-2290ee269660"
        (codex_home / "session_index.jsonl").write_text(
            json.dumps(
                {
                    "id": thread_id,
                    "thread_name": "History thread",
                    "updated_at": "2026-04-02T14:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rollout = sessions_root / f"rollout-{thread_id}.jsonl"
        rollout.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-04-02T14:00:00Z",
                            "type": "session_meta",
                            "payload": {"id": thread_id, "cwd": "/tmp/project-9"},
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-04-02T14:00:01Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "agent_message",
                                "phase": "commentary",
                                "message": "Working through it",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-04-02T14:00:02Z",
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": "tool-1",
                                "output": [{"type": "output_text", "text": "done"}],
                            },
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        manager = SessionManager(
            codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
        )
        manager.register_live_process(
            "@9",
            "/tmp/project-9",
            runtime_kind="codex",
            thread_id=thread_id,
        )
        manager.bind_thread(100, 7, "@9", window_name="proj-9")

        messages, total = await manager.get_recent_messages("@9")

        assert total == 2
        assert messages[0]["content_type"] == "commentary"
        assert messages[0]["event_kind"] == "commentary"
        assert messages[1]["content_type"] == "tool_result"
        assert "```text" in messages[1]["text"]
        assert "done" in messages[1]["text"]

    @pytest.mark.asyncio
    async def test_resolve_thread_for_external_codex_binding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        codex_home = tmp_path / ".codex"
        sessions_root = codex_home / "sessions" / "2026" / "04" / "02"
        sessions_root.mkdir(parents=True)
        thread_id = "019d4e76-7fae-7a90-bc40-2290ee269660"
        (codex_home / "session_index.jsonl").write_text(
            json.dumps(
                {
                    "id": thread_id,
                    "thread_name": "External thread",
                    "updated_at": "2026-04-02T14:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        rollout = sessions_root / f"rollout-{thread_id}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-04-02T14:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": thread_id, "cwd": "/tmp/project-9"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        manager = SessionManager(
            codex_thread_catalog=CodexThreadCatalog(codex_home=codex_home)
        )
        binding_window_id = manager.bind_external_thread(
            100,
            7,
            runtime_kind="codex",
            source_thread_id=thread_id,
            summary="External thread",
            cwd="/tmp/project-9",
            file_path=str(rollout),
            read_only=True,
        )

        locator = await manager.resolve_thread_for_window(binding_window_id)

        assert locator is not None
        assert locator.thread_id == thread_id
        assert locator.runtime_kind == "codex"
        assert locator.cwd == "/tmp/project-9"
        assert locator.file_path.endswith(f"rollout-{thread_id}.jsonl")


class TestRuntimeInputDriverIntegration:
    @pytest.mark.asyncio
    async def test_send_to_window_uses_runtime_input_driver(self, mgr: SessionManager):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_raw_slash_command = AsyncMock(
                return_value=(True, "Sent text to @1")
            )

            success, message = await mgr.send_to_window("@1", "/usage")

        assert success is True
        assert message == "Sent to @1"
        mock_driver.send_raw_slash_command.assert_awaited_once_with(
            "@1",
            "/usage",
            runtime_kind="codex",
        )
        mock_driver.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_to_window_single_line_codex_waits_for_rollout_ack(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.01)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.05)

        async def submit_and_append_ack(*args, **kwargs):
            with rollout.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "turn_context"}) + "\n")
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_raw_slash_command = AsyncMock()
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_ack
            )

            success, message = await mgr.send_to_window(
                "@1",
                "hello $oh-my-codex:deep-interview",
            )

        assert success is True
        assert message == "Sent to @1"
        mock_driver.send_text.assert_awaited_once_with(
            "@1",
            "hello $oh-my-codex:deep-interview",
            runtime_kind="codex",
            submit=False,
        )
        mock_driver.send_multiline_submit_key.assert_awaited_once_with(
            "@1",
            runtime_kind="codex",
        )
        mock_driver.send_raw_slash_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_to_window_single_line_codex_reports_delivered_without_short_ack(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.003)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_MAX_ATTEMPTS", 1)

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                return_value=(True, "Submitted text to @1")
            )

            success, message = await mgr.send_to_window("@1", "hello")

        assert success is True
        assert message == CODEX_DELIVERED_NO_ACK_MESSAGE
        mock_driver.send_text.assert_awaited_once_with(
            "@1",
            "hello",
            runtime_kind="codex",
            submit=False,
        )

    @pytest.mark.asyncio
    async def test_send_to_window_codex_closes_completion_popup_before_ack_retry(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        text = (
            "и еще, проанализируй диалог за сутки, нужен план улучшений. "
            "$deep-interview"
        )
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_MAX_ATTEMPTS", 2)
        submit_attempts = 0

        async def submit_and_ack_on_second_attempt(*args, **kwargs):
            nonlocal submit_attempts
            submit_attempts += 1
            if submit_attempts == 2:
                with rollout.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "turn_context"}) + "\n")
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(
                side_effect=[
                    "",
                    (
                        "› и еще, проанализируй диалог за сутки. $deep-interview\n\n"
                        "  no matches\n\n"
                        "  Press enter to insert or esc to close"
                    ),
                ]
            )
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_ack_on_second_attempt
            )
            mock_driver.send_special_key = AsyncMock(return_value=(True, "Sent Escape"))

            success, message = await mgr.send_to_window("@1", text)

        assert success is True
        assert message == "Sent to @1"
        assert mock_driver.send_multiline_submit_key.await_count == 2
        mock_driver.send_special_key.assert_awaited_once_with(
            "@1",
            "Escape",
            runtime_kind="codex",
        )

    @pytest.mark.asyncio
    async def test_send_to_window_codex_confirms_workflow_autocomplete_alias(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_MAX_ATTEMPTS", 2)
        submit_attempts = 0

        async def submit_and_ack_on_confirmation(*args, **kwargs):
            nonlocal submit_attempts
            submit_attempts += 1
            if submit_attempts == 2:
                with rollout.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": "$ralph"}
                                    ],
                                },
                            }
                        )
                        + "\n"
                    )
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(side_effect=["", "› $oh-my-codex:ralph"])
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_ack_on_confirmation
            )

            success, message = await mgr.send_to_window("@1", "$ralph")

        assert success is True
        assert message == "Sent to @1"
        assert mock_driver.send_multiline_submit_key.await_count == 2

    @pytest.mark.asyncio
    async def test_send_to_window_codex_multiline_waits_for_rollout_ack(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        text = "line one\nline two\n$ralph"
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.01)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.05)

        async def submit_and_append_ack(*args, **kwargs):
            with rollout.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "turn_context",
                            "payload": {"cwd": "/tmp/project"},
                        }
                    )
                    + "\n"
                )
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_ack
            )
            mock_driver.send_raw_slash_command = AsyncMock()

            success, message = await mgr.send_to_window("@1", text)

        assert success is True
        assert message == "Sent to @1"
        mock_driver.send_text.assert_awaited_once_with(
            "@1",
            text,
            runtime_kind="codex",
            submit=False,
        )
        mock_driver.send_multiline_submit_key.assert_awaited_once_with(
            "@1",
            runtime_kind="codex",
        )

    @pytest.mark.asyncio
    async def test_send_to_window_codex_multiline_fails_without_rollout_evidence(
        self,
        mgr: SessionManager,
    ):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(return_value=None)

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock()

            success, message = await mgr.send_to_window("@1", "line one\nline two")

        assert success is False
        assert "missing or mismatched persisted rollout evidence" in message
        mock_driver.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_to_window_codex_fails_on_mismatched_runtime_identity(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ):
        rollout = tmp_path / "other-thread.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="other-thread",
                summary="Other",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock()

            success, message = await mgr.send_to_window("@1", "hello")

        assert success is False
        assert "mismatched persisted rollout evidence" in message
        mock_driver.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_to_window_codex_multiline_fails_closed_when_busy(
        self,
        mgr: SessionManager,
    ):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(
                return_value=(
                    "previous output\n"
                    "· Working (3m 09s • esc to interrupt)\n"
                    "────────────────────────────────────────\n"
                )
            )
            mock_driver.send_text = AsyncMock()

            success, message = await mgr.send_to_window("@1", "hello")

        assert success is False
        assert "requires an idle/input-ready pane" in message
        mock_driver.send_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_to_window_bang_command_bypasses_codex_ack(
        self, mgr: SessionManager
    ):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(return_value=None)

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
            patch.object(
                mgr,
                "_submit_codex_text_with_rollout_ack",
                new_callable=AsyncMock,
            ) as mock_ack,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))

            success, message = await mgr.send_to_window("@1", "!pwd")

        assert success is True
        assert message == "Sent to @1"
        mock_driver.send_text.assert_awaited_once_with(
            "@1",
            "!pwd",
            runtime_kind="codex",
        )
        mock_ack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fast_codex_send_returns_before_async_ack(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_TIMEOUT_SECONDS", 0.05)
        done = asyncio.Event()

        async def on_complete(proof):
            done.set()

        async def submit_and_append_ack(*args, **kwargs):
            with rollout.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "hello"}],
                            },
                        }
                    )
                    + "\n"
                )
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_ack
            )

            success, message, proof = await mgr.send_to_window_fast_unverified(
                "@1",
                "hello",
                proof_id="proof-1",
                user_id=100,
                chat_id=-100,
                thread_id=42,
                surface_key="t:42",
                on_complete=on_complete,
            )

        assert success is True
        assert message == "Sent text to @1"
        assert proof is not None
        assert proof.status == "pending"
        assert mgr.has_pending_fast_input("@1") is True
        await asyncio.wait_for(done.wait(), timeout=1)
        assert proof.status == "ack_confirmed"
        assert mgr.has_pending_fast_input("@1") is False
        mock_driver.send_text.assert_awaited_once_with(
            "@1",
            "hello",
            runtime_kind="codex",
            submit=False,
        )
        mock_driver.send_multiline_submit_key.assert_awaited_once_with(
            "@1",
            runtime_kind="codex",
        )

    @pytest.mark.asyncio
    async def test_fast_codex_ack_rejects_bare_turn_context_and_does_not_retry(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_TIMEOUT_SECONDS", 0.005)
        done = asyncio.Event()

        async def on_complete(proof):
            done.set()

        async def submit_and_append_turn_context(*args, **kwargs):
            with rollout.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "turn_context"}) + "\n")
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_turn_context
            )

            success, _, proof = await mgr.send_to_window_fast_unverified(
                "@1",
                "hello",
                proof_id="proof-2",
                user_id=100,
                chat_id=-100,
                thread_id=42,
                surface_key="t:42",
                on_complete=on_complete,
            )

        assert success is True
        assert proof is not None
        await asyncio.wait_for(done.wait(), timeout=1)
        assert proof.status == CODEX_DELIVERED_NO_ACK_STATUS
        mock_driver.send_multiline_submit_key.assert_awaited_once()

    def test_codex_ack_text_match_tolerates_trailing_line_spaces_only(self) -> None:
        assert _codex_text_matches_expected_exact(
            "first line   \n\nsecond line \n",
            "first line\n\nsecond line",
        )
        assert not _codex_text_matches_expected_exact(
            "prefix first line\n\nsecond line suffix",
            "first line\n\nsecond line",
        )

    @pytest.mark.asyncio
    async def test_fast_codex_input_confirms_workflow_autocomplete_alias(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_TIMEOUT_SECONDS", 0.05)
        done = asyncio.Event()

        async def on_complete(proof):
            done.set()

        submit_attempts = 0

        async def submit_and_ack_on_confirmation(*args, **kwargs):
            nonlocal submit_attempts
            submit_attempts += 1
            if submit_attempts == 2:
                with rollout.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": "$ralph"}
                                    ],
                                },
                            }
                        )
                        + "\n"
                    )
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="› $oh-my-codex:ralph")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_ack_on_confirmation
            )

            success, message, proof = await mgr.send_to_window_fast_unverified(
                "@1",
                "$ralph",
                proof_id="proof-workflow",
                user_id=100,
                chat_id=-100,
                thread_id=42,
                surface_key="t:42",
                on_complete=on_complete,
            )

        assert success is True
        assert message == "Sent text to @1"
        assert proof is not None
        assert mock_driver.send_multiline_submit_key.await_count == 2
        await asyncio.wait_for(done.wait(), timeout=1)
        assert proof.status == "ack_confirmed"

    @pytest.mark.asyncio
    async def test_fast_codex_ack_rejects_unrelated_superstring_user_message(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "please do ping now"}
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        confirmed, byte_offset = await mgr._codex_rollout_has_strict_user_ack(
            file_path=rollout,
            start_byte=0,
            expected_text="ping",
        )

        assert confirmed is False
        assert byte_offset is None

    @pytest.mark.asyncio
    async def test_fast_codex_second_input_does_not_overlap_pending_proof(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_POLL_SECONDS", 0.01)
        monkeypatch.setattr("ccbot.session.FAST_CODEX_ACK_TIMEOUT_SECONDS", 0.05)

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                return_value=(True, "Submitted text to @1")
            )

            first = await mgr.send_to_window_fast_unverified(
                "@1",
                "first",
                proof_id="proof-3",
                user_id=100,
                chat_id=-100,
                thread_id=42,
                surface_key="t:42",
            )
            second = await mgr.send_to_window_fast_unverified(
                "@1",
                "second",
                proof_id="proof-4",
                user_id=100,
                chat_id=-100,
                thread_id=42,
                surface_key="t:42",
            )

        assert first[0] is True
        assert second[0] is False
        assert "waiting for Codex replay ACK" in second[1]
        assert mock_driver.send_text.await_count == 1

    def test_fast_user_echo_match_allows_delivered_no_ack_delayed_replay(
        self,
        mgr: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = 1_000.0
        monkeypatch.setattr("ccbot.session.time.monotonic", lambda: now)
        proof = FastRuntimeInputProof(
            proof_id="delayed",
            user_id=100,
            chat_id=-100,
            thread_id=42,
            surface_key="t:42",
            window_id="@1",
            runtime_kind="codex",
            runtime_thread_id="thread-1",
            text_hash="",
            text_len=11,
            text_preview="hello world",
            rollout_file="/tmp/rollout.jsonl",
            start_byte=40,
            created_at_monotonic=now - 300.0,
            status=CODEX_DELIVERED_NO_ACK_STATUS,
        )
        proof.text_hash = _stable_text_hash("hello world")
        mgr.fast_input_proofs = {"delayed": proof}

        assert (
            mgr.match_fast_user_echo_proof(
                window_id="@1",
                thread_id=42,
                runtime_thread_id="thread-1",
                text="hello world",
                include_pending=True,
            )
            is proof
        )

    def test_fast_user_echo_match_requires_runtime_thread_recent_ack_and_pending_race(
        self,
        mgr: SessionManager,
        monkeypatch: pytest.MonkeyPatch,
    ):
        now = 1_000.0
        monkeypatch.setattr("ccbot.session.time.monotonic", lambda: now)
        stale = FastRuntimeInputProof(
            proof_id="old",
            user_id=100,
            chat_id=-100,
            thread_id=42,
            surface_key="t:42",
            window_id="@1",
            runtime_kind="codex",
            runtime_thread_id="thread-1",
            text_hash="",
            text_len=5,
            text_preview="hello",
            rollout_file="/tmp/rollout.jsonl",
            start_byte=0,
            created_at_monotonic=0.0,
            status="ack_confirmed",
            ack_confirmed_at_monotonic=now - 300.0,
        )
        current = FastRuntimeInputProof(
            proof_id="current",
            user_id=100,
            chat_id=-100,
            thread_id=42,
            surface_key="t:42",
            window_id="@1",
            runtime_kind="codex",
            runtime_thread_id="thread-1",
            text_hash="",
            text_len=5,
            text_preview="hello",
            rollout_file="/tmp/rollout.jsonl",
            start_byte=20,
            created_at_monotonic=now - 1.0,
            status="ack_confirmed",
            ack_confirmed_at_monotonic=now - 1.0,
        )
        pending = FastRuntimeInputProof(
            proof_id="pending",
            user_id=100,
            chat_id=-100,
            thread_id=42,
            surface_key="t:42",
            window_id="@1",
            runtime_kind="codex",
            runtime_thread_id="thread-1",
            text_hash="",
            text_len=5,
            text_preview="hello",
            rollout_file="/tmp/rollout.jsonl",
            start_byte=40,
            created_at_monotonic=now - 0.5,
            status="pending",
        )
        stale.text_hash = current.text_hash = (
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        )
        pending.text_hash = current.text_hash
        mgr.fast_input_proofs = {"old": stale, "current": current, "pending": pending}

        assert (
            mgr.match_fast_user_echo_proof(
                window_id="@1",
                thread_id=42,
                runtime_thread_id="other-thread",
                text="hello",
            )
            is None
        )
        assert (
            mgr.match_fast_user_echo_proof(
                window_id="@1",
                thread_id=42,
                runtime_thread_id="thread-1",
                text="hello",
                include_pending=True,
            )
            is None
        )
        mgr._fast_input_represented_proofs.add("current")
        assert (
            mgr.match_fast_user_echo_proof(
                window_id="@1",
                thread_id=42,
                runtime_thread_id="thread-1",
                text="hello",
                include_pending=True,
            )
            is pending
        )

    @pytest.mark.asyncio
    async def test_codex_turn_context_ack_requires_guard_flag(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text(json.dumps({"type": "turn_context"}) + "\n")

        assert not await mgr._codex_rollout_has_submit_ack(
            file_path=rollout,
            start_byte=0,
            expected_text="hello",
            allow_turn_context=True,
        )
        assert await mgr._codex_rollout_has_submit_ack(
            file_path=rollout,
            start_byte=0,
            expected_text="hello",
            allow_turn_context=True,
            turn_context_start_byte=0,
        )

    @pytest.mark.asyncio
    async def test_codex_submit_ignores_turn_context_before_submit_key(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text(json.dumps({"type": "turn_context"}) + "\n")
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.01)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.05)

        async def submit_and_append_user_message(*args, **kwargs):
            with rollout.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "hello"}],
                            },
                        }
                    )
                    + "\n"
                )
            return True, "Submitted text to @1"

        with patch("ccbot.session.runtime_input_driver") as mock_driver:
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_user_message
            )

            success, message = await mgr._submit_codex_text_with_rollout_ack(
                window_id="@1",
                file_path=rollout,
                start_byte=0,
                text="hello",
                allow_turn_context=True,
            )

        assert success is True
        assert message == "Sent text to @1"
        mock_driver.send_multiline_submit_key.assert_awaited_once_with(
            "@1",
            runtime_kind="codex",
        )

    @pytest.mark.asyncio
    async def test_codex_ack_after_first_submit_does_not_retry(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.01)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_MAX_ATTEMPTS", 3)

        async def submit_and_append_ack(*args, **kwargs):
            with rollout.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "turn_context"}) + "\n")
            return True, "Submitted text to @1"

        with patch("ccbot.session.runtime_input_driver") as mock_driver:
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_ack
            )

            success, message = await mgr._submit_codex_text_with_rollout_ack(
                window_id="@1",
                file_path=rollout,
                start_byte=0,
                text="hello",
                allow_turn_context=True,
            )

        assert success is True
        assert message == "Sent text to @1"
        mock_driver.send_multiline_submit_key.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_codex_mismatched_user_message_does_not_ack(
        self,
        mgr: SessionManager,
        tmp_path: Path,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "other"}],
                    },
                }
            )
            + "\n"
        )

        assert not await mgr._codex_rollout_has_submit_ack(
            file_path=rollout,
            start_byte=0,
            expected_text="hello",
            allow_turn_context=True,
        )

    def test_codex_ack_path_does_not_write_replay_evidence(self):
        source = "\n".join(
            (
                inspect.getsource(SessionManager._codex_rollout_has_submit_ack),
                inspect.getsource(SessionManager._submit_codex_text_with_rollout_ack),
                inspect.getsource(SessionManager.send_to_window),
            )
        )

        assert '.open("a"' not in source
        assert ".open('a'" not in source
        assert "write_text(" not in source

    @pytest.mark.asyncio
    async def test_same_window_codex_sends_are_serialized_by_ack_guard(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.01)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.1)
        first_submit_started = asyncio.Event()
        release_first_submit = asyncio.Event()
        submit_count = 0

        async def submit_and_append_ack(*args, **kwargs):
            nonlocal submit_count
            submit_count += 1
            if submit_count == 1:
                first_submit_started.set()
                await release_first_submit.wait()
                with rollout.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"type": "turn_context"}) + "\n")
            else:
                with rollout.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": "second"}
                                    ],
                                },
                            }
                        )
                        + "\n"
                    )
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_ack
            )

            first = asyncio.create_task(mgr.send_to_window("@1", "first"))
            await asyncio.wait_for(first_submit_started.wait(), timeout=1)
            second = asyncio.create_task(mgr.send_to_window("@1", "second"))
            await asyncio.sleep(0)

            assert mock_driver.send_text.await_count == 1

            release_first_submit.set()
            assert await first == (True, "Sent to @1")
            assert await second == (True, "Sent to @1")

        assert mock_driver.send_text.await_count == 2
        assert mock_driver.send_multiline_submit_key.await_count == 2

    @pytest.mark.asyncio
    async def test_send_to_window_fails_closed_when_codex_window_falls_back_to_shell(
        self, mgr: SessionManager
    ):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="bash"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value="user@host:/tmp/project$ ")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_raw_slash_command = AsyncMock()

            success, message = await mgr.send_to_window("@1", "ls")

        assert success is False
        assert "Codex live process is not active" in message
        mock_driver.send_text.assert_not_awaited()
        mock_driver.send_raw_slash_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_to_window_fails_closed_when_shell_uses_generic_prompt_glyph(
        self, mgr: SessionManager
    ):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(window_id="@1", pane_current_command="zsh")
            )
            mock_tmux.capture_pane = AsyncMock(return_value="❯ ")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_raw_slash_command = AsyncMock()

            success, message = await mgr.send_to_window("@1", "hello")

        assert success is False
        assert "Codex live process is not active" in message
        mock_driver.send_text.assert_not_awaited()
        mock_driver.send_raw_slash_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_to_window_keeps_codex_conversation_interrupted_writable_on_node_command(
        self,
        mgr: SessionManager,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        rollout = tmp_path / "thread-1.jsonl"
        rollout.write_text("", encoding="utf-8")
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )
        mgr.resolve_thread_for_window = AsyncMock(
            return_value=ThreadLocator(
                thread_id="thread-1",
                summary="Thread One",
                message_count=1,
                file_path=str(rollout),
                runtime_kind="codex",
                cwd="/tmp/project",
            )
        )
        monkeypatch.setattr(
            "ccbot.session.CODEX_MULTILINE_ACK_INITIAL_DELAY_SECONDS", 0
        )
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_RETRY_SECONDS", 0.01)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_POLL_SECONDS", 0.001)
        monkeypatch.setattr("ccbot.session.CODEX_MULTILINE_ACK_TIMEOUT_SECONDS", 0.05)
        pane_text = (
            "previous output\n"
            "■ Conversation interrupted - tell the model what to do differently.\n"
            "› Find and fix a bug in @filename\n"
            "  gpt-5.5 high · main · Context 29% left\n"
        )

        async def submit_and_append_ack(*args, **kwargs):
            with rollout.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "turn_context"}) + "\n")
            return True, "Submitted text to @1"

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(
                    window_id="@1", pane_current_command="node"
                )
            )
            mock_tmux.capture_pane = AsyncMock(return_value=pane_text)
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_multiline_submit_key = AsyncMock(
                side_effect=submit_and_append_ack
            )
            mock_driver.send_raw_slash_command = AsyncMock()

            success, message = await mgr.send_to_window("@1", "try again")

        assert success is True
        assert message == "Sent to @1"
        mock_driver.send_text.assert_awaited_once_with(
            "@1",
            "try again",
            runtime_kind="codex",
            submit=False,
        )
        mock_driver.send_raw_slash_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_to_window_fails_closed_on_blocked_prompt(
        self, mgr: SessionManager
    ):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(window_id="@1")
            )
            mock_tmux.capture_pane = AsyncMock(
                return_value="OpenAI Codex\n› ping\n■ Approval required\n"
            )

            success, message = await mgr.send_to_window("@1", "hello")

        assert success is False
        assert message == "Input blocked by a visible prompt in the terminal"
        mock_driver.send_text.assert_not_called()
        mock_driver.send_raw_slash_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_to_window_fails_closed_for_external_binding(
        self, mgr: SessionManager
    ):
        success, message = await mgr.send_to_window("external:codex:thread-1", "ping")
        assert success is False
        assert "read-only mode" in message

    @pytest.mark.asyncio
    async def test_send_special_key_uses_runtime_input_driver(
        self, mgr: SessionManager
    ):
        mgr.window_states["@1"] = LiveProcessDescriptor(
            thread_id="thread-1",
            cwd="/tmp/project",
            runtime_kind="codex",
        )

        with (
            patch("ccbot.session.tmux_manager") as mock_tmux,
            patch("ccbot.session.runtime_input_driver") as mock_driver,
        ):
            mock_tmux.find_window_by_id = AsyncMock(
                return_value=SimpleNamespace(window_id="@1")
            )
            mock_driver.send_special_key = AsyncMock(return_value=(True, "Sent Escape"))

            success, message = await mgr.send_special_key_to_window("@1", "Escape")

        assert success is True
        assert message == "Sent Escape"
        mock_driver.send_special_key.assert_awaited_once_with(
            "@1",
            "Escape",
            runtime_kind="codex",
        )


class TestTopicControlStateMachine:
    def test_defaults_allow_implicit_bind(self, mgr: SessionManager) -> None:
        assert mgr.get_topic_policy(100, 42) == TOPIC_POLICY_IMPLICIT_BIND_ALLOWED
        assert mgr.get_topic_binding_state(100, 42) == BINDING_STATE_NONE

    def test_bind_thread_marks_bound_without_touching_policy(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_topic_policy(100, 42, TOPIC_POLICY_MANUAL_BIND_REQUIRED)
        mgr.start_topic_bind_flow(100, 42)
        assert mgr.get_topic_binding_state(100, 42) == BINDING_STATE_BIND_FLOW
        version, nonce = mgr.get_topic_bind_flow_credentials(100, 42)
        assert mgr.validate_topic_bind_flow_callback(100, 42, version, nonce)

        mgr.bind_thread(100, 42, "@7", window_name="proj")

        assert mgr.get_window_for_thread(100, 42) == "@7"
        assert mgr.get_topic_binding_state(100, 42) == BINDING_STATE_BOUND
        assert mgr.get_topic_policy(100, 42) == TOPIC_POLICY_MANUAL_BIND_REQUIRED
        rotated_version, rotated_nonce = mgr.get_topic_bind_flow_credentials(100, 42)
        assert (rotated_version, rotated_nonce) != (version, nonce)

    def test_unbind_thread_preserves_policy_but_clears_binding_state(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_topic_policy(100, 42, TOPIC_POLICY_MANUAL_BIND_REQUIRED)
        mgr.bind_thread(100, 42, "@7", window_name="proj")

        assert mgr.unbind_thread(100, 42) == "@7"
        assert mgr.get_window_for_thread(100, 42) is None
        assert mgr.get_topic_binding_state(100, 42) == BINDING_STATE_NONE
        assert mgr.get_topic_policy(100, 42) == TOPIC_POLICY_MANUAL_BIND_REQUIRED

    def test_manual_and_implicit_policy_updates_are_independent(
        self, mgr: SessionManager
    ) -> None:
        mgr.require_manual_bind(100, 42)
        assert mgr.get_topic_policy(100, 42) == TOPIC_POLICY_MANUAL_BIND_REQUIRED
        assert mgr.get_topic_binding_state(100, 42) == BINDING_STATE_NONE

        mgr.allow_implicit_bind(100, 42)
        assert mgr.get_topic_policy(100, 42) == TOPIC_POLICY_IMPLICIT_BIND_ALLOWED
        assert mgr.get_topic_binding_state(100, 42) == BINDING_STATE_NONE

    def test_require_manual_bind_invalidates_old_bind_flow_credentials(
        self, mgr: SessionManager
    ) -> None:
        mgr.start_topic_bind_flow(100, 42)
        version, nonce = mgr.get_topic_bind_flow_credentials(100, 42)

        mgr.require_manual_bind(100, 42)

        assert not mgr.validate_topic_bind_flow_callback(100, 42, version, nonce)
        rotated_version, rotated_nonce = mgr.get_topic_bind_flow_credentials(100, 42)
        assert rotated_version > version
        assert rotated_nonce != nonce


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False
