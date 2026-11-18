"""Empty capabilities used when memory is disabled."""

from codey.memory.contracts import (
    DeleteResult,
    DeleteScopeRequest,
    MemoryCapabilities,
    MemoryDecision,
    MemoryEvent,
    MemoryProposal,
    MemoryQuery,
    MemoryResult,
)
from codey.types.json_value import JsonValue


class NullMemory:
    def retrieve(self, query: MemoryQuery) -> MemoryResult:
        return MemoryResult()

    def observe(self, event: MemoryEvent) -> None:
        if event.kind == "memory.proposed":
            raise ValueError("memory is disabled")

    def proposals(self, scope: str) -> tuple[MemoryProposal, ...]:
        return ()

    def decide(self, decision: MemoryDecision) -> None:
        raise ValueError("memory is disabled")

    def delete_scope(self, request: DeleteScopeRequest) -> DeleteResult:
        return DeleteResult()

    def export_state(self) -> dict[str, JsonValue]:
        return {"enabled": False, "items": [], "proposals": []}

    def maintain(self, *, as_of: str) -> None:
        pass


def null_capabilities() -> MemoryCapabilities:
    empty = NullMemory()
    return MemoryCapabilities(empty, empty, empty, empty, empty)
