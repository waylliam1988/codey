"""Dispatch and normalize one tool operation for both inline and owned tasks."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from codey.operations.kernel_result import (
    PolicyDeniedResult,
    _consistent_tool_result,
    _error_result,
    _normalize_delegate_result,
    _normalize_explicit_result,
    policy_denial_result,
    strict_exit_code_or_none,
)
from codey.runtime.core.models import ToolResult

if TYPE_CHECKING:
    from pathlib import Path

    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.operations.kernel_contracts import ToolExecutor
    from codey.operations.task_execution import ProjectPolicyCheck
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall
    from codey.workspace.revision import WorkspaceRevisionStore


class ToolDelegate(Protocol):
    def handles(self, name: str) -> bool: ...

    def execute(
        self,
        call: ToolCall,
        *,
        turn: int = 0,
        tool_index: int = 0,
    ) -> tuple[ToolResult, bool, int | None]: ...


@runtime_checkable
class PolicyCheckingDelegate(Protocol):
    def check_policy(self, call: ToolCall) -> ProjectPolicyCheck: ...


def _explicit_policy_denial(
    delegate: ToolDelegate | None, call: ToolCall, *, project_path: Path | None = None
) -> tuple[ToolResult, bool, int | None] | None:
    """Delegate guard for the explicit executor; fail-closed on outage.

    With a project path, a missing delegate means the project guard is
    unavailable: deny instead of allowing the explicit executor to bypass
    path policy. Without a project path (pure test injection), None still
    means allow.
    """
    if delegate is None:
        if project_path is not None:
            return (
                _error_result(
                    call,
                    "delegate unavailable with project path; refusing to run explicit executor",
                ),
                False,
                None,
            )
        return None
    try:
        handles = bool(delegate.handles(call.name))
    except Exception:
        return (
            _error_result(call, "policy check unavailable; refusing to run executor"),
            False,
            None,
        )
    if not handles:
        return None
    try:
        if isinstance(delegate, PolicyCheckingDelegate):
            decision = delegate.check_policy(call)
            if decision.denied:
                return (
                    policy_denial_result(call, decision.message, reason_code=decision.reason_code),
                    False,
                    None,
                )
    except Exception as exc:
        # Fail closed: a policy-check outage never authorizes the
        # explicit executor. The executor is not invoked.
        return (
            _error_result(call, f"policy check unavailable; refusing to run {call.name or '?'}: {exc}"),
            False,
            None,
        )
    return None


def _run_via_delegate_or_fn(
    delegate: ToolDelegate | None,
    runnable: Mapping[str, ToolExecutor],
    session: TaskSession,
    call: ToolCall,
    name: str,
    *,
    active_turn: int,
    tool_index: int,
    project_path: Path | None = None,
) -> tuple[ToolResult, bool, int | None]:
    """Execute via explicit executor first, delegate as fallback.

    An injected ``executors`` entry wins so tests (and custom harnesses)
    are never silently ignored when a real ``project_path`` creates a
    delegate. Production project runs pass no executors, so the delegate
    still owns real tools. Injected fakes still pass the delegate's project
    guards (path traversal, policy deny) so safety is never bypassed.
    """
    fn = runnable.get(name)
    if fn is not None:
        denial = _explicit_policy_denial(delegate, call, project_path=project_path)
        if denial is not None:
            return denial
        try:
            produced = _structured_executor_result(fn(call))
        except Exception as exc:
            return _error_result(call, str(exc) or "tool failed"), False, None
        if produced is None:
            return _error_result(call, "executor must return a structured ToolResult"), False, None
        result = _consistent_tool_result(call, produced)
        result, ok = _normalize_explicit_result(name, result)
        exit_code = strict_exit_code_or_none(result.audit.get("exit_code")) if name == "run" else None
        return result, ok, exit_code
    if delegate is not None and delegate.handles(name):
        result, ok, exit_code = delegate.execute(
            call,
            turn=active_turn,
            tool_index=tool_index,
        )
        runtime_guard = name == "run" and isinstance(result, PolicyDeniedResult)
        result = _consistent_tool_result(call, result, runtime_guard=runtime_guard)
        result, ok, exit_code = _normalize_delegate_result(name, result, ok, exit_code)
        return result, ok, exit_code
    return _error_result(call, f"unknown tool executor: {name or '?'}"), False, None


def execute_tool_operation(
    session: TaskSession, call: ToolCall, *, delegate: ToolDelegate | None,
    runnable: Mapping[str, ToolExecutor], turn: int, tool_index: int, project_path: Path | None,
    workspace_revision_store: WorkspaceRevisionStore | None = None,
    snapshot: TurnSnapshot | None = None,
    workspace_ignored_paths: tuple[str, ...] = (),
) -> tuple[ToolResult, bool, int | None]:
    """The same business operation and normalization for inline and owned work."""
    from codey.operations.project.operation_evidence import attach_operation_evidence, prepare_operation

    prepared = prepare_operation(session, call, project_path=project_path, snapshot=snapshot)
    if isinstance(prepared, ToolResult):
        return prepared, False, None
    result, ok, code = _run_via_delegate_or_fn(delegate, runnable, session, replace(call, args=prepared.args), call.name,
        active_turn=turn, tool_index=tool_index, project_path=project_path)
    result = attach_operation_evidence(session, call, result, prepared, ok=ok, exit_code=code,
        project_path=project_path, workspace_revision_store=workspace_revision_store,
        workspace_ignored_paths=workspace_ignored_paths)
    return result, ok, code


def _structured_executor_result(value: object) -> ToolResult | None:
    """Injected executors are runtime boundaries even when statically annotated."""
    return value if isinstance(value, ToolResult) else None
