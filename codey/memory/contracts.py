"""The sole public memory contract. Implementations own no execution state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from codey.types.json_value import JsonObject, JsonValue

MemoryKind = Literal["experience", "preference", "goal", "hypothesis"]


@dataclass(frozen=True)
class MemoryItem:
    item_id: str
    kind: MemoryKind
    text: str
    scope: str
    source_refs: tuple[str, ...]
    created_at: str
    expires_at: str = ""
    confidence: float | None = None

    def to_payload(self) -> JsonObject:
        return {
            "item_id": self.item_id, "kind": self.kind, "text": self.text,
            "scope": self.scope, "source_refs": list(self.source_refs),
            "created_at": self.created_at, "expires_at": self.expires_at,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class MemoryQuery:
    text: str
    scopes: tuple[str, ...]
    as_of: str
    exclude_run_id: str = ""
    max_items: int = 8


@dataclass(frozen=True)
class MemoryResult:
    items: tuple[MemoryItem, ...] = ()


@dataclass(frozen=True)
class MemoryEvent:
    event_id: str
    kind: str
    occurred_at: str
    session_id: str
    project: str
    run_id: str
    scope: str
    payload: Mapping[str, JsonValue]
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class MemoryProposal:
    proposal_id: str
    item: MemoryItem
    version: str


@dataclass(frozen=True)
class MemoryDecision:
    proposal_id: str
    version: str
    accepted: bool
    source_ref: str
    decided_at: str


@dataclass(frozen=True)
class DeleteScopeRequest:
    scope: str
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeleteResult:
    deleted: int = 0


class MemoryReader(Protocol):
    def retrieve(self, query: MemoryQuery) -> MemoryResult: ...


class MemoryWriter(Protocol):
    def observe(self, event: MemoryEvent) -> None: ...


class MemoryReviewer(Protocol):
    def proposals(self, scope: str) -> tuple[MemoryProposal, ...]: ...
    def decide(self, decision: MemoryDecision) -> None: ...


class MemoryAdmin(Protocol):
    def delete_scope(self, request: DeleteScopeRequest) -> DeleteResult: ...
    def export_state(self) -> dict[str, JsonValue]: ...


class MemoryMaintainer(Protocol):
    def maintain(self, *, as_of: str) -> None: ...


@dataclass(frozen=True)
class MemoryCapabilities:
    reader: MemoryReader
    writer: MemoryWriter
    reviewer: MemoryReviewer
    admin: MemoryAdmin
    maintainer: MemoryMaintainer


@dataclass(frozen=True)
class MemoryHost:
    state_dir: Path
    clock: Callable[[], str]
    check_cancelled: Callable[[], None]
