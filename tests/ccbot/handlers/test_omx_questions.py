import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import message_queue, omx_questions
from ccbot.handlers.callback_data import (
    CB_OMX_QUESTION_REFRESH,
    CB_OMX_QUESTION_SELECT,
    CB_OMX_QUESTION_TOGGLE,
)
from ccbot.tmux_manager import TmuxWindow


def _write_question(
    root: Path,
    *,
    question_id: str = "question-2026-04-30T01-00-00-000Z-a1b2c3d4",
    scope: str = "session",
    status: str = "prompting",
    multi_select: bool = False,
    question: str = "Pick a path",
    target: str = "%207",
    return_target: str = "%0",
    allow_other: bool = False,
    error: dict[str, object] | None = None,
    include_renderer: bool = True,
) -> Path:
    if scope == "root":
        path = root / ".omx/state/questions" / f"{question_id}.json"
    elif scope == "session":
        path = root / ".omx/state/sessions/s1/questions" / f"{question_id}.json"
    else:
        raise ValueError(f"unsupported question scope: {scope}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "omx.question/v1",
        "question_id": question_id,
        "created_at": "2026-04-30T01:00:00.000Z",
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": status,
        "header": "Deep interview",
        "question": question,
        "options": [
            {
                "label": "Proceed",
                "value": "proceed",
                "description": "Continue safely",
            },
            {"label": "Revise", "value": "revise"},
        ],
        "allow_other": allow_other,
        "other_label": "Other",
        "multi_select": multi_select,
        "type": "multi-answerable" if multi_select else "single-answerable",
        "source": "deep-interview",
    }
    if include_renderer:
        payload["renderer"] = {
            "renderer": "tmux-pane",
            "target": target,
            "return_target": return_target,
            "return_transport": "tmux-send-keys",
        }
    if error is not None:
        payload["error"] = error
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture(autouse=True)
def _clear_question_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        omx_questions.session_manager,
        "get_surface_coordinates_for_window",
        lambda user_id, window_id: (None, None, None),
    )
    omx_questions._question_msgs.clear()
    omx_questions._question_windows.clear()
    omx_questions._question_selections.clear()
    omx_questions._question_render_state.clear()
    omx_questions._question_prompt_deferrals.clear()
    omx_questions._question_prompt_defer_audited.clear()
    message_queue._turn_generations.clear()
    message_queue._pre_final_visible_closed.clear()
    message_queue._technical_status_closed.clear()
    yield
    omx_questions._question_msgs.clear()
    omx_questions._question_windows.clear()
    omx_questions._question_selections.clear()
    omx_questions._question_render_state.clear()
    omx_questions._question_prompt_deferrals.clear()
    omx_questions._question_prompt_defer_audited.clear()
    message_queue._turn_generations.clear()
    message_queue._pre_final_visible_closed.clear()
    message_queue._technical_status_closed.clear()


def test_find_active_omx_question_reads_durable_record(tmp_path: Path) -> None:
    _write_question(tmp_path)
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )

    record = omx_questions.find_active_omx_question(window)

    assert record is not None
    assert record.question == "Pick a path"
    assert record.options[0].label == "Proceed"
    assert record.short_id == "a1b2c3d4"


def test_find_active_omx_question_reads_root_scoped_record(tmp_path: Path) -> None:
    path = _write_question(tmp_path, scope="root")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )

    record = omx_questions.find_active_omx_question(window)

    assert record is not None
    assert record.path == path
    assert record.question == "Pick a path"


def test_find_active_omx_question_matches_split_panes_by_window(
    tmp_path: Path,
) -> None:
    _write_question(tmp_path, target="%207", return_target="%0")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%999",
        pane_ids=("%999", "%207", "%0"),
    )

    assert omx_questions.find_active_omx_question(window) is not None

    other_window = TmuxWindow(
        window_id="@8",
        window_name="other",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%999",
        pane_ids=("%999",),
    )

    assert omx_questions.find_active_omx_question(other_window) is None


