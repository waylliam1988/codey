from __future__ import annotations

from pathlib import Path

from codey.app.context import AppContext

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
        assert restarted.user_questions.get("question-1")["question"] == "选择范围？"
        assert restarted.user_questions.pending("chat")["id"] == "question-1"
    finally:
        restarted.close()
