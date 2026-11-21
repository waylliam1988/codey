from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from codey.codemode.catalog import build_catalog_snapshot, snapshot_catalog
from codey.codemode.host import CodeModeHost
from codey.codemode.identity import child_call_index, parent_execution_id
from codey.codemode.output import externalize_value
from codey.codemode.protocol import CodeModeInputError, parse_codemode_args
from codey.codemode.replay import CodeModeRepairContext, DurableCodeModeReplay, ExecutionTrace, digest
from codey.codemode.runtime import CodeModeRuntime, NodeWorkerRuntime
from codey.codemode.state import CodeModeStateStore
from codey.codemode.types import CodeModeLimits, DeliveryReference, ToolCatalogEntry, ToolCatalogSnapshot
from codey.operations.browser_references import BrowserReferences
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import ToolSpec
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.agents.request import ChangeTracker
    from codey.agents.tools import AgentToolFns
    from codey.automation.browser_session import BrowserSession
    from codey.durable import DurableStore
    from codey.durable.session import DurableSession
    from codey.operations.kernel_contracts import StopSignal, ToolExecutor
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.research.tools import ResearchTools
    from codey.runtime.observe.events import RunEvent
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.workspace.revision import WorkspaceRevisionStore


def _settled_child_description(ref: str, receipt: ToolResult) -> str:
    description = f"{ref} ({receipt.call.name}, ok={receipt.ok})"
    if receipt.kind == "accepted":
        description += f" admission={receipt.model_text}"
    return description


def _simplified_failure(result: ToolResult, spec: ToolSpec, children: Mapping[str, ToolResult]) -> ToolResult:
    from codey.operations.browser_copy import EXPIRED, captured_data, write_unconfirmed
    from codey.toolchain.constants import STALE_READ_REFERENCE_CODE
    from codey.toolchain.tool_prompt import tool_repair_text

    detail = "The fixed operation did not complete."
    audit = {**result.audit, "simplified_input": True}
    error_code = audit.get("error_code")
    if error_code in {"syntax_error", "missing_return", "result_encoding", "result_contract"}:
        audit["template_error"] = result.model_text
        detail = f"The fixed runtime template failed ({error_code}); the model cannot repair its JavaScript."
    else:
        audit["runtime_error"] = result.model_text
    if children:
        detail += "\nSettled child receipts (the parent failure does not undo their effects): " + ", ".join(children)
        edits = [child for child in children.values() if child.call.name == "edit"]
        if any(write_unconfirmed(child) for child in edits):
            detail += ("\nThe file write already occurred, but workspace identity is unconfirmed. "
                       "Do not repeat the write. Copying is blocked pending a check of the file and workspace identity.")
        elif any(child.ok for child in edits):
            detail += "\nThe file write completed. Do not repeat the write."
        elif edits and all(not child.ok and child.audit.get("error_code") == STALE_READ_REFERENCE_CODE for child in edits):
            detail += ("\nThe file write was rejected before writing because its file version changed. "
                       "Retry with the same source; the fixed program reads the current file before editing.")
        elif edits and all(not child.ok and child.audit.get("error_code") == EXPIRED for child in edits):
            detail += "\nThe file write was rejected: the browser source expired. Observe the authorized page again."
        elif any(captured_data(child) is not None for child in children.values()):
            detail += "\nThe observation completed. Continue from the committed observation; do not fetch the source again."
        else:
            detail += ("\nThe receipts do not establish a completed write or a safely retryable rejection. "
                       "Do not replay the whole operation while delivery is uncertain.")
        detail += "\nThe next request derives its available action from these committed receipts."
    else:
        detail += "\nNo settled child receipt is available; this alone does not prove that no effect occurred."
    return replace(result, model_text=tool_repair_text(detail, spec), audit=audit)


class CodeModeReplay(CodeModeStateStore, Protocol):
    @property
    def context(self) -> Mapping[str, object]: ...

    def assert_valid(self) -> None: ...

    def admission(self) -> AbstractContextManager[None]: ...

    @property
    def delivery(self) -> Sequence[DeliveryReference]: ...

    def record_delivery(self, call_id: str, child_index: int | None) -> None: ...

    def child(self, call: ToolCall, effect_id: str) -> None: ...