@pytest.mark.asyncio
async def test_answer_omx_question_marks_record_and_bridges_to_return_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_question(tmp_path)
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%207",
    )
    record = omx_questions.find_active_omx_question(window)
    assert record is not None
    sent: list[tuple[str, str]] = []
    killed: list[tuple[str, str]] = []

    async def fake_send(target: str, text: str) -> bool:
        sent.append((target, text))
        return True

    async def fake_kill(target: str, *, return_target: str = "") -> bool:
        killed.append((target, return_target))
        return True

    monkeypatch.setattr(omx_questions, "_tmux_send_line", fake_send)
    monkeypatch.setattr(omx_questions, "_tmux_kill_pane", fake_kill)

    answer = await omx_questions.answer_omx_question(record, [0])

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "answered"
    assert payload["answer"]["value"] == "proceed"
    assert answer["selected_labels"] == ["Proceed"]
    assert sent == [("%0", "[omx question answered] proceed")]
    assert killed == [("%207", "%0")]


@pytest.mark.asyncio
async def test_answer_omx_question_other_uses_free_text_and_return_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_question(tmp_path, allow_other=True, target="%12", return_target="%5")
    window = TmuxWindow(
        window_id="@4",
        window_name="comfy-agent",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%5",
        pane_ids=("%5", "%12"),
    )
    record = omx_questions.find_active_omx_question(window)
    assert record is not None
    sent: list[tuple[str, str]] = []
    killed: list[tuple[str, str]] = []

    async def fake_send(target: str, text: str) -> bool:
        sent.append((target, text))
        return True

    async def fake_kill(target: str, *, return_target: str = "") -> bool:
        killed.append((target, return_target))
        return True

    monkeypatch.setattr(omx_questions, "_tmux_send_line", fake_send)
    monkeypatch.setattr(omx_questions, "_tmux_kill_pane", fake_kill)

    answer = await omx_questions.answer_omx_question_other(
        record,
        "  apply the gate to all external deliverables  ",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "answered"
    assert payload["answer"] == {
        "kind": "other",
        "value": "apply the gate to all external deliverables",
        "selected_labels": ["Other"],
        "selected_values": ["apply the gate to all external deliverables"],
    }
    assert answer["kind"] == "other"
    assert sent == [
        ("%5", "[omx question answered] apply the gate to all external deliverables")
    ]
    assert killed == [("%12", "%5")]


@pytest.mark.asyncio
async def test_handle_omx_question_ui_sends_once_then_reuses_existing_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path)
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=55)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )

    assert await omx_questions.handle_omx_question_ui(bot, 1, "@7", 42) is True
    assert await omx_questions.handle_omx_question_ui(bot, 1, "@7", 42) is True

    bot.send_message.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()
    text = bot.send_message.await_args.kwargs["text"]
    assert "❓ OMX Question" in text
    assert "Pick a path" in text
    assert "1. Proceed" in text


@pytest.mark.asyncio
async def test_handle_omx_question_ui_audits_prompt_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccbot import delivery_audit

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    _write_question(tmp_path)
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=56)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )

    assert await omx_questions.handle_omx_question_ui(bot, 1, "@7", 42) is True

    rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["action"] == "send"
    assert rows[-1]["task_type"] == "question_prompt"
    assert rows[-1]["semantic_kind"] == "interactive_question"
    assert rows[-1]["content_type"] == "question"
    assert rows[-1]["message_id"] == 56
    assert "question-2026-04-30T01-00-00-000Z-a1b2c3d4" in rows[-1]["preview"]
    assert "Pick a path" in rows[-1]["preview"]


@pytest.mark.asyncio
async def test_handle_omx_question_ui_defers_first_prompt_until_gate_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccbot import delivery_audit

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(delivery_audit.config, "telegram_delivery_audit_file", audit_path)
    _write_question(tmp_path)
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=57)
    monotonic_values = [100.0, 146.0]
    monkeypatch.setattr(
        omx_questions.time,
        "monotonic",
        lambda: monotonic_values.pop(0) if monotonic_values else 146.0,
    )
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )

    assert (
        await omx_questions.handle_omx_question_ui(
            bot,
            1,
            "@7",
            42,
            send_if_missing=False,
            defer_reason="pre_final_lane_open",
        )
        is True
    )
    bot.send_message.assert_not_awaited()

    assert (
        await omx_questions.handle_omx_question_ui(
            bot,
            1,
            "@7",
            42,
            send_if_missing=False,
            defer_reason="pre_final_lane_open",
        )
        is True
    )
    bot.send_message.assert_awaited_once()

    rows = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["action"] == "suppress"
    assert rows[0]["reason"] == "pre_final_lane_open"
    assert rows[0]["semantic_kind"] == "interactive_question"
    assert rows[-1]["action"] == "send"
    assert rows[-1]["reason"] == "pre_final_gate_timeout"


