from __future__ import annotations

from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.durable import DurableSession, MemoryDurableStore
from codey.durable.questions import DurableUserQuestionStore

QUESTION = {"question": "选择范围？", "options": ["只读", "完整"]}


def test_app_questions_are_task_owned_and_survive_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state = AppContext(root, start_scheduler=False)
    state.durable.admit_input(
        conversation_id="chat",
        request_id="request-1",
        task_id="run_" + "c" * 32,
        text="inspect",
        provider_snapshot={"provider_id": "local"},
    )
    try:
        item = state.user_questions.request(
            "question-1", "chat", "run_" + "c" * 32, QUESTION, {"provider": "local"}
        )
        assert item["state"] == "pending"
        assert not (root / "conversation_questions.sqlite3").exists()
    finally:
        state.close()

    restarted = AppContext(root, start_scheduler=False)
    try:
        stored = restarted.user_questions.get("question-1")
        pending = restarted.user_questions.pending("chat")
        assert stored is not None and stored["question"] == "选择范围？"
        assert pending is not None and pending["id"] == "question-1"
    finally:
        restarted.close()


def test_question_request_requires_its_tasks_conversation() -> None:
    session = DurableSession(MemoryDurableStore())
    session.create_conversation("other")
    session.admit_input(
        conversation_id="chat", request_id="request", task_id="run",
        text="inspect", provider_snapshot={},
    )
    questions = DurableUserQuestionStore(session)
    sequence = session.commit_sequence()
    try:
        with pytest.raises(ValueError, match="question.*conversation"):
            questions.request("question", "other", "run", QUESTION, {})

        assert session.commit_sequence() == sequence
        assert questions.pending("chat") is None
        assert questions.pending("other") is None
        created = questions.request("question", "chat", "run", QUESTION, {})
        assert questions.pending("chat") == created
    finally:
        session.close()
