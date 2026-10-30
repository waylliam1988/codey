"""Knowledge admission for a fresh shared-kernel generation, separate from Ghost."""

from __future__ import annotations

from typing import Any

from codey.knowledge.recall import render_recall


def knowledge_background(tools: Any, session: Any, question: str) -> str:
    policy = getattr(session, "policy", None)
    if tools is None or getattr(tools, "store", None) is None or session.task_kind == "evidence_followup":
        return ""
    allows = getattr(policy, "allows", None)
    if not callable(allows) or not allows("knowledge.read"):
        return ""
    # Reports remain available by explicit search/read, but automatic background
    # must not replay the current draft into its own synthesis or count previous
    # answers as fresh evidence. Filter before retrieval limits and expansion.
    hits = tools.store.search(question, project=tools.project, session_id=tools.session_id, limit=10, depth=2,
                              exclude_kinds=("answer",))
    scope_guidance = (f"Personal knowledge owner for this isolated knowledge space: {tools.store.owner_id}. "
        "Use scope=personal with that scope_id for authorized user facts and their private inferences; "
        "scope=session is limited to the current conversation. Retain observation time and user_statement origin. "
        "Public research uses global or the current project scope.\n")
    return scope_guidance + render_recall(hits, max_chars=6000)