@pytest.mark.asyncio
async def test_handle_omx_question_ui_reads_helper_pane_state_path_outside_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_cwd = tmp_path / "runtime"
    runtime_cwd.mkdir()
    run_root = tmp_path / "omx-runs" / "run-20260515065148-f3a6"
    question_path = _write_question(
        run_root,
        question=(
            "Round 1 | Target: Scope | Ambiguity: 100%\n\n"
            "Для первого плана улучшений какой класс результатов должен получать "
            "обязательный pre-delivery self-test gate перед отправкой пользователю?"
        ),
        target="%12",
        return_target="%5",
        allow_other=True,
    )
    window = TmuxWindow(
        window_id="@4",
        window_name="comfy-agent",
        cwd=str(runtime_cwd),
        pane_current_command="node",
        pane_id="%5",
        pane_ids=("%5", "%12", "%6"),
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=91)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -1003685295814,
    )
    monkeypatch.setattr(
        omx_questions,
        "_list_pane_processes",
        AsyncMock(
            return_value=[
                ("%5", 2258523),
                ("%12", 2694043),
                ("%6", 2258908),
            ]
        ),
    )
    monkeypatch.setattr(
        omx_questions,
        "_cmdline_for_pid",
        lambda pid: (
            [
                "node",
                "/data/iqdoctor/.nvm/versions/node/v24.14.0/lib/node_modules/"
                "oh-my-codex/dist/cli/omx.js",
                "question",
                "--ui",
                "--state-path",
                str(question_path),
            ]
            if pid == 2694043
            else ["node", "omx.js", "hud", "--watch"]
        ),
    )

    assert await omx_questions.handle_omx_question_ui(bot, 3045664, "@4", 555) is True

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == -1003685295814
    assert kwargs["message_thread_id"] == 555
    assert "❓ OMX Question" in kwargs["text"]
    assert "pre-delivery self-test gate" in kwargs["text"]
    assert "1. Proceed" in kwargs["text"]
    assert "Other is available" in kwargs["text"]


@pytest.mark.asyncio
async def test_handle_omx_question_ui_reopens_timeout_error_with_live_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_cwd = tmp_path / "runtime"
    runtime_cwd.mkdir()
    run_root = tmp_path / "omx-runs" / "run-20260515065148-f3a6"
    question_path = _write_question(
        run_root,
        question=(
            "Round 1 | Target: Scope | Ambiguity: 100%\n\n"
            "Для первого плана улучшений какой класс результатов должен получать "
            "обязательный pre-delivery self-test gate перед отправкой пользователю?"
        ),
        status="error",
        target="%12",
        return_target="%5",
        allow_other=True,
        error={
            "code": "question_runtime_failed",
            "message": "Timed out waiting for question answer after 1800000ms",
        },
    )
    window = TmuxWindow(
        window_id="@4",
        window_name="comfy-agent",
        cwd=str(runtime_cwd),
        pane_current_command="node",
        pane_id="%5",
        pane_ids=("%5", "%12", "%6"),
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=92)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -1003685295814,
    )
    monkeypatch.setattr(
        omx_questions,
        "_list_pane_processes",
        AsyncMock(return_value=[("%5", 1), ("%12", 2), ("%6", 3)]),
    )
    monkeypatch.setattr(
        omx_questions,
        "_cmdline_for_pid",
        lambda pid: [
            "node",
            "omx.js",
            "question",
            "--ui",
            "--state-path",
            str(question_path),
        ]
        if pid == 2
        else ["node", "omx.js", "hud", "--watch"],
    )
    monkeypatch.setattr(
        omx_questions,
        "_capture_renderer_pane",
        AsyncMock(
            return_value=(
                "Round 1 | Target: Scope | Ambiguity: 100%\n\n"
                "Для первого плана улучшений какой класс результатов должен получать "
                "обязательный pre-delivery self-test gate перед отправкой пользователю?\n\n"
                "› [x] 1. Proceed\n"
                "  [ ] 2. Revise\n"
                "  [ ] 4. Other\n"
            )
        ),
    )

    assert await omx_questions.handle_omx_question_ui(bot, 3045664, "@4", 555) is True

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "❓ OMX Question" in text
    assert "pre-delivery self-test gate" in text
    assert "Timed out waiting" not in text
    assert "Other is available" in text


