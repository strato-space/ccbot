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
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


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

    def test_clear_window_session(self, mgr: SessionManager) -> None:
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
        assert messages[1]["text"] == "done"


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

        mgr.bind_thread(100, 42, "@7", window_name="proj")

        assert mgr.get_window_for_thread(100, 42) == "@7"
        assert mgr.get_topic_binding_state(100, 42) == BINDING_STATE_BOUND
        assert mgr.get_topic_policy(100, 42) == TOPIC_POLICY_MANUAL_BIND_REQUIRED

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
