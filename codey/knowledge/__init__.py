"""Durable knowledge primitives used by Codey Research and project memory.

Public convenience exports resolve lazily so importing a leaf (or the
package itself) never loads the whole store/graph stack. Mirrors
``codey.providers`` and ``codey.research``.
"""

from __future__ import annotations

from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "ConceptGraphBuilder": ("codey.knowledge.concepts", "ConceptGraphBuilder"),
    "KnowledgeBriefBuilder": ("codey.knowledge.brief", "KnowledgeBriefBuilder"),
    "KnowledgeChanges": ("codey.knowledge.changes", "KnowledgeChanges"),
    "KnowledgeChangesSnapshot": ("codey.knowledge.changes", "KnowledgeChangesSnapshot"),
    "KnowledgeGraphBuilder": ("codey.knowledge.graph", "KnowledgeGraphBuilder"),
    "KnowledgeNote": ("codey.knowledge.note", "KnowledgeNote"),
    "KnowledgeStore": ("codey.knowledge.store", "KnowledgeStore"),
    "NOTE_TYPES": ("codey.knowledge.note", "NOTE_TYPES"),
    "ResearchBrief": ("codey.knowledge.brief", "ResearchBrief"),
    "ResearchGraphArtifact": ("codey.knowledge.graph", "ResearchGraphArtifact"),
    "ResearchInterestCandidate": (
        "codey.knowledge.research_interest",
        "ResearchInterestCandidate",
    ),
    "RestoreResult": ("codey.knowledge.changes", "RestoreResult"),
    "build_research_interest_candidates": (
        "codey.knowledge.research_interest",
        "build_research_interest_candidates",
    ),
    "build_unified_research_graph": (
        "codey.knowledge.concepts",
        "build_unified_research_graph",
    ),
    "candidate_to_topic_hint": (
        "codey.knowledge.research_interest",
        "candidate_to_topic_hint",
    ),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = __import__(module_name, fromlist=[attr_name])
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
