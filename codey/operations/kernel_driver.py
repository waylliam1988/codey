"""Execute concrete operation actions through the existing durable writer.

Normal sends and a restart with a prepared request use this same provider
path. There is no private execution cursor: the operation leaf and original
effect receipts determine every action. Control actions and recovery use the
same executors and durable mutation line.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.durable.checkpoint import checkpoint_entries
from codey.durable.model import thaw_json
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_contracts import KernelDecision, KernelResult, KernelRunRequest, result_from_payload
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.prepared_provider_request import PreparedProviderRequest
from codey.operations.provider_receipts import REQUEST_KIND, read_provider_artifact
from codey.operations.provider_request_context import ProviderRequestContext
from codey.providers.transport import ProviderTransport
from codey.types.json_value import decode_json_object

if TYPE_CHECKING:
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
from codey.providers.base import AssistantTurn
from codey.providers.compat_transport import optional_callable, provider_window_ref
from codey.providers.request_codec import decode_provider_request
from codey.providers.requests import ProviderRequest, SendTextRequest, SendTurnRequest
from codey.runtime.core.operation_payload import (
    LEAF_CONTROL_READY,
    LEAF_TOOL_DELIVERY_PENDING,
    RuntimeOperationState,
)
from codey.runtime.core.operation_reducer import (
    ACTION_COMMIT_GENERATION_BOUNDARY,
    ACTION_SUBMIT_PROVIDER_REQUEST,
    RuntimeAction,
)
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_NOT_SENT,
    SETTLEMENT_STATUS_ERROR,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    compute_args_digest,
)
from codey.runtime.log.session_view import SessionView, load_session_view, not_sent_continuation_effect
from codey.runtime.write.drive import peek_next_action


def decode_provider_invocation(payload: object) -> ProviderRequest:
    """Decode only the frozen request; registry state cannot redefine it."""
    try:
        return decode_provider_request(payload)
    except ValueError as exc:
        raise RuntimeEffectError(str(exc)) from exc


def load_prepared_request(sink: KernelEffectSink, request_ref: str) -> PreparedProviderRequest:
    artifact = sink.durable.artifact(request_ref)
    envelope = sink.managed_outputs.read_json_artifact(dict[str, object](decode_json_object(thaw_json(artifact.metadata["ref"]))))
    if not isinstance(envelope, dict) or not isinstance(envelope.get("effect_id"), str):
        raise RuntimeEffectError("prepared request has no stable effect identity")
    payload = read_provider_artifact(sink.managed_outputs, artifact, kind=REQUEST_KIND,
        session_id=sink.session_id, run_id=sink.run_id, effect_id=envelope["effect_id"])
    if not isinstance(payload, dict) or not isinstance(payload.get("admission"), dict):
        raise RuntimeEffectError("prepared request has no admission data")
    admission = decode_json_object(payload["admission"])
    if set(admission) != {"intent", "expected_counters", "counters"}:
        raise RuntimeEffectError("prepared request admission data is invalid")
    intent = RuntimeEffectIntent.from_payload(decode_json_object(admission["intent"]))
    if intent.effect_id != envelope["effect_id"] or intent.request_ref:
        raise RuntimeEffectError("prepared request identity conflicts with its envelope")
    transport = {key: value for key, value in payload.items() if key != "admission"}
    if intent.args_digest != compute_args_digest(json.dumps(transport, ensure_ascii=False, sort_keys=True)):
        raise RuntimeEffectError("prepared request differs from its frozen intent")
    try:
        return PreparedProviderRequest.from_payload(payload, intent=replace(intent, request_ref=request_ref))
    except (ValueError, TypeError) as exc:
        raise RuntimeEffectError(str(exc)) from exc


def restore_provider_window(sink: KernelEffectSink, provider: ProviderTransport, state: RuntimeOperationState) -> ProviderRequestContext | None:
    """Rebuild the window projection from this operation's frozen request."""
    from codey.operations.provider_receipts import request_artifact_for_effect

    if state.request_ref:
        prepared = load_prepared_request(sink, state.request_ref)
        return _restore_window_context(provider, prepared.context)
    elif state.response_ref:
        response = sink.durable.artifact(state.response_ref)
        effect = next(row for row in RuntimeEffectStore(durable=sink.durable).load_effects(sink.session_id, sink.run_id)
                      if row.intent.effect_id == response.owner_effect_id)
        payload = read_provider_artifact(sink.managed_outputs, request_artifact_for_effect(sink.durable, effect),
            kind=REQUEST_KIND, session_id=sink.session_id, run_id=sink.run_id, effect_id=effect.intent.effect_id)
    elif state.leaf in {LEAF_CONTROL_READY, LEAF_TOOL_DELIVERY_PENDING}:
        task, effects = sink.durable.task_facts(sink.run_id)
        view = load_session_view(checkpoint_entries(task, effects, session_id=sink.session_id),
                                 session_id=sink.session_id, run_id=sink.run_id)
        effect = not_sent_continuation_effect(view)
        payload = read_provider_artifact(sink.managed_outputs, request_artifact_for_effect(sink.durable, effect),
            kind=REQUEST_KIND, session_id=sink.session_id, run_id=sink.run_id, effect_id=effect.intent.effect_id)
    else:
        return None
    try:
        context = ProviderRequestContext.from_payload(payload) if "snapshot" in payload else None
    except ValueError as exc:
        raise RecoveryFailed(str(exc)) from exc
    return _restore_window_context(provider, context)


