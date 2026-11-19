"""Small model-facing projections of existing read and verification receipts."""
from __future__ import annotations

import json

from codey.operations.task_session import TaskSession
from codey.runtime.core.models import ToolResult
from codey.runtime.core.verification_observation import CompleteVerification, verification_observations
from codey.types.json_value import JsonObject, decode_json_object


def verification_rows(session: TaskSession) -> list[JsonObject]:
    latest: dict[tuple[str, str], JsonObject] = {}
    for observation in verification_observations(session.verifications):
        outcome = observation.outcome
        state = "unknown"
        row: JsonObject = {"command": observation.command, "cwd": observation.cwd}
        if isinstance(outcome, CompleteVerification):
            state = ("stale" if outcome.workspace_revision != session.workspace_revision
                     or outcome.workspace_fingerprint != session.workspace_fingerprint
                     else "current_pass" if outcome.succeeded else "current_fail")
            row.update(exit_code=outcome.exit_code, workspace_revision=outcome.workspace_revision,
                       workspace_fingerprint=outcome.workspace_fingerprint)
        row["state"] = state
        latest[observation.key] = row
    for ref, receipt in session._memory_results.items():
        if receipt.kind != "completed" or receipt.call.name not in {"run", "shell"}:
            continue
        key = (str(receipt.call.args.get("command", "")), str(receipt.call.args.get("path", ".")))
        record = session.executed.get(ref)
        if (key in latest and record is not None and "exit_code" in record
                and all(receipt.audit.get(field) == latest[key].get(field)
                        for field in ("exit_code", "workspace_revision", "workspace_fingerprint"))):
            latest[key]["result_ref"] = ref
    return list(latest.values())[-12:]


def tool_task_observations(session: TaskSession) -> dict[str, JsonObject]:
    observations: dict[str, JsonObject] = {}
    for result in session._memory_results.values():
        if result.ok and result.call.name in {"task_output", "task_cancel"}:
            value = decode_json_object(result.canonical.get("value"))
            ref = value.get("task_ref")
            if isinstance(ref, str):
                observations[ref] = value
    return observations


def verification_retry_guidance(result: ToolResult, session: TaskSession) -> str:
    if result.call.name != "run":
        return ""
    for row in verification_rows(session):
        if (row["state"] != "current_fail" or row["command"] != result.call.args.get("command")
                or row["cwd"] != result.call.args.get("path", ".") or "result_ref" not in row):
            continue
        read: JsonObject = {"result_ref": row["result_ref"], "view": "output", "limit": 4000}
        from codey.runtime.core.execution_receipt import execution_receipt

        prior = session._memory_results.get(str(row["result_ref"]))
        execution = execution_receipt(prior) if prior is not None else None
        if execution is not None and "FAILED " in execution.output:
            read["query"] = "FAILED "
        return ("\nThis exact command already failed against the current files. Recover its details with read_tool_result("
                + json.dumps(read)
                + "). Inspect and fix the cause before running again; rerunning does not repair code. "
                "An intentional transient-failure investigation may justify a repeat.")
    return ""


def coding_result_guidance(result: ToolResult, result_ref: str) -> str:
    if result.call.name == "edit" and not result.ok:
        from codey.toolchain.runtime import READ_MAX_LINES

        return ("\nNo successful edit is established by this receipt. Read the current target with read_file("
                + json.dumps({"path": result.call.args.get("path", ""), "offset": 1, "limit": READ_MAX_LINES})
                + "), then use that new complete read's result_ref and qualified region name. "
                "Do not retry the same mismatched text or reuse a stale baseline.")
    if result.call.name != "read_file" or not result.ok or result.truncated or not result_ref:
        return ""
    value = decode_json_object(result.canonical.get("value"))
    if (value.get("start_line") != 1 or value.get("next_offset") != 0 or not value.get("text_complete")
            or value.get("end_line") != value.get("total_lines")):
        return ""
    path, text = value.get("path"), value.get("text")
    if not isinstance(path, str) or not isinstance(text, str) or not path.endswith(".py"):
        return ""
    from codey.operations.region_edit import python_region_hints

    regions = python_region_hints(text)
    if not regions:
        return ""
    return ("\nVersion-bound Python regions (names and computed line boundaries from this read): "
            + json.dumps(regions, ensure_ascii=False)
            + "\nFor a focused edit, pass path=" + json.dumps(path)
            + ", base_result_ref=" + json.dumps(result_ref)
            + ", region set to an exact name listed above, and content containing only its replacement. "
            "relative_indent=true can align the whole block; preserve its internal indentation. "
            "After a successful edit, read again before editing another region.")
