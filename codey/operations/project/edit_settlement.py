"""Prepare authoritative workspace receipts for edits and verification."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.operations.kernel_provenance import _with_trusted_workspace_state, sync_workspace_state_after_edit
from codey.operations.kernel_result import _error_result
from codey.runtime.core.models import ToolResult
from codey.workspace.revision import WorkspaceIdentity, WorkspaceScanError

if TYPE_CHECKING:
    from pathlib import Path

    from codey.operations.kernel_recovery_context import RecoveryContext
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.workspace.revision import WorkspaceRevisionStore


def recheck_unconfirmed_workspace(
    session: TaskSession, call: ToolCall, context: RecoveryContext, execution_evidence: ExecutionEvidence | None,
) -> ToolResult | None:
    """A fresh complete observation can restore verification, never redo a mutation."""
    if call.name != "run" or context.revision_store is None:
        return None
    if WorkspaceIdentity.trusted_pair(session.workspace_revision, session.workspace_fingerprint).trusted:
        return None
    state = context.current_state(refresh=True)
    identity = WorkspaceIdentity.trusted_pair(state.revision, state.fingerprint) if state is not None else WorkspaceIdentity()
    if not identity.trusted:
        reason = (state.identity_reason if state is not None else "") or "scan_unavailable"
        return _error_result(call, f"workspace identity unconfirmed ({reason}); verification was not executed")
    session.set_workspace_state(identity.revision, identity.fingerprint)
    if execution_evidence is not None:
        execution_evidence.set_workspace_state(identity.revision, identity.fingerprint)
    return None


def prepare_edit_settlement(
    session: TaskSession,
    call: ToolCall,
    result: ToolResult,
    ok: bool,
    *,
    project_path: Path | None,
    execution_evidence: ExecutionEvidence | None,
    workspace_ignored_paths: tuple[str, ...],
    workspace_revision_store: WorkspaceRevisionStore | None,
) -> tuple[ToolResult, bool] | None:
    """Prepare the receipt for an applied edit and report an unconfirmed identity."""
    if call.name != "edit" or not ok:
        return None
    if "changed" in result.audit:
        raw_changed = result.audit.get("changed")
        if type(raw_changed) is not bool:
            return _unconfirmed_edit(call, "edit result changed flag must be a boolean"), True
        changed = raw_changed
    else:
        changed = True
    if not changed:
        return None
    try:
        rev, fp = sync_workspace_state_after_edit(
            session,
            project_path,
            execution_evidence,
            ignored_paths=workspace_ignored_paths,
            revision_store=workspace_revision_store,
        )
    except Exception as exc:
        reason = exc.reason if isinstance(exc, WorkspaceScanError) else "identity_settlement_failed"
        return _unconfirmed_edit(call, reason), True
    if rev and fp:
        try:
            trusted = _with_trusted_workspace_state(result, revision=rev, fingerprint=fp)
        except Exception:
            return _unconfirmed_edit(call, "provenance attach failed"), True
        return trusted, False
    return _unconfirmed_edit(call, "revision_store_missing" if workspace_revision_store is None else "identity_invalid"), True


def _unconfirmed_edit(call: ToolCall, reason: str) -> ToolResult:
    failed = _error_result(
        call,
        f"edit happened but workspace identity unconfirmed ({reason}); "
        "re-check workspace before verifying, do not re-apply the edit",
    )
    return ToolResult(ok=failed.ok, call=call, model_text=failed.model_text,
                        audit={**failed.audit, "changed": True, "mutation_applied": True},
                        canonical={"workspace_unconfirmed": True, "mutation_applied": True,
                                   "identity_status": "unconfirmed", "identity_reason": reason})


def invalidate_workspace_identity(session: TaskSession, evidence: ExecutionEvidence | None) -> None:
    session.set_workspace_state(0, "")
    if evidence is not None:
        evidence.set_workspace_state(0, "")
