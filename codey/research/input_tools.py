"""Resources consumed by research finding and material inputs."""
from __future__ import annotations

from typing import Protocol

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.ledger import ResearchLedger
from codey.research.staging import StagedKnowledgeChanges, StagedKnowledgeStore
from codey.research.tool_output import ResearchToolOutput
from codey.types.json_value import ToolArgs


class ResearchInputTools(Protocol):
    @property
    def store(self) -> KnowledgeStore | StagedKnowledgeStore | None: ...
    @property
    def changes(self) -> KnowledgeChanges | StagedKnowledgeChanges | None: ...
    @property
    def ledger(self) -> ResearchLedger: ...
    @property
    def project(self) -> str: ...
    @property
    def session_id(self) -> str: ...
    @property
    def created_ids(self) -> list[str]: ...
    @property
    def updated_ids(self) -> list[str]: ...

    def knowledge_write(self, args: ToolArgs, *, finding_id: str = "", locator: str = "",
                        evidence_locators: tuple[str, ...] = (),
                        evidence_context: ResearchLedger | None = None) -> ResearchToolOutput: ...
