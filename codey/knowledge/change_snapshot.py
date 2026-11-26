from __future__ import annotations

from dataclasses import dataclass, field

from codey.workspace.changes import RestoreResult


@dataclass(frozen=True)
class KnowledgeChangesSnapshot:
    before: dict[str, str | None]
    after_hashes: dict[str, str]
    touched: frozenset[str]
    last_restore_result: RestoreResult | None = None
    document_revisions: dict[str, int] = field(default_factory=dict)
