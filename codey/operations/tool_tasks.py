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
from codey.durable.tool_task import ToolTaskInput, execution_effect_id, tool_task_id
from codey.operations.kernel_receipts import restore_result_receipt, result_receipt_fields
from codey.operations.kernel_result import _error_result
from codey.operations.task_session import TaskSession
from codey.operations.task_state import TaskSubmissionStores
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core import cancellation
from codey.runtime.core.execution_receipt import execution_receipt
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.process_io import ProcessOptions
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
from codey.types.json_value import (
    canonical_json_object,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)
from codey.utils.failure_output import failure_excerpt

COMMAND_YIELD_MS = 10_000


def owned_tool_task(durable: DurableSession, task_ref: str, *, owner_task_id: str) -> TaskCheckpoint:
    owner = durable.task(owner_task_id)
    child = durable.task(task_ref)
    if (child.execution_kind != "tool" or child.owner_task_id != owner.task_id
            or child.conversation_id != owner.conversation_id or child.submission_id != owner.submission_id):
        raise ValueError("tool task is outside the caller's ownership")
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    origin = durable.effect(request.origin_effect_id)
    if (origin.task_id != owner_task_id
            or origin.intent.get("tool_name") != request.call_name
            or origin.args_digest != compute_args_digest(request.args)):
        raise ValueError("tool task has no committed original admission")
    return child


def automatic_command_result(durable: DurableSession, call: ToolCall, *, origin_effect_id: str,
                             owner_task_id: str, store: ManagedOutputStore | None,
                             wait_ms: int = COMMAND_YIELD_MS) -> ToolResult:
    """Observe the existing child; the original call never owns a second process."""
    ref = tool_task_id(origin_effect_id)
    child = wait_tool_task(durable, ref, owner_task_id=owner_task_id, wait_ms=wait_ms)
    if child.status not in TASK_TERMINAL_STATUSES:
        value: dict[str, object] = {"kind": "accepted", "task_ref": ref}
        return ToolResult(call, json.dumps(value), ok=True, kind="accepted", canonical={"value": value})
    observed = tool_execution_result(durable, child, store=store)
    if observed is None:
        return ToolResult(call, f"Command {child.status}: {child.state.get('failure_reason', '')}", ok=False,
            canonical={"tool_task": {"task_ref": ref}})
    identity, result = observed
    # The execution receipt stays on its child. This is the caller's bounded
    # observation, with a reference rather than a second execution contract.
    text = result.model_text
    if len(text) > 4000:
        text = (failure_excerpt(text) or text[:1800] + "\n...\n" + text[-1800:])
        text = text[:4000] + f"\nOutput excerpt; task_output can read task_ref={ref}."
    return ToolResult(call, text, ok=result.ok, truncated=result.truncated or len(result.model_text) > 4000,
        audit={"exit_code": result.audit.get("exit_code")},
        canonical={"tool_task": {"task_ref": ref, "execution_ref": identity}})


