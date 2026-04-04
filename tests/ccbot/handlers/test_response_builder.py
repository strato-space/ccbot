"""Tests for response_builder.build_response_parts."""

import json

import pytest

from ccbot.handlers.response_builder import build_response_parts, format_response_text
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestBuildResponseParts:
    def test_user_message_has_emoji_prefix(self):
        parts = build_response_parts("hello", is_complete=True, role="user")
        assert len(parts) == 1
        assert "\U0001f464" in parts[0]

    def test_user_message_truncated_at_3000_chars(self):
        long_text = "a" * 4000
        parts = build_response_parts(long_text, is_complete=True, role="user")
        assert len(parts) == 1
        short_parts = build_response_parts("b" * 100, is_complete=True, role="user")
        assert len(parts[0]) < len(long_text)
        assert len(short_parts[0]) < len(parts[0])

    def test_thinking_content_truncated_at_500_chars(self):
        inner = "x" * 800
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, is_complete=True, content_type="thinking")
        assert len(parts) == 1
        assert "truncated" in parts[0].lower()

    def test_plain_text_single_part(self):
        parts = build_response_parts("short text", is_complete=True)
        assert len(parts) == 1

    def test_plain_text_multi_part_has_page_suffix(self):
        long_text = "\n".join(f"line {i} " + "padding" * 50 for i in range(200))
        parts = build_response_parts(long_text, is_complete=True)
        assert len(parts) > 1
        assert "1/" in parts[0]

    def test_expandable_quote_stays_atomic(self):
        inner = "thought " * 100
        text = f"{EXP_START}{inner}{EXP_END}"
        parts = build_response_parts(text, is_complete=False, content_type="thinking")
        assert len(parts) == 1

    def test_thinking_has_prefix(self):
        parts = build_response_parts(
            "some thought", is_complete=True, content_type="thinking"
        )
        assert len(parts) == 1
        assert "Thinking" in parts[0]

    def test_assistant_text_no_prefix(self):
        parts = build_response_parts(
            "hello world", is_complete=True, content_type="text", role="assistant"
        )
        assert len(parts) == 1
        assert "\U0001f464" not in parts[0]
        assert "Thinking" not in parts[0]

    @pytest.mark.parametrize(
        ("content_type", "expected_prefix"),
        [
            ("commentary", "Commentary"),
            ("reasoning", "Reasoning"),
            ("command_execution", "Command"),
            ("tool_use", "Tool"),
            ("tool_progress", "Tool Progress"),
            ("tool_result", "Tool Output"),
            ("file_change", "Files"),
        ],
    )
    def test_specialized_content_types_get_prefixes(
        self, content_type: str, expected_prefix: str
    ):
        parts = build_response_parts(
            "payload",
            is_complete=True,
            content_type=content_type,
            role="assistant",
        )
        assert len(parts) == 1
        assert expected_prefix in parts[0]

    def test_history_format_preserves_reasoning_without_truncation(self):
        formatted = format_response_text(
            "x" * 800,
            is_complete=True,
            content_type="reasoning",
            role="assistant",
            for_history=True,
        )
        assert "Reasoning" in formatted
        assert "truncated" not in formatted.lower()
        assert len(formatted) > 800

    def test_tool_use_function_call_json_renders_as_json_block(self):
        formatted = format_response_text(
            'write_stdin({"session_id": 2041, "chars": "ok go\\n"})',
            is_complete=True,
            content_type="tool_use",
            role="assistant",
        )

        assert "Tool" in formatted
        assert "write_stdin" in formatted
        assert "```json" in formatted
        assert '"session_id": 2041' in formatted

    def test_tool_result_json_renders_as_json_block(self):
        formatted = format_response_text(
            '{"status":"ok","count":2}',
            is_complete=True,
            content_type="tool_result",
            role="assistant",
        )

        assert "Tool Output" in formatted
        assert "```json" in formatted
        assert '"count": 2' in formatted

    def test_tool_result_json_truncation_footer_stays_outside_code_block(self):
        formatted = format_response_text(
            json.dumps({"items": list(range(40))}),
            is_complete=True,
            content_type="tool_result",
            role="assistant",
        )

        assert "```json" in formatted
        assert "\n```\n\npreview " in formatted

    def test_file_change_multiline_renders_as_shell_block(self):
        formatted = format_response_text(
            "applied\nmodified src/ccbot/bot.py\nadded tests/ccbot/test_bot_contracts.py",
            is_complete=True,
            content_type="file_change",
            role="assistant",
        )

        assert "Files" in formatted
        assert "```sh" in formatted
        assert "modified src/ccbot/bot.py" in formatted

    def test_orchestration_content_keeps_codex_style_text_without_prefix(self):
        formatted = format_response_text(
            "• Spawned Mill [explorer] (gpt-5.4 medium)\n  └ Review this implementation plan",
            is_complete=True,
            content_type="orchestration",
            role="assistant",
        )

        assert formatted.startswith("• Spawned Mill [explorer]")
        assert "Commentary" not in formatted
        assert "Tool" not in formatted
