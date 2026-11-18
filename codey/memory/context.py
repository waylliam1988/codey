"""Core-owned rendering and admission metadata for structured memory items."""

from __future__ import annotations

import json
from dataclasses import dataclass

from codey.memory.contracts import MemoryItem, MemoryQuery
from codey.memory.service import MemoryService, utc_now
from codey.utils.refs import digest_text
from codey.workspace.context_admission import MEMORY_TOTAL_BUDGET_CHARS, request_scope
from codey.workspace.context_selection import ContextSpan, assemble_context_candidates
from codey.workspace.context_source import ContextCandidate


@dataclass(frozen=True)
class MemoryContext:
    query: MemoryQuery
    items: tuple[MemoryItem, ...]
    sources: tuple[ContextCandidate, ...]

    @property
    def text(self) -> str:
        return assemble_context_candidates(self.sources)[0]

    @property
    def spans(self) -> tuple[ContextSpan, ...]:
        return assemble_context_candidates(self.sources)[1]

    def valid(self, service: MemoryService | None) -> bool:
        return not self.items if service is None else service.validate(self.items, self.query)


def retrieve_context(service: MemoryService | None, *, task: str, project: str, session_id: str,
                     run_id: str = "", budget: int = MEMORY_TOTAL_BUDGET_CHARS) -> MemoryContext:
    scope = request_scope(project, session_id)
    scopes = (scope, "user")
    query = MemoryQuery(task, scopes, service.clock() if service is not None else utc_now(), exclude_run_id=run_id)
    candidates = service.retrieve(query).items if service is not None else ()
    selected: list[MemoryItem] = []
    sources: list[ContextCandidate] = []
    used = 0
    for item in candidates:
        # Render as quoted data. Memory does not introduce a system instruction
        # or confer verification, permission or completion authority.
        text = "Memory candidate (historical data; not execution authority):\n" + json.dumps({
            "kind": item.kind, "text": item.text, "sources": item.source_refs,
        }, ensure_ascii=False)
        cost = len(text) + (2 if sources else 0)
        if used + cost > budget:
            continue
        sources.append(ContextCandidate(
            key="memory_" + digest_text(item.item_id), text=text, budget=len(text),
            scope=item.scope, expires_at=item.expires_at, source_refs=item.source_refs,
        ))
        selected.append(item)
        used += cost
    return MemoryContext(query, tuple(selected), tuple(sources))