@pytest.mark.asyncio
async def test_handle_omx_question_ui_recovers_renderer_start_failure_from_state_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_cwd = tmp_path / "runtime"
    runtime_cwd.mkdir()
    run_root = tmp_path / "omx-runs" / "run-20260515131437-9496"
    question_path = _write_question(
        run_root,
        question=(
            "Round 2 | Target: Success criteria | Ambiguity: 72%\n\n"
            "Для Comfy media only: какой минимальный pre-delivery gate?"
        ),
        status="error",
        allow_other=True,
        include_renderer=False,
        error={
            "code": "question_runtime_failed",
            "message": (
                "omx question cannot open a visible renderer because this tmux "
                "session has no attached client."
            ),
        },
    )
    state_path = question_path.parent.parent / "deep-interview-state.json"
    state_path.write_text(
        json.dumps(
            {
                "active": False,
                "mode": "deep-interview",
                "current_phase": "blocked",
                "tmux_pane_id": "%16",
                "tmux_window_id": "@8",
            }
        ),
        encoding="utf-8",
    )
    window = TmuxWindow(
        window_id="@8",
        window_name="comfy-agent",
        cwd=str(runtime_cwd),
        pane_current_command="node",
        pane_id="%16",
        pane_ids=("%16", "%18"),
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=93)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -1003685295814,
    )
    monkeypatch.setattr(
        omx_questions,
        "_candidate_question_paths_from_window_processes",
        AsyncMock(return_value=[question_path]),
    )
    monkeypatch.setattr(
        omx_questions,
        "_launch_renderer_pane",
        AsyncMock(return_value=None),
    )

    assert await omx_questions.handle_omx_question_ui(bot, 3045664, "@8", 555) is True

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "❓ OMX Question" in text
    assert "local OMX question renderer did not open" in text
    assert "минимальный pre-delivery gate" in text

    record = await omx_questions.find_answerable_omx_question_for_window(window)
    assert record is not None
    assert record.renderer["return_target"] == "%16"
    send_line = AsyncMock(return_value=True)
    monkeypatch.setattr(omx_questions, "_tmux_send_line", send_line)

    await omx_questions.answer_omx_question_other(record, "risk-tiered gates")

    send_line.assert_awaited_once_with(
        "%16",
        "[omx question answered] risk-tiered gates",
    )


@pytest.mark.asyncio
async def test_handle_omx_question_ui_materializes_renderer_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_cwd = tmp_path / "runtime"
    runtime_cwd.mkdir()
    run_root = tmp_path / "omx-runs" / "run-20260515131437-9496"
    question_path = _write_question(
        run_root,
        question="Round 3 | Target: Non-goals | Ambiguity: 48%",
        status="error",
        include_renderer=False,
        error={
            "code": "question_runtime_failed",
            "message": "omx question cannot open a visible renderer: no attached client",
        },
    )
    (question_path.parent.parent / "deep-interview-state.json").write_text(
        json.dumps({"tmux_pane_id": "%16", "tmux_window_id": "@8"}),
        encoding="utf-8",
    )
    window = TmuxWindow(
        window_id="@8",
        window_name="comfy-agent",
        cwd=str(runtime_cwd),
        pane_current_command="node",
        pane_id="%16",
        pane_ids=("%16", "%18"),
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=94)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_candidate_question_paths_from_window_processes",
        AsyncMock(return_value=[question_path]),
    )
    monkeypatch.setattr(omx_questions, "_tmux_pane_exists", AsyncMock(return_value=True))
    launch = AsyncMock(return_value="%20")
    monkeypatch.setattr(omx_questions, "_launch_renderer_pane", launch)

    assert await omx_questions.handle_omx_question_ui(bot, 3045664, "@8", 555) is True

    launch.assert_awaited_once()
    payload = json.loads(question_path.read_text(encoding="utf-8"))
    assert payload["status"] == "prompting"
    assert payload["renderer"] == {
        "renderer": "tmux-pane",
        "target": "%20",
        "return_target": "%16",
        "return_transport": "tmux-send-keys",
    }
    assert "error" not in payload


