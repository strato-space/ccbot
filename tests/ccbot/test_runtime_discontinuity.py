from pathlib import Path

from ccbot.runtime_discontinuity import (
    extract_codex_termination_summary_from_rollout,
    extract_terminal_tail_block,
    format_codex_termination_summary_for_telegram,
    is_codex_termination_summary_text,
)


def test_is_codex_termination_summary_text_detects_usage_summary() -> None:
    assert is_codex_termination_summary_text(
        "Token usage: total=12 input=10 output=2\n"
        "To continue this session, run codex resume comfy"
    )


def test_format_codex_termination_summary_wraps_resume_command() -> None:
    text = (
        "Token usage: total=12 input=10 output=2\n"
        "To continue this session, run codex resume comfy"
    )

    assert format_codex_termination_summary_for_telegram(text) == (
        "Token usage: total=12 input=10 output=2\n"
        "To continue this session, run `codex resume comfy`"
    )


def test_extract_codex_termination_summary_from_rollout_reads_tail(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-04-07T10:00:00Z","type":"event_msg","payload":{"type":"agent_message","phase":"assistant_message","message":"Token usage: total=12 input=10 output=2\\nTo continue this session, run codex resume comfy"}}',
                '{"timestamp":"2026-04-07T10:00:01Z","type":"event_msg","payload":{"type":"turn_completed","turn_id":"thread-1"}}',
            ]
        ),
        encoding="utf-8",
    )

    assert extract_codex_termination_summary_from_rollout(
        rollout,
        thread_id="thread-1",
    ) == (
        "Token usage: total=12 input=10 output=2\n"
        "To continue this session, run `codex resume comfy`"
    )


def test_extract_terminal_tail_block_drops_shell_prompt() -> None:
    pane_text = (
        "work finished\n\n"
        "Token usage: total=12 input=10 output=2\n"
        "To continue this session, run codex resume comfy\n\n"
        "root@host:/repo#\n"
    )

    assert extract_terminal_tail_block(pane_text) == (
        "Token usage: total=12 input=10 output=2\n"
        "To continue this session, run `codex resume comfy`"
    )
