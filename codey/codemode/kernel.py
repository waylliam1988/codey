from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from typing import Any

from codey.codemode.catalog import build_catalog_snapshot, build_codemode_catalog
from codey.codemode.host import CodeModeHost
from codey.codemode.identity import parent_execution_id
from codey.codemode.output import externalize_value
from codey.codemode.protocol import parse_codemode_args
from codey.codemode.runtime import CodeModeRuntime, NodeWorkerRuntime
from codey.codemode.types import CodeModeLimits, ToolCatalogEntry, ToolCatalogSnapshot
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject


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
        allow_writes: bool = False,
        state_store: Any = None,
        replay: Any = None,
        replay_child: Callable[[int, str, JsonObject], ToolResult] | None = None,
    ) -> None:
        self.execution_id = str(execution_id or "").strip()
        if not self.execution_id:
            raise ValueError("execution_id must be non-empty")
        self.catalog = catalog if isinstance(catalog, ToolCatalogSnapshot) else build_catalog_snapshot(catalog)
        if any(not entry.read_only for entry in self.catalog.entries) and not allow_writes:
            raise ValueError("CodeMode write tools require allow_writes=True")
        self.execute_child = execute_child
        self.runtime = runtime or NodeWorkerRuntime()
        self.limits = limits or CodeModeLimits()
        self.artifact_store = artifact_store
        self.session_id = session_id
        self.run_id = run_id
        if isinstance(max_inline_bytes, bool) or not isinstance(max_inline_bytes, int) or max_inline_bytes <= 0:
            raise ValueError("max_inline_bytes must be a positive integer")
        self.max_inline_bytes = max_inline_bytes
        self.allow_writes = bool(allow_writes)
        self.state_store = state_store
        self.replay = replay
        self.replay_child = replay_child

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            program = parse_codemode_args(call.args, limits=self.limits)
        except ValueError as exc:
            return ToolResult(ok=False, call=call, model_text=str(exc), canonical={"error": str(exc)})
        audit: dict[str, object] = {
            "codemode_execution_id": self.execution_id,
            "catalog_digest": self.catalog.digest,
        }
        host = CodeModeHost(
            execution_id=self.execution_id,
            catalog=self.catalog.entries,
            execute=self.execute_child,
            allow_writes=self.allow_writes,
            replay=self.replay_child,
        )
        try:
            result = self.runtime.execute(
                program.code,
                inputs=program.inputs,
                catalog=self.catalog.entries,
                invoke=host.invoke,
                limits=self.limits,
                state_store=self.replay if self.replay is not None else self.state_store,
                replay_context=self.replay.context if self.replay is not None else None,
            )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded) as exc:
            # The runtime has joined its host callbacks. Return through the
            # normal parent settlement so committed child references survive.
            return ToolResult(ok=False, call=call, model_text=str(exc), audit=audit)
        if self.replay is not None:
            try:
                self.replay.assert_valid()
            except ValueError as exc:
                return ToolResult(ok=False, call=call, model_text=str(exc), audit=audit)
        audit["child_calls"] = result.child_calls
        if not result.ok:
            audit["error_code"] = result.error_code
            return ToolResult(ok=False, call=call, model_text=result.error or "codemode execution failed", audit=audit)
        try:
            value = externalize_value(
                result.value,
                store=self.artifact_store,
                session_id=self.session_id,
                run_id=self.run_id,
                max_inline_bytes=self.max_inline_bytes,
            )
            text = json.dumps({"ok": True, "value": value}, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            return ToolResult(ok=False, call=call, model_text=str(exc), audit=audit)
        return ToolResult(ok=True, call=call, model_text=text, audit=audit, canonical={"value": value})


def build_execute_turn_executor(  # noqa: PLR0915
    *,
    session: Any,
    snapshot: Any,
    run_id: str,
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
    durable: Any = None,
    allow_writes: bool = False,
    state_store: Any = None,
    on_event: Callable[[Any], None] | None = None,
    runtime_factory: Callable[[], CodeModeRuntime] | None = None,
) -> Callable[[ToolCall], ToolResult]:
    """Build the production parent executor; every child re-enters execute_turn."""
    from codey.operations.kernel_execution import execute_turn

    frozen_specs = getattr(snapshot, "frozen_specs", ()) or ()
    specs = {str(getattr(spec, "name", "")): spec for spec in frozen_specs}
    catalog = build_codemode_catalog(
        {name: spec for name, spec in specs.items() if name in snapshot.nested_tool_names},
        allow_writes=allow_writes,
    )
    child_executors = {key: value for key, value in dict(executors or {}).items() if key != "codemode"}

    def run_parent(call: ToolCall) -> ToolResult:
        from codey.operations.task_session import turn_effect_id

        child_effect_ids: list[str] = []
        parent_id = parent_execution_id(run_id, turn, call)
        child_scope = f"{effect_scope}:codemode:{parent_id}"
        repair_context = None
        repair_token_value = call.args.get("repair_of")
        if isinstance(repair_token_value, str) and repair_token_value:
            from codey.codemode.replay import CodeModeReplayError, resolve_repair_context

            try:
                repair_context = resolve_repair_context(
                    session, repair_token_value, run_id=str(run_id or ""), catalog=catalog,
                )
            except CodeModeReplayError as exc:
                return ToolResult(
                    ok=False, call=call, model_text=str(exc),
                    audit={"error_code": "repair_rejected"}, canonical={"child_effect_ids": []},
                )
            child_effect_ids.extend(child.effect_id for child in repair_context.children)
        from codey.operations.task_effects import KernelEffectSink

        child_sink = intent_sink.for_child(call, turn=turn) if isinstance(intent_sink, KernelEffectSink) else intent_sink
        runtime = (runtime_factory or NodeWorkerRuntime)()
        replay = None
        if durable is not None:
            if not isinstance(child_sink, KernelEffectSink):
                raise ValueError("durable CodeMode requires the original effect sink")
            from codey.codemode.replay import DurableCodeModeReplay, digest
            replay = DurableCodeModeReplay(durable, str(run_id), parent_id,
                digest({"args": call.args, "catalog": catalog.digest, "allow_writes": allow_writes,
                        "limits": asdict(limits or CodeModeLimits()),
                        "runtime": runtime.contract_digest()}),
                parent_effect_id=child_sink.parent_effect_id, effect_scope=effect_scope, allow_writes=allow_writes)
            child_sink.admission_guard = replay.admission

        def execute_child(child: ToolCall) -> ToolResult:
            from codey.operations.kernel_events import _event_call
            from codey.runtime.observe.events import RunEvent
            from codey.toolchain.runtime import ToolOutcome
            if on_event is not None:
                event = RunEvent.tool_started(turn, _event_call(session, child), child.name)
                event.metadata['parent_call_id'] = parent_id
                event.metadata['tool_name'] = child.name
                on_event(event)
            try:
                child_index = int(child.call_id.rsplit(":child:", 1)[1])
            except (IndexError, ValueError) as exc:
                raise ValueError("invalid CodeMode child identity") from exc
            child_effect_scope = f"{child_scope}:child:{child_index}"
            child_identity = turn_effect_id(f"{run_id}:{child_effect_scope}", turn, child_index)
            if replay is not None:
                replay.child(child, child_identity)
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
                intent_sink=child_sink,
                controller_allowed=controller_allowed,
                execution_evidence=execution_evidence,
                workspace_ignored_paths=workspace_ignored_paths,
                workspace_revision_store=workspace_revision_store,
                stop_flag=stop_flag,
                snapshot=snapshot,
                durable=durable,
                nested=True,
            )
            if not results:
                raise RuntimeError("nested kernel returned no tool result")
            if replay is not None and results[0].audit.get("recovery_disposition") in {"failed", "mismatch"}:
                reason = "CodeMode child recovery could not verify its original receipt"
                replay.invalidate(reason)
                raise ValueError(reason)
            child_identity = turn_effect_id(f"{run_id}:{child_effect_scope}", turn, child_index)
            if child_identity not in session.executed:
                raise RuntimeError(f"nested kernel result was not committed: {results[0].model_text}")
            child_effect_ids.append(child_identity)
            if on_event is not None:
                result = results[0]
                outcome = ToolOutcome(model_text=result.model_text, ok=result.ok,
                                      changed=result.audit.get('changed') is True,
                                      canonical=result.canonical, audit=result.audit,
                                      presentation=result.presentation, truncated=result.truncated)
                event = RunEvent.tool_finished(turn, _event_call(session, child), outcome)
                event.metadata['parent_call_id'] = parent_id
                event.metadata['tool_name'] = child.name
                on_event(event)
            return replace(results[0], audit={**results[0].audit, "result_ref": child_identity})

        result = CodeModeKernelExecutor(
            execution_id=parent_id,
            catalog=catalog,
            execute_child=execute_child,
            runtime=runtime,
            limits=limits,
            allow_writes=allow_writes,
            state_store=state_store,
            replay=replay,
            replay_child=repair_context.replay_child if repair_context is not None else None,
            artifact_store=managed_outputs,
            session_id=session_id,
            run_id=str(run_id or ""),
        ).execute(call)
        if repair_context is not None:
            try:
                repair_context.assert_complete()
            except ValueError as exc:
                return ToolResult(
                    ok=False, call=call, model_text=str(exc),
                    audit={**result.audit, "error_code": "repair_rejected"},
                    canonical={"child_effect_ids": child_effect_ids},
                )
            result = replace(
                result,
                audit={**result.audit, "recovery_disposition": "receipt_replay",
                       "repaired_from": repair_context.parent_effect_id},
                canonical={**result.canonical, "repaired_from": repair_context.parent_effect_id},
            )
        # References only: child facts stay in their original committed receipts.
        result = replace(result, canonical={**result.canonical, "child_effect_ids": list(child_effect_ids)})
        if not result.ok:
            from codey.toolchain.result_schema import schema_typescript
            from codey.toolchain.tool_prompt import tool_repair_text

            detail = result.model_text
            if result.audit.get("error_code") in {"missing_return", "result_encoding"} and child_effect_ids:
                names = sorted({session.executed[ref]["name"] for ref in child_effect_ids})
                detail += "\nInvoked tool return types (Result<T> success data is result.value):\n" + "\n".join(
                    f"tools.{name}: Result<{schema_typescript(specs[name].output_schema)}>" for name in names)
                detail += "\nCommitted child receipts (effects already executed): " + ", ".join(child_effect_ids)
                from codey.codemode.replay import repair_catalog_digest, repair_metadata
                child_names = [session.executed[ref]["name"] for ref in child_effect_ids]
                metadata = repair_metadata(
                    parent_id, str(result.audit["error_code"]), child_effect_ids,
                    repair_catalog_digest(catalog, child_names),
                )
                result = replace(result, canonical={**result.canonical, "repair": metadata})
                detail += ". Correct only the return expression. Use repair_of=" + str(metadata["token"])
                detail += " to replay these settled child receipts; do not repeat the child calls."
            result = replace(result, model_text=tool_repair_text(detail, specs["codemode"]))
        return result

    return run_parent
