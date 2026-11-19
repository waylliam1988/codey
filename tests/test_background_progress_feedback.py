from __future__ import annotations

import json

from codey.operations.kernel_progress import KernelProgress
from codey.operations.progress_comparison import progress_comparison
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject


def _accepted(ref: str) -> ToolResult:
    value: JsonObject = {"kind": "accepted", "task_ref": ref}
    return ToolResult(ToolCall("run", {"command": "python -m pytest", "run_in_background": True}),
                      json.dumps(value), ok=True, kind="accepted", canonical={"value": value})


def _output(ref: str, text: str = "same failure") -> ToolResult:
    value: JsonObject = {"task_ref": ref, "status": "completed", "result": {
        "ok": False, "exit_code": 1, "result_ref": "execution-" + ref},
        "output": {"stdout": {"execution_id": "execution-" + ref, "revision": 3,
                   "chunks": [{"start": 0, "text": text}], "next_cursor": len(text), "total_bytes": len(text)}}}
    return ToolResult(ToolCall("task_output", {"task_ref": ref}), json.dumps(value), ok=True, canonical={"value": value})


def test_new_admission_id_alone_is_not_progress_or_execution_evidence() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.verify"})))
    progress = KernelProgress()
    first, second = _accepted("one"), _accepted("two")
    assert first.model_text != second.model_text
    assert progress_comparison(first, {}).information == progress_comparison(second, {}).information
    progress.observe([first], session)
    progress.observe([second], session)
    assert progress.idle == 1
    assert second.kind == "accepted" and not session.verifications


def test_control_progress_uses_known_operation_and_preserves_actual_output() -> None:
    receipts = {"a": _accepted("one"), "b": _accepted("two")}
    first, second = progress_comparison(_output("one"), receipts), progress_comparison(_output("two"), receipts)
    assert first.call == second.call
    assert first.information == second.information
    assert progress_comparison(_output("two", "changed failure"), receipts).information != first.information
    literal = "a business string containing execution-two"
    assert literal in progress_comparison(_output("two", literal), receipts).information


def test_unknown_control_reference_keeps_its_identity() -> None:
    assert progress_comparison(_output("one"), {}).information != progress_comparison(_output("two"), {}).information


def test_different_commands_and_changed_exit_results_remain_new_observations() -> None:
    first = _accepted("one")
    different = ToolResult(ToolCall("run", {"command": "python -m unittest", "run_in_background": True}),
                           first.model_text, ok=True, kind="accepted", canonical=first.canonical)
    assert progress_comparison(first, {}).information != progress_comparison(different, {}).information
    failure = _output("one")
    success_value: JsonObject = {"task_ref": "one", "status": "completed", "result": {"ok": True, "exit_code": 0}}
    success = ToolResult(failure.call, json.dumps(success_value), ok=True, canonical={"value": success_value})
    receipts = {"a": first}
    before = json.dumps(failure.canonical)
    assert progress_comparison(failure, receipts).information != progress_comparison(success, receipts).information
    assert json.dumps(failure.canonical) == before