def recover_command_admission(durable: DurableSession, *, origin_effect_id: str, owner_task_id: str,
                              store: ManagedOutputStore | None) -> ToolResult | None:
    """Recover a reserved caller by following its original child, never by spawning."""
    try:
        child = durable.task(tool_task_id(origin_effect_id))
    except KeyError:
        return None
    owned_tool_task(durable, child.task_id, owner_task_id=owner_task_id)
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    origin = durable.effect(origin_effect_id)
    call = ToolCall(request.call_name, request.args, require_json_string(origin.intent.get("call_id", ""), "call_id"))
    return automatic_command_result(durable, call, origin_effect_id=origin_effect_id,
        owner_task_id=owner_task_id, store=store, wait_ms=0)


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
    intent_payload: dict[str, object] = dict(canonical_json_object(effect.intent))
    settlement_payload: dict[str, object] = dict(canonical_json_object(effect.settlement))
    projection = RuntimeEffectProjection(RuntimeEffectIntent.from_payload(intent_payload),
        RuntimeEffectSettlement.from_payload(settlement_payload))
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
        finally:
            durable.tool_resources.release_browser(task_id)


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
    project = Path(request.project) if request.project else None
    task_id = child.task_id
    identity = execution_effect_id(task_id)

    def run(root: Path, rel: str, command: str, *, permission_profile: str, phase: str = "writer") -> ToolOutcome:
        raw = run_command_raw(root, rel, command, permission_profile=permission_profile, phase=phase,
            execution_store=stores.execution_outputs, session_id=child.conversation_id, run_id=task_id,
            execution_id=identity, process_options=ProcessOptions(
                stdin=require_json_boolean(request.args.get("stdin", request.args.get("tty", False)), "stdin"),
                tty=require_json_boolean(request.args.get("tty", False), "tty"),
                cols=require_json_integer(request.args.get("cols", 100), "cols"),
                rows=require_json_integer(request.args.get("rows", 30), "rows")),
            on_process_started=lambda endpoint: durable.tool_resources.register_process(task_id, child.attempt_token, endpoint))
        return raw if isinstance(raw, ToolOutcome) else project_run_command_result(root, raw)

    session = TaskSession(snapshot.policy, project=request.project)
    if call.name == "browser_wait" and call.args.get("condition") == "text_changed":
        from codey.operations.browser_waits import restore_text_wait_snapshot

        owner_task_id = require_json_string(child.owner_task_id, "background wait owner")
        owned_tool_task(durable, child.task_id, owner_task_id=owner_task_id)
        restore_text_wait_snapshot(session, durable, owner_task_id=owner_task_id,
            result_ref=require_json_string(call.args.get("value"), "value"), store=stores.managed_outputs)
    from codey.automation.browser_session import BrowserSession

    browser: BrowserSession | None = None
    document = None
    if spec.executor == "browser":
        if request.resource_binding is None:
            raise ValueError("background browser operation has no original binding")
        browser = durable.tool_resources.browser(task_id, request.resource_binding)
        document = decode_json_object(request.resource_binding["browser_document"])
    elif spec.executor != "project" or project is None:
        raise ValueError("background command requires a project")
    delegate = ExecutionDelegate(session=session, project_path=project, tool_fns=AgentToolFns(run_command=run),
        permission_profile=request.permission_profile, managed_outputs=stores.managed_outputs,
        session_id=child.conversation_id, run_id=task_id, frozen_specs=specs, custom_executors={},
        browser_session=browser, browser_wait_document=document)
    try:
        from contextlib import nullcontext

        admission = (ToolDispatch.for_resource(str(project.resolve()).casefold()).admit(parallel=False)
                     if spec.executor == "project" and project is not None else nullcontext())
        with admission:
            cancellation.check()
            durable.start_task(task_id, state={"tool_worker_active": True})
            if project is not None and spec.executor == "project":
                state = stores.workspace_revisions.current_state(project)
                if not state.identity_reason:
                    session.set_workspace_state(state.revision, state.fingerprint)
            intent = prepare_intent(child.conversation_id, task_id, RuntimeEffectIntent(effect_id=identity, effect_category="tool_call",
                session_id=child.conversation_id, run_id=task_id, phase="writer", turn=request.turn,
                tool_index=request.tool_index, tool_name=call.name, args_digest=compute_args_digest(call.args),
                replay_class=spec.replay_class))
            intent_payload: object = intent.to_payload()
            durable.record_effect_intent(task_id, EffectRecord(identity, task_id, "tool_call", spec.replay_class,
                intent.args_digest, "pending", canonical_json_object(intent_payload)))
            from codey.automation.browser_invocation import invocation_scope
            from codey.operations.browser_progress import browser_invocation

            with invocation_scope(browser_invocation(identity, durable=durable, store=stores.managed_outputs,
                    session_id=child.conversation_id, run_id=task_id)):
                result, ok, code = execute_tool_operation(session, call, delegate=delegate, runnable={},
                    turn=request.turn, tool_index=request.tool_index, project_path=project,
                    workspace_revision_store=stores.workspace_revisions)
            fields = result_receipt_fields(result, store=stores.managed_outputs,
                session_id=child.conversation_id, run_id=task_id, effect_id=identity)
            receipt = prepare_settlement(child.conversation_id, task_id, RuntimeEffectSettlement(effect_id=identity, effect_category="tool_call",
                session_id=child.conversation_id, run_id=task_id, status="ok" if ok else "error",
                replay_class=spec.replay_class, exit_code=code, **fields), (RuntimeEffectProjection(intent, None),))
            receipt_payload: object = receipt.to_payload()
            durable.settle_effect(task_id, EffectRecord(identity, task_id, "tool_call", spec.replay_class,
                intent.args_digest, receipt.status, canonical_json_object(intent_payload), canonical_json_object(receipt_payload)))
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
        forwarded = result.canonical.get("tool_task")
        if result.kind == "completed" and result.call.name == "run" and isinstance(forwarded, dict):
            observed.add(require_json_string(forwarded["task_ref"], "task_ref"))
        if result.ok and result.call.name in {"task_output", "task_cancel"}:
            value = decode_json_object(result.canonical["value"])
            if value["status"] in TASK_TERMINAL_STATUSES:
                observed.add(require_json_string(value["task_ref"], "task_ref"))
    return observed


def control_tool_task(call: ToolCall, *, durable: object, owner_task_id: str,
                      store: ManagedOutputStore | None, caller_policy: TaskPolicy | None = None) -> ToolResult:
    if not isinstance(durable, DurableSession):
        return _error_result(call, "task controls require durable execution")
    try:
        task_ref = require_json_string(call.args.get("task_ref"), "task_ref")
        child = owned_tool_task(durable, task_ref, owner_task_id=owner_task_id)
        if call.name in {"task_input", "task_signal", "task_resize"}:
            return _control_process(call, durable, child, caller_policy=caller_policy)
        wait_ms = require_json_integer(call.args.get("wait_ms", 0), "wait_ms")
        if call.name == "task_cancel":
            durable.request_cancel(task_ref, reason="cancelled by owning task")
        child = wait_tool_task(durable, task_ref, owner_task_id=owner_task_id, wait_ms=wait_ms)
        payload = tool_task_snapshot(durable, child, store=store, args=call.args)
        return ToolResult(call, json.dumps(payload, ensure_ascii=False), ok=True, canonical={"value": payload})
    except (KeyError, ValueError, OSError) as exc:
        return _error_result(call, str(exc))


