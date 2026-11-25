"""Oversized JSON integers are invalid memory numeric inputs."""

import pytest

from codey.memory.codec import item_from_payload, trace_from_payload
from codey.memory.contracts import MemoryItem, MemoryTrace


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400), True], ids=["huge", "negative-huge", "bool"])
def test_memory_confidence_rejects_out_of_range_json_numbers(value: int | bool) -> None:
    item = MemoryItem("item", "goal", "Measure latency", "user", ("record:source",), "2026-11-20T00:00:00Z")
    with pytest.raises(ValueError, match="confidence must be a finite number"):
        item_from_payload({**item.to_payload(), "confidence": value})
    assert item_from_payload({**item.to_payload(), "confidence": 1}).confidence == 1.0


@pytest.mark.parametrize("value", [10 ** 400, -(10 ** 400), True], ids=["huge", "negative-huge", "bool"])
def test_memory_trace_rejects_unrepresentable_scores(value: int | bool) -> None:
    trace = MemoryTrace("item", "candidate").to_payload()
    with pytest.raises(ValueError, match="trace score must be finite"):
        trace_from_payload({**trace, "score": value})
    assert trace_from_payload({**trace, "score": -2}).score == -2.0
