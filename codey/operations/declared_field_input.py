"""Lower a frozen destination choice to the existing versioned read/edit leaves."""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from codey.completion.contract import CHECK_NOT_APPLICABLE, CHECK_PASS
from codey.operations.browser_copy import write_unconfirmed
from codey.operations.completion_output import tool_facts
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.operations.completion_context import CompletionContext
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.operations.work_status import WorkStatus

PROGRAM = (
    "const base=await tools.read_file({path:inputs.path});unwrap(base);"
    "return unwrap(await tools.edit({path:inputs.path,base_result_ref:base.result_ref,declared_fields:true}));"
)


def model_spec(original: ToolSpec, paths: tuple[str, ...]) -> ToolSpec:
    return freeze_spec_parameters(replace(original, required=("path",), declarations="", json_examples=(),
        parameters=(("path", {"type": "string", "enum": list(paths)}),),
        description="Copy the task-declared fields from their complete committed source receipts. "
        "Choose an allowed destination path. The fixed program reads the complete current file, "
        "passes its outer receipt reference and performs the versioned edit. "
        "After an explicit file version rejection, the next attempt reads the current file again."))


def lower(args: JsonObject) -> JsonObject:
    return {"code": PROGRAM, "inputs": dict(args)}


def offer(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
          status: WorkStatus) -> tuple[TurnSnapshot, str]:
    from codey.operations.kernel_protocol import ModelInputBinding, refresh_snapshot_contract

    fields = tuple(row for row in status.tools if row.rule.file_path)
    names = {"codemode", "read_file", "edit"}
    edit = next((spec for spec in snapshot.frozen_specs if spec.name == "edit"), None)
    if (not fields or snapshot.input_binding is not None or snapshot.browser_copy is not None
            or "codemode" not in snapshot.tool_names or not names <= set(snapshot.inventory_names)
            or names & set(session.controller_denied) or edit is None
            or "declared_fields" not in dict(edit.parameters)
            or not all(row.observation.evaluation.passed for row in fields)
            or any(not row.outcome.passed and not row.rule.file_path and row.rule.tool not in {"read_file", "edit"}
                   for row in status.tools) or any(not row.passed for row in status.browser)
            or any(row.status not in {CHECK_PASS, CHECK_NOT_APPLICABLE} for row in status.other)):
        return snapshot, ""
    records, results = tool_facts(session, context)
    if any(write_unconfirmed(row) for row in results.values()):
        return snapshot, "A file write occurred with unconfirmed workspace identity. Verify settlement before another write."
    paths = tuple({row.rule.file_path: None for row in fields if not row.outcome.passed})
    if not paths:
        return snapshot, ""
    retryable: list[str] = []
    for path in paths:
        attempts = 0
        for ref in reversed(tuple(records)):
            result = results.get(ref)
            if result is None or result.call.name != "edit" or result.call.args.get("path") != path:
                continue
            if result.audit.get("error_code") != STALE_READ_REFERENCE_CODE:
                break
            attempts += 1
        if attempts < 3:
            retryable.append(path)
    if not retryable:
        return snapshot, "Repeated file version conflicts exhausted the fixed copy retry limit. Inspect the concurrent changes."
    original = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    browser_names = {spec.name for spec in snapshot.frozen_specs if spec.executor == "browser"}
    offered = replace(snapshot, input_binding=ModelInputBinding("declared_fields", model_spec(original, tuple(retryable))),
                      tool_names=tuple(name for name in snapshot.tool_names if name not in browser_names),
                      browser_followups=False, browser_references=None, browser_fill_text=None, task_navigation_url=None)
    return refresh_snapshot_contract(offered), (
        "The declared source evidence is complete. Copy the pending fields with codemode using only an allowed path: "
        + ", ".join(retryable) + ". The application supplies the read/edit program, values and outer receipt reference."
    )