def _control_process(call: ToolCall, durable: DurableSession, child: TaskCheckpoint, *,
                     caller_policy: TaskPolicy | None) -> ToolResult:
    from codey.operations.provider_receipts import restore_snapshot

    request = ToolTaskInput.from_payload(child.state["tool_input"])
    snapshot = restore_snapshot(request.snapshot)
    target = next((spec for spec in snapshot.frozen_specs if spec.name == request.call_name), None)
    if (caller_policy is None or snapshot.policy is None or target is None
            or not caller_policy.allows(target.grant) or not snapshot.policy.allows(target.grant)
            or request.call_name != "run"):
        raise ValueError("process control requires the original execution capability")
    if child.status in TASK_TERMINAL_STATUSES or child.state.get("cancel_requested"):
        raise ValueError("process task is stopped or cancelling")
    process = durable.tool_resources.process(child.task_id, child.attempt_token)

    def valid() -> bool:
        current = durable.task(child.task_id)
        return (current.attempt_token == child.attempt_token and current.status not in TASK_TERMINAL_STATUSES
                and not current.state.get("cancel_requested"))

    payload: dict[str, object] = {"task_ref": child.task_id}
    if call.name == "task_input":
        data = require_json_string(call.args.get("data", ""), "data")
        eof = require_json_boolean(call.args.get("eof", False), "eof")
        if not data and not eof:
            raise ValueError("task_input requires nonempty data or eof=true")
        delivery = process.write_input(data.encode("utf-8"), eof=eof, valid=valid)
        payload.update(bytes_written=delivery.bytes_written, eof=delivery.eof,
                       delivery="complete" if delivery.complete else "incomplete")
        return ToolResult(call, json.dumps(payload), ok=delivery.complete, canonical={"value": payload})
    if call.name == "task_signal":
        if call.args.get("signal") != "interrupt":
            raise ValueError("unsupported terminal signal")
        process.interrupt(valid)
    elif call.name == "task_resize":
        process.resize(require_json_integer(call.args.get("cols"), "cols"),
                       require_json_integer(call.args.get("rows"), "rows"), valid)
    payload["delivery"] = "complete"
    return ToolResult(call, json.dumps(payload), ok=True, canonical={"value": payload})


def tool_task_snapshot(durable: DurableSession, child: TaskCheckpoint, *, store: ManagedOutputStore | None,
                       args: Mapping[str, object], include_output: bool = True) -> dict[str, object]:
    """One bounded projection shared by explicit reads and automatic notifications."""
    limit = require_json_integer(args.get("limit", 4000), "limit")
    payload: dict[str, object] = {"task_ref": child.task_id, "status": "stopping"
        if child.state.get("cancel_requested") and child.status not in TASK_TERMINAL_STATUSES else str(child.status)}
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    if request.args.get("tty") is True:
        payload["output_mode"] = "terminal"
        payload["streams_merged"] = True
    result = tool_execution_result(durable, child, store=store)
    if result is not None:
        payload["execution_ref"] = result[0]
        shown = result[1].model_text[:limit]
        receipt = execution_receipt(result[1])
        if receipt is not None and receipt.status == "exit":
            excerpt = failure_excerpt(result[1].model_text, limit=limit) if receipt.exit_code != 0 else ""
            if receipt.exit_code == 0 or excerpt:
                shown = excerpt or f"exit {receipt.exit_code}"
        payload["result"] = {"ok": result[1].ok, "text": shown,
                             "text_truncated": shown != result[1].model_text,
                             "exit_code": result[1].audit.get("exit_code")}
    payload["outcome"] = _task_outcome(child, result[1] if result is not None else None)
    if child.state.get("failure_reason"):
        payload["failure_reason"] = require_json_string(child.state["failure_reason"], "failure_reason")[:limit]
    if child.state.get("cleanup_unknown"):
        payload["cleanup_unknown"] = True
    if store is not None and include_output:
        try:
            snapshot = ExecutionOutputStore.read_existing(store.root.parent, execution_effect_id(child.task_id))
        except KeyError:
            snapshot = None
        if snapshot is not None:
            payload["output"] = {channel: ExecutionOutputStore.stream_since(snapshot, channel,
                require_json_integer(args.get(f"{channel}_cursor", 0), f"{channel}_cursor"), limit=limit)
                for channel in ("stdout", "stderr")}
    return payload


def _task_outcome(child: TaskCheckpoint, result: ToolResult | None) -> str:
    if child.status not in TASK_TERMINAL_STATUSES or child.status == "cancelled":
        return str(child.status)
    if child.state.get("cleanup_unknown"):
        return "interrupted"
    if result is None:
        return "failed"
    receipt = execution_receipt(result)
    status = receipt.status if receipt is not None else ""
    if status == "timeout":
        return "timed_out"
    if status in {"output_drain_timeout", "output_read_error"}:
        return "incomplete"
    return "succeeded" if result.ok else "failed"
