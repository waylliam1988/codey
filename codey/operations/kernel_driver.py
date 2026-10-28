"""Execute concrete operation actions through the existing durable writer.

Normal sends and a restart with a prepared request use this same provider
path. There is no private execution cursor: the operation leaf and original
effect receipts determine every action. Other kernel actions are being moved
here from the old controller, without adding another executor or ledger.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.provider_receipts import REQUEST_KIND, json_value, read_provider_artifact, write_provider_artifact
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.operation_reducer import (
    ACTION_COMMIT_GENERATION_BOUNDARY,
    ACTION_SUBMIT_PROVIDER_REQUEST,
)
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_NOT_SENT,
    SETTLEMENT_STATUS_ERROR,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    compute_args_digest,
    new_effect_id,
    prepare_intent,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.write.drive import peek_next_action

_METHODS = frozenset({"send", "send_turn", "send_tool_results", "acknowledge_tool_results",
                      "send_tool_results_bound", "acknowledge_tool_results_bound"})


def prepare_provider_request(
    sink: Any, method: str, args: tuple[Any, ...], kwargs: dict[str, Any], *,
    counters: tuple[RuntimeExecutionCounters, RuntimeExecutionCounters] | None,
) -> str:
    """Freeze one invocation and stable effect identity before any send."""
    if method not in _METHODS or sink.managed_outputs is None:
        raise RuntimeEffectError("provider request requires a supported method and managed storage")
    task, effects = sink.durable.task_facts(sink.run_id)
    state = operation_from_checkpoint(task, sink.session_id)
    if state is None:
        raise RuntimeEffectError("provider request requires an admitted operation")
    turn = max((effect.intent.get("turn", 0) for effect in effects
                if effect.category == EFFECT_CATEGORY_PROVIDER_SEND), default=0) + 1
    transport = {"method": method, "args": json_value(args), "kwargs": json_value(kwargs), **sink._request_context}
    intent = prepare_intent(sink.session_id, sink.run_id, RuntimeEffectIntent(
        effect_id=new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, sink.run_id),
        effect_category=EFFECT_CATEGORY_PROVIDER_SEND, session_id=sink.session_id, run_id=sink.run_id,
        phase=sink.phase, provider_id=sink.provider_id, turn=turn, display_ref=method,
        args_digest=compute_args_digest(json.dumps(transport, ensure_ascii=False, sort_keys=True)),
        replay_class=ReplayClass.UNSAFE, delivery_batch_id=sink.delivery_batch_id,
    ))
    before, proposed = counters if counters is not None else (state.counters, state.counters)
    payload = {**transport, "admission": {"intent": intent.to_payload(),
        "expected_counters": before.to_payload(), "counters": proposed.to_payload()}}
    artifact = write_provider_artifact(sink.managed_outputs, session_id=sink.session_id, run_id=sink.run_id,
                                      effect_id=intent.effect_id, kind=REQUEST_KIND, payload=payload)
    sink.mutations.prepare_provider_request(sink.session_id, sink.run_id, expected=state, artifact=artifact, turn=turn)
    return str(artifact.handle)


def _tools(value: object) -> list[ProviderToolDefinition]:
    if not isinstance(value, list):
        raise RuntimeEffectError("frozen provider declarations are not a list")
    tools = []
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"name", "description", "parameters"}
                or not isinstance(row["name"], str) or not isinstance(row["description"], str)
                or not isinstance(row["parameters"], dict)):
            raise RuntimeEffectError("frozen provider declaration is invalid")
        tools.append(ProviderToolDefinition(row["name"], row["description"], row["parameters"]))
    return tools


def _results(value: object) -> list[ProviderToolResult]:
    if not isinstance(value, list):
        raise RuntimeEffectError("frozen provider results are not a list")
    results = []
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"call_id", "content", "user_content"}
                or not isinstance(row["call_id"], str) or not isinstance(row["content"], str)
                or (row["user_content"] is not None and not isinstance(row["user_content"], str))):
            raise RuntimeEffectError("frozen provider result is invalid")
        results.append(ProviderToolResult(row["call_id"], row["content"], row["user_content"]))
    return results


def decode_provider_invocation(payload: dict[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
    """Rebuild transport types from the original request, never today's registry."""
    method, raw_args, raw_kwargs = payload.get("method"), payload.get("args"), payload.get("kwargs")
    if method not in _METHODS or not isinstance(raw_args, list) or not isinstance(raw_kwargs, dict):
        raise RuntimeEffectError("frozen provider invocation is invalid")
    args, kwargs = list(raw_args), dict(raw_kwargs)
    if method not in {"send", "send_turn"}:
        if args:
            args[0] = _results(args[0])
        elif "results" in kwargs:
            kwargs["results"] = _results(kwargs["results"])
    if method.endswith("_bound"):
        if len(args) > 1:
            args[1] = ProtocolWindowRef.from_payload(args[1])
        elif "window_ref" in kwargs:
            kwargs["window_ref"] = ProtocolWindowRef.from_payload(kwargs["window_ref"])
        if len(args) > 2 and args[2] is not None:
            args[2] = _tools(args[2])
    elif method != "send" and len(args) > 1 and args[1] is not None:
        args[1] = _tools(args[1])
    for key in ("tools", "declared_tools"):
        if key in kwargs and kwargs[key] is not None:
            kwargs[key] = _tools(kwargs[key])
    return str(method), args, kwargs