def _runtime_contract(runtime: CodeModeRuntime, limits: CodeModeLimits) -> str:
    limit_payload: dict[str, int] = asdict(limits)
    return digest({"runtime": runtime.contract_digest(), "limits": limit_payload})


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
        artifact_store: ManagedOutputStore | None = None,
        session_id: str = "",
        run_id: str = "",
        max_inline_bytes: int = 200_000,
        allow_writes: bool = False,
        state_store: CodeModeStateStore | None = None,
        replay: CodeModeReplay | None = None,
        repair_context: CodeModeRepairContext | None = None,
        runtime_contract: str | None = None,
        task_navigation_url: str | None = None,
        browser_references: BrowserReferences | None = None,
        bound_edit: Callable[[int, JsonObject], JsonObject] | None = None,
        browser_stage: Callable[[], int] | None = None,
        original_browser_wait: Callable[[int], JsonObject | None] | None = None,
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
        self.repair_context = repair_context
        self.runtime_contract = runtime_contract
        self.task_navigation_url = task_navigation_url
        self.browser_references = browser_references
        self.bound_edit = bound_edit
        self.browser_stage = browser_stage
        self.original_browser_wait = original_browser_wait

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            program = parse_codemode_args(call.args, limits=self.limits)
        except ValueError as exc:
            return ToolResult(ok=False, call=call, model_text=str(exc), canonical={"error": str(exc)},
                              audit={"error_code": exc.code if isinstance(exc, CodeModeInputError) else "invalid_args",
                                     "child_calls": 0})
        audit: dict[str, object] = {
            "codemode_execution_id": self.execution_id,
            "catalog_digest": self.catalog.digest,
        }
        repair = self.repair_context

        def replay_child(index: int, name: str, args: JsonObject) -> ToolResult:
            assert repair is not None
            receipt = repair.replay_child(index, name, args)
            if self.replay is not None:
                child = next(child for child in repair.children if child.position == index)
                self.replay.child(receipt.call, child.effect_id)
            return receipt

        host = CodeModeHost(
            execution_id=self.execution_id,
            catalog=self.catalog.entries,
            execute=self.execute_child,
            allow_writes=self.allow_writes,
            replay=replay_child if repair is not None else None,
            task_navigation_url=self.task_navigation_url,
            browser_references=self.browser_references,
            replay_browser_ref=repair.resolve_browser_ref if repair is not None else None,
            browser_stage=self.browser_stage,
            original_browser_wait=repair.original_wait_args if repair is not None else self.original_browser_wait,
            bound_edit=self.bound_edit,
        )
        trace: ExecutionTrace | None = None

        def record_delivery(call_id: str, child_index: int | None) -> None:
            assert trace is not None
            trace["delivery"].append({"call_id": call_id, "child_index": child_index})

        context: Mapping[str, object]
        delivery: Sequence[DeliveryReference]
        on_delivery: Callable[[str, int | None], None]
        if self.replay is not None:
            context, delivery, on_delivery = self.replay.context, self.replay.delivery, self.replay.record_delivery
        elif repair is not None:
            context, delivery, on_delivery = repair.context, repair.delivery, repair.record_delivery
        else:
            trace = {"epoch_ms": int(time.time() * 1000), "seed": secrets.randbits(32),
                     "runtime_contract": self.runtime_contract or _runtime_contract(self.runtime, self.limits),
                     "delivery": []}
            context = {"epochMs": trace["epoch_ms"], "seed": trace["seed"], "executionId": self.execution_id}
            delivery, on_delivery = (), record_delivery
        try:
            state_store = self.state_store if self.state_store is not None else self.replay
            result = self.runtime.execute(
                program.code,
                inputs=program.inputs,
                catalog=self.catalog.entries,
                invoke=host.invoke,
                limits=self.limits,
                state_store=state_store,
                replay_context=context,
                delivery=delivery,
                on_delivery=on_delivery,
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
            if trace is not None:
                audit["codemode_replay"] = trace
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
            text = json.dumps(dict[str, object](ok=True, value=value), ensure_ascii=False,
                              sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            return ToolResult(ok=False, call=call, model_text=str(exc), audit=audit)
        return ToolResult(ok=True, call=call, model_text=text, audit=audit, canonical={"value": value})


def build_execute_turn_executor(  # noqa: C901, PLR0915
    *,
    session: TaskSession,
    snapshot: TurnSnapshot,
    run_id: str,
    effect_scope: str,
    turn: int,
    project_path: Path | None = None,
    tool_fns: AgentToolFns | None = None,
    research_tools: ResearchTools | None = None,
    change_tracker: ChangeTracker | None = None,
    managed_outputs: ManagedOutputStore | None = None,
    session_id: str = "",
    permission_profile: str = "coding_writer",
    delivered: Mapping[str, ToolResult] | None = None,
    intent_sink: KernelEffectSink | None = None,
    controller_allowed: Collection[str] | None = None,
    execution_evidence: ExecutionEvidence | None = None,
    workspace_ignored_paths: tuple[str, ...] = (),
    workspace_revision_store: WorkspaceRevisionStore | None = None,
    stop_flag: StopSignal | None = None,
    executors: Mapping[str, ToolExecutor] | None = None,
    limits: CodeModeLimits | None = None,
    durable: DurableStore | DurableSession | None = None,
    allow_writes: bool = False,
    state_store: CodeModeStateStore | None = None,
    on_event: Callable[[RunEvent], None] | None = None,
    runtime_factory: Callable[[], CodeModeRuntime] | None = None,
    browser_session: BrowserSession | None = None,
) -> Callable[[ToolCall], ToolResult]:
    """Build the production parent executor; every child re-enters execute_turn."""
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_tool_args import _validate_tool_args
    from codey.operations.task_effects import KernelEffectSink

    specs = {spec.name: spec for spec in snapshot.frozen_specs}
    catalog = snapshot_catalog(snapshot, allow_writes=allow_writes)
    # Observed choices describe this turn's controls; replay authorization uses
    # the frozen execution contracts and the original child arguments instead.
    repair_catalog = snapshot_catalog(snapshot, allow_writes=allow_writes, observed_choices=False)
    # The catalog has already selected project writes and browser capabilities
    # independently. Host mutation support is not a project permission grant.
    allow_mutations = any(not entry.read_only for entry in catalog.entries)
    child_executors = {key: value for key, value in dict(executors or {}).items() if key != "codemode"}
    effective_session_id = session_id
    if not effective_session_id and isinstance(intent_sink, KernelEffectSink):
        effective_session_id = intent_sink.session_id

    def prepare_replay(
        call: ToolCall, parent_id: str, child_sink: KernelEffectSink | None,
        repair: CodeModeRepairContext | None, runtime_contract: str,
    ) -> DurableCodeModeReplay | None:
        if durable is None:
            if repair is not None:
                repair.bind_trace(repair.parent.audit.get("codemode_replay"), runtime_contract)
            return None
        from codey.durable.session import DurableSession

        if not isinstance(durable, DurableSession):
            raise TypeError("durable CodeMode requires a DurableSession")
        if not isinstance(child_sink, KernelEffectSink):
            raise ValueError("durable CodeMode requires the original effect sink")
        if repair is not None:
            return DurableCodeModeReplay.for_repair(durable, run_id, repair, runtime_contract)
        fingerprint: dict[str, object] = {"args": call.args, "catalog": catalog.digest, "allow_writes": allow_writes,
            "task_navigation_url": snapshot.task_navigation_url,
            "browser_references": snapshot.browser_references.payload() if snapshot.browser_references else None,
            "runtime": runtime_contract}
        if snapshot.input_binding is not None and snapshot.input_binding.source_refs:
            fingerprint["bound_sources"] = snapshot.input_binding.source_refs
        return DurableCodeModeReplay(durable, run_id, parent_id, digest(fingerprint),
            parent_effect_id=child_sink.parent_effect_id, effect_scope=effect_scope, allow_writes=allow_mutations,
            runtime_contract=runtime_contract)

    def run_parent(call: ToolCall) -> ToolResult:  # noqa: C901, PLR0915
        from codey.operations.task_session import turn_effect_id

        child_effect_ids_by_index: dict[int, str] = {}

        def child_effect_ids() -> list[str]:
            """Return child receipts in the RPC call order, not completion order."""
            return [child_effect_ids_by_index[index] for index in sorted(child_effect_ids_by_index)]
        parent_id = parent_execution_id(run_id, turn, call)
        child_scope = f"{effect_scope}:codemode:{parent_id}"
        repair_context = None
        repair_token_value = call.args.get("repair_of")
        if isinstance(repair_token_value, str) and repair_token_value:
            from codey.codemode.replay import CodeModeReplayError, resolve_repair_context

            try:
                repair_context = resolve_repair_context(
                    session, repair_token_value, session_id=effective_session_id,
                    run_id=str(run_id or ""), catalog=repair_catalog,
                )
            except CodeModeReplayError as exc:
                return ToolResult(
                    ok=False, call=call, model_text=str(exc),
                    audit={"error_code": "repair_rejected"}, canonical={"child_effect_ids": []},
                )
            child_effect_ids_by_index.update(
                (child.position, child.effect_id) for child in repair_context.children
            )
        child_sink = intent_sink.for_child(call, turn=turn) if isinstance(intent_sink, KernelEffectSink) else intent_sink
        runtime = (runtime_factory or NodeWorkerRuntime)()
        runtime_contract = _runtime_contract(runtime, limits or CodeModeLimits())
        try:
            replay = prepare_replay(call, parent_id, child_sink, repair_context, runtime_contract)
        except ValueError as exc:
            if repair_context is None:
                raise
            return ToolResult(ok=False, call=call, model_text=str(exc), audit={"error_code": "repair_rejected"},
                              canonical={"child_effect_ids": child_effect_ids()})
        if replay is not None and child_sink is not None:
            child_sink.admission_guard = replay.admission

        def bound_edit(index: int, args: JsonObject) -> JsonObject:
            from codey.agents.protocol import canonical_project_path
            from codey.durable.session import DurableSession
            from codey.operations.edit_base import edit_base_text
            from codey.operations.file_field_binding import bound_receipt_content
            from codey.types.json_value import decode_json_object, require_json_string

            binding = snapshot.input_binding
            if binding is None or project_path is None or snapshot.policy is None:
                raise ValueError("bound edit has no frozen project contract")
            path = require_json_string(args.get("path"), "path")
            ref = require_json_string(args.get("base_result_ref"), "base_result_ref")
            if (set(args) not in ({"path", "base_result_ref"}, {"path", "base_result_ref", "declared_fields"})
                    or "declared_fields" in args and args["declared_fields"] is not True):
                raise ValueError("bound edit accepts only its destination and complete read reference")
            from codey.toolchain.tool_spec import validate_args_with_spec

            if error := validate_args_with_spec(binding.model_spec, {"path": path}):
                raise ValueError(error)
            identity = turn_effect_id(f"{run_id}:{child_scope}:child:{index}", turn, index)
            if repair_context is not None:
                original = next((child.effect_id for child in repair_context.children if child.position == index), "")
                receipt = repair_context.receipts.get(original)
            else:
                receipt = session._memory_results.get(identity)
            if receipt is not None:
                if (receipt.call.name != "edit" or receipt.call.args.get("path") != path
                        or receipt.call.args.get("base_result_ref") != ref):
                    raise ValueError("bound edit recovery changed its original destination or read reference")
                return dict(receipt.call.args)
            if isinstance(durable, DurableSession):
                try:
                    durable.effect(identity)
                except KeyError:
                    pass
                else:
                    raise ValueError("bound edit intent has no restorable receipt; refusing regeneration")
            canonical = canonical_project_path(project_path, path)
            baseline = edit_base_text(session, project_path, canonical, ref)
            if any(source not in session.executed or source not in session._memory_results for source in binding.source_refs):
                raise ValueError("bound edit source receipts are unavailable")
            records = {source: session.executed[source] for source in binding.source_refs}
            results = {source: session._memory_results[source] for source in binding.source_refs}
            documents = [row.audit.get("browser_document") for row in results.values() if row.call.name == "browser_read"]
            document = decode_json_object(documents[-1]) if documents else None
            content = bound_receipt_content(snapshot.policy.required_checks, canonical, baseline, records, results, document)
            return {"path": path, "content": content, "base_result_ref": ref}

        def execute_child(child: ToolCall) -> ToolResult:
            from codey.operations.kernel_events import _event_call
            from codey.runtime.observe.events import RunEvent
            from codey.toolchain.runtime import ToolOutcome
            if (child.name == "edit" and snapshot.input_binding is None and project_path is not None
                    and snapshot.policy is not None):
                from codey.agents.protocol import canonical_project_path
                from codey.operations.declared_field_input import free_program_required
                from codey.operations.file_field_binding import file_rules

                path = child.args.get("path")
                rules = (file_rules(snapshot.policy.required_checks, canonical_project_path(project_path, path))
                         if isinstance(path, str) else ())
                if (rules and not free_program_required(session.task_text) and any(
                        session.executed[ref]["name"] in {rule.tool for rule in rules}
                        for ref in child_effect_ids_by_index.values())):
                    raise ValueError("The declared file source was acquired in this program. Return its evidence first. "
                                     "The next request supplies the frozen destination-only copy input; "
                                     "do not parse, convert or write the declared values in this program.")
            # Kernel argument rejection creates no intent or receipt. Validate
            # before recording a child that durable replay must account for.
            _, error = _validate_tool_args(child.name, child.args, frozen_specs=specs)
            if error:
                raise ValueError(error)
            if on_event is not None:
                event = RunEvent.tool_started(turn, _event_call(session, child), child.name)
                event.metadata['parent_call_id'] = parent_id
                event.metadata['tool_name'] = child.name
                on_event(event)
            child_index = child_call_index(parent_id, child.call_id)
            child_effect_scope = f"{child_scope}:child:{child_index}"
            child_identity = turn_effect_id(f"{run_id}:{child_effect_scope}", turn, child_index)
            if replay is not None:
                replay.child(child, child_identity)
            results = execute_child_turn(child, child_effect_scope, child_index)
            if not results:
                raise RuntimeError("nested kernel returned no tool result")
            if replay is not None and results[0].audit.get("recovery_disposition") in {"failed", "mismatch"}:
                reason = "CodeMode child recovery could not verify its original receipt"
                replay.invalidate(reason)
                raise ValueError(reason)
            if child_identity not in session.executed:
                raise RuntimeError(f"nested kernel result was not committed: {results[0].model_text}")
            if child_index in child_effect_ids_by_index and child_effect_ids_by_index[child_index] != child_identity:
                raise RuntimeError("CodeMode child identity was reused for a different receipt")
            child_effect_ids_by_index[child_index] = child_identity
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

        def execute_child_turn(child: ToolCall, child_effect_scope: str, child_index: int) -> list[ToolResult]:
            return execute_turn(
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
                browser_session=browser_session,
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

        repair_state_store = None
        if repair_context is not None:
            base_state_store = replay if replay is not None else state_store
            if base_state_store is not None:
                repair_state_store = repair_context.readonly_state(base_state_store)
        def browser_stage() -> int:
            from codey.operations.browser_completion import browser_click_stage
            from codey.operations.browser_followups import query_document

            browser = browser_session or (research_tools.browser_session if research_tools is not None else None)
            document = browser.document_identity() if browser is not None else query_document(session._memory_results)
            return browser_click_stage(session.executed, session._memory_results, document)

        def original_browser_wait(index: int) -> JsonObject | None:
            identity = turn_effect_id(f"{run_id}:{child_scope}:child:{index}", turn, index)
            receipt = session._memory_results.get(identity)
            return receipt.call.args if receipt is not None and receipt.call.name == "browser_wait" else None

        result = CodeModeKernelExecutor(
            execution_id=parent_id,
            catalog=catalog,
            execute_child=execute_child,
            runtime=runtime,
            limits=limits,
            allow_writes=allow_mutations,
            state_store=repair_state_store if repair_context is not None else state_store,
            replay=replay,
            repair_context=repair_context,
            runtime_contract=runtime_contract,
            artifact_store=managed_outputs,
            session_id=session_id,
            run_id=str(run_id or ""),
            task_navigation_url=snapshot.task_navigation_url,
            browser_references=snapshot.browser_references,
            browser_stage=browser_stage,
            original_browser_wait=original_browser_wait,
            bound_edit=bound_edit if snapshot.input_binding is not None
                and snapshot.input_binding.kind == "declared_fields" and snapshot.input_binding.source_refs else None,
        ).execute(call)
        if repair_context is not None:
            try:
                repair_context.assert_complete()
            except ValueError as exc:
                return ToolResult(
                    ok=False, call=call,
                    model_text=repair_context.failure or (result.model_text if not result.ok
                        and result.audit.get("error_code") != "delivery_journal" else str(exc)),
                    audit={**result.audit, "error_code": "repair_rejected"},
                    canonical={"child_effect_ids": child_effect_ids()},
                )
            result = replace(
                result,
                audit={**result.audit, "recovery_disposition": "receipt_replay",
                       "repaired_from": repair_context.parent_effect_id},
                canonical={**result.canonical, "repaired_from": repair_context.parent_effect_id},
            )
        # References only: child facts stay in their original committed receipts.
        ordered_child_effect_ids = child_effect_ids()
        result = replace(result, canonical={**result.canonical, "child_effect_ids": ordered_child_effect_ids})
        if not result.ok and result.audit.get("error_code") in {"missing_return", "result_encoding", "execution_error", "result_contract"}:
            from codey.codemode.shape_repair import readonly_settled, shape_diagnostic

            source = call.args.get("code")
            readonly = readonly_settled(parent_id, ordered_child_effect_ids, session._memory_results, catalog)
            if (isinstance(source, str) and result.audit.get("child_calls") == len(ordered_child_effect_ids)
                    and (diagnostic := shape_diagnostic(source, catalog, receipt_replay=readonly))):
                result = replace(result, audit={**result.audit, **({"repair_category": "shape_mismatch"} if readonly else {})},
                                 model_text=result.model_text + "\n" + diagnostic)
        if not result.ok:
            from codey.toolchain.result_schema import schema_typescript
            from codey.toolchain.tool_prompt import tool_repair_text

            detail = result.model_text
            if ordered_child_effect_ids:
                detail += "\nSettled child receipts (the parent failure does not undo them): " + ", ".join(
                    _settled_child_description(ref, session._memory_results[ref])
                    for ref in ordered_child_effect_ids)
                detail += "\nContinue only the missing work. Do not resubmit the original program to repeat completed actions."
            if result.audit.get("error_code") == "syntax_error":
                detail += (
                    "\nSyntax repair required: submit one new syntactically valid CodeMode program; "
                    "do not repeat the rejected source."
                )
            if result.audit.get("error_code") == "result_contract":
                for ref in ordered_child_effect_ids:
                    receipt = session._memory_results.get(ref)
                    if (receipt is not None and receipt.ok and receipt.call.name == "browser_download"
                            and "value" in receipt.canonical):
                        args = json.dumps(dict[str, str](result_ref=ref, view="value"))
                        detail += ("\nBrowser download already completed. Recover the path and SHA256 with "
                                   f"read_tool_result({args}). Do not repeat the download.")
            if (result.audit.get("error_code") in {"missing_return", "result_encoding"}
                    or result.audit.get("repair_category") == "shape_mismatch") and ordered_child_effect_ids:
                names = sorted({session.executed[ref]["name"] for ref in ordered_child_effect_ids})
                detail += "\nInvoked tool return types (Result<T> success data is result.value):\n" + "\n".join(
                    f"tools.{name}: Result<{schema_typescript(specs[name].output_schema)}>" for name in names)
                detail += "\nCommitted child receipts (effects already executed): " + ", ".join(ordered_child_effect_ids)
                from codey.codemode.replay import browser_file_replay_value, repair_catalog_digest, repair_metadata
                child_names = [session.executed[ref]["name"] for ref in ordered_child_effect_ids]
                metadata = repair_metadata(
                    effective_session_id, parent_id, str(result.audit.get("repair_category", result.audit["error_code"])), ordered_child_effect_ids,
                    repair_catalog_digest(repair_catalog, child_names),
                )
                result = replace(result, canonical={**result.canonical, "repair": metadata})
                if durable is not None and repair_context is None:
                    value = browser_file_replay_value(session, result, str(metadata["token"]),
                        session_id=effective_session_id, run_id=str(run_id or ""), catalog=repair_catalog)
                    if value is not None:
                        audit = dict(result.audit)
                        audit["original_error_code"] = audit.pop("error_code")
                        audit["recovery_disposition"] = "receipt_replay"
                        return replace(result, ok=True, audit=audit,
                            model_text=json.dumps(dict[str, object](ok=True, value=value), ensure_ascii=False,
                                                  sort_keys=True, separators=(",", ":")),
                            canonical={"value": value, "child_effect_ids": ordered_child_effect_ids,
                                       "recovered_return": True})
                detail += ". Correct only value handling and the return expression. Use repair_of=" + str(metadata["token"])
                detail += " to replay these settled child receipts; do not repeat the child calls."
            result = (_simplified_failure(result, snapshot.input_binding.model_spec,
                                         {ref: session._memory_results[ref] for ref in ordered_child_effect_ids})
                      if snapshot.input_binding is not None
                      else replace(result, model_text=tool_repair_text(detail, specs["codemode"])))
        return result

    return run_parent
