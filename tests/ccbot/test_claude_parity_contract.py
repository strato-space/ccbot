"""Executable Claude parity baseline derived from upstream ccbot."""

from __future__ import annotations

import json
from pathlib import Path

from ccbot.transcript_parser import TranscriptParser
from ccbot.terminal_parser import classify_input_surface


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "claude"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_claude_parity_contract_note_is_source_backed() -> None:
    note = (REPO_ROOT / "doc" / "claude-parity-contract.md").read_text(
        encoding="utf-8"
    )

    assert "transcript_parser.py" in note
    assert "message_queue.py" in note
    assert "interactive_ui.py" in note
    assert "status_polling.py" in note
    assert "AskUserQuestion" in note
    assert "tool_use_id" in note


def test_claude_parity_contract_documents_upstream_delivery_baseline() -> None:
    contract = _load_json(FIXTURE_ROOT / "parity_contract.json")

    assert contract["telegram_delivery_categories"] == [
        "text",
        "thinking",
        "tool_use",
        "tool_result",
        "local_command",
    ]
    assert contract["tool_use_result_contract"]["pairing_key"] == "tool_use_id"
    assert "status updates" in contract["final_result_delivery"]["ordering"]
    assert contract["interactive_gate"]["blocked_prompts"] == [
        "AskUserQuestion",
        "ExitPlanMode",
        "PermissionPrompt",
        "RestoreCheckpoint",
    ]


def test_claude_parity_transcript_sample_pins_parsed_categories() -> None:
    entries, remaining = TranscriptParser.parse_entries(
        _load_jsonl(FIXTURE_ROOT / "parity_transcript.jsonl")
    )

    assert remaining == {}
    assert [entry.content_type for entry in entries] == [
        "thinking",
        "text",
        "tool_use",
        "tool_result",
        "local_command",
        "text",
    ]
    assert entries[2].tool_use_id == "toolu_1"
    assert entries[3].tool_use_id == "toolu_1"
    assert "**Read**(src/app.py)" in entries[2].text
    assert "Read 2 lines" in entries[3].text
    assert entries[-1].text == "Final answer for the topic chat"


def test_claude_prompt_samples_gate_normal_message_delivery() -> None:
    contract = _load_json(FIXTURE_ROOT / "parity_contract.json")

    for sample in contract["prompt_samples"]:
        surface = classify_input_surface(sample["pane_text"])

        assert surface.kind == sample["expected"]["kind"]
        assert surface.prompt_name == sample["expected"]["prompt_name"]
        assert surface.has_visible_prompt is True
        assert surface.has_interactive_ui is True
