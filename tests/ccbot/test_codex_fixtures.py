"""Sanity checks for the curated Codex fixture corpus used by adaptation tasks."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _load_json(path: Path):
    return json.loads(path.read_text())


def _load_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_manifest_files_exist_and_cover_required_cases() -> None:
    manifest = _load_json(FIXTURE_ROOT / "manifest.json")

    required = {
        "fresh-thread",
        "resumed-thread",
        "same-cwd-multiple-threads",
        "stale-index-entry",
        "missing-rollout-file",
        "interrupted-turn",
        "reasoning",
        "command-execution",
        "tool-call-output",
        "prompt-snapshot",
    }
    assert required.issubset(set(manifest["coverage"]))

    for relpath in manifest["files"].values():
        assert (FIXTURE_ROOT / relpath).exists(), relpath


def test_thread_metadata_preserves_root_and_non_root_path_shapes() -> None:
    metadata = _load_json(FIXTURE_ROOT / "thread_metadata.json")
    threads = metadata["threads"]

    cwds = {thread["cwd"] for thread in threads}
    assert "/root" in cwds
    assert "/home" in cwds
    assert "/home/strato-space" in cwds

    same_cwd_groups = metadata["same_cwd_groups"]
    assert any(group["cwd"] == "/home" and len(group["session_ids"]) >= 3 for group in same_cwd_groups)
    assert any(
        group["cwd"] == "/home/strato-space" and len(group["session_ids"]) >= 3
        for group in same_cwd_groups
    )


def test_session_index_rows_include_stale_entry() -> None:
    rows = _load_json(FIXTURE_ROOT / "session_index_rows.json")["rows"]
    stale = next(row for row in rows if row["fixture_id"] == "stale-index-entry")
    assert stale["row"]["id"].startswith("019dffff-")


def test_missing_rollout_reference_uses_non_root_shaped_path() -> None:
    state = _load_json(FIXTURE_ROOT / "monitor_state_missing_rollout.json")
    missing = state["tracked_sessions"]["stale-nonroot-rollout"]
    assert missing["file_path"].startswith("/home/service-user/.codex/")


def test_rollout_excerpts_cover_reasoning_interrupts_and_tool_calls() -> None:
    reasoning = _load_jsonl(FIXTURE_ROOT / "rollouts" / "nonroot_reasoning_turn.jsonl")
    interrupted = _load_jsonl(FIXTURE_ROOT / "rollouts" / "interrupted_turn_nonroot.jsonl")
    tooling = _load_jsonl(FIXTURE_ROOT / "rollouts" / "root_tool_call_and_output.jsonl")
    resumed = _load_jsonl(FIXTURE_ROOT / "rollouts" / "resumed_home_thread.jsonl")

    assert any(item["type"] == "response_item" and item["payload"]["type"] == "reasoning" for item in reasoning)
    assert any(item["type"] == "event_msg" and item["payload"]["type"] == "turn_aborted" for item in interrupted)
    assert any(item["type"] == "response_item" and item["payload"]["type"] == "function_call" for item in tooling)
    assert any(
        item["type"] == "response_item" and item["payload"]["type"] == "function_call_output"
        for item in tooling
    )
    assert any(item["type"] == "session_meta" and item["payload"].get("forked_from_id") for item in resumed)


def test_prompt_snapshot_contains_resume_and_input_ready_prompt() -> None:
    prompt = _load_json(FIXTURE_ROOT / "panes" / "tmux_session_0_resume_prompt.json")

    assert prompt["source"]["tmux_session"] == "0"
    assert "codex resume" in prompt["ansi_text"]
    assert any("Explain this codebase" in line for line in prompt["visible_text"])
