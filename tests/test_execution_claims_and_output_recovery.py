"""Completion and receipt reading consume persisted execution facts."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.execution_claims import validate_execution_claims
from codey.operations.task_session import TaskSession
from codey.operations.tool_result_reader import read_tool_result
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.execution_receipt import ExecutionReceipt
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.execution_output_store import ExecutionOutputObserver, ExecutionOutputStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, decode_json_object

REF = "task_turn_effect:execution_test"


def execution_session(*, status: str = "exit", code: int | None = 1) -> TaskSession:
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat", task_text="报告实际退出码")
    receipt = ExecutionReceipt(status, code, "proof")
    session._memory_results[REF] = ToolResult(ToolCall("shell", {"command": 'python -c "import sys;sys.exit(7)"'}),
        receipt.render(), ok=status == "exit", canonical={"execution": receipt.facts()}, audit={"exit_code": code})
    return session


def claim(code: object = 1) -> JsonObject:
    return decode_json_object({"result_ref": REF, "status": "exit", "exit_code_scope": "shell", "exit_code": code})


def test_missing_claim_is_required_when_user_requests_exit_code() -> None:
    verdict = evaluate(execution_session(), "The code is 7")
    assert not verdict.complete and "execution_claims" in verdict.followup


@pytest.mark.parametrize("code", [7, True, "1", None])
def test_model_cannot_replace_observed_code_with_command_text_or_coercion(code: object) -> None:
    session = execution_session()
    session.last_done_args = {"execution_claims": [claim(code)]}
    verdict = evaluate(session, "claimed code")
    assert not verdict.complete and "disagrees" in verdict.followup


def test_matching_claim_is_checked_by_existing_completion_gate() -> None:
    session = execution_session()
    session.last_done_args = {"execution_claims": [claim()]}
    verdict = evaluate(session, "实际 Shell 退出码是 1")
    assert verdict.complete and verdict.proof is not None
    assert any(row.check_id == "execution_claims" and row.status == "pass" for row in verdict.proof.checks)


def test_native_model_completed_status_has_copyable_factual_repair() -> None:
    from codey.operations.kernel_prompt import _result_context

    session = execution_session()
    delivered = _result_context(session._memory_results[REF], session)
    expected = json.loads(delivered.split("Observed execution claim (copy verbatim into done.execution_claims): ", 1)[1])
    assert expected == claim()
    session.last_done_args = {"execution_claims": [{**expected, "status": "completed", "exit_code": 7}]}
    verdict = evaluate(session, "退出码是 7")
    assert not verdict.complete
    repaired, _ = json.JSONDecoder().raw_decode(verdict.followup.split("Copy this observed claim verbatim: ", 1)[1])
    session.last_done_args = {"execution_claims": [repaired]}
    assert evaluate(session, "实际退出码是 1").complete


def test_unknown_exit_is_omitted_and_not_converted_to_success() -> None:
    session = execution_session(status="denied", code=None)
    selected = {"result_ref": REF, "status": "denied", "exit_code_scope": "shell"}
    session.last_done_args = {"execution_claims": [selected]}
    assert validate_execution_claims(session) is not None
    session.last_done_args = {"execution_claims": [{**selected, "exit_code": 0}]}
    with pytest.raises(ValueError, match="disagrees"):
        validate_execution_claims(session)


def test_forged_reference_and_scope_are_rejected() -> None:
    session = execution_session()
    for selected in ({**claim(), "result_ref": "another-task"}, {**claim(), "exit_code_scope": "process"}):
        session.last_done_args = {"execution_claims": [selected]}
        assert not evaluate(session, "reported result").complete


@dataclass
class Reader:
    session: TaskSession
    managed_outputs: ManagedOutputStore | None
    session_id: str = "s"
    run_id: str = "r"


def output_reader(root: Path, *, limit: int, payload: bytes) -> Reader:
    store = ExecutionOutputStore(root, max_bytes=limit)
    store.create("execution_test", session_id="s", run_id="r")
    observer = ExecutionOutputObserver(store, "execution_test")
    observer.offer("stdout", payload)
    observer.close()
    snapshot = store.settled("execution_test", 1)
    store.close()
    session = execution_session()
    truncated = snapshot["stdout"]["truncated"]
    receipt = ExecutionReceipt("exit", 1, "preview only", truncated=True,
        capture_truncated=truncated, execution_id="execution_test", full_output_retained=not truncated)
    session._memory_results[REF] = ToolResult(ToolCall("shell", {}), receipt.render(), ok=True, truncated=True,
        canonical={"execution": receipt.facts()}, audit={"exit_code": 1, "capture_truncated": truncated,
                                                       "execution_id": "execution_test"})
    return Reader(session, ManagedOutputStore(root))


def test_display_omission_is_recoverable_after_store_restart(tmp_path: Path) -> None:
    reader = output_reader(tmp_path, limit=4096, payload=b"A" * 500 + b"MIDDLE_PROOF" + b"Z" * 500)
    result = read_tool_result(reader, ToolCall("read_tool_result", {"result_ref": REF, "query": "MIDDLE_PROOF"}))
    data = decode_json_object(json.loads(result.model_text))
    assert result.ok and "MIDDLE_PROOF" in str(data["text"])
    assert data["full_output_recoverable"] is True
    assert data["original_output_incomplete"] is False
    assert data["display_truncated"] is True


def test_discarded_capture_cannot_be_read_back_as_full_output(tmp_path: Path) -> None:
    reader = output_reader(tmp_path, limit=64, payload=b"A" * 100 + b"LOST_MIDDLE" + b"Z" * 100)
    result = read_tool_result(reader, ToolCall("read_tool_result", {"result_ref": REF, "query": "LOST_MIDDLE"}))
    data = decode_json_object(json.loads(result.model_text))
    assert result.ok and data["match_offset"] is None
    assert data["full_output_recoverable"] is False and data["capture_truncated"] is True
    assert data["storage_truncated"] is True


def test_output_store_scope_is_checked_against_owning_task(tmp_path: Path) -> None:
    reader = output_reader(tmp_path, limit=4096, payload=b"secret")
    reader.run_id = "other"
    result = read_tool_result(reader, ToolCall("read_tool_result", {"result_ref": REF}))
    assert not result.ok and "another task" in result.model_text


def test_receipt_page_continuation_targets_original_receipt(tmp_path: Path) -> None:
    from codey.operations.kernel_prompt import _result_context

    reader = output_reader(tmp_path, limit=4096, payload=b"A" * 500 + b"MIDDLE_PROOF" + b"Z" * 500)
    result = read_tool_result(reader, ToolCall("read_tool_result", {"result_ref": REF, "limit": 100}))
    reader.session._memory_results["task_turn_effect:page"] = result
    prompt = _result_context(result, reader.session)
    args = json.loads(prompt.split("To continue the original receipt, use read_tool_result(", 1)[1].split(").", 1)[0])
    assert args == {"result_ref": REF, "view": "output", "offset": 100}
    next_page = read_tool_result(reader, ToolCall("read_tool_result", args))
    assert json.loads(next_page.model_text)["offset"] == 100


def test_missing_value_view_says_which_view_is_available() -> None:
    result = read_tool_result(Reader(execution_session(), None),
                              ToolCall("read_tool_result", {"result_ref": REF, "view": "value"}))
    assert not result.ok and "available views: output" in result.model_text
    assert "does not authorize re-execution" in result.model_text


def test_unexecuted_receipt_cannot_promise_a_complete_execution_log() -> None:
    reader = Reader(execution_session(status="denied", code=None), None)
    result = read_tool_result(reader, ToolCall("read_tool_result", {"result_ref": REF}))
    assert result.ok
    assert json.loads(result.model_text)["full_output_recoverable"] is False


def test_status_header_does_not_promise_recovery_after_formatter_clipping() -> None:
    receipt = ExecutionReceipt("exit", 0, "A" * 50_000).bounded(limit=1000)
    assert not receipt.full_output_recoverable
    assert "Full execution output recoverable: false" in receipt.render()
