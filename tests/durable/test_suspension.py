from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.task_services import build_task_deps
from codey.app.task_submit import dispatch_durable_task
from codey.app.user_questions import DurableUserQuestionStore
from codey.durable import CommitConflict, DurableSession, SQLiteDurableStore, TaskStatus
from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.model import DurableMutation
from codey.durable.scheduler import DurableTaskScheduler
from tests.test_headless_real_kernel_lifecycle import ScriptedProvider

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")


def test_question_releases_execution_and_restart_wakes_the_same_task_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    provider = ScriptedProvider([{"tool": "ask_user", "args": {"question": "Which scope?", "options": ["read", "all"]}}])
    root = tmp_path / "state"
    result = run_headless(
        HeadlessRequest(project=None, task="Inspect after asking my scope.", provider_id="local", intent="chat",
                        max_turns=4, state_home=root),
        emit_jsonl=lambda _row: None, connect_provider=lambda *_a, **_kw: provider,
    )
    assert result.stop_reason == "question"
    restarted = AppContext(root, start_scheduler=False)
    try:
        task = restarted.durable.task(result.run_id)
        assert task.status == TaskStatus.WAITING
        assert not task.attempt_token
        operation = operation_from_checkpoint(task, result.session_id)
        assert operation is not None and operation.leaf != "terminal"
        question = restarted.user_questions.pending(result.session_id)
        assert question is not None
        answered = restarted.user_questions.answer(question["id"], question["revision"], "read")
        assert answered["state"] == "answered"
        assert restarted.user_questions.answer(question["id"], question["revision"], "read") == answered
        ready = restarted.durable.task(result.run_id)
        assert ready.status == TaskStatus.PENDING
        assert ready.provider_snapshot == task.provider_snapshot
        assert ready.state["task_policy"] == task.state["task_policy"]
        assert len(restarted.durable.tasks()) == 1
        assert len([entry for entry in restarted.durable.entries(result.session_id)
                    if entry.kind == "user_message" and entry.payload["text"] == "read"]) == 1
        resumed_provider = ScriptedProvider([{"tool": "done", "args": {"summary": "Read-only scope recorded."}}])
        monkeypatch.setattr("codey.app.task_services.build_task_deps", lambda state, **kwargs:
            build_task_deps(state, connect_provider=lambda *_a, **_kw: resumed_provider, **kwargs))

        def submit(fn, *args, **kwargs):
            fn(*args, **kwargs)
            return True

        monkeypatch.setattr("codey.app.task_submit.submit_browser_task", submit)
        seen: list[str] = []
        errors: list[Exception] = []

        def dispatch(task):
            seen.append(task.task_id)
            try:
                return dispatch_durable_task(task, state=restarted)
            except Exception as exc:
                errors.append(exc)
                raise

        scheduler = DurableTaskScheduler(restarted.durable, dispatch)
        try:
            scheduler._reconcile()
            scheduler._reconcile()
            assert seen == [result.run_id]
            assert not errors
            assert restarted.durable.task(result.run_id).phase == "completed"
            assert restarted.durable.task(result.run_id).submission_id == task.submission_id
            assert len(resumed_provider.prompts) == 1
            assert "Answer: read" in resumed_provider.prompts[0]
        finally:
            scheduler.close()
    finally:
        restarted.close()


def test_parked_attempt_cannot_mutate_after_suspension(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        session.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
        session.claim_task("t")
        captured = session.task("t")
        with session.execution("t", captured.attempt_token):
            session.wait_task("t", state={"wait_reason": "user"})
            with pytest.raises(CommitConflict):
                session.transact_task("t", lambda task, _effects, revision:
                    DurableMutation("c", revision, task=replace(task, revision=task.revision + 1)))
    finally:
        session.close()


@pytest.mark.parametrize("park_first", [False, True])
def test_question_cancellation_closes_the_original_task_and_submission(tmp_path: Path, park_first: bool) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    questions = DurableUserQuestionStore(session)
    session.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
    session.claim_task("t")
    task = session.task("t")
    questions.request("q", "c", "t", {"question": "Which?", "options": ["one", "two"]}, {})
    if park_first:
        session.wait_task("t", state={"stop_reason": "question"})
    cancelled = questions.cancel("q", 1)
    assert cancelled["state"] == "cancelled"
    if not park_first:
        assert session.task("t").state["cancel_requested"]
        session.wait_task("t", state={"stop_reason": "question"})
    assert session.task("t").status == TaskStatus.CANCELLED
    assert session.store.submission(task.submission_id).status == "cancelled"
    assert questions.cancel("q", 1) == cancelled
    session.close()
    reopened = DurableSession(SQLiteDurableStore(path))
    try:
        assert reopened.task("t").status == TaskStatus.CANCELLED
        assert reopened.pending_tasks() == ()
        with pytest.raises(ValueError):
            DurableUserQuestionStore(reopened).answer("q", 1, "one")
    finally:
        reopened.close()


def test_answer_before_parking_survives_a_lost_wakeup_and_repeated_questions(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    questions = DurableUserQuestionStore(session)
    session.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
    for index in range(2):
        session.claim_task("t")
        token = session.task("t").attempt_token
        question = questions.request(f"q{index}", "c", "t", {"question": "Which?", "options": ["one", "two"]}, {})
        questions.answer(question["id"], 1, "one")
        assert session.task("t").attempt_token == token
        session.wait_task("t", state={"stop_reason": "question"})
        assert session.task("t").status == TaskStatus.PENDING
        session.close()
        session = DurableSession(SQLiteDurableStore(path))
        questions = DurableUserQuestionStore(session)
        assert questions.answer(question["id"], 1, "one")["state"] == "answered"
    try:
        assert len(session.tasks()) == 1
        assert len(session.entries("c")) == 3
        assert session.task("t").state["resume_entry_id"] == "answer-q1"
    finally:
        session.close()
