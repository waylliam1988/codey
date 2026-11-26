from __future__ import annotations

from typing import TYPE_CHECKING

from codey.operations.kernel_contracts import (
    ClosingDecision,
    DoneDecision,
    KernelDecision,
    KernelResult,
    QuestionDecision,
    RetryDecision,
    result_from_payload,
)
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.types.json_value import decode_json_object, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


def validate_decision(payload: object, *, schema_version: object = 2) -> KernelDecision:
    """Decode committed facts to a finite decision without whole-object casts."""
    if type(schema_version) is not int or schema_version not in (1, 2):
        raise ValueError("unsupported control decision schema")
    row = decode_json_object(payload)
    purpose = row.get("purpose")
    if purpose == "retry":
        required = {"purpose", "prompt", "error"} | ({"counters"} if schema_version == 1 else set())
        if set(row) not in (required, required | {"error_source", "error_type"}):
            raise ValueError("invalid retry decision")
        retry = RetryDecision(purpose="retry", prompt=require_json_string(row["prompt"], "prompt"),
                              error=require_json_string(row["error"], "error"))
        if schema_version == 1:
            retry["counters"] = RuntimeExecutionCounters.from_payload(row["counters"]).to_payload()
        if "error_source" in row:
            retry["error_source"] = require_json_string(row["error_source"], "error_source")
            retry["error_type"] = require_json_string(row["error_type"], "error_type")
        return retry
    if purpose == "done":
        if set(row) != {"purpose", "result", "workspace_revision", "workspace_fingerprint"}:
            raise ValueError("invalid completion decision")
        revision = require_json_integer(row["workspace_revision"], "workspace_revision")
        result = result_from_payload(row["result"])
        if revision < 0 or not result.completed or result.stop_reason != "done":
            raise ValueError("completion decision has no accepted result")
        return DoneDecision(purpose="done", result=result.to_payload(), workspace_revision=revision,
            workspace_fingerprint=require_json_string(row["workspace_fingerprint"], "workspace_fingerprint"))
    if purpose == "question":
        from codey.task.questions import question_args

        if set(row) != {"purpose", "question", "question_id", "result"}:
            raise ValueError("invalid question decision")
        question = question_args(row["question"])
        identity = require_json_string(row["question_id"], "question_id")
        if not identity or question != row["question"]:
            raise ValueError("invalid question decision")
        result = result_from_payload(row["result"])
        if result.completed or result.stop_reason != "question":
            raise ValueError("question decision has no waiting result")
        return QuestionDecision(purpose="question", question=question, question_id=identity, result=result.to_payload())
    if purpose == "closing":
        if set(row) != {"purpose", "result"}:
            raise ValueError("invalid closing decision")
        return ClosingDecision(purpose="closing", result=result_from_payload(row["result"]).to_payload())
    raise ValueError("unknown control decision purpose")


def no_progress_result(session: TaskSession, turn: int) -> KernelResult:
    from codey.operations.explicit_execution_requirements import readonly_fix_obstruction

    obstruction = readonly_fix_obstruction(session)
    return KernelResult(False, obstruction or "stopped after repeated tool results without progress", turn,
                        "blocked" if obstruction is not None else "no_progress")
