"""Knowledge admission for a fresh shared-kernel generation, separate from Ghost."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.knowledge.recall import render_recall
from codey.types.json_value import decode_json_object, decode_json_value, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession
    from codey.research.tools import ResearchTools


def knowledge_background(tools: ResearchTools | None, session: TaskSession, question: str) -> str:
    policy = session.policy
    if tools is None or tools.store is None or session.task_kind == "evidence_followup":
        return ""
    allows = policy.allows
    if not allows("knowledge.read"):
        return ""
    # Reports remain available by explicit search/read, but automatic background
    # must not replay the current draft into its own synthesis or count previous
    # answers as fresh evidence. Filter before retrieval limits and expansion.
    current_ids: set[str] = set()
    if session.task_kind == "research":
        for note_id in (*tools.created_ids, *tools.updated_ids):
            raw_references: object = tools.store.note_references(note_id)
            references = decode_json_value(raw_references)
            assert isinstance(references, list)
            for raw in references:
                ref = decode_json_object(raw)
                value = tools.store.repository.read(require_json_string(ref["target"], "knowledge id"),
                                                    require_json_integer(ref["revision"], "knowledge revision"))
                if value is not None and value.value.kind != "entity":
                    current_ids.add(value.value.id)
        # These versions already travel in the receipt-backed evidence handoff
        # and are pinned by the synthesis archive, not prior-work recall.
    hits = tools.store.search(question, project=tools.project, session_id=tools.session_id, limit=10, depth=2,
                              exclude_kinds=("answer",), exclude_ids=tuple(sorted(current_ids)))
    scope_guidance = (f"Personal knowledge owner for this isolated knowledge space: {tools.store.owner_id}. "
        "Use scope=personal with that scope_id for authorized user facts and their private inferences; "
        "scope=session is limited to the current conversation. Retain observation time and user_statement origin. "
        "Durable research defaults to personal scope without a project, or project scope with one. "
        "Global is explicitly public; temporary content uses session scope.\n") if allows("knowledge.write") else ""
    strict_research: object = getattr(policy, "strict_research", False)
    if strict_research:
        from codey.research.answer_contract import whole_research

        # Whole-answer publication derives scope; its input accepts no scope fields.
        prefix = "" if whole_research(session) else scope_guidance
        return prefix + render_recall(hits, max_chars=4000, include_answer_refs=False, compact=True)
    return scope_guidance + render_recall(hits, max_chars=6000)
