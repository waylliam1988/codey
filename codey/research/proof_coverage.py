"""Coverage scoring for research proof reviews."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from codey.research.proof_terms import (
    normalized_search_text,
    question_terms,
    safe_term_ref,
    search_term_present,
)
from codey.utils.refs import clip, identifier

MAX_GAPS = 12


@dataclass(frozen=True)
class CoverageGap:
    reason_code: str
    term_ref: str = ""
    detail: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reason_code": identifier(self.reason_code, 80) or "coverage_gap",
        }
        if self.term_ref:
            payload["term_ref"] = identifier(self.term_ref, 80)
        if self.detail:
            payload["detail"] = clip(self.detail, 120)
        return payload


@dataclass(frozen=True)
class CoverageResult:
    score: float
    gaps: tuple[CoverageGap, ...]
    unmatched_terms: tuple[str, ...]


def answer_coverage(
    question: str,
    claims: Mapping[str, Mapping[str, object]],
    evidence: Mapping[str, Mapping[str, object]],
    bounded_score: Callable[[object], float],
) -> CoverageResult:
    terms = question_terms(question)
    if not terms:
        supported = any(
            identifier(item.get("claim_section"), 80) in {"conclusion", "evidence"}
            and identifier(item.get("status"), 40) == "evidence_backed"
            for item in claims.values()
        )
        return CoverageResult(1.0 if supported else 0.0, (), ())
    haystack = normalized_search_text(
        " ".join(
            [
                *(str(item.get("claim_text") or "") for item in claims.values()),
                *(str(item.get("bounded_excerpt") or "") for item in evidence.values()),
            ]
        )
    )
    unmatched = tuple(term for term in terms if not search_term_present(term, haystack))
    score = bounded_score((len(terms) - len(unmatched)) / max(1, len(terms)))
    gaps = tuple(
        CoverageGap("missing_question_term", term_ref=safe_term_ref(term))
        for term in unmatched[:MAX_GAPS]
        if safe_term_ref(term)
    )
    if unmatched and not gaps:
        gaps = (CoverageGap("missing_question_term"),)
    return CoverageResult(score, gaps, unmatched)

