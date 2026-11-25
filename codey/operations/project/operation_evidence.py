"""Prepare a project write and attach evidence to its execution result."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.operations.kernel_provenance import _with_trusted_workspace_state
from codey.operations.kernel_result import _error_result
from codey.operations.project.project_delivery import (
    PAGE_AFTER_EDIT,
    ProjectFileBasis,
    edit_scope_violation,
    project_file_basis,
    project_source_binding,
    project_write_basis,
)
from codey.toolchain.execution_options import split_execution_options
from codey.workspace.revision import WorkspaceIdentity

if TYPE_CHECKING:
    from pathlib import Path

    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.types.json_value import JsonObject
    from codey.workspace.revision import WorkspaceRevisionStore


@dataclass(frozen=True)
class PreparedOperation:
    args: JsonObject
    source_basis: tuple[str, ToolResult] | None
    file_basis: ProjectFileBasis | None


def prepare_operation(
    session: TaskSession, call: ToolCall, *, project_path: Path | None, snapshot: TurnSnapshot | None,
) -> PreparedOperation | ToolResult:
    source_basis = None
    file_basis: ProjectFileBasis | None = None
    if call.name == "edit":
        source_basis, error = project_write_basis(session)
        if error:
            return _error_result(call, error)
    if (call.name == "edit" and project_path is not None and snapshot is not None
            and isinstance(target := call.args.get("path"), str)):
        from codey.operations.project.project_instructions import INSTRUCTION_CHANGE, changed_instruction_target

        try:
            if violation := edit_scope_violation(project_path, target, session.policy.edit_scope, task=session.task_text):
                return _error_result(call, violation)
            if source_basis is not None:
                file_basis = project_file_basis(session, project_path, target, call.args)
            changed = changed_instruction_target(project_path, target, snapshot.project_instructions)
        except (OSError, ValueError) as exc:
            return _error_result(call, str(exc))
        if changed is not None:
            return replace(_error_result(call, f"Repository instructions for {changed} must be observed before writing."),
                canonical={INSTRUCTION_CHANGE: [changed]}, audit={"error_code": INSTRUCTION_CHANGE})

    args, _options = split_execution_options(call.args)
    if file_basis is not None and file_basis.version and "replacements" in args:
        expected = file_basis.version.removeprefix("raw:")
        if args.get("expected_version", expected) != expected:
            return _error_result(call, "The patch version and selected file receipt disagree.")
        args = {**args, "expected_version": expected}
    return PreparedOperation(args, source_basis, file_basis)


def attach_operation_evidence(
    session: TaskSession, call: ToolCall, result: ToolResult, prepared: PreparedOperation, *,
    ok: bool, exit_code: int | None, project_path: Path | None,
    workspace_revision_store: WorkspaceRevisionStore | None, workspace_ignored_paths: tuple[str, ...],
) -> ToolResult:
    result = replace(result, call=call)
    if ok and prepared.source_basis is not None and prepared.file_basis is not None:
        result = replace(result, canonical={**result.canonical,
            "project_source_basis": project_source_binding(prepared.source_basis, prepared.file_basis)})
    verify_run = call.name == "run" and exit_code is not None
    verify_page = call.name == "browser_read" and ok and PAGE_AFTER_EDIT in session.policy.required_checks
    stable_workspace = True
    if (verify_run or verify_page) and project_path is not None and workspace_revision_store is not None:
        after = workspace_revision_store.current_state(project_path, ignored_paths=workspace_ignored_paths if verify_page else ())
        stable_workspace = (not after.identity_reason and after.revision == session.workspace_revision
                            and after.fingerprint == session.workspace_fingerprint)
    if (verify_run or verify_page) and stable_workspace and WorkspaceIdentity.trusted_pair(
            session.workspace_revision, session.workspace_fingerprint).trusted:
        result = _with_trusted_workspace_state(result, revision=session.workspace_revision,
                                              fingerprint=session.workspace_fingerprint)
    return result
