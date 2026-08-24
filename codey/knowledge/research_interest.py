"""Structured research-interest candidates from bounded knowledge facts."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Any, Iterable

from codey.knowledge.concept_schema import normalize_concept
from codey.knowledge.concepts import ConceptGraphBuilder, MissingConceptLink
from codey.knowledge.note import clean_open_questions
from codey.knowledge.store import KnowledgeStore


MAX_RESEARCH_INTEREST_CANDIDATES = 8
MAX_RESEARCH_INTEREST_REFS = 10
MAX_RESEARCH_QUESTION_CHARS = 140
MAX_RESEARCH_WHY_CHARS = 240


@dataclass(frozen=True)
class ResearchInterestCandidate:
    id: str
    question: str
    related_concepts: tuple[str, ...]
    shared_neighbors: tuple[str, ...]
    source_refs: tuple[str, ...]
    scope: str
    scope_ref: str
    priority: float
    confidence: float
    why_now: str
    source: str
    source_ref: str
    strong_support: bool = False


def build_research_interest_candidates(
    store: KnowledgeStore | None,
    *,
    session_id: str = "",
    project: str = "",
    limit: int = MAX_RESEARCH_INTEREST_CANDIDATES,
) -> tuple[ResearchInterestCandidate, ...]:
    if store is None or getattr(store, "index", None) is None:
        return ()
    limit = _bounded_int(limit, MAX_RESEARCH_INTEREST_CANDIDATES, 1, 32)
    rows: list[ResearchInterestCandidate] = []
    rows.extend(_candidates_from_recent_research_notes(
        store,
        session_id=session_id,
        project=project,
        limit=limit,
    ))
    remaining = max(0, limit - len(rows))
    if remaining:
        rows.extend(_candidates_from_missing_concept_links(
            store,
            session_id=session_id,
            project=project,
            limit=remaining,
        ))
    return _dedupe_candidates(rows, limit=limit)


def candidate_to_topic_hint(candidate: ResearchInterestCandidate) -> dict[str, object]:
    """Neutral bounded hint dict for downstream topic-continuity projections.

    Keeps the knowledge layer as the single owner of its own object shape:
    consumers (e.g. research topic continuity) never import this module's
    dataclass, they consume the neutral mapping produced here.
    """
    return {
        "ref": f"research_interest:{candidate.id}",
        "question": candidate.question,
        "why_now": candidate.why_now,
        "related_concepts": list(candidate.related_concepts[:4]),
        "priority": float(candidate.priority),
        "confidence": float(candidate.confidence),
        "strong_support": bool(candidate.strong_support),
        "source": candidate.source,
        "source_ref": candidate.source_ref,
    }


def apply_research_affinity_hints(
    candidates: Iterable[ResearchInterestCandidate],
    hints: Iterable[Any],
) -> tuple[ResearchInterestCandidate, ...]:
    rows = tuple(candidates or ())
    if not rows:
        return ()
    hint_weights = _hint_weight_by_target(hints, kind="research_priority")
    if not hint_weights:
        return rows
    boosted = tuple(
        replace(
            candidate,
            priority=_unit_float(candidate.priority + min(0.14, hint_weights.get(candidate.id, 0.0) * 0.14)),
        )
        for candidate in rows
    )
    return tuple(sorted(boosted, key=lambda item: (-item.priority, -item.confidence, item.question)))


def _candidates_from_recent_research_notes(
    store: KnowledgeStore,
    *,
    session_id: str,
    project: str,
    limit: int,
) -> list[ResearchInterestCandidate]:
    clean_session = _clip(session_id, 120)
    clean_project = _clip(project, 240)
    try:
        notes = list(store.index.recent(
            6,
            session_id=clean_session,
            project=clean_project,
            types=("synthesis", "decision"),
        ))
    except Exception:
        return []
    out: list[ResearchInterestCandidate] = []
    for note in notes:
        if str(note.get("status") or "active") != "active":
            continue
        note_id = _clip(_get(note, "id"), 120)
        if not note_id:
            continue
        scope, scope_ref = _scope_for_note(note, session_id=session_id, project=project)
        for question in _structured_open_questions(_get(note, "open_questions")):
            candidate = _candidate(
                question=question,
                related_concepts=(),
                shared_neighbors=(),
                source_refs=(f"note:{note_id}",),
                scope=scope,
                scope_ref=scope_ref,
                priority=0.68,
                confidence=0.78,
                why_now="Open question from a bounded local Research note.",
                source="research_note",
                source_ref=note_id,
                strong_support=True,
            )
            if candidate is not None:
                out.append(candidate)
            if len(out) >= limit:
                return out
    return out


def _candidates_from_missing_concept_links(
    store: KnowledgeStore,
    *,
    session_id: str,
    project: str,
    limit: int,
) -> list[ResearchInterestCandidate]:
    try:
        links = ConceptGraphBuilder(store).missing_links_for_session(
            session_id=_clip(session_id, 120),
            project=_clip(project, 240),
            strict_scope=True,
            limit=limit,
        )
    except Exception:
        return []
    scope = "session" if _clip(session_id, 120) else "project" if _clip(project, 240) else "user"
    scope_ref = _clip(session_id, 120) or _clip(project, 240)
    out: list[ResearchInterestCandidate] = []
    for link in links:
        candidate = _candidate_from_missing_link(link, scope=scope, scope_ref=scope_ref)
        if candidate is not None:
            out.append(candidate)
        if len(out) >= limit:
            break
    return out


def _candidate_from_missing_link(
    link: MissingConceptLink,
    *,
    scope: str,
    scope_ref: str,
) -> ResearchInterestCandidate | None:
    left = normalize_concept(link.left)
    right = normalize_concept(link.right)
    if not left or not right or left == right:
        return None
    neighbors = tuple(
        concept
        for concept in (normalize_concept(item) for item in link.shared_neighbors)
        if concept and concept not in {left, right}
    )[:6]
    if not neighbors:
        return None
    support_refs = tuple(
        f"note:{ref.note_id}"
        for ref in link.support_refs
        if _clip(ref.note_id, 120)
    )[:MAX_RESEARCH_INTEREST_REFS]
    strong = len(set(support_refs)) >= 2 or len(neighbors) >= 2 or bool(link.session_focus)
    question = f"Research whether {left} and {right} are connected."
    why = "Shared declared neighbor"
    if len(neighbors) != 1:
        why += "s"
    why += f": {', '.join(neighbors[:3])}."
    return _candidate(
        question=question,
        related_concepts=(left, right),
        shared_neighbors=neighbors,
        source_refs=support_refs,
        scope=scope,
        scope_ref=scope_ref,
        priority=link.priority,
        confidence=0.76 if strong else 0.62,
        why_now=why,
        source="concept_open_question",
        source_ref=_concept_pair_ref(left, right),
        strong_support=strong,
    )


def _candidate(
    *,
    question: str,
    related_concepts: Iterable[str],
    shared_neighbors: Iterable[str],
    source_refs: Iterable[str],
    scope: str,
    scope_ref: str,
    priority: float,
    confidence: float,
    why_now: str,
    source: str,
    source_ref: str,
    strong_support: bool,
) -> ResearchInterestCandidate | None:
    clean_question = _clean_text(question, MAX_RESEARCH_QUESTION_CHARS)
    clean_why = _clean_text(why_now, MAX_RESEARCH_WHY_CHARS)
    clean_source = _clean_token(source, 80)
    clean_source_ref = _clean_token(source_ref, 160)
    if not clean_question or not clean_source or not clean_source_ref:
        return None
    clean_scope = scope if scope in {"user", "project", "session"} else "user"
    refs = _bounded_refs(source_refs)
    concepts = tuple(
        concept
        for concept in (normalize_concept(item) for item in related_concepts)
        if concept
    )[:6]
    neighbors = tuple(
        concept
        for concept in (normalize_concept(item) for item in shared_neighbors)
        if concept
    )[:6]
    candidate_id = _stable_id(
        clean_scope,
        _clip(scope_ref, 240),
        clean_source,
        clean_source_ref,
        clean_question,
    )
    return ResearchInterestCandidate(
        id=candidate_id,
        question=clean_question,
        related_concepts=concepts,
        shared_neighbors=neighbors,
        source_refs=refs,
        scope=clean_scope,
        scope_ref=_clip(scope_ref, 240),
        priority=_unit_float(priority),
        confidence=_unit_float(confidence),
        why_now=clean_why,
        source=clean_source,
        source_ref=clean_source_ref,
        strong_support=bool(strong_support),
    )


def _structured_open_questions(value: object, *, limit: int = 4) -> tuple[str, ...]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    out: list[str] = []
    for item in clean_open_questions(raw):
        cleaned = _clean_text(item, MAX_RESEARCH_QUESTION_CHARS)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= limit:
            break
    return tuple(out)


def _scope_for_note(note: dict[str, object], *, session_id: str, project: str) -> tuple[str, str]:
    note_session = _clip(_get(note, "session_id") or session_id, 120)
    if note_session:
        return "session", note_session
    note_project = _clip(_get(note, "project") or project, 240)
    if note_project:
        return "project", note_project
    return "user", ""


def _dedupe_candidates(
    candidates: Iterable[ResearchInterestCandidate],
    *,
    limit: int,
) -> tuple[ResearchInterestCandidate, ...]:
    by_id: dict[str, ResearchInterestCandidate] = {}
    for candidate in candidates:
        if candidate.id not in by_id:
            by_id[candidate.id] = candidate
    rows = sorted(by_id.values(), key=lambda item: (-item.priority, -item.confidence, item.question))
    return tuple(rows[:limit])


def _bounded_refs(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = _clean_token(value, 160)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_RESEARCH_INTEREST_REFS:
            break
    return tuple(out)


def _concept_pair_ref(left: str, right: str) -> str:
    ordered = sorted((left, right))
    return "concept:" + hashlib.sha256("|".join(ordered).encode("utf-8", errors="replace")).hexdigest()[:16]


def _stable_id(scope: str, scope_ref: str, source: str, source_ref: str, question: str) -> str:
    key = "|".join((scope, scope_ref, source, source_ref, " ".join(question.split()).casefold()))
    return "ric_" + hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:24]


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    text = _clip(text, limit).rstrip(".")
    lower = text.casefold()
    if not text or "ghost" in lower or "work queue" in lower or "concept graph" in lower:
        return ""
    if any(secret in lower for secret in ("api_key", "api key", "password", "token sk-")):
        return ""
    return text


def _clean_token(value: object, limit: int) -> str:
    text = _clip(value, limit)
    if not text:
        return ""
    if any(char in text for char in "\r\n\t"):
        return ""
    return text


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _get(row: dict[str, object], key: str) -> object:
    return row.get(key)


def _unit_float(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, number)), 4)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _hint_weight_by_target(hints: Iterable[Any], *, kind: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for hint in list(hints or ()):
        if str(_field(hint, "kind") or "").strip().lower() != kind:
            continue
        target = _clip(_field(hint, "target"), 120)
        if not target:
            continue
        try:
            weight = float(_field(hint, "weight") or 0.0) * float(_field(hint, "confidence") or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        out[target] = max(out.get(target, 0.0), max(0.0, min(1.0, weight)))
    return out


def _field(value: Any, key: str) -> object:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, "")


__all__ = [
    "MAX_RESEARCH_INTEREST_CANDIDATES",
    "ResearchInterestCandidate",
    "apply_research_affinity_hints",
    "build_research_interest_candidates",
    "candidate_to_topic_hint",
]
