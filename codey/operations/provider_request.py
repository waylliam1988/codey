"""Freeze provider invocations before they cross the durable boundary."""

from __future__ import annotations

import json
from typing import Any

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.provider_receipts import REQUEST_KIND, write_provider_artifact
from codey.providers.response_record import json_value
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    RuntimeEffectError,
    RuntimeEffectIntent,
    compute_args_digest,
    new_effect_id,
    prepare_intent,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.log.session_view import driver_for_state

_METHODS = frozenset({
    "send",
    "send_turn",
    "send_tool_results",
    "acknowledge_tool_results",
    "send_tool_results_bound",
    "acknowledge_tool_results_bound",
})


def prepare_provider_request(
    sink: Any,
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
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
        phase=driver_for_state(state), provider_id=sink.provider_id, turn=turn,
        display_ref="closing_acknowledgement" if sink._request_context.get("delivery_purpose") == "closing"
            and method.startswith("acknowledge_tool_results") else method,
        args_digest=compute_args_digest(json.dumps(transport, ensure_ascii=False, sort_keys=True)),
        replay_class=ReplayClass.UNSAFE, delivery_batch_id=sink.delivery_batch_id,
    ))
    before, proposed = counters if counters is not None else (state.counters, state.counters)
    payload = {**transport, "admission": {"intent": intent.to_payload(),
        "expected_counters": before.to_payload(), "counters": proposed.to_payload()}}
    artifact = write_provider_artifact(sink.managed_outputs, session_id=sink.session_id, run_id=sink.run_id,
                                      effect_id=intent.effect_id, kind=REQUEST_KIND, payload=payload)
    sink.mutations.prepare_provider_request(sink.session_id, sink.run_id, expected=state, artifact=artifact, turn=turn,
                                           logical_turn=sink._request_context.get("logical_turn"))
    return str(artifact.handle)


__all__ = ["prepare_provider_request"]
