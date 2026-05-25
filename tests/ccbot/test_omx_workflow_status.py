import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ccbot.omx_workflow_status import (
    OmxWorkflowStatus,
    load_omx_workflow_status_path,
    parse_omx_statusline,
    read_omx_workflow_status,
    render_omx_workflow_status,
)


def test_renders_compact_line_and_clipped_summary():
    status = OmxWorkflowStatus(
        workflow="ultragoal",
        state="running",
        progress_current=1,
        progress_total=6,
        unit_id="G002",
        current_unit_summary="G002 — WorkflowCard and ArollSemanticOrchestrator contracts",
        last_age="543m ago",
    )

    assert render_omx_workflow_status(status) == (
        "🧭 OMX ultragoal 1/6 · G002 · running · last 543m ago\n"
        "↳ G002 — WorkflowCard and ArollSemanticOrchestrator contracts"
    )


def test_parser_accepts_strict_ultragoal_statusline():
    pane = (
        "previous output\n"
        "ultragoal 1/6 ▶ G002-g002-workflowcard-and-arollsemantico: "
        "G002 — WorkflowCard and ArollSemantic…estrator contracts · "
        "objective: G002 — WorkflowCard and ArollSemanticOrchestrator contracts\n"
        "turns:235 | session:14h5m | last:543m ago\n"
    )

    status = parse_omx_statusline(pane)

    assert status is not None
    assert status.workflow == "ultragoal"
    assert status.progress_current == 1
    assert status.progress_total == 6
    assert status.unit_id == "G002"
    assert status.state == "running"
    assert status.last_age == "543m ago"
    assert status.current_unit_summary == "G002 — WorkflowCard and ArollSemantic…estrator contracts"


def test_parser_rejects_similar_assistant_prose():
    pane = "The ultragoal 1/6 item is worth mentioning in a final answer."

    assert parse_omx_statusline(pane) is None


def test_reads_ultragoal_goals_state_first(tmp_path: Path):
    goals_path = tmp_path / ".omx" / "ultragoal" / "goals.json"
    goals_path.parent.mkdir(parents=True)
    goals_path.write_text(
        json.dumps(
            {
                "updatedAt": "2026-05-25T06:00:00Z",
                "goals": [
                    {
                        "id": "G001-implement-omx-workflow-status-reader",
                        "title": "Implement OMX workflow status reader and renderer",
                        "status": "in_progress",
                        "updatedAt": "2026-05-25T06:00:00Z",
                    },
                    {"id": "G002-docs", "title": "Docs", "status": "pending"},
                ],
            }
        )
    )

    status = read_omx_workflow_status(
        tmp_path,
        now=datetime(2026, 5, 25, 6, 5, tzinfo=UTC),
    )

    assert status is not None
    assert status.workflow == "ultragoal"
    assert status.state == "running"
    assert status.progress_current == 1
    assert status.progress_total == 2
    assert status.unit_id == "G001"
    assert status.current_unit_summary == "Implement OMX workflow status reader and renderer"


def test_reads_generic_workflow_state(tmp_path: Path):
    state_path = tmp_path / ".omx" / "state" / "sessions" / "abc" / "ralph-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "mode": "ralph",
                "active": True,
                "current_phase": "executing",
                "current": 2,
                "total": 5,
                "goal_id": "G002-example",
                "current_unit_summary": "Tighten delivery semantics",
                "updated_at": "2026-05-25T06:00:00Z",
            }
        )
    )

    status = read_omx_workflow_status(
        tmp_path,
        now=datetime(2026, 5, 25, 6, 1, tzinfo=UTC),
    )

    assert status is not None
    assert status.workflow == "ralph"
    assert status.state == "running"
    assert status.progress_current == 2
    assert status.progress_total == 5
    assert status.unit_id == "G002"





def test_window_id_filter_suppresses_goals_json_without_window_proof(tmp_path: Path):
    goals_path = tmp_path / ".omx" / "ultragoal" / "goals.json"
    goals_path.parent.mkdir(parents=True)
    goals_path.write_text(json.dumps({"goals": [{"id": "G001-x", "status": "in_progress"}]}))

    assert read_omx_workflow_status(tmp_path, window_id="@5") is None


def test_window_id_filter_accepts_matching_session_state(tmp_path: Path):
    state_path = tmp_path / ".omx" / "state" / "sessions" / "abc" / "ultraqa-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "mode": "ultraqa",
                "status": "running",
                "tmux_window_id": "@5",
                "current_unit_summary": "Matched window",
            }
        )
    )

    status = read_omx_workflow_status(tmp_path, window_id="@5")

    assert status is not None
    assert status.workflow == "ultraqa"
    assert status.current_unit_summary == "Matched window"


def test_completed_ultragoal_plan_reports_last_goal_progress(tmp_path: Path):
    goals_path = tmp_path / ".omx" / "ultragoal" / "goals.json"
    goals_path.parent.mkdir(parents=True)
    goals_path.write_text(
        json.dumps(
            {
                "goals": [
                    {"id": "G001-first", "title": "First", "status": "complete"},
                    {"id": "G002-second", "title": "Second", "status": "complete"},
                ]
            }
        )
    )

    status = read_omx_workflow_status(tmp_path)

    assert status is not None
    assert status.state == "complete"
    assert status.progress_current == 2
    assert status.progress_total == 2
    assert status.unit_id == "G002"


def test_generic_pending_state_renders_as_waiting(tmp_path: Path):
    state_path = tmp_path / ".omx" / "state" / "ultraqa-state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"mode": "ultraqa", "status": "pending"}))

    status = load_omx_workflow_status_path(state_path, cwd=tmp_path)

    assert status is not None
    assert status.state == "waiting"


def test_suppresses_corrupt_unknown_stale_and_unrelated_state(tmp_path: Path):
    corrupt = tmp_path / ".omx" / "state" / "bad-state.json"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{")
    assert load_omx_workflow_status_path(corrupt, cwd=tmp_path) is None

    unknown = tmp_path / ".omx" / "state" / "unknown-state.json"
    unknown.write_text(json.dumps({"mode": "not-omx", "status": "running"}))
    assert load_omx_workflow_status_path(unknown, cwd=tmp_path) is None

    stale = tmp_path / ".omx" / "state" / "ultraqa-state.json"
    stale.write_text(json.dumps({"mode": "ultraqa", "status": "running"}))
    old = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    os.utime(stale, (old, old))
    assert load_omx_workflow_status_path(stale, cwd=tmp_path, max_age_seconds=60) is None

    unrelated = tmp_path / "elsewhere" / ".omx" / "state" / "ralph-state.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(json.dumps({"mode": "ralph", "status": "running"}))
    assert load_omx_workflow_status_path(unrelated, cwd=tmp_path) is None


def test_sanitizes_raw_internal_summary():
    status = OmxWorkflowStatus(
        workflow="ultragoal",
        state="running",
        current_unit_summary="Read .omx/ultragoal/goals.json for raw ledger details",
    )

    assert render_omx_workflow_status(status) == "🧭 OMX ultragoal · running"