def _restore_window_context(provider: ProviderTransport, context: ProviderRequestContext | None) -> ProviderRequestContext | None:
    if context is None or context.window_ref is None:
        return context
    ref = context.window_ref
    if provider_window_ref(provider) == ref:
        return context
    restore = optional_callable(provider, "restore_protocol_window")
    if restore is None:
        raise RecoveryFailed("provider cannot restore the original protocol window")
    restore(ref)
    if provider_window_ref(provider) != ref:
        raise RecoveryFailed("provider did not restore the original protocol window")
    return context


def drive_provider_request(sink: KernelEffectSink, provider: object, request_ref: str) -> AssistantTurn | str:
    """Close the explicit prior boundary, admit once, execute, then save once."""
    from codey.operations.provider_session import adapt_provider

    transport = adapt_provider(provider)
    while True:
        action = peek_next_action(sink.durable, session_id=sink.session_id, run_id=sink.run_id)
        if action.request_ref != request_ref:
            raise RecoveryFailed(f"prepared request cannot run during {action.kind}: {action.reason}")
        if action.kind == ACTION_COMMIT_GENERATION_BOUNDARY and action.reason == "before_provider_request":
            pending = sink.pending_provider_settlement()
            if pending is None or pending[0].effect_id != action.effect_id:
                raise RuntimeEffectError("prepared request has no reliable previous generation")
            artifact = sink.durable.artifact(action.response_ref)
            if artifact.owner_effect_id != action.effect_id:
                raise RuntimeEffectError("previous generation response belongs to another effect")
            # This internal boundary never admits user submissions. Input
            # placement remains at the original post-tools/final boundaries.
            sink.mutations.settle_provider_effect(sink.session_id, sink.run_id, pending[0], messages=pending[1])
            continue
        if action.kind != ACTION_SUBMIT_PROVIDER_REQUEST:
            raise RecoveryFailed(f"prepared request requires {action.kind}: {action.reason}")
        prepared = load_prepared_request(sink, request_ref)
        intent, invocation = prepared.intent, prepared.request
        admitted = sink.mutations.admit_provider_request(sink.session_id, sink.run_id,
            intent,
            expected_counters=prepared.expected_counters,
            counters=prepared.counters)
        if not admitted.newly_admitted:
            # A lost admission acknowledgement conveys no execution right.
            # Loading that original pending effect must never resend it.
            raise RecoveryFailed("provider intent was already admitted; its external outcome requires reconciliation")
        return _execute_provider(sink, transport, admitted.intent, invocation)


