"""The durable-backed runtime adapters owned by :class:`AppContext`.

This is an explicit domain boundary for the runtime projection readers and
writer.  It is intentionally a fixed record rather than a generic service
container: every member has a distinct durable contract and all five share the
same ``DurableSession``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.write.mutation_line import RuntimeMutationLine


@dataclass
class DurableRuntimeAdapters:
    durable: Any
    mutations: RuntimeMutationLine
    operations: RuntimeOperationStore
    effects: RuntimeEffectStore
    delivery: ToolResultDeliveryStore

    @classmethod
    def open(cls, durable: Any) -> DurableRuntimeAdapters:
        if durable is None:
            raise ValueError("durable runtime adapters require durable storage")
        return cls(
            durable=durable,
            mutations=RuntimeMutationLine(durable=durable),
            operations=RuntimeOperationStore(durable=durable),
            effects=RuntimeEffectStore(durable=durable),
            delivery=ToolResultDeliveryStore(durable=durable),
        )


__all__ = ["DurableRuntimeAdapters"]
