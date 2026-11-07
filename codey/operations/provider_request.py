"""Freeze provider invocations before they cross the durable boundary."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from codey.providers.request_codec import decode_provider_request, encode_provider_request
from codey.providers.requests import ProviderRequest
from codey.types.json_value import require_json_integer

if TYPE_CHECKING:
    from codey.operations.task_effects import KernelEffectSink

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.provider_receipts import REQUEST_KIND, write_provider_artifact
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


def prepare_provider_request(
    sink: KernelEffectSink,
    request: ProviderRequest,
    *,
    counters: tuple[RuntimeExecutionCounters, RuntimeExecutionCounters] | None,
) -> str:
    """Freeze one invocation and stable effect identity before any send."""
    request = decode_provider_request(encode_provider_request(request))
    task, effects = sink.durable.task_facts(sink.run_id)
    state = operation_from_checkpoint(task, sink.session_id)
    if state is None:
        raise RuntimeEffectError("provider request requires an admitted operation")
    turn = max((require_json_integer(effect.intent.get("turn", 0), "provider turn") for effect in effects
                if effect.category == EFFECT_CATEGORY_PROVIDER_SEND), default=0) + 1
    context = sink.request_context
    transport = {**encode_provider_request(request), **(context.to_payload() if context is not None else {})}
    intent = prepare_intent(sink.session_id, sink.run_id, RuntimeEffectIntent(
        effect_id=new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, sink.run_id),
        effect_category=EFFECT_CATEGORY_PROVIDER_SEND, session_id=sink.session_id, run_id=sink.run_id,
        phase=driver_for_state(state), provider_id=sink.provider_id, turn=turn,
        display_ref="closing_acknowledgement" if context is not None and context.delivery_purpose == "closing"
            and request.method.startswith("acknowledge_tool_results") else request.method,
        args_digest=compute_args_digest(json.dumps(transport, ensure_ascii=False, sort_keys=True)),
        replay_class=ReplayClass.UNSAFE, delivery_batch_id=sink.delivery_batch_id,
    ))
    before, proposed = counters if counters is not None else (state.counters, state.counters)
    payload = {**transport, "admission": {"intent": intent.to_payload(),
        "expected_counters": before.to_payload(), "counters": proposed.to_payload()}}
    artifact = write_provider_artifact(sink.managed_outputs, session_id=sink.session_id, run_id=sink.run_id,
                                      effect_id=intent.effect_id, kind=REQUEST_KIND, payload=payload)
    sink.mutations.prepare_provider_request(sink.session_id, sink.run_id, expected=state, artifact=artifact, turn=turn,
                                           logical_turn=context.logical_turn if context is not None else None)
    return str(artifact.handle)


__all__ = ["prepare_provider_request"]
