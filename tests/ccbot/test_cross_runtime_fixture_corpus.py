"""Cross-runtime fixture corpus coverage checks."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "cross_runtime"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _iter_relative_paths(node):
    if isinstance(node, str):
        yield node
        return
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_relative_paths(value)
        return
    if isinstance(node, list):
        for value in node:
            yield from _iter_relative_paths(value)


def _assert_manifest_is_complete(manifest_path: Path) -> dict:
    manifest = _load_json(manifest_path)

    assert set(manifest["coverage"]) == {
        "live_semantic_stream",
        "persisted_replay_evidence",
        "terminal_surface_observation",
    }

    for relpath in _iter_relative_paths(manifest["files"]):
        assert (manifest_path.parent / relpath).exists(), relpath

    return manifest


def test_root_manifest_lists_all_runtime_corpora() -> None:
    root_manifest = _load_json(FIXTURE_ROOT / "manifest.json")

    assert set(root_manifest["coverage"]) == {
        "live_semantic_stream",
        "persisted_replay_evidence",
        "terminal_surface_observation",
    }
    assert set(root_manifest["runtimes"]) == {"claude", "codex", "fast-agent"}

    for relpath in root_manifest["runtimes"].values():
        assert (FIXTURE_ROOT / relpath).exists(), relpath


def test_each_runtime_manifest_has_the_required_file_graph() -> None:
    for runtime in ("claude", "codex", "fast-agent"):
        manifest = _assert_manifest_is_complete(FIXTURE_ROOT / runtime / "manifest.json")
        assert manifest["runtime_kind"] == runtime


def test_claude_corpus_is_consumable_without_live_session_state() -> None:
    manifest = _assert_manifest_is_complete(FIXTURE_ROOT / "claude" / "manifest.json")
    live_stream = _load_jsonl(FIXTURE_ROOT / "claude" / manifest["files"]["live_semantic_stream"])
    progress = _load_jsonl(
        FIXTURE_ROOT
        / "claude"
        / manifest["files"]["persisted_replay_evidence"]["progress_status_stream"]
    )
    tool_transitions = _load_jsonl(
        FIXTURE_ROOT
        / "claude"
        / manifest["files"]["persisted_replay_evidence"]["tool_transitions"]
    )
    blocked = _load_json(
        FIXTURE_ROOT / "claude" / manifest["files"]["terminal_surface_observation"]["blocked_input"]
    )
    prompt_visible = _load_json(
        FIXTURE_ROOT
        / "claude"
        / manifest["files"]["terminal_surface_observation"]["interactive_prompt_visible"]
    )

    assert any(
        item["type"] == "assistant"
        and any(block["type"] == "tool_use" for block in item["message"]["content"])
        for item in live_stream
    )
    assert any(item["kind"] == "status" for item in progress)
    assert any(item["phase"] == "tool_result" for item in tool_transitions)
    assert blocked["should_persist"] is False
    assert prompt_visible["should_persist"] is False


def test_codex_corpus_reuses_existing_fixtures_and_classifies_prompt_surfaces() -> None:
    manifest = _assert_manifest_is_complete(FIXTURE_ROOT / "codex" / "manifest.json")
    live_stream = _load_jsonl(FIXTURE_ROOT / "codex" / manifest["files"]["live_semantic_stream"])
    launch = _load_json(
        FIXTURE_ROOT
        / "codex"
        / manifest["files"]["persisted_replay_evidence"]["launch_metadata"]
    )
    interrupted = _load_jsonl(
        FIXTURE_ROOT / "codex" / manifest["files"]["persisted_replay_evidence"]["degraded_failure_case"]
    )
    blocked = _load_json(
        FIXTURE_ROOT / "codex" / manifest["files"]["terminal_surface_observation"]["blocked_input"]
    )
    prompt_visible = _load_json(
        FIXTURE_ROOT
        / "codex"
        / manifest["files"]["terminal_surface_observation"]["interactive_prompt_visible"]
    )

    assert any(item["type"] == "response_item" for item in live_stream)
    assert launch["thread_id"] == "019d4e4b-7fac-77f3-b559-cb8e9b4c39a9"
    assert launch["rollout_file"].endswith("fresh_home_thread.jsonl")
    assert any(item.get("payload", {}).get("type") == "turn_aborted" for item in interrupted)
    assert blocked["prompt_name"] == "UsageLimitBlockedInput"
    assert blocked["should_persist"] is False
    assert "usage limit" in blocked["prompt_text"].lower()
    assert prompt_visible["prompt_name"] == "ResumePromptVisible"
    assert prompt_visible["should_persist"] is False
    assert "Explain this codebase" in prompt_visible["prompt_text"]
    assert blocked != prompt_visible


def test_fast_agent_corpus_covers_session_resume_progress_and_fail_closed_states() -> None:
    manifest = _assert_manifest_is_complete(FIXTURE_ROOT / "fast-agent" / "manifest.json")
    live_stream = _load_jsonl(
        FIXTURE_ROOT / "fast-agent" / manifest["files"]["live_semantic_stream"]
    )
    launch = _load_json(
        FIXTURE_ROOT
        / "fast-agent"
        / manifest["files"]["persisted_replay_evidence"]["launch_metadata"]
    )
    resume_case = _load_json(
        FIXTURE_ROOT / "fast-agent" / manifest["files"]["persisted_replay_evidence"]["resume_case"]
    )
    session = _load_json(
        FIXTURE_ROOT / "fast-agent" / manifest["files"]["persisted_replay_evidence"]["session"]
    )
    history = _load_json(
        FIXTURE_ROOT / "fast-agent" / manifest["files"]["persisted_replay_evidence"]["history"]
    )
    progress = _load_jsonl(
        FIXTURE_ROOT
        / "fast-agent"
        / manifest["files"]["persisted_replay_evidence"]["progress_status_stream"]
    )
    tool_transitions = _load_jsonl(
        FIXTURE_ROOT
        / "fast-agent"
        / manifest["files"]["persisted_replay_evidence"]["tool_transitions"]
    )
    failure = _load_jsonl(
        FIXTURE_ROOT
        / "fast-agent"
        / manifest["files"]["persisted_replay_evidence"]["degraded_failure_case"]
    )
    blocked = _load_json(
        FIXTURE_ROOT
        / "fast-agent"
        / manifest["files"]["terminal_surface_observation"]["blocked_input"]
    )
    prompt_visible = _load_json(
        FIXTURE_ROOT
        / "fast-agent"
        / manifest["files"]["terminal_surface_observation"]["interactive_prompt_visible"]
    )

    assert any(
        item["type"] == "info"
        and item["data"]["data"].get("progress_action") == "Streaming"
        for item in live_stream
    )
    assert launch["session_cookie"]["title"] == "Daily planner"
    assert resume_case["history_file"].endswith("history_agent.json")
    assert session["metadata"]["title"] == "Daily planner"
    assert any("tool_calls" in message for message in history["messages"])
    assert any(
        item["data"]["data"].get("progress_action") == "Tool Progress"
        for item in progress
    )
    assert any(item["status"] == "completed" for item in tool_transitions)
    assert any(item.get("sessionUpdate") == "tool_call_update" for item in failure)
    assert blocked["should_persist"] is False
    assert prompt_visible["should_persist"] is False
