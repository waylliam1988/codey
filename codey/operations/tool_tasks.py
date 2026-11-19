"""Execute, observe and cancel owned work through the existing durable task line."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from codey.agents.tools import AgentToolFns
from codey.durable.model import (
    TASK_TERMINAL_STATUSES,
    CommitResult,
    DurableMutation,
    EffectRecord,
    TaskCheckpoint,
)
from codey.durable.session import DurableSession
from codey.durable.tool_task import ToolTaskInput, execution_effect_id
from codey.operations.kernel_receipts import restore_result_receipt, result_receipt_fields
from codey.operations.kernel_result import _error_result
from codey.operations.task_session import TaskSession
from codey.operations.task_state import TaskSubmissionStores
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.tool_dispatch import ToolDispatch
from codey.runtime.effects.effect_records import (
    RuntimeEffectIntent,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
    compute_args_digest,
    prepare_intent,
    prepare_settlement,
)
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.toolchain.runtime import ToolOutcome, project_run_command_result, run_command_raw
from codey.types.json_value import canonical_json_object, decode_json_object, require_json_integer, require_json_string


def owned_tool_task(durable: DurableSession, task_ref: str, *, owner_task_id: str) -> TaskCheckpoint:
    owner = durable.task(owner_task_id)
    child = durable.task(task_ref)
    if (child.execution_kind != "tool" or child.owner_task_id != owner.task_id
            or child.conversation_id != owner.conversation_id or child.submission_id != owner.submission_id):
        raise ValueError("tool task is outside the caller's ownership")
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    origin = durable.effect(request.origin_effect_id)
    if (origin.task_id != owner_task_id or origin.settlement is None or origin.status != "ok"
            or origin.intent.get("tool_name") != request.call_name
            or origin.args_digest != compute_args_digest({**request.args, "run_in_background": True})):
        raise ValueError("tool task has no committed original admission")
    return child


def tool_execution_result(durable: DurableSession, child: TaskCheckpoint, *, store: ManagedOutputStore | None,
                          project_path: Path | None = None) -> tuple[str, ToolResult] | None:
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    effects = durable.effects(child.task_id)
    if not effects:
        return None
    if len(effects) != 1 or effects[0].effect_id != execution_effect_id(child.task_id):
        raise ValueError("tool task has conflicting execution effects")
    effect = effects[0]
    if effect.settlement is None:
        return None
    if effect.status == "interrupted":
        return effect.effect_id, _error_result(ToolCall(request.call_name, request.args),
            "execution interrupted; external outcome and cleanup could not be confirmed; not automatically repeated")
    projection = RuntimeEffectProjection(RuntimeEffectIntent.from_payload(dict(canonical_json_object(effect.intent))),
        RuntimeEffectSettlement.from_payload(dict(canonical_json_object(effect.settlement))))
    return effect.effect_id, restore_result_receipt(projection, store=store,
        session_id=child.conversation_id, run_id=child.task_id, project_path=project_path)


@dataclass(frozen=True)
class _ToolSignal:
    durable: DurableSession
    task_id: str
    attempt_token: str
    shutdown: threading.Event
    permits_execution: Callable[[], bool] | None

    def is_set(self) -> bool:
        current = self.durable.task(self.task_id)
        return (self.shutdown.is_set() or bool(current.state.get("cancel_requested"))
                or (self.permits_execution is not None and not self.permits_execution())
                or current.attempt_token != self.attempt_token)


def run_tool_task(durable: DurableSession, captured: TaskCheckpoint, *, stores: TaskSubmissionStores,
                  signal: threading.Event, permits_execution: Callable[[], bool] | None = None) -> None:
    """One worker owns execution, output collection and cleanup for this attempt."""
    task_id = captured.task_id
    with durable.execution(task_id, captured.attempt_token):
        try:
            _resume_tool_task(durable, captured, stores=stores, signal=signal, permits_execution=permits_execution)
        except Exception as exc:
            current, effects = durable.task_facts(task_id)
            if current.attempt_token != captured.attempt_token or current.status in TASK_TERMINAL_STATUSES:
                raise
            uncertain = False
            for effect in effects:
                if effect.settlement is None:
                    durable.settle_unknown_effect(task_id, effect.effect_id)
                    uncertain = True
            if current.state.get("cancel_requested") and not uncertain:
                durable.cancel_task(task_id)
                return
            durable.fail_task(task_id, f"{type(exc).__name__}: {exc}",
                state={"tool_worker_active": False, "cleanup_unknown": uncertain})


def _resume_tool_task(durable: DurableSession, captured: TaskCheckpoint, *, stores: TaskSubmissionStores,
                      signal: threading.Event, permits_execution: Callable[[], bool] | None) -> None:
    task_id = captured.task_id
    request = ToolTaskInput.from_payload(captured.state["tool_input"])
    current, effects = durable.task_facts(task_id)
    if effects:
        # An existing intent may already have caused an external side effect.
        # Recover its result or explicitly record uncertainty; never spawn again.
        if effects[0].settlement is None:
            durable.settle_unknown_effect(task_id, effects[0].effect_id)
            durable.fail_task(task_id, "execution interrupted", state={"cleanup_unknown": True, "tool_worker_active": False})
        elif effects[0].status == "interrupted":
            durable.fail_task(task_id, "execution interrupted", state={"cleanup_unknown": True, "tool_worker_active": False})
        elif current.state.get("cancel_requested"):
            durable.cancel_task(task_id)
        else:
            durable.complete_task(task_id, "", state={"tool_worker_active": False})
        return
    if current.state.get("cancel_requested"):
        durable.cancel_task(task_id)
        return
    stop = _ToolSignal(durable, task_id, captured.attempt_token, signal, permits_execution)
    deadline = (time.monotonic() + max(0.0, request.deadline - time.time())
                if request.deadline is not None else None)
    with cancellation.linked_scope(stop), cancellation.deadline_scope(deadline):
        _run_admitted_operation(durable, captured, request, stores)


def _run_admitted_operation(durable: DurableSession, child: TaskCheckpoint, request: ToolTaskInput,
                            stores: TaskSubmissionStores) -> None:
    from codey.operations.kernel_execution import execute_tool_operation
    from codey.operations.kernel_tool_args import _validate_tool_args
    from codey.operations.provider_receipts import restore_snapshot
    from codey.operations.task_execution import ExecutionDelegate

    snapshot = restore_snapshot(request.snapshot)
    if not snapshot.implementation_compatible or snapshot.policy is None:
        raise ValueError("captured tool implementation or authorization is unavailable")
    specs = {spec.name: spec for spec in snapshot.frozen_specs}
    spec = specs.get(request.call_name)
    if (spec is None or not spec.supports_background or not snapshot.policy.allows(spec.grant)
            or dict(snapshot.custom_executors).get(request.call_name) is not None):
        raise ValueError("captured authorization does not permit this background operation")
    call = ToolCall(request.call_name, request.args)
    _args, error = _validate_tool_args(call.name, call.args, frozen_specs=specs)
    if error:
        raise ValueError(error)
    project = Path(request.project)
    task_id = child.task_id
    identity = execution_effect_id(task_id)

    def run(root: Path, rel: str, command: str, *, permission_profile: str, phase: str = "writer") -> ToolOutcome:
        raw = run_command_raw(root, rel, command, permission_profile=permission_profile, phase=phase,
            execution_store=stores.execution_outputs, session_id=child.conversation_id, run_id=task_id,
            execution_id=identity)
        return raw if isinstance(raw, ToolOutcome) else project_run_command_result(root, raw)

    session = TaskSession(snapshot.policy, project=request.project)
    delegate = ExecutionDelegate(session=session, project_path=project, tool_fns=AgentToolFns(run_command=run),
        permission_profile=request.permission_profile, managed_outputs=stores.managed_outputs,
        session_id=child.conversation_id, run_id=task_id, frozen_specs=specs, custom_executors={})
    try:
        with ToolDispatch.for_resource(str(project.resolve()).casefold()).admit(parallel=False):
            cancellation.check()
            durable.start_task(task_id, state={"tool_worker_active": True})
            state = stores.workspace_revisions.current_state(project)
            if not state.identity_reason:
                session.set_workspace_state(state.revision, state.fingerprint)
            intent = prepare_intent(child.conversation_id, task_id, RuntimeEffectIntent(effect_id=identity, effect_category="tool_call",
                session_id=child.conversation_id, run_id=task_id, phase="writer", turn=request.turn,
                tool_index=request.tool_index, tool_name=call.name, args_digest=compute_args_digest(call.args),
                replay_class=spec.replay_class))
            durable.record_effect_intent(task_id, EffectRecord(identity, task_id, "tool_call", spec.replay_class,
                intent.args_digest, "pending", intent.to_payload()))
            result, ok, code = execute_tool_operation(session, call, delegate=delegate, runnable={},
                turn=request.turn, tool_index=request.tool_index, project_path=project,
                workspace_revision_store=stores.workspace_revisions)
            fields = result_receipt_fields(result, store=stores.managed_outputs,
                session_id=child.conversation_id, run_id=task_id, effect_id=identity)
            receipt = prepare_settlement(child.conversation_id, task_id, RuntimeEffectSettlement(effect_id=identity, effect_category="tool_call",
                session_id=child.conversation_id, run_id=task_id, status="ok" if ok else "error",
                replay_class=spec.replay_class, exit_code=code, **fields), (RuntimeEffectProjection(intent, None),))
            durable.settle_effect(task_id, EffectRecord(identity, task_id, "tool_call", spec.replay_class,
                intent.args_digest, receipt.status, intent.to_payload(), receipt.to_payload()))
            if durable.task(task_id).state.get("cancel_requested"):
                durable.cancel_task(task_id)
            else:
                durable.complete_task(task_id, "", state={"tool_worker_active": False})
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        # No process is left outside its sole execution/collection owner.
        current = durable.task(task_id)
        if not durable.effects(task_id):
            durable.request_cancel(task_id)
            durable.cancel_task(task_id)
        elif current.state.get("cancel_requested"):
            durable.cancel_task(task_id)
        else:
            raise


def wait_tool_task(durable: DurableSession, task_ref: str, *, owner_task_id: str, wait_ms: int) -> TaskCheckpoint:
    event = threading.Event()

    def changed(_result: CommitResult, mutation: DurableMutation) -> None:
        if mutation.task is not None and mutation.task.task_id == task_ref:
            event.set()

    unsubscribe = durable.subscribe_commits(changed)
    try:
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            event.clear()
            current = owned_tool_task(durable, task_ref, owner_task_id=owner_task_id)
            if current.status in TASK_TERMINAL_STATUSES or time.monotonic() >= deadline:
                return current
            cancellation.check()
            event.wait(min(cancellation.POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
    finally:
        unsubscribe()


def terminal_tool_task_observations(results: Mapping[str, ToolResult]) -> set[str]:
    """Derive observed terminal states from committed control receipts."""
    observed: set[str] = set()
    for result in results.values():
        if result.ok and result.call.name in {"task_output", "task_cancel"}:
            value = decode_json_object(result.canonical["value"])
            if value["status"] in TASK_TERMINAL_STATUSES:
                observed.add(require_json_string(value["task_ref"], "task_ref"))
    return observed


def control_tool_task(call: ToolCall, *, durable: object, owner_task_id: str,
                      store: ManagedOutputStore | None) -> ToolResult:
    if not isinstance(durable, DurableSession):
        return _error_result(call, "task controls require durable execution")
    try:
        task_ref = require_json_string(call.args.get("task_ref"), "task_ref")
        owned_tool_task(durable, task_ref, owner_task_id=owner_task_id)
        wait_ms = require_json_integer(call.args.get("wait_ms", 0), "wait_ms")
        if call.name == "task_cancel":
            durable.request_cancel(task_ref, reason="cancelled by owning task")
        child = wait_tool_task(durable, task_ref, owner_task_id=owner_task_id, wait_ms=wait_ms)
        payload: dict[str, object] = {"task_ref": task_ref, "status": "stopping"
            if child.state.get("cancel_requested") and child.status not in TASK_TERMINAL_STATUSES else str(child.status)}
        result = tool_execution_result(durable, child, store=store)
        if result is not None:
            payload["result"] = {"ok": result[1].ok, "text": result[1].model_text[:4000],
                                 "text_truncated": len(result[1].model_text) > 4000,
                                 "exit_code": result[1].audit.get("exit_code")}
        if child.state.get("failure_reason"):
            payload["failure_reason"] = child.state["failure_reason"]
        if child.state.get("cleanup_unknown"):
            payload["cleanup_unknown"] = True
        if store is not None:
            limit = require_json_integer(call.args.get("limit", 4000), "limit")
            output_id = execution_effect_id(task_ref)
            try:
                snapshot = ExecutionOutputStore.read_existing(store.root.parent, output_id)
            except KeyError:
                snapshot = None
            if snapshot is not None:
                payload["output"] = {channel: ExecutionOutputStore.stream_since(snapshot, channel,
                    require_json_integer(call.args.get(f"{channel}_cursor", 0), f"{channel}_cursor"), limit=limit)
                    for channel in ("stdout", "stderr")}
        return ToolResult(call, json.dumps(payload, ensure_ascii=False), ok=True, canonical={"value": payload})
    except (KeyError, ValueError) as exc:
        return _error_result(call, str(exc))
