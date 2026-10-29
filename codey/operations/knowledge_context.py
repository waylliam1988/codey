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
    hits = tools.store.search(question, project=tools.project, session_id=tools.session_id, limit=10, depth=2)
    return render_recall(hits, max_chars=6000)
