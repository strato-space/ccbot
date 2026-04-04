"""Tests for terminal_parser — terminal surface detection and UI parsing."""

import json
from pathlib import Path

import pytest

from ccbot.terminal_parser import (
    classify_input_surface,
    extract_pending_input_preview,
    extract_bash_output,
    extract_interactive_content,
    is_interactive_ui,
    parse_status_line,
    strip_pane_chrome,
)

# ── parse_status_line ────────────────────────────────────────────────────


class TestParseStatusLine:
    @pytest.mark.parametrize(
        ("spinner", "rest", "expected"),
        [
            ("·", "Working on task", "Working on task"),
            ("✻", "  Reading file  ", "Reading file"),
            ("✽", "Thinking deeply", "Thinking deeply"),
            ("✶", "Analyzing code", "Analyzing code"),
            ("✳", "Processing input", "Processing input"),
            ("✢", "Building project", "Building project"),
        ],
    )
    def test_spinner_chars(self, spinner: str, rest: str, expected: str, chrome: str):
        pane = f"some output\n{spinner}{rest}\n{chrome}"
        assert parse_status_line(pane) == expected

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("just normal text\nno spinners here\n", id="no_spinner"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert parse_status_line(pane) is None

    def test_no_chrome_returns_none(self):
        """Without chrome separator, status can't be determined."""
        pane = "output\n✻ Doing work\nno chrome here\n"
        assert parse_status_line(pane) is None

    def test_blank_line_between_status_and_chrome(self, chrome: str):
        """Status line with blank lines before separator."""
        pane = f"output\n✻ Doing work\n\n{chrome}"
        assert parse_status_line(pane) == "Doing work"

    def test_idle_no_status(self, chrome: str):
        """Idle pane (no status line above chrome) returns None."""
        pane = f"some output\n● Tool result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_false_positive_bullet(self, chrome: str):
        """· in regular output must NOT be detected as status."""
        pane = f"· bullet point one\n· bullet point two\nsome result\n{chrome}"
        assert parse_status_line(pane) is None

    def test_uses_fixture(self, sample_pane_status_line: str):
        assert parse_status_line(sample_pane_status_line) == "Reading file src/main.py"


# ── extract_interactive_content ──────────────────────────────────────────


class TestExtractInteractiveContent:
    def test_exit_plan_mode(self, sample_pane_exit_plan: str):
        result = extract_interactive_content(sample_pane_exit_plan)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Would you like to proceed?" in result.content
        assert "ctrl-g to edit in" in result.content

    def test_exit_plan_mode_variant(self):
        pane = (
            "  Claude has written up a plan\n  ─────\n  Details here\n  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ExitPlanMode"
        assert "Claude has written up a plan" in result.content

    def test_ask_user_multi_tab(self, sample_pane_ask_user_multi_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_multi_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "←" in result.content

    def test_ask_user_single_tab(self, sample_pane_ask_user_single_tab: str):
        result = extract_interactive_content(sample_pane_ask_user_single_tab)
        assert result is not None
        assert result.name == "AskUserQuestion"
        assert "Enter to select" in result.content

    def test_permission_prompt(self, sample_pane_permission: str):
        result = extract_interactive_content(sample_pane_permission)
        assert result is not None
        assert result.name == "PermissionPrompt"
        assert "Do you want to proceed?" in result.content

    def test_restore_checkpoint(self):
        pane = (
            "  Restore the code to a previous state?\n"
            "  ─────\n"
            "  Some details\n"
            "  Enter to continue\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "RestoreCheckpoint"
        assert "Restore the code" in result.content

    def test_settings(self):
        pane = "  Settings: press tab to cycle\n  ─────\n  Option 1\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Settings:" in result.content

    def test_settings_model_picker(self, sample_pane_settings: str):
        result = extract_interactive_content(sample_pane_settings)
        assert result is not None
        assert result.name == "Settings"
        assert "Select model" in result.content
        assert "Sonnet" in result.content
        assert "Enter to confirm" in result.content

    def test_settings_esc_to_cancel_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● claude-sonnet-4-20250514\n"
            "  ○ claude-opus-4-20250514\n"
            "  Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Esc to cancel" in result.content

    def test_settings_esc_to_exit_bottom(self):
        pane = (
            "  Settings: press tab to cycle\n"
            "  ─────\n"
            "  Model\n"
            "  ─────\n"
            "  ● Default (Opus 4.6)\n"
            "  ○ claude-sonnet-4-20250514\n"
            "\n"
            "  Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
        assert "Enter to confirm" in result.content

    @pytest.mark.parametrize(
        "pane",
        [
            pytest.param("$ echo hello\nhello\n$\n", id="no_ui"),
            pytest.param("", id="empty"),
        ],
    )
    def test_returns_none(self, pane: str):
        assert extract_interactive_content(pane) is None

    def test_min_gap_too_small_returns_none(self):
        pane = "  Do you want to proceed?\n  Esc to cancel\n"
        assert extract_interactive_content(pane) is None


# ── is_interactive_ui ────────────────────────────────────────────────────


class TestIsInteractiveUI:
    def test_true_when_ui_present(self, sample_pane_exit_plan: str):
        assert is_interactive_ui(sample_pane_exit_plan) is True

    def test_false_when_no_ui(self, sample_pane_no_ui: str):
        assert is_interactive_ui(sample_pane_no_ui) is False

    def test_settings_is_interactive(self, sample_pane_settings: str):
        assert is_interactive_ui(sample_pane_settings) is True

    def test_false_for_empty_string(self):
        assert is_interactive_ui("") is False


# ── classify_input_surface ──────────────────────────────────────────────


class TestClassifyInputSurface:
    def test_detects_codex_trust_prompt(self):
        pane = (
            "  > You are in /tmp/example\n"
            "\n"
            "  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt injection.\n"
            "\n"
            "› 1. Yes, continue\n"
            "  2. No, quit\n"
            "\n"
            "  Press enter to continue\n"
        )

        surface = classify_input_surface(pane)

        assert surface.kind == "blocked_prompt"
        assert surface.prompt_name == "CodexTrustPrompt"
        assert surface.allows_remote_actions is True

    def test_detects_codex_exec_approval_prompt(self):
        pane = (
            "\n"
            "  Would you like to run the following command?\n"
            "\n"
            "  Reason: because the model asked to do it\n"
            "\n"
            "  $ echo hello world\n"
            "\n"
            "› 1. Yes, proceed (y)\n"
            "  2. Yes, and don't ask again for commands that start with `echo hello world` (p)\n"
            "  3. No, and tell Codex what to do differently (esc)\n"
            "\n"
            "  Press enter to confirm or esc to cancel\n"
        )

        surface = classify_input_surface(pane)

        assert surface.kind == "blocked_prompt"
        assert surface.prompt_name == "CodexExecApproval"
        assert surface.allows_remote_actions is True

    def test_detects_codex_model_picker(self):
        pane = (
            "  Select Model and Effort\n"
            "  Access legacy models by running codex -m <model_name> or in your config.toml\n"
            "\n"
            "› 1. gpt-5.3-codex (default)  Latest frontier agentic coding model.\n"
            "  2. gpt-5.4                  Latest frontier agentic coding model.\n"
            "\n"
            "  Press enter to select reasoning effort, or esc to dismiss.\n"
        )

        surface = classify_input_surface(pane)

        assert surface.kind == "blocked_prompt"
        assert surface.prompt_name == "CodexModelPicker"
        assert surface.allows_remote_actions is True

    def test_detects_interactive_ui(self, sample_pane_settings: str):
        surface = classify_input_surface(sample_pane_settings)

        assert surface.has_interactive_ui is True
        assert surface.has_visible_prompt is True
        assert surface.kind == "blocked_prompt"
        assert surface.prompt_name == "Settings"
        assert surface.allows_remote_actions is False

    def test_detects_status_surface(self, sample_pane_status_line: str):
        surface = classify_input_surface(sample_pane_status_line)

        assert surface.kind == "busy"
        assert surface.status_line == "Reading file src/main.py"

    def test_detects_prompt_like_surface(self):
        pane = "root@p2:/home/strato-space# codex resume\n› ping\n"

        surface = classify_input_surface(pane)

        assert surface.kind == "input_ready"
        assert surface.has_visible_prompt is True

    def test_detects_visible_prompt_error_as_blocked_prompt(self):
        pane = (
            "OpenAI Codex\n"
            "› ping\n"
            "■ You've hit your usage limit. Try again later.\n"
            "› Explain this codebase\n"
        )

        surface = classify_input_surface(pane)

        assert surface.kind == "blocked_prompt"
        assert surface.has_visible_prompt is True
        assert surface.prompt_name == "VisiblePromptError"

    def test_detects_blocked_prompt_from_real_codex_fixture(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "panes" / "tmux_session_0_resume_prompt.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        pane = "\n".join(payload["visible_text"])

        surface = classify_input_surface(pane)

        assert surface.kind == "blocked_prompt"
        assert surface.has_visible_prompt is True
        assert surface.prompt_name == "VisiblePromptError"

    def test_unknown_for_empty_string(self):
        surface = classify_input_surface("")

        assert surface.kind == "unknown"
        assert surface.has_visible_prompt is False


class TestExtractPendingInputPreview:
    def test_extracts_queued_follow_up_messages(self):
        pane = (
            "some output\n"
            "Queued follow-up messages\n"
            "◻ update docs\n"
            "◻ continue infra\n"
            "shift+← edit last queued message\n"
            "──────────────────────────────\n"
            "❯\n"
        )

        preview = extract_pending_input_preview(pane)

        assert preview.queued_messages == ("update docs", "continue infra")
        assert preview.edit_hint == "shift+← edit last queued message"

    def test_returns_empty_when_no_pending_input_preview_exists(self):
        preview = extract_pending_input_preview("Working (2m 03s • esc to interrupt)")

        assert preview.is_empty is True

    def test_preserves_literal_message_text_inside_pending_section(self):
        pane = (
            "Queued follow-up messages\n"
            "> quote this\n"
            "Waiting for deploy confirmation\n"
            "/review\n"
            "# heading\n"
            "$ echo hi\n"
            "shift+← edit last queued message\n"
            "──────────────────────────────\n"
            "❯\n"
        )

        preview = extract_pending_input_preview(pane)

        assert preview.queued_messages == (
            "> quote this",
            "Waiting for deploy confirmation",
            "/review",
            "# heading",
            "$ echo hi",
        )

    def test_strips_only_known_codex_checkbox_markers(self):
        pane = (
            "Queued follow-up messages\n"
            "☐ update docs\n"
            "↳ continue infra\n"
            "• run smoke\n"
            "shift+← edit last queued message\n"
            "──────────────────────────────\n"
            "❯\n"
        )

        preview = extract_pending_input_preview(pane)

        assert preview.queued_messages == (
            "update docs",
            "continue infra",
            "run smoke",
        )

    def test_extracts_codex_pending_and_rejected_steers_sections(self):
        pane = (
            "Messages to be submitted after next tool call\n"
            "• continue infra\n"
            "• git commit push\n"
            "Messages to be submitted at end of turn\n"
            "• send executive summary\n"
            "Queued follow-up messages\n"
            "◻ review rollout\n"
            "shift+← edit last queued message\n"
            "──────────────────────────────\n"
            "❯\n"
        )

        preview = extract_pending_input_preview(pane)

        assert preview.pending_steers == (
            "continue infra",
            "git commit push",
        )
        assert preview.rejected_steers == ("send executive summary",)
        assert preview.queued_messages == ("review rollout",)
        assert preview.edit_hint == "shift+← edit last queued message"

    def test_prefers_last_pending_header_block_when_multiple_are_visible(self):
        pane = (
            "Queued follow-up messages\n"
            "◻ stale old item\n"
            "some unrelated transcript text\n"
            "Messages to be submitted after next tool call\n"
            "• continue infra\n"
            "Queued follow-up messages\n"
            "◻ fresh item\n"
            "shift+← edit last queued message\n"
            "──────────────────────────────\n"
            "❯\n"
        )

        preview = extract_pending_input_preview(pane)

        assert preview.pending_steers == ("continue infra",)
        assert preview.queued_messages == ("fresh item",)


# ── strip_pane_chrome ───────────────────────────────────────────────────


class TestStripPaneChrome:
    def test_strips_from_separator(self):
        lines = [
            "some output",
            "more output",
            "─" * 30,
            "❯",
            "─" * 30,
            "  [Opus 4.6] Context: 34%",
        ]
        assert strip_pane_chrome(lines) == ["some output", "more output"]

    def test_no_separator_returns_all(self):
        lines = ["line 1", "line 2", "line 3"]
        assert strip_pane_chrome(lines) == lines

    def test_short_separator_not_triggered(self):
        lines = ["output", "─" * 10, "more output"]
        assert strip_pane_chrome(lines) == lines

    def test_only_searches_last_10_lines(self):
        # Separator at line 0 with 15 lines total — outside the last-10 window
        lines = ["─" * 30] + [f"line {i}" for i in range(14)]
        assert strip_pane_chrome(lines) == lines


# ── extract_bash_output ─────────────────────────────────────────────────


class TestExtractBashOutput:
    def test_extracts_command_output(self):
        pane = "some context\n! echo hello\n⎿ hello\n"
        result = extract_bash_output(pane, "echo hello")
        assert result is not None
        assert "! echo hello" in result
        assert "hello" in result

    def test_command_not_found_returns_none(self):
        pane = "some context\njust normal output\n"
        assert extract_bash_output(pane, "echo hello") is None

    def test_chrome_stripped(self):
        pane = (
            "some context\n"
            "! ls\n"
            "⎿ file.txt\n"
            + "─" * 30
            + "\n"
            + "❯\n"
            + "─" * 30
            + "\n"
            + "  [Opus 4.6] Context: 34%\n"
        )
        result = extract_bash_output(pane, "ls")
        assert result is not None
        assert "file.txt" in result
        assert "Opus" not in result

    def test_prefix_match_long_command(self):
        pane = "! long_comma…\n⎿ output\n"
        result = extract_bash_output(pane, "long_command_that_gets_truncated")
        assert result is not None
        assert "output" in result

    def test_trailing_blank_lines_stripped(self):
        pane = "! echo hi\n⎿ hi\n\n\n"
        result = extract_bash_output(pane, "echo hi")
        assert result is not None
        assert not result.endswith("\n")