@pytest.mark.asyncio
async def test_omx_question_callback_answers_current_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_question(tmp_path)
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    generation_at_bridge: list[int] = []

    async def send_to_window(window_id: str, text: str) -> tuple[bool, str]:
        generation_at_bridge.append(message_queue.current_turn_generation(1, 42))
        return True, "ok"

    kill_pane = AsyncMock(return_value=True)
    raw_send = AsyncMock()
    monkeypatch.setattr(omx_questions.session_manager, "send_to_window", send_to_window)
    monkeypatch.setattr(omx_questions, "_tmux_send_line", raw_send)
    monkeypatch.setattr(omx_questions, "_tmux_kill_pane", kill_pane)
    query = SimpleNamespace(
        data=f"{CB_OMX_QUESTION_SELECT}:1:a1b2c3d4:@7",
        message=SimpleNamespace(message_thread_id=42),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=AsyncMock())

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "answered"
    assert payload["answer"]["value"] == "revise"
    kill_pane.assert_awaited_once_with("%207", return_target="%0")
    assert generation_at_bridge == [1]
    raw_send.assert_not_awaited()
    query.edit_message_text.assert_awaited_once()
    query.answer.assert_awaited_once_with("Answered")
    assert message_queue.current_turn_generation(1, 42) == 1


@pytest.mark.asyncio
async def test_omx_question_toggle_mirrors_selection_to_renderer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_question(
        tmp_path,
        multi_select=True,
        target="%20",
        return_target="%16",
    )
    window = TmuxWindow(
        window_id="@8",
        window_name="comfy-agent",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%16",
        pane_ids=("%16", "%20"),
    )
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        omx_questions,
        "_capture_renderer_pane",
        AsyncMock(
            return_value=(
                "› [ ] 1. Proceed\n"
                "  [ ] 2. Revise\n"
                "  [ ] 3. Other\n"
            )
        ),
    )
    sent_keys: list[tuple[str, str]] = []

    async def fake_send_key(target: str, key: str) -> bool:
        sent_keys.append((target, key))
        return True

    monkeypatch.setattr(omx_questions, "_tmux_send_key", fake_send_key)
    query = SimpleNamespace(
        data=f"{CB_OMX_QUESTION_TOGGLE}:1:a1b2c3d4:@8",
        message=SimpleNamespace(message_thread_id=42),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=AsyncMock())

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    assert sent_keys == [("%20", "Down"), ("%20", "Space")]
    query.answer.assert_awaited_once_with("Updated")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "prompting"


@pytest.mark.asyncio
async def test_omx_question_callback_answers_recoverable_timeout_error_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_cwd = tmp_path / "runtime"
    runtime_cwd.mkdir()
    run_root = tmp_path / "omx-runs" / "run-20260515065148-f3a6"
    path = _write_question(
        run_root,
        question_id="question-2026-05-15T07-54-50-805Z-91cbac1d",
        status="error",
        target="%12",
        return_target="%5",
        error={
            "code": "question_runtime_failed",
            "message": "Timed out waiting for question answer after 1800000ms",
        },
    )
    window = TmuxWindow(
        window_id="@4",
        window_name="comfy-agent",
        cwd=str(runtime_cwd),
        pane_current_command="node",
        pane_id="%5",
        pane_ids=("%5", "%12", "%6"),
    )
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        omx_questions,
        "_list_pane_processes",
        AsyncMock(return_value=[("%5", 1), ("%12", 2), ("%6", 3)]),
    )
    monkeypatch.setattr(
        omx_questions,
        "_cmdline_for_pid",
        lambda pid: (
            [
                "node",
                "omx.js",
                "question",
                "--ui",
                "--state-path",
                str(path),
            ]
            if pid == 2
            else ["node", "omx.js", "hud", "--watch"]
        ),
    )
    monkeypatch.setattr(
        omx_questions,
        "_capture_renderer_pane",
        AsyncMock(return_value="Pick a path\n\n› [x] 1. Proceed\n  [ ] 2. Revise\n"),
    )
    raw_send = AsyncMock(return_value=True)
    kill = AsyncMock(return_value=True)
    send_to_window = AsyncMock(return_value=(True, "ok"))
    monkeypatch.setattr(omx_questions, "_tmux_send_line", raw_send)
    monkeypatch.setattr(omx_questions, "_tmux_kill_pane", kill)
    monkeypatch.setattr(omx_questions.session_manager, "send_to_window", send_to_window)
    query = SimpleNamespace(
        data=f"{CB_OMX_QUESTION_SELECT}:0:91cbac1d:@4",
        message=SimpleNamespace(message_thread_id=555, chat_id=-1003685295814),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=3045664),
    )
    context = SimpleNamespace(bot=AsyncMock())

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "answered"
    assert payload["answer"]["value"] == "proceed"
    kill.assert_awaited_once_with("%12", return_target="%5")
    send_to_window.assert_awaited_once_with("@4", "[omx question answered] proceed")
    raw_send.assert_not_awaited()
    query.answer.assert_awaited_once_with("Answered")


