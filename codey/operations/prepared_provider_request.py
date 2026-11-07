"""Validated durable admission data; no generic invocation bag enters the driver."""

from __future__ import annotations

from dataclasses import dataclass

from codey.operations.provider_request_context import ProviderRequestContext
from codey.providers.request_codec import decode_provider_request
from codey.providers.requests import ProviderRequest
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.effects.effect_records import RuntimeEffectIntent
from codey.types.json_value import decode_json_object


@dataclass(frozen=True)
class PreparedProviderRequest:
    request: ProviderRequest
    context: ProviderRequestContext | None
    intent: RuntimeEffectIntent
    expected_counters: RuntimeExecutionCounters
    counters: RuntimeExecutionCounters

    @classmethod
    def from_payload(cls, value: object, *, intent: RuntimeEffectIntent) -> PreparedProviderRequest:
        row = decode_json_object(value)
        admission = decode_json_object(row.get("admission"))
        if set(admission) != {"intent", "expected_counters", "counters"}:
            raise ValueError("prepared request admission data is invalid")
        return cls(decode_provider_request(row),
            ProviderRequestContext.from_payload(row) if "snapshot" in row else None, intent,
            RuntimeExecutionCounters.from_payload(admission["expected_counters"]),
            RuntimeExecutionCounters.from_payload(admission["counters"]))
