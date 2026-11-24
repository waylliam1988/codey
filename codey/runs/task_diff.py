"""Freeze task-owned changes at settlement and read them by run identity."""
from __future__ import annotations

from codey.durable.session import DurableSession
from codey.operations.tool_tasks import is_settled_project_check, owned_tool_task, tool_execution_result
from codey.runs.details import _verification_summary
from codey.runs.ledger import RunLedgerStore, RunLedgerWriter, read_ledger
from codey.runs.ledger_projection import project_run_ledger
from codey.runtime.core.execution_receipt import execution_receipt
from codey.storage.managed_outputs import ManagedOutputStore
from codey.utils.object_fields import object_fields


def record_task_changes(
    ledger: RunLedgerWriter,
    outputs: ManagedOutputStore | None,
    changes: dict[str, object] | None,
    *,
    checks_passed: bool | None,
    receipt: dict[str, object],
) -> None:
    reference = None
    reason = ""
    if changes is not None:
        if changes.get("ok") is False:
            reason = str(changes.get("error") or "Task changes could not be collected.")
        elif changes.get("mode") == "snapshot" and changes.get("changed_count"):
            reason = "Historical diff could not be saved."
            if outputs is not None:
                try:
                    reference = outputs.write_json_artifact(
                        session_id=ledger.session_id, run_id=ledger.run_id, payload=changes,
                    ).to_payload()
                    reason = ""
                except (OSError, ValueError, TimeoutError):
                    pass
    ledger.append_changes_collected(
        changes, checks_passed=checks_passed, receipt=receipt,
        diff_artifact=reference, diff_unavailable=reason,
    )


def load_task_changes(
    ledgers: RunLedgerStore | None, outputs: ManagedOutputStore | None, session_id: str, run_id: str,
    *, durable: DurableSession | None = None,
) -> dict[str, object] | None:
    if ledgers is None:
        return None
    records = read_ledger(ledgers.path_for(session_id, run_id))
    if not records or any(row.payload.get("session_id") != session_id or row.payload.get("run_id") != run_id
                          for row in records):
        return None
    projection = project_run_ledger(records)
    summary, tone = _verification_summary(projection)
    verification: dict[str, object] = {}
    if summary:
        verification.update(summary=summary, tone=tone)
    if projection.final_changes is not None and projection.final_changes.receipt is not None:
        receipt = projection.final_changes.receipt
        verification.update(checks_passed=receipt.verification.checks_passed, trust=receipt.verification.trust,
                            integrity=receipt.integrity.status)
    if durable is not None and (failed := _failed_check(durable, outputs, session_id, run_id)) is not None:
        verification["failed_check"] = failed
    extra = {"verification": verification} if verification else {}
    row = next((record.payload for record in reversed(records) if record.payload.get("type") == "changes_collected"), {})
    ref = row.get("diff_artifact")
    reason = str(row.get("diff_unavailable") or "Historical diff was not retained.")
    if object_fields(ref) and outputs is not None:
        try:
            if ref.get("session_id") != session_id or ref.get("run_id") != run_id:
                raise ValueError("task diff identity mismatch")
            data = outputs.read_json_artifact(ref)
            if object_fields(data):
                return {**data, **extra}
        except (OSError, ValueError, TimeoutError):
            pass
        reason = "Historical diff could not be read."
    return {"ok": True, "files": row.get("files", []), "changed_count": row.get("changed_count", 0),
            "diff": "", "error": reason, "truncated": row.get("files_truncated", False), **extra}


def _failed_check(durable: DurableSession, outputs: ManagedOutputStore | None,
                  session_id: str, run_id: str) -> dict[str, object] | None:
    try:
        if durable.task(run_id).conversation_id != session_id:
            return None
        effects = durable.settled_effects(run_id)
    except (KeyError, ValueError):
        return None
    seen: set[tuple[str, str]] = set()
    for effect in reversed(effects):
        if effect.task_id == run_id or effect.intent.get("tool_name") != "run":
            continue
        try:
            child = owned_tool_task(durable, effect.task_id, owner_task_id=run_id)
            execution = tool_execution_result(durable, child, store=outputs) if is_settled_project_check(child) else None
        except (KeyError, OSError, ValueError):
            continue
        if execution is None or execution[0] != effect.effect_id:
            continue
        result = execution[1]
        command, cwd = result.call.args.get("command"), result.call.args.get("path", ".")
        fact = execution_receipt(result)
        if (not isinstance(command, str) or not isinstance(cwd, str) or (command, cwd) in seen
                or fact is None or fact.status != "exit" or fact.exit_code_scope != "process" or fact.exit_code is None):
            continue
        seen.add((command, cwd))
        if fact.exit_code == 0:
            continue
        ref = fact.output_ref
        failure: dict[str, object] = {"command": command, "cwd": cwd, "exit_code": fact.exit_code}
        if ref is not None and ref.session_id == session_id and ref.run_id == child.task_id:
            failure["output"] = {"execution_id": ref.execution_id, "session_id": ref.session_id, "run_id": ref.run_id}
        return failure
    return None
