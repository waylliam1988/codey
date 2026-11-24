"""Freeze task-owned changes at settlement and read them by run identity."""
from __future__ import annotations

from codey.runs.ledger import RunLedgerStore, RunLedgerWriter, read_ledger
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
) -> dict[str, object] | None:
    if ledgers is None:
        return None
    records = read_ledger(ledgers.path_for(session_id, run_id))
    if not records or any(row.payload.get("session_id") != session_id or row.payload.get("run_id") != run_id
                          for row in records):
        return None
    row = next((record.payload for record in reversed(records) if record.payload.get("type") == "changes_collected"), {})
    ref = row.get("diff_artifact")
    reason = str(row.get("diff_unavailable") or "Historical diff was not retained.")
    if object_fields(ref) and outputs is not None:
        try:
            if ref.get("session_id") != session_id or ref.get("run_id") != run_id:
                raise ValueError("task diff identity mismatch")
            data = outputs.read_json_artifact(ref)
            if object_fields(data):
                return dict(data)
        except (OSError, ValueError, TimeoutError):
            pass
        reason = "Historical diff could not be read."
    return {"ok": True, "files": row.get("files", []), "changed_count": row.get("changed_count", 0),
            "diff": "", "error": reason, "truncated": row.get("files_truncated", False)}
