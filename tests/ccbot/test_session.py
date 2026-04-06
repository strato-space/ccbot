"""Tests for SessionManager pure dict operations."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.codex_threads import CodexThreadCatalog
from ccbot.session import SessionManager
from ccbot.runtime_types import LiveProcessDescriptor
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

    def test_get_topic_binding_preserves_runtime_kind(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        mgr.get_window_state("@1").runtime_kind = "codex"
        binding = mgr.get_topic_binding(100, 1)
        assert binding is not None
        assert binding.runtime_kind == "codex"

    def test_iter_topic_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="one")
        mgr.bind_thread(100, 2, "@2", window_name="two")
        result = {(b.user_id, b.thread_id, b.window_id) for b in mgr.iter_topic_bindings()}
        assert result == {(100, 1, "@1"), (100, 2, "@2")}

    def test_iter_topic_bindings_preserves_runtime_kind(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="one")
        mgr.get_window_state("@1").runtime_kind = "codex"
        binding = next(mgr.iter_topic_bindings())
        assert binding.runtime_kind == "codex"

    def test_bind_external_thread_exposes_external_topic_binding(self, mgr: SessionManager) -> None:
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
        assert mgr.get_window_for_surface(100, surface_key="c:-100123") == binding_window_id
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
        assert mgr.peek_surface_pending_slot(100, surface_key="t:42") == second

        consumed = mgr.consume_surface_pending_slot(
            100,
            "activation-1",
            surface_key="t:42",
        )
        assert consumed is not None
        assert consumed["status"] == "consumed"
        assert consumed["consumed_by_activation_id"] == "activation-1"
        assert mgr.consume_surface_pending_slot(
            100,
            "activation-1",
            surface_key="t:42",
        ) is None
        assert mgr.consume_surface_pending_slot(
            100,
            "activation-2",
            surface_key="t:42",
        ) is None

    def test_clear_surface_pending_slot_returns_previous_record(
        self, mgr: SessionManager
    ) -> None:
        mgr.set_surface_pending_slot(100, "queued", surface_key="c:-100123")

        cleared = mgr.clear_surface_pending_slot(100, surface_key="c:-100123")

        assert cleared is not None
        assert cleared["text"] == "queued"
        assert mgr.peek_surface_pending_slot(100, surface_key="c:-100123") is None


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

    def test_surface_policy_and_binding_state_for_chat(self, mgr: SessionManager) -> None:
        mgr.require_manual_bind_for_surface(100, chat_id=-100200300)
        assert mgr.get_surface_policy(100, chat_id=-100200300) == TOPIC_POLICY_MANUAL_BIND_REQUIRED
        assert mgr.get_surface_binding_state(100, chat_id=-100200300) == BINDING_STATE_NONE

        mgr.start_surface_bind_flow(100, chat_id=-100200300)
        assert mgr.get_surface_binding_state(100, chat_id=-100200300) == BINDING_STATE_BIND_FLOW
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
        assert mgr.peek_surface_pending_slot(100, chat_id=-100200300)["text"] == "updated"

        consumed = mgr.consume_surface_pending_slot(
            100,
            "activation-1",
            chat_id=-100200300,
        )
        assert consumed is not None
        assert consumed["status"] == "consumed"
        assert consumed["consumed_by_activation_id"] == "activation-1"
        assert mgr.consume_surface_pending_slot(
            100,
            "activation-2",
            chat_id=-100200300,
        ) is None

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
                return_value=SimpleNamespace(window_id="@1")
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
    async def test_send_to_window_uses_text_path_for_regular_messages(
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
            mock_tmux.capture_pane = AsyncMock(return_value="")
            mock_driver.send_text = AsyncMock(return_value=(True, "Sent text to @1"))
            mock_driver.send_raw_slash_command = AsyncMock()

            success, message = await mgr.send_to_window("@1", "hello")

        assert success is True
        assert message == "Sent to @1"
        mock_driver.send_text.assert_awaited_once_with(
            "@1",
            "hello",
            runtime_kind="codex",
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
    async def test_send_to_window_fails_closed_for_external_binding(self, mgr: SessionManager):
        success, message = await mgr.send_to_window("external:codex:thread-1", "ping")
        assert success is False
        assert "read-only mode" in message

    @pytest.mark.asyncio
    async def test_send_special_key_uses_runtime_input_driver(self, mgr: SessionManager):
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
            mock_driver.send_special_key = AsyncMock(
                return_value=(True, "Sent Escape")
            )

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

    def test_bind_thread_marks_bound_without_touching_policy(self, mgr: SessionManager) -> None:
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
