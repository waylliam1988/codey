"""Typed research tool results shared by acquisition and stored material reads."""
from __future__ import annotations

from dataclasses import dataclass, field

from codey.knowledge.model import KnowledgeReference
from codey.research.ledger import ResearchLedger
from codey.research.passages import PassageDescriptor
from codey.research.source_acquisition import SourceFailureCode
from codey.types.json_value import JsonObject, canonical_json_object


@dataclass(frozen=True)
class ResearchToolOutput:
    """Model-visible text plus the full text reserved for durable receipts."""

    model_text: str
    receipt_text: str = ""
    read_passages: tuple[PassageDescriptor, ...] = ()
    read_observation: JsonObject = field(default_factory=dict)
    ok: bool = field(kw_only=True)
    knowledge_search_empty: bool = field(default=False, kw_only=True)
    knowledge_read_versions: tuple[KnowledgeReference, ...] = field(default=(), kw_only=True)
    acquisition: JsonObject = field(default_factory=dict, kw_only=True)
    failure_code: SourceFailureCode = field(default="request_failed", kw_only=True)

    def __post_init__(self) -> None:
        if type(self.ok) is not bool:
            raise TypeError("research output ok must be an exact boolean")
        observation = canonical_json_object(self.read_observation)
        object.__setattr__(self, "read_observation", observation)
        object.__setattr__(self, "acquisition", canonical_json_object(self.acquisition))
        if self.read_passages:
            from codey.research.passages import restore_passages

            ledger = ResearchLedger()
            restore_passages(ledger, observation, self.read_passages)
            object.__setattr__(self, "read_passages", tuple(row.descriptor() for row in ledger.read_passages.values()))

