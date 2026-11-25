"""Lower a frozen destination choice to the existing versioned read/edit leaves."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from codey.completion.contract import CHECK_NOT_APPLICABLE, CHECK_PASS
from codey.operations.browser.browser_copy import write_unconfirmed
from codey.operations.completion_output import tool_facts
from codey.task.instruction_text import instruction_text
from codey.toolchain.constants import STALE_READ_REFERENCE_CODE
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.operations.completion_context import CompletionContext
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.operations.work_status import WorkStatus

PROGRAM = (
    "const base=await tools.read_file({path:inputs.path});unwrap(base);"
    "return unwrap(await tools.edit({path:inputs.path,base_result_ref:base.result_ref,declared_fields:true}));"
)
CONTENT_PROGRAM = (
    "const base=await tools.read_file({path:inputs.path});unwrap(base);"
    "return unwrap(await tools.edit({path:inputs.path,base_result_ref:base.result_ref}));"
)


def write_form(edit: ToolSpec) -> Literal["declared_fields", "content"] | None:
    if "content" in edit.required:
        return "content"
    return "declared_fields" if "declared_fields" in dict(edit.parameters) else None


def free_program_required(task: str) -> bool:
    text = instruction_text(task)
    required = False
    for match in re.finditer(r"(?:\b(?:use\s+free|write\s+your\s+own)\s+|必须\s*手写\s*)"
                             r"(?:JavaScript|JS)(?![A-Za-z0-9_])", text, re.I):
        prefix = text[:match.start()]
        required = re.search(
            r"(?:\bdo\s+not|\bdon['’]t|\bnever|\bmust\s+not|\bshould\s+not|\bno\s+need\s+to|"
            r"\bneed\s+not|\bwithout|不要|禁止|不允许|无需|不需要|不用|不必|别)(?:\s|直接|再|请|please\b)*$",
            prefix, re.I) is None
    return required


def model_spec(original: ToolSpec, paths: tuple[str, ...]) -> ToolSpec:
    example = json.dumps({"tool": original.name, "args": {"path": paths[0]}}, separators=(",", ":"))
    return freeze_spec_parameters(replace(original, required=("path",), declarations="", json_examples=(example,),
        parameters=(("path", {"type": "string", "enum": list(paths)}),),
        description="Copy the task-declared fields from their complete committed source receipts. "
        "Choose an allowed destination path. The fixed program reads the complete current file, "
        "passes its outer receipt reference and performs the versioned edit. "
        "After an explicit file version rejection, the next attempt reads the current file again."))


def lower(args: JsonObject, edit: ToolSpec) -> JsonObject:
    form = write_form(edit)
    if form is None:
        raise ValueError("The frozen edit contract does not support a declared field copy")
    return {"code": CONTENT_PROGRAM if form == "content" else PROGRAM, "inputs": dict(args)}


def offer(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
          status: WorkStatus) -> tuple[TurnSnapshot, str]:
    from codey.operations.kernel.turn_contract import ModelInputBinding
    from codey.operations.kernel.turn_presentation import refresh_snapshot_contract

    fields = tuple(row for row in status.tools if row.rule.file_path)
    names = {"codemode", "read_file", "edit"}
    edit = next((spec for spec in snapshot.frozen_specs if spec.name == "edit"), None)
    if (not fields or free_program_required(session.task_text)
            or snapshot.input_binding is not None or snapshot.browser.copy is not None
            or "codemode" not in snapshot.tool_names or not names <= set(snapshot.inventory_names)
            or names & set(session.controller_denied) or edit is None
            or write_form(edit) is None
            or not all(row.observation.evaluation.passed for row in fields)
            or any(not row.observation.evaluation.passed and not row.rule.file_path
                   and row.rule.tool.startswith("browser_") for row in status.tools)
            or any(not row.passed for row in status.browser)
            or any(row.status not in {CHECK_PASS, CHECK_NOT_APPLICABLE} for row in status.before_edit)):
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
    original = edit if "edit" in snapshot.tool_names else next(
        spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    witnesses = {ref for row in fields for ref in row.observation.evaluation.evidence_refs}
    sources = tuple(ref for ref in records if ref in witnesses)
    binding = ModelInputBinding("declared_fields", model_spec(original, tuple(retryable)), sources)
    offered = replace(snapshot, input_binding=binding)
    from codey.research.answer_contract import submitted_answer
    from codey.research.task_mode import whole_research

    report = ("Report submission is still pending. Use submit_research_answer with the acquired evidence and citations; "
              "if the task requests a report before editing, submit it before this copy. "
              if whole_research(session) and submitted_answer(session) is None else "")
    return refresh_snapshot_contract(offered), (
        "The declared source evidence is complete. The requested file changes are still pending: "
        + ", ".join(retryable) + ". A report submission does not perform these edits. " + report
        + "Use the fixed copy entry to apply the pending file changes, for example: " + binding.model_spec.json_examples[0]
        + ". Choose an allowed path: " + ", ".join(retryable)
        + ". The application supplies the read/edit program, values and outer receipt reference. "
        "Other authorized tools remain available. Preserve the user's ordering."
    )
