"""Task submissions use explicit input variants for start, resume, and approval."""

from pathlib import Path

import pytest

from codey.app.submission_types import TaskSubmitter
from codey.task.entry_auth import EntryAuth
from codey.types.json_value import JsonValue


def _recorder(calls: list[tuple[str, str]]) -> TaskSubmitter:
    def submit(
        session_id: str, project: str | None, task: str, max_turns: int,
        continue_task: bool, provider_id: str, intent: str, **kwargs: object,
    ) -> str:
        previous = kwargs.get("previous_run_id", "")
        assert isinstance(previous, str)
        calls.append((task, previous))
        return "run-2"

    return submit


def test_explicit_task_inputs_are_disjoint() -> None:
    from codey.task.model import ResolveShellApproval, ResumeRun, StartTask

    assert StartTask("hello").text == "hello"
    assert ResumeRun("run-1").source_run_id == "run-1"
    assert ResolveShellApproval("approval-1").approval_id == "approval-1"


def test_approval_resume_requires_only_approval_id() -> None:
    from codey.task.model import ResolveShellApproval

    with pytest.raises(ValueError):
        ResolveShellApproval("")


def test_http_submission_rejects_implicit_continue_boolean() -> None:
    from codey.app.api import run_submit_response

    status, payload = run_submit_response(
        {"session_id": "s", "task": "continue", "continue_task": True, "provider": "local"},
        _recorder([]),
    )
    assert status == 400
    assert "continue_task" in str(payload["error"])


def test_http_resume_input_forwards_source_run_without_boolean() -> None:
    from codey.app.api import run_submit_response

    calls: list[tuple[str, str]] = []
    submit = _recorder(calls)
    status, payload = run_submit_response(
        {"session_id": "s", "provider": "deepseek", "input": {"kind": "resume_run", "source_run_id": "run-1"}},
        submit,
    )
    assert status == 200 and payload["run_id"] == "run-2"
    assert len(calls) == 1
    assert calls[0][1] == "run-1"


RESUME_CASES: list[tuple[dict[str, object], str]] = [
    ({}, "Continue the source run"),
    ({"instruction": ""}, "Continue the source run"),
    ({"instruction": "  "}, "Continue the source run"),
    ({"instruction": "  Inspect the results.  "}, "Inspect the results."),
]


@pytest.mark.parametrize("fields,expected_task", RESUME_CASES)
def test_resume_instruction_uses_the_declared_default_when_omitted(
    fields: dict[str, object], expected_task: str,
) -> None:
    from codey.app.api import run_submit_response

    # ResumeRun.instruction is optional with the empty string default. Omitting
    # it and explicitly supplying that default must admit the same task.
    calls: list[tuple[str, str]] = []
    status, _payload = run_submit_response({
        "session_id": "s", "provider": "deepseek",
        "input": {"kind": "resume_run", "source_run_id": "run-1", **fields},
    }, _recorder(calls))
    assert status == 200
    assert calls == [(expected_task, "run-1")]


INVALID_SOURCES: list[JsonValue] = [None, True, False, 42, [], {}, ["run-1"], {"id": "run-1"}]


@pytest.mark.parametrize("intent,inferred,resume,expected", [
    (None, "hybrid", False, "hybrid"),
    ("auto", "hybrid", False, "hybrid"),
    ("auto", "", False, "auto"),
    ("project", "hybrid", False, "project"),
    ("research", "hybrid", False, "research"),
    ("auto", "hybrid", True, "auto"),
])
def test_captured_intent_inference_applies_only_to_new_automatic_tasks(
    tmp_path: Path, intent: str | None, inferred: str, resume: bool, expected: str,
) -> None:
    from codey.app.api import run_submit_response

    calls: list[tuple[bool, str, dict[str, object]]] = []

    def submit(
        session_id: str, project: str | None, task: str, max_turns: int,
        continue_task: bool, provider_id: str, intent: str, **kwargs: object,
    ) -> str:
        calls.append((continue_task, intent, kwargs))
        return "run-2"

    body: dict[str, object] = {"project": str(tmp_path), "provider": "deepseek", "task": "Inspect"}
    if intent is not None:
        body["intent"] = intent
    if resume:
        body["input"] = {"kind": "resume_run", "source_run_id": "run-1"}
    auth = EntryAuth(
        strict_research=True, sources_open_required=True,
        denied_capabilities=("project.write",), inferred_intent=inferred,
    )
    status, payload = run_submit_response(body, submit, captured_auth=auth)
    assert status == 200 and payload["run_id"] == "run-2"
    assert len(calls) == 1
    continued, submitted_intent, options = calls[0]
    assert continued is resume and submitted_intent == expected
    assert options["denied_capabilities"] == ("project.write",)
    assert options["strict_research"] is True
    assert options["sources_open_required"] is True
    assert options["project_changes_required"] is False
    assert options.get("previous_run_id", "") == ("run-1" if resume else "")


@pytest.mark.parametrize("source", INVALID_SOURCES)
def test_resume_source_preserves_the_public_string_contract(source: JsonValue) -> None:
    from codey.app.api import run_submit_response

    calls: list[tuple[str, str]] = []
    status, _payload = run_submit_response({
        "session_id": "s", "provider": "deepseek",
        "input": {"kind": "resume_run", "source_run_id": source},
    }, _recorder(calls))
    assert status == 400
    assert calls == []


def test_a_durable_pending_question_blocks_admission_with_its_original_identity(tmp_path: Path) -> None:
    from codey.app.api import run_submit_response
    from codey.app.context import AppContext
    from codey.utils.object_fields import object_fields

    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    try:
        source_run = "run_" + "c" * 32
        ctx.durable.admit_input(
            conversation_id="s", request_id="request-1", task_id=source_run,
            text="Inspect", provider_snapshot={"provider_id": "local"},
        )
        question_fields: dict[str, object] = {"question": "Choose scope?", "options": ["Read only", "Full"]}
        metadata: dict[str, object] = {"provider": "local"}
        pending: object = ctx.user_questions.request(
            "question-1", "s", source_run, question_fields, metadata,
        )
        assert object_fields(pending)
        assert pending["state"] == "pending"
        calls: list[tuple[str, str]] = []
        status, payload = run_submit_response({
            "session_id": "s", "task": "Start another task", "provider": "deepseek",
        }, _recorder(calls), ctx=ctx)
        assert status == 409
        assert payload["code"] == "question_pending"
        question = payload["question"]
        assert object_fields(question)
        assert question["id"] == "question-1"
        assert calls == []
    finally:
        ctx.close()
