import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import omx_questions
from ccbot.handlers.callback_data import CB_OMX_QUESTION_SELECT
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
) -> Path:
    if scope == "root":
        path = root / ".omx/state/questions" / f"{question_id}.json"
    elif scope == "session":
        path = root / ".omx/state/sessions/s1/questions" / f"{question_id}.json"
    else:
        raise ValueError(f"unsupported question scope: {scope}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
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
                "allow_other": False,
                "other_label": "Other",
                "multi_select": multi_select,
                "type": "multi-answerable" if multi_select else "single-answerable",
                "source": "deep-interview",
                "renderer": {
                    "renderer": "tmux-pane",
                    "target": target,
                    "return_target": return_target,
                    "return_transport": "tmux-send-keys",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
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
    yield
    omx_questions._question_msgs.clear()
    omx_questions._question_windows.clear()
    omx_questions._question_selections.clear()
    omx_questions._question_render_state.clear()


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
    monkeypatch.setattr(omx_questions, "_tmux_send_line", AsyncMock())
    monkeypatch.setattr(omx_questions, "_tmux_kill_pane", AsyncMock())
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
    query.edit_message_text.assert_awaited_once()
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
        message=SimpleNamespace(message_thread_id=42),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=1),
    )
    context = SimpleNamespace(bot=bot)

    assert await omx_questions.handle_omx_question_callback(update, context) is True

    text = bot.send_message.await_args.kwargs["text"]
    assert "Old prompt" in text
    assert "New prompt" not in text
    assert (
        omx_questions._question_selections[
            (1, "t:42", "question-2026-04-30T01-00-00-000Z-old12345")
        ]
        == {0}
    )
    query.answer.assert_awaited_once_with("Updated")


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
