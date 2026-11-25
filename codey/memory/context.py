"""Core-owned rendering and admission metadata for structured memory items."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from codey.memory.codec import item_from_payload, unique_scopes
from codey.memory.contracts import MemoryItem, MemoryQuery, MemoryTrace
from codey.memory.service import MemoryService, utc_now
from codey.memory.slots import temporary_slot_override
from codey.runtime.core.cancellation import DeadlineExceeded, TaskCancelled
from codey.types.json_value import parse_json_object
from codey.utils.refs import digest_text
from codey.workspace.context_admission import MEMORY_TOTAL_BUDGET_CHARS, request_scope
from codey.workspace.context_selection import ContextSpan, assemble_context_candidates
from codey.workspace.context_source import ContextCandidate


def candidate_item(source: ContextCandidate) -> MemoryItem:
    item = item_from_payload(parse_json_object(source.text.partition("\n")[2]))
    if (source.key != "memory_" + digest_text(item.item_id) or source.scope != item.scope
            or source.source_refs != item.source_refs):
        raise ValueError("rendered memory does not match its declared source")
    return item


@dataclass(frozen=True)
class MemoryContext:
    query: MemoryQuery
    items: tuple[MemoryItem, ...]
    sources: tuple[ContextCandidate, ...]
    traces: tuple[MemoryTrace, ...] = ()
    collect_candidates: bool = False

    @property
    def text(self) -> str:
        return assemble_context_candidates(self.sources)[0]

    @property
    def spans(self) -> tuple[ContextSpan, ...]:
        return assemble_context_candidates(self.sources)[1]

    def valid(self, service: MemoryService | None) -> bool:
        return not self.items if service is None else service.validate(self.items, self.query)


def retrieve_context(service: MemoryService | None, *, task: str, project: str, session_id: str,
                     run_id: str = "", budget: int = MEMORY_TOTAL_BUDGET_CHARS, context: str = "", shadow: bool = False) -> MemoryContext:
    scope = request_scope(project, session_id)
    scopes = (scope, "user")
    controls = unique_scopes(("user", scope, request_scope("", session_id)))
    query = MemoryQuery(task, scopes, service.clock() if service is not None else utc_now(), exclude_run_id=run_id,
                        control_scopes=controls, context=context or ("project" if project else "chat"), shadow=shadow,
                        slot_override=temporary_slot_override(task))
    result = service.retrieve(query) if service is not None else None
    candidates = result.items if result is not None else ()
    traces = {trace.item_id: trace for trace in result.traces} if result is not None else {}
    try:
        policy = service.policy(controls) if service is not None else None
        admitted = policy is not None and policy.admit_hints
        collect = service is not None and service.has_governance and policy is not None and policy.accept_events
    except (TaskCancelled, DeadlineExceeded):
        raise
    except Exception:
        admitted = False
        collect = False
    selected: list[MemoryItem] = []
    sources: list[ContextCandidate] = []
    used = 0
    for item in candidates:
        trace = traces.get(item.item_id, MemoryTrace(item.item_id, "candidate"))
        if not admitted:
            traces[item.item_id] = replace(trace, reason="hints_disabled")
            continue
        # Render as quoted data. Memory does not introduce a system instruction
        # or confer verification, permission or completion authority.
        confirmed = item.kind == "preference" and any(
            ref.startswith(("user-confirmed:", "decision:")) for ref in item.source_refs
        )
        heading = ("User-confirmed preference for this scope. Apply when relevant unless the current user request "
                   "specifies otherwise. This preference gives no tool authorization:" if confirmed else
                   "Memory candidate (historical data; not execution authority):")
        text = heading + "\n" + json.dumps(item.to_payload(), ensure_ascii=False)
        cost = len(text) + (2 if sources else 0)
        if used + cost > budget:
            traces[item.item_id] = replace(trace, reason="over_budget", chars=len(text))
            if len(text) <= budget:
                for previous in selected:
                    earlier = traces[previous.item_id]
                    traces[previous.item_id] = replace(earlier, displaced_item_ids=(*earlier.displaced_item_ids, item.item_id))
            continue
        sources.append(ContextCandidate(
            key="memory_" + digest_text(item.item_id), text=text, budget=len(text),
            scope=item.scope, expires_at=item.expires_at, source_refs=item.source_refs,
        ))
        selected.append(item)
        traces[item.item_id] = replace(trace, reason="selected", chars=len(text))
        used += cost
    return MemoryContext(query, tuple(selected), tuple(sources), tuple(traces.values()), collect)