def _execute_provider(sink: KernelEffectSink, provider: ProviderTransport, intent: RuntimeEffectIntent,
                      invocation: ProviderRequest) -> AssistantTurn | str:
    try:
        gate_prompt = optional_callable(provider, "set_attempt_gate_prompt")
        if callable(gate_prompt):
            prompt = invocation.prompt if isinstance(invocation, (SendTextRequest, SendTurnRequest)) else ""
            gate_prompt(prompt if isinstance(prompt, str) else "")
        result_refs = optional_callable(provider, "set_result_refs")
        if callable(result_refs):
            result_refs(tuple(effect.effect_id for effect in sink.durable.effects(sink.run_id)
                              if effect.category == "tool_call" and effect.settlement is not None))
        reply = provider.execute_request(invocation)
        from codey.operations.provider_session import normalize_provider_reply
        reply = normalize_provider_reply(provider, reply)
    except Exception as exc:
        from codey.providers.error_classification import ContextOverflowError, RequestPrepError

        not_sent = (isinstance(exc, (ContextOverflowError, RequestPrepError, StopIteration))
                    or getattr(exc, "provider_failure_kind", "") == "not_submitted")
        sink.mutations.settle_provider_effect(sink.session_id, sink.run_id, RuntimeEffectSettlement(
            effect_id=intent.effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=sink.session_id, run_id=sink.run_id, status=SETTLEMENT_STATUS_ERROR,
            error_code=type(exc).__name__[:80], sent_state=SENT_STATE_NOT_SENT if not_sent else SENT_STATE_MAYBE_SENT))
        raise
    # Saving may retry CAS, but execution above is never inside that retry.
    sink.record_response(intent.effect_id, reply)
    sink.delivery_batch_id = ""
    return reply


def drive_kernel(session: TaskSession, request: KernelRunRequest) -> KernelResult:
    """Run normal work and restart work through one operation decision loop."""
    from codey.operations.kernel_actions import _provider_failure
    from codey.operations.kernel_context import KernelContext
    from codey.operations.kernel_contracts import KernelResult
    from codey.runtime.core import operation_reducer as rules

    try:
        context = KernelContext(session, request)
    except Exception as exc:
        return KernelResult(False, f"controller configuration error: {exc}", 0, "controller_failure")
    last_decision: KernelDecision | None = None
    last_action = ""
    window_restored = False
    try:
        while True:
            view = context.load_view()
            state = view.state
            if state is None:
                raise RecoveryFailed("operation view has no runtime state")
            last_decision = context.decision(state) if state.work_ref else None
            action = rules.next_runtime_action(view, context.decision_inputs(view))
            last_action = action.kind
            if action.kind == rules.ACTION_RETURN_KERNEL_RESULT:
                context.restore_facts()
                decision = context.decision(state)
                if decision["purpose"] == "retry":
                    raise RecoveryFailed("settled kernel has a retry decision")
                result = result_from_payload(decision["result"])
                if context.native and decision["purpose"] in {"done", "question"}:
                    result = replace(result, delivery="success")
                return result
            if action.kind == rules.ACTION_FAIL_INVARIANT:
                raise RecoveryFailed(f"{action.reason}: {action.effect_ids}")
            if not window_restored:
                original_request = restore_provider_window(context.sink, context.provider, state)
                if original_request is not None:
                    context.restore_context(original_request)
                context.window = provider_window_ref(context.provider) or context.transport.window_ref
                window_restored = True
            outcome = _execute_kernel_action(context, view, action)
            if outcome is not None:
                return outcome
            after = context.load_view()
            if after == view:
                raise RecoveryFailed(f"operation action made no progress: {action.kind}")
    except RecoveryFailed as exc:
        return KernelResult(False, f"recovery failed: {exc}", session.turn, "recovery_failure")
    except Exception as exc:
        # Reporting a failed commit must not read the failed store again or
        # mask its error. This is diagnostic data, never an execution fallback.
        if last_decision is not None and (last_decision["purpose"] == "done" or last_decision["purpose"] == "question"):
            original = result_from_payload(last_decision["result"])
            return KernelResult(False, f"protocol delivery failed [{type(exc).__name__}]: {exc}",
                original.turns, "delivery_pending", proof=original.proof,
                delivery="unknown" if getattr(exc, "provider_failure_kind", "") == "submission_uncertain" else "failed")
        if last_action in {rules.ACTION_PREPARE_TURN, rules.ACTION_REDELIVER_SETTLED_BATCH}:
            return KernelResult(False, f"controller failed [{type(exc).__name__}]: {exc}", session.turn, "controller_failure")
        failure: KernelResult = _provider_failure(
            exc, session.turn, propagate=request.observation.propagate_provider_failure)
        return failure