def load_prepared_request(sink: Any, request_ref: str) -> tuple[dict[str, Any], RuntimeEffectIntent]:
    artifact = sink.durable.artifact(request_ref)
    envelope = sink.managed_outputs.read_json_artifact(dict(artifact.metadata["ref"]))
    if not isinstance(envelope, dict) or not isinstance(envelope.get("effect_id"), str):
        raise RuntimeEffectError("prepared request has no stable effect identity")
    payload = read_provider_artifact(sink.managed_outputs, artifact, kind=REQUEST_KIND,
        session_id=sink.session_id, run_id=sink.run_id, effect_id=envelope["effect_id"])
    if not isinstance(payload, dict) or not isinstance(payload.get("admission"), dict):
        raise RuntimeEffectError("prepared request has no admission data")
    admission = payload["admission"]
    if set(admission) != {"intent", "expected_counters", "counters"}:
        raise RuntimeEffectError("prepared request admission data is invalid")
    intent = RuntimeEffectIntent.from_payload(admission["intent"])
    if intent.effect_id != envelope["effect_id"] or intent.request_ref:
        raise RuntimeEffectError("prepared request identity conflicts with its envelope")
    transport = {key: value for key, value in payload.items() if key != "admission"}
    if intent.args_digest != compute_args_digest(json.dumps(transport, ensure_ascii=False, sort_keys=True)):
        raise RuntimeEffectError("prepared request differs from its frozen intent")
    return payload, replace(intent, request_ref=request_ref)


def drive_provider_request(sink: Any, provider: Any, request_ref: str) -> Any:
    """Close the explicit prior boundary, admit once, execute, then save once."""
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
        payload, intent = load_prepared_request(sink, request_ref)
        admission = payload["admission"]
        method, args, kwargs = decode_provider_invocation(payload)
        admitted = sink.mutations.admit_provider_request(sink.session_id, sink.run_id,
            intent,
            expected_counters=RuntimeExecutionCounters.from_payload(admission["expected_counters"]),
            counters=RuntimeExecutionCounters.from_payload(admission["counters"]))
        if not admitted.newly_admitted:
            # A lost admission acknowledgement conveys no execution right.
            # Loading that original pending effect must never resend it.
            raise RecoveryFailed("provider intent was already admitted; its external outcome requires reconciliation")
        return _execute_provider(sink, provider, admitted.intent, method, args, kwargs)


def _execute_provider(sink: Any, provider: Any, intent: RuntimeEffectIntent,
                      method: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    try:
        reply = getattr(provider, method)(*args, **kwargs)
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
