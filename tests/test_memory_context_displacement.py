"""A candidate that fits by itself can be displaced by an earlier item."""

from dataclasses import dataclass

from codey.memory.context import retrieve_context
from codey.memory.contracts import MemoryCapabilities, MemoryItem, MemoryQuery, MemoryResult
from codey.memory.null import NullMemory
from codey.memory.service import MemoryService


@dataclass
class _Reader(NullMemory):
    items: tuple[MemoryItem, ...]

    def retrieve(self, query: MemoryQuery) -> MemoryResult:
        return MemoryResult(self.items)


def test_memory_records_displacement_at_exact_standalone_budget() -> None:
    at = "2026-11-25T00:00:00Z"
    first = MemoryItem("first", "goal", "Short memory", "user", ("record:short",), at)
    second = MemoryItem("second", "goal", "Longer memory " * 10, "user", ("record:long",), at)
    backend = _Reader((first, second))

    def available(_ref: str) -> bool:
        return True

    memory = MemoryService(MemoryCapabilities(backend, backend, backend, backend, backend),
                           source_available=available, clock=lambda: at)
    complete = retrieve_context(memory, task="plan", project="", session_id="test")
    second_size = len(complete.sources[1].text)

    limited = retrieve_context(memory, task="plan", project="", session_id="test", budget=second_size)

    assert limited.items == (first,)
    assert {trace.item_id: trace.reason for trace in limited.traces} == {"first": "selected", "second": "over_budget"}
    first_trace = next(trace for trace in limited.traces if trace.item_id == first.item_id)
    assert first_trace.displaced_item_ids == (second.item_id,)

    smaller = retrieve_context(memory, task="plan", project="", session_id="test", budget=second_size - 1)
    assert smaller.items == (first,)
    first_trace = next(trace for trace in smaller.traces if trace.item_id == first.item_id)
    assert first_trace.displaced_item_ids == ()
