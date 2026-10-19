from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from codey.codemode.catalog import build_catalog_snapshot, build_read_only_catalog
from codey.codemode.host import CodeModeHost
from codey.codemode.output import externalize_value
from codey.codemode.protocol import parse_codemode_args
from codey.codemode.runtime import CodeModeRuntime, NodeWorkerRuntime
from codey.codemode.types import CodeModeLimits, ToolCatalogEntry, ToolCatalogSnapshot
from codey.runtime.core.models import ToolCall, ToolResult


class CodeModeKernelExecutor:
    """Execute a parent codemode call while routing every child to the kernel."""

    def __init__(
        self,
        *,
        execution_id: str,
        catalog: Sequence[Mapping[str, object] | ToolCatalogEntry] | ToolCatalogSnapshot,
        execute_child: Callable[[ToolCall], ToolResult],
        runtime: CodeModeRuntime | None = None,
        limits: CodeModeLimits | None = None,
        artifact_store: Any = None,
        session_id: str = "",
        run_id: str = "",
        max_inline_bytes: int = 200_000,
    ) -> None:
        self.execution_id = str(execution_id or "").strip()
        if not self.execution_id:
            raise ValueError("execution_id must be non-empty")
        self.catalog = catalog if isinstance(catalog, ToolCatalogSnapshot) else build_catalog_snapshot(catalog)
        if any(not entry.read_only for entry in self.catalog.entries):
            raise ValueError("CodeMode first version accepts read-only tools only")
        self.execute_child = execute_child
        self.runtime = runtime or NodeWorkerRuntime()
        self.limits = limits or CodeModeLimits()
        self.artifact_store = artifact_store
        self.session_id = session_id
        self.run_id = run_id
        if isinstance(max_inline_bytes, bool) or not isinstance(max_inline_bytes, int) or max_inline_bytes <= 0:
            raise ValueError("max_inline_bytes must be a positive integer")
        self.max_inline_bytes = max_inline_bytes

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            program = parse_codemode_args(call.args, limits=self.limits)
        except ValueError as exc:
            return ToolResult(ok=False, call=call, model_text=str(exc), canonical={"error": str(exc)})
        host = CodeModeHost(
            execution_id=self.execution_id,
            catalog=self.catalog.entries,
            execute=self.execute_child,
            transform_value=lambda value: externalize_value(
                value,
                store=self.artifact_store,
                session_id=self.session_id,
                run_id=self.run_id,
                max_inline_bytes=self.max_inline_bytes,
            ),
        )
        result = self.runtime.execute(
            program.code,
            catalog=self.catalog.entries,
            invoke=host.invoke,
            limits=self.limits,
        )
        audit = {
            "codemode_execution_id": self.execution_id,
            "catalog_digest": self.catalog.digest,
            "child_calls": result.child_calls,
        }
        if not result.ok:
            return ToolResult(ok=False, call=call, model_text=result.error or "codemode execution failed", audit=audit)
        try:
            value = externalize_value(
                result.value,
                store=self.artifact_store,
                session_id=self.session_id,
                run_id=self.run_id,
                max_inline_bytes=self.max_inline_bytes,
            )
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, OverflowError) as exc:
            return ToolResult(ok=False, call=call, model_text=str(exc), audit=audit)
        return ToolResult(ok=True, call=call, model_text=text, audit=audit, canonical={"value": value})


def build_execute_turn_executor(
    *,
    session: Any,
    snapshot: Any,
    run_id: object,
    effect_scope: str,
    turn: int,
    project_path: Any = None,
    tool_fns: Any = None,
    research_tools: Any = None,
    change_tracker: Any = None,
    managed_outputs: Any = None,
    session_id: str = "",
    permission_profile: str = "coding_writer",
    delivered: Mapping[str, ToolResult] | None = None,
    intent_sink: Any = None,
    controller_allowed: Any = None,
    execution_evidence: Any = None,
    workspace_ignored_paths: Any = (),
    workspace_revision_store: Any = None,
    stop_flag: Any = None,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
    limits: CodeModeLimits | None = None,
) -> Callable[[ToolCall], ToolResult]:
    """Build the production parent executor; every child re-enters execute_turn."""
    from codey.operations.kernel_execution import execute_turn

    frozen_specs = getattr(snapshot, "frozen_specs", ()) or ()
    catalog = build_read_only_catalog({str(getattr(spec, "name", "")): spec for spec in frozen_specs})
    child_executors = {key: value for key, value in dict(executors or {}).items() if key != "codemode"}

    def run_parent(call: ToolCall) -> ToolResult:
        digest = hashlib.sha256(
            json.dumps(call.args, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        parent_id = f"{run_id}:turn:{turn}:codemode:{call.call_id or digest}"
        child_scope = f"{effect_scope}:codemode:{digest}"

        def execute_child(child: ToolCall) -> ToolResult:
            try:
                child_index = int(child.call_id.rsplit(":child:", 1)[1])
            except (IndexError, ValueError):
                child_index = 0
            child_effect_scope = f"{child_scope}:child:{child_index}"
            results = execute_turn(
                session,
                [child],
                executors=child_executors,
                run_id=run_id,
                effect_scope=child_effect_scope,
                turn=turn,
                tool_index_base=child_index,
                project_path=project_path,
                tool_fns=tool_fns,
                research_tools=research_tools,
                change_tracker=change_tracker,
                managed_outputs=managed_outputs,
                session_id=session_id,
                permission_profile=permission_profile,
                delivered=delivered,
                intent_sink=intent_sink,
                controller_allowed=controller_allowed,
                execution_evidence=execution_evidence,
                workspace_ignored_paths=workspace_ignored_paths,
                workspace_revision_store=workspace_revision_store,
                stop_flag=stop_flag,
                snapshot=snapshot,
            )
            if not results:
                raise RuntimeError("nested kernel returned no tool result")
            return results[0]

        return CodeModeKernelExecutor(
            execution_id=parent_id,
            catalog=catalog,
            execute_child=execute_child,
            limits=limits,
            artifact_store=managed_outputs,
            session_id=session_id,
            run_id=str(run_id or ""),
        ).execute(call)

    return run_parent