@pytest.mark.asyncio
async def test_omx_question_multiselect_toggle_rerenders_exact_record_not_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(
        tmp_path,
        question_id="question-2026-04-30T01-00-00-000Z-old12345",
        question="Old prompt",
        multi_select=True,
        target="",
        return_target="",
    )
    newer = _write_question(
        tmp_path,
        question_id="question-2026-04-30T01-00-01-000Z-new67890",
        question="New prompt",
        multi_select=True,
        target="",
        return_target="",
    )
    newer.touch()
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=77)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    query = SimpleNamespace(
        data=f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:0:old12345:@7",
        message=SimpleNamespace(message_id=77, message_thread_id=42),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 77
    text = bot.edit_message_text.await_args.kwargs["text"]
    assert "Old prompt" in text
    assert "New prompt" not in text
    assert omx_questions._question_msgs[(1, "t:42")] == 77
    assert (
        omx_questions._question_selections[
            (1, "t:42", "question-2026-04-30T01-00-00-000Z-old12345")
        ]
        == {0}
    )
    query.answer.assert_awaited_once_with("Updated")


@pytest.mark.asyncio
async def test_omx_question_multiselect_repeated_toggles_keep_callback_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:0:a1b2c3d4:@7",
            message=SimpleNamespace(message_id=77, message_thread_id=42),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        ),
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True
    update.callback_query.data = (
        f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:1:a1b2c3d4:@7"
    )
    assert await omx_questions.handle_omx_question_callback(update, context) is True

    bot.send_message.assert_not_awaited()
    assert bot.edit_message_text.await_count == 2
    assert {
        call.kwargs["message_id"] for call in bot.edit_message_text.await_args_list
    } == {77}
    assert (
        omx_questions._question_selections[
            (1, "t:42", "question-2026-04-30T01-00-00-000Z-a1b2c3d4")
        ]
        == {0, 1}
    )