def _execute_kernel_action(context: KernelContext, view: SessionView, action: RuntimeAction) -> KernelResult | None:
    from codey.operations import kernel_control
    from codey.runtime.core import operation_reducer as rules

    state = view.state
    assert state is not None, "operation action has no runtime state"
    sink = context.sink
    assert sink is not None, "operation action has no admitted sink"
    if action.kind == rules.ACTION_BEGIN_CLOSING:
        context.close(state, KernelResult(False, "stopped", state.logical_turn, "stopped"))
    elif action.kind == rules.ACTION_RESUME_QUESTION:
        from codey.runtime.core.operation_payload import LEAF_REPAIR_RUNNING, LEAF_WRITER_RUNNING
        context.advance(state, LEAF_REPAIR_RUNNING if state.driver == "repair" else LEAF_WRITER_RUNNING)
    elif action.kind == rules.ACTION_RETURN_WAITING:
        return KernelResult(False, "shell command requires approval", state.logical_turn, "approval")
    elif action.kind == rules.ACTION_PREPARE_TURN:
        context.prepare_turn(state)
    elif action.kind in {rules.ACTION_SUBMIT_PROVIDER_REQUEST, rules.ACTION_COMMIT_GENERATION_BOUNDARY}:
        if action.kind == rules.ACTION_SUBMIT_PROVIDER_REQUEST or action.reason == "before_provider_request":
            reply = drive_provider_request(sink, context.provider, state.request_ref)
            current = context.load_view().state
            assert current is not None, "provider request removed operation state"
            received = context.response(current)
            snapshot = context.snapshots.get(received.logical_turn, received.snapshot)
            context.responses[current.response_ref] = replace(received, reply=reply, snapshot=snapshot)
        else:
            context.boundary(action)
    elif action.kind == rules.ACTION_INTERPRET_PROVIDER_RESPONSE:
        context.interpret(state)
    elif action.kind in {rules.ACTION_EXECUTE_TOOL_PLAN, rules.ACTION_REPLAY_SAFE_TOOL_BATCH}:
        if action.delivery_batch_id:
            sink.delivery_batch_id = action.delivery_batch_id
        result: KernelResult | None = context.execute_tools(state)
        if result is not None:
            return result
    elif action.kind == rules.ACTION_OBSERVE_BATCH_PROGRESS:
        context.observe_progress(view, action)
    elif action.kind == rules.ACTION_REDELIVER_SETTLED_BATCH:
        context.deliver_results(view, action)
    elif action.kind == rules.ACTION_EVALUATE_COMPLETION:
        kernel_control.evaluate_completion(context, state)
    elif action.kind == rules.ACTION_PREPARE_CONTROL_DELIVERY:
        kernel_control.prepare_control_delivery(context, state)
    elif action.kind == rules.ACTION_CLOSE_PROTOCOL:
        kernel_control.close_protocol(context, view)
    elif action.kind in {rules.ACTION_SETTLE_PROVIDER_UNKNOWN, rules.ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS}:
        from codey.durable.effects import interrupted_effect
        from codey.durable.model import thaw_json

        task, effects = context.durable.task_facts(sink.run_id)
        for identity in action.effect_ids or (action.effect_id,):
            original = next(effect for effect in effects if effect.effect_id == identity)
            receipt = thaw_json(interrupted_effect(task, original).settlement)
            if not isinstance(receipt, dict):
                raise RecoveryFailed("interrupted effect has no settlement")
            sink.mutations.settle_effect(sink.session_id, sink.run_id,
                RuntimeEffectSettlement.from_payload(receipt))
        raise RecoveryFailed("external outcome is unknown; the original action was not replayed")
    else:
        _execute_wait_action(context, state, action)
    return None


def _execute_wait_action(context: KernelContext, state: RuntimeOperationState, action: RuntimeAction) -> None:
    from codey.runtime.core.operation_reducer import ACTION_RECONCILE_APPROVAL

    if action.kind != ACTION_RECONCILE_APPROVAL:
        raise RecoveryFailed(f"kernel cannot advance {action.kind} at {action.leaf}")
    context.reconcile_approval(state, action.effect_id, cancelled=action.reason == "stopped")