@pytest.mark.asyncio
async def test_omx_question_poll_after_callback_toggle_does_not_send_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    update = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:0:a1b2c3d4:@7",
            message=SimpleNamespace(message_id=77, message_thread_id=42),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        ),
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True
    bot.send_message.reset_mock()
    bot.edit_message_text.reset_mock()

    assert await omx_questions.handle_omx_question_ui(bot, 1, "@7", 42) is True

    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_omx_question_refresh_edits_callback_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    query = SimpleNamespace(
        data=f"{CB_OMX_QUESTION_REFRESH}:a1b2c3d4:@7",
        message=SimpleNamespace(message_id=77, message_thread_id=42),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_awaited_once()
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 77
    assert omx_questions._question_msgs[(1, "t:42")] == 77
    query.answer.assert_awaited_once_with("Refreshed")


@pytest.mark.asyncio
async def test_omx_question_idempotent_edit_keeps_tracking_without_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = Exception("Message is not modified")
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    query = SimpleNamespace(
        data=f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:0:a1b2c3d4:@7",
        message=SimpleNamespace(message_id=77, message_thread_id=42, chat_id=-100),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    bot.send_message.assert_not_awaited()
    assert omx_questions._question_msgs[(1, "t:-100:42")] == 77
    assert (
        omx_questions._question_render_state[(1, "t:-100:42")]
        == ("question-2026-04-30T01-00-00-000Z-a1b2c3d4", (0,))
    )
    query.answer.assert_awaited_once_with("Updated")


@pytest.mark.asyncio
async def test_omx_question_toggle_replacement_send_is_tracked_after_real_edit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = Exception("message to edit not found")
    bot.send_message.return_value = SimpleNamespace(message_id=88)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    query = SimpleNamespace(
        data=f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:0:a1b2c3d4:@7",
        message=SimpleNamespace(message_id=77, message_thread_id=42),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    bot.edit_message_text.assert_awaited_once()
    bot.send_message.assert_awaited_once()
    assert omx_questions._question_msgs[(1, "t:42")] == 88
    assert (
        omx_questions._question_selections[
            (1, "t:42", "question-2026-04-30T01-00-00-000Z-a1b2c3d4")
        ]
        == {0}
    )
    query.answer.assert_awaited_once_with("Updated")


@pytest.mark.asyncio
async def test_omx_question_toggle_unknown_edit_failure_does_not_send_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = Exception("temporary Telegram timeout")
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    query = SimpleNamespace(
        data=f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:0:a1b2c3d4:@7",
        message=SimpleNamespace(message_id=77, message_thread_id=42, chat_id=-100),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    bot.send_message.assert_not_awaited()
    assert omx_questions._question_msgs[(1, "t:-100:42")] == 77
    assert (1, "t:-100:42") not in omx_questions._question_render_state
    query.answer.assert_awaited_once_with(
        "Could not update question prompt; please retry",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_omx_question_replacement_send_preserves_callback_chat_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    bot = AsyncMock()
    bot.edit_message_text.side_effect = Exception("message to edit not found")
    bot.send_message.return_value = SimpleNamespace(message_id=88)
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    query = SimpleNamespace(
        data=f"{omx_questions.CB_OMX_QUESTION_TOGGLE}:0:a1b2c3d4:@7",
        message=SimpleNamespace(message_id=77, chat_id=-12345),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    assert bot.send_message.await_args.kwargs["chat_id"] == -12345
    assert omx_questions._question_msgs[(1, "c:-12345")] == 88


@pytest.mark.asyncio
async def test_omx_question_multiselect_submit_answers_selected_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_question(tmp_path, multi_select=True, target="", return_target="")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
    )
    omx_questions._question_selections[
        (1, "t:42", "question-2026-04-30T01-00-00-000Z-a1b2c3d4")
    ] = {0, 1}
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(omx_questions, "_tmux_send_line", AsyncMock())
    monkeypatch.setattr(omx_questions, "_tmux_kill_pane", AsyncMock())
    query = SimpleNamespace(
        data=f"{omx_questions.CB_OMX_QUESTION_SUBMIT}:a1b2c3d4:@7",
        message=SimpleNamespace(message_thread_id=42),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=AsyncMock())

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "answered"
    assert payload["answer"]["kind"] == "multi"
    assert payload["answer"]["value"] == ["proceed", "revise"]
    query.edit_message_text.assert_awaited_once()
    assert (1, "t:42") not in omx_questions._question_msgs
    assert (
        1,
        "t:42",
        "question-2026-04-30T01-00-00-000Z-a1b2c3d4",
    ) not in omx_questions._question_selections
    query.answer.assert_awaited_once_with("Answered")


@pytest.mark.asyncio
async def test_omx_question_callback_rejects_unbound_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_question(tmp_path)
    monkeypatch.setattr(
        omx_questions,
        "_callback_window_authorized",
        lambda *args, **kwargs: False,
    )
    query = SimpleNamespace(
        data=f"{CB_OMX_QUESTION_SELECT}:1:a1b2c3d4:@7",
        message=SimpleNamespace(message_thread_id=42, chat_id=-100),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=AsyncMock())

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "prompting"
    query.answer.assert_awaited_once_with(
        "Question is not bound to this surface",
        show_alert=True,
    )
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_omx_question_ui_edits_terminal_answer_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    question_id = "question-2026-04-30T01-00-00-000Z-a1b2c3d4"
    path = _write_question(tmp_path, question_id=question_id, status="answered")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["answer"] = {
        "kind": "option",
        "value": "proceed",
        "selected_labels": ["Proceed"],
        "selected_values": ["proceed"],
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    window = TmuxWindow(
        window_id="@7",
        window_name="work",
        cwd=str(tmp_path),
        pane_current_command="node",
        pane_id="%0",
        pane_ids=("%0", "%207"),
    )
    bot = AsyncMock()
    omx_questions._question_msgs[(1, "t:42")] = 55
    omx_questions._question_windows[(1, "t:42")] = "@7"
    omx_questions._question_render_state[(1, "t:42")] = (question_id, ())
    monkeypatch.setattr(
        omx_questions.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=window),
    )
    monkeypatch.setattr(
        omx_questions.session_manager,
        "resolve_chat_id",
        lambda user_id, thread_id=None: -100,
    )

    assert await omx_questions.handle_omx_question_ui(bot, 1, "@7", 42) is True

    bot.edit_message_text.assert_awaited_once()
    assert (
        "✅ OMX Question answered" in bot.edit_message_text.await_args.kwargs["text"]
    )
    assert "Answer: Proceed" in bot.edit_message_text.await_args.kwargs["text"]
    assert (1, "t:42") not in omx_questions._question_msgs
