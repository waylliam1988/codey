"""Deterministic Research proof review.

This module is a projection layer over ResearchRecord/EvidenceLedger facts. It
does not call models, fetch sources, inspect raw webpages, or read Ghost state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from codey.research.evidence_runtime import normalize_runtime_ref as _normalize_runtime_ref
from codey.research.identity import (
    bounded_refs,
    clip,
    digest_json,
    identifier,
    nonnegative_int,
    stable_ref,
)
from codey.research.object_model import ResearchRecord
from codey.research.shape import digest_ref as _digest_ref
from codey.research.redaction import looks_sensitive_signal


MAX_GAPS = 12
MAX_SIGNALS = 8
MAX_WARNINGS = 12
MAX_DIAGNOSTICS = 64
MIN_QUEUE_COVERAGE_SCORE = 0.62
DIAGNOSTIC_REF_KINDS = (
    ("claim_ref", "claim"),
    ("evidence_ref", "evidence"),
    ("source_ref", "source"),
    ("relation_ref", "relation"),
)


@dataclass(frozen=True)
class ProofDiagnostic:
    """One located proof problem with the refs needed to act on it.

    Reason codes stay the single source of truth for meaning; the extra fields
    only record where the problem was observed, so downstream projections can
    point at the exact claim/evidence/source/relation instead of re-walking
    the relation graph.
    """

    reason_code: str
    claim_ref: str = ""
    evidence_ref: str = ""
    source_ref: str = ""
    relation_ref: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reason_code": identifier(self.reason_code, 80),
        }
        for key, ref_kind in DIAGNOSTIC_REF_KINDS:
            value = _normalize_runtime_ref(getattr(self, key), kind=ref_kind)
            if value:
                payload[key] = value
        return payload

    def _key(self) -> tuple[str, str, str, str, str]:
        return (
            self.reason_code,
            self.claim_ref,
            self.evidence_ref,
            self.source_ref,
            self.relation_ref,
        )


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
class PlannerSignal:
    kind: str
    text: str
    reason_code: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": identifier(self.kind, 80) or "followup_question",
            "text": clip(self.text, 180),
            "reason_code": identifier(self.reason_code, 80),
        }


@dataclass(frozen=True)
class ResearchProofReview:
    ok: bool
    answers_question: bool
    answer_status: str
    answer_coverage_score: float
    citation_present: bool
    citation_locator_verified: bool
    support_relation_verified: bool
    counterevidence_checked: bool
    ledger_record_verified: bool
    question_digest: str = ""
    coverage_gaps: tuple[CoverageGap, ...] = ()
    followup_questions: tuple[PlannerSignal, ...] = ()
    query_rewrite_candidates: tuple[PlannerSignal, ...] = ()
    source_trust_warnings: tuple[str, ...] = ()
    overclaim_warnings: tuple[str, ...] = ()
    stale_warnings: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    proof_ref: str = ""
    record_id: str = ""
    record_digest: str = ""
    # Not serialized by to_payload(): existing payload/trace shapes stay
    # byte-identical. Consume via diagnostics_payload() or the attribute.
    diagnostics: tuple[ProofDiagnostic, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": bool(self.ok),
            "answers_question": bool(self.answers_question),
            "answer_status": identifier(self.answer_status, 40) or "not_answered",
            "answer_coverage_score": _bounded_score(self.answer_coverage_score),
            "citation_present": bool(self.citation_present),
            "citation_locator_verified": bool(self.citation_locator_verified),
            "support_relation_verified": bool(self.support_relation_verified),
            "counterevidence_checked": bool(self.counterevidence_checked),
            "ledger_record_verified": bool(self.ledger_record_verified),
            "question_digest": _digest_ref(self.question_digest),
            "coverage_gaps": [item.to_payload() for item in self.coverage_gaps[:MAX_GAPS]],
            "followup_questions": [
                item.to_payload() for item in self.followup_questions[:MAX_SIGNALS]
            ],
            "query_rewrite_candidates": [
                item.to_payload() for item in self.query_rewrite_candidates[:MAX_SIGNALS]
            ],
            "source_trust_warnings": list(bounded_refs(self.source_trust_warnings, limit=MAX_WARNINGS)),
            "overclaim_warnings": list(bounded_refs(self.overclaim_warnings, limit=MAX_WARNINGS)),
            "stale_warnings": list(bounded_refs(self.stale_warnings, limit=MAX_WARNINGS)),
            "missing_evidence": list(bounded_refs(self.missing_evidence, limit=MAX_WARNINGS)),
            "proof_ref": _proof_ref_or_empty(self.proof_ref),
            "record_id": _record_id_or_empty(self.record_id),
            "record_digest": _digest_ref(self.record_digest),
        }

    def to_trace_payload(self) -> dict[str, object]:
        return proof_review_trace_payload(self)

    def diagnostics_payload(self) -> list[dict[str, object]]:
        return [item.to_payload() for item in self.diagnostics[:MAX_DIAGNOSTICS]]


def review_research_proof(
    record: ResearchRecord | Mapping[str, object] | None,
    *,
    question: str = "",
    evidence_ledger: Mapping[str, object] | None = None,
    require_ledger_record: bool = False,
) -> ResearchProofReview:
    requested_question = str(question or "").strip()
    question_digest = _question_digest(requested_question)
    payload = _record_payload(record)
    if not payload:
        return _review(
            ok=False,
            answers_question=False,
            answer_status="not_answered",
            answer_coverage_score=0.0,
            citation_present=False,
            citation_locator_verified=False,
            support_relation_verified=False,
            counterevidence_checked=False,
            ledger_record_verified=False,
            question_digest=question_digest,
            missing_evidence=("missing_research_record",),
        )

    record_id = _record_id_or_empty(payload.get("record_id"))
    record_digest = _digest_ref(payload.get("record_digest"))
    answer_status = _answer_status(payload.get("answer_status"))
    sources = _source_map(payload.get("sources"))
    evidence = _evidence_map(payload.get("evidence"))
    claims = _claim_map(payload.get("claims"))
    assumptions = _assumption_map(payload.get("assumptions"))
    relations = _relation_list(payload.get("relations"))
    relation_review = _review_relations(
        claims=claims,
        evidence=evidence,
        assumptions=assumptions,
        relations=relations,
        sources=sources,
    )
    proof_question = requested_question or _question_text(payload)
    question_digest = _question_digest(proof_question)
    coverage = _answer_coverage(proof_question, claims, evidence)
    trust = _source_trust_warnings(sources)
    overclaim = _overclaim_warnings(claims, relation_review["supported_claim_ids"])
    missing = list(relation_review["missing_evidence"])
    if answer_status == "not_answered":
        missing.append("not_answered")
    elif answer_status == "insufficient_evidence":
        missing.append("insufficient_evidence")
    elif answer_status == "partial":
        missing.append("partial_answer")
    if nonnegative_int(payload.get("unsupported_claim_count")):
        missing.append("unsupported_claims")
    if coverage.score < MIN_QUEUE_COVERAGE_SCORE:
        missing.append("answer_coverage_gap")
    if not relation_review["counterevidence_checked"]:
        missing.append("counterevidence_not_checked")

    ledger_verified = (not require_ledger_record) or _ledger_has_record(
        evidence_ledger,
        record_id=record_id,
        record_digest=record_digest,
    )
    if not ledger_verified:
        missing.append("missing_evidence_ledger_record")

    answers_question = answer_status == "answered" and coverage.score >= MIN_QUEUE_COVERAGE_SCORE
    ok = (
        answers_question
        and bool(record_id)
        and bool(record_digest)
        and relation_review["citation_present"]
        and relation_review["citation_locator_verified"]
        and relation_review["support_relation_verified"]
        and relation_review["counterevidence_checked"]
        and ledger_verified
        and not relation_review["hard_failures"]
        and answer_status == "answered"
        and nonnegative_int(payload.get("unsupported_claim_count")) == 0
    )
    missing.extend(str(item) for item in relation_review["hard_failures"])
    missing = list(dict.fromkeys(identifier(item, 80) for item in missing if identifier(item, 80)))
    gaps = coverage.gaps
    followups, rewrites = _planner_signals(
        terms=coverage.unmatched_terms,
        missing_evidence=tuple(missing),
        source_warnings=trust,
    )
    return _review(
        ok=ok,
        answers_question=answers_question,
        answer_status=answer_status,
        answer_coverage_score=coverage.score,
        citation_present=relation_review["citation_present"],
        citation_locator_verified=relation_review["citation_locator_verified"],
        support_relation_verified=relation_review["support_relation_verified"],
        counterevidence_checked=relation_review["counterevidence_checked"],
        ledger_record_verified=ledger_verified,
        question_digest=question_digest,
        coverage_gaps=gaps,
        followup_questions=followups,
        query_rewrite_candidates=rewrites,
        source_trust_warnings=trust,
        overclaim_warnings=overclaim,
        stale_warnings=tuple(item for item in trust if "stale" in item or "undated" in item),
        missing_evidence=tuple(missing),
        record_id=record_id,
        record_digest=record_digest,
        diagnostics=_dedupe_diagnostics(relation_review["diagnostics"]),
    )


def proof_ref_for_review(review: ResearchProofReview | Mapping[str, object]) -> str:
    payload = review.to_payload() if isinstance(review, ResearchProofReview) else dict(review)
    return _proof_ref_from_payload(payload)


def proof_review_trace_payload(review: ResearchProofReview | Mapping[str, object]) -> dict[str, object]:
    payload = review.to_payload() if isinstance(review, ResearchProofReview) else dict(review)
    proof_ref = proof_ref_for_review(payload)
    reasons = tuple(bounded_refs(payload.get("missing_evidence", ()), limit=MAX_WARNINGS))
    return {
        "proof_ref": proof_ref,
        "record_id": _record_id_or_empty(payload.get("record_id")),
        "record_digest": _digest_ref(payload.get("record_digest")),
        "question_digest": _digest_ref(payload.get("question_digest")),
        "ok": bool(payload.get("ok")),
        "answers_question": bool(payload.get("answers_question")),
        "answer_status": _answer_status(payload.get("answer_status")),
        "answer_coverage_score": _bounded_score(payload.get("answer_coverage_score")),
        "gap_count": min(MAX_GAPS, len(payload.get("coverage_gaps", ()) or ())),
        "warning_count": min(
            MAX_WARNINGS,
            len(payload.get("source_trust_warnings", ()) or ())
            + len(payload.get("overclaim_warnings", ()) or ())
            + len(payload.get("stale_warnings", ()) or ()),
        ),
        "planner_signal_count": min(
            MAX_SIGNALS * 2,
            len(payload.get("followup_questions", ()) or ())
            + len(payload.get("query_rewrite_candidates", ()) or ()),
        ),
        "reason_codes": list(reasons),
    }


@dataclass(frozen=True)
class _CoverageResult:
    score: float
    gaps: tuple[CoverageGap, ...]
    unmatched_terms: tuple[str, ...]


def _dedupe_diagnostics(diagnostics: object) -> tuple[ProofDiagnostic, ...]:
    if not isinstance(diagnostics, (list, tuple)):
        return ()
    seen: set[tuple[str, str, str, str, str]] = set()
    rows: list[ProofDiagnostic] = []
    for item in diagnostics:
        if not isinstance(item, ProofDiagnostic):
            continue
        key = item._key()
        if key in seen:
            continue
        seen.add(key)
        rows.append(item)
        if len(rows) >= MAX_DIAGNOSTICS:
            break
    return tuple(rows)


def _review(**kwargs: object) -> ResearchProofReview:
    base = ResearchProofReview(**kwargs)  # type: ignore[arg-type]
    proof_ref = _proof_ref_from_payload(base.__dict__)
    return ResearchProofReview(**{**base.__dict__, "proof_ref": proof_ref})


def _proof_ref_from_payload(payload: Mapping[str, object]) -> str:
    return stable_ref(
        "research_proof",
        _record_id_or_empty(payload.get("record_id")),
        _digest_ref(payload.get("record_digest")),
        _digest_ref(payload.get("question_digest")),
        bool(payload.get("ok")),
        bool(payload.get("answers_question")),
        _bounded_score(payload.get("answer_coverage_score")),
        tuple(bounded_refs(payload.get("missing_evidence", ()), limit=MAX_WARNINGS)),
    )


def _record_payload(record: ResearchRecord | Mapping[str, object] | None) -> dict[str, object]:
    if record is None:
        return {}
    if isinstance(record, ResearchRecord):
        try:
            return dict(record.to_jsonable())
        except Exception:
            return {}
    if isinstance(record, Mapping):
        return dict(record)
    return {}


def _question_text(payload: Mapping[str, object]) -> str:
    question = payload.get("question")
    if not isinstance(question, Mapping):
        return ""
    # The object model intentionally stores only a digest/chars for the question.
    return ""


def _source_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        source_id = identifier(item.get("source_id"), 80)
        if source_id:
            rows[source_id] = item
    return rows


def _evidence_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        evidence_id = identifier(item.get("evidence_id"), 80)
        if evidence_id:
            rows[evidence_id] = item
    return rows


def _claim_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        claim_id = identifier(item.get("claim_id"), 80)
        if claim_id:
            rows[claim_id] = item
    return rows


def _assumption_map(value: object) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    for item in _list_of_mappings(value):
        assumption_id = identifier(item.get("assumption_id"), 80)
        if assumption_id:
            rows[assumption_id] = item
    return rows


def _relation_list(value: object) -> tuple[Mapping[str, object], ...]:
    return tuple(_list_of_mappings(value))


def _list_of_mappings(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _review_relations(
    *,
    claims: Mapping[str, Mapping[str, object]],
    evidence: Mapping[str, Mapping[str, object]],
    assumptions: Mapping[str, Mapping[str, object]],
    relations: tuple[Mapping[str, object], ...],
    sources: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    source_ids = set(sources)
    evidence_ids = set(evidence)
    assumption_ids = set(assumptions)
    support_by_claim: dict[str, set[str]] = {}
    supported_claim_ids: set[str] = set()
    counter_checked = False
    hard: list[str] = []
    diagnostics: list[ProofDiagnostic] = []

    def add_hard(
        reason_code: str,
        *,
        claim_ref: str = "",
        evidence_ref: str = "",
        source_ref: str = "",
        relation_ref: str = "",
    ) -> None:
        hard.append(reason_code)
        diagnostics.append(ProofDiagnostic(
            reason_code=reason_code,
            claim_ref=claim_ref if claim_ref in claims else "",
            evidence_ref=evidence_ref if evidence_ref in evidence else "",
            source_ref=source_ref if source_ref in sources else "",
            relation_ref=_normalize_runtime_ref(relation_ref, kind="relation"),
        ))

    for relation in relations:
        kind = identifier(relation.get("relation_kind"), 40)
        from_ref = identifier(relation.get("from_ref"), 80)
        to_ref = identifier(relation.get("to_ref"), 80)
        relation_id = identifier(relation.get("relation_id"), 80)
        if from_ref not in claims:
            add_hard("relation_missing_claim", relation_ref=relation_id)
            continue
        if kind == "supports":
            ev = evidence.get(to_ref)
            if ev is None:
                add_hard(
                    "support_relation_missing_evidence",
                    claim_ref=from_ref,
                    relation_ref=relation_id,
                )
                continue
            if identifier(ev.get("stance"), 40) != "supports":
                add_hard(
                    "support_relation_wrong_stance",
                    claim_ref=from_ref,
                    evidence_ref=to_ref,
                    relation_ref=relation_id,
                )
                continue
            if not _evidence_locator_ok(ev, source_ids):
                add_hard(
                    "support_relation_bad_locator",
                    claim_ref=from_ref,
                    evidence_ref=to_ref,
                    source_ref=identifier(ev.get("source_id"), 80),
                    relation_ref=relation_id,
                )
                continue
            support_by_claim.setdefault(from_ref, set()).add(to_ref)
        elif kind in {"refutes", "limits"}:
            if to_ref in evidence:
                ev = evidence[to_ref]
                if _evidence_locator_ok(ev, source_ids):
                    counter_checked = True
                else:
                    add_hard(
                        "counter_relation_bad_locator",
                        evidence_ref=to_ref,
                        source_ref=identifier(ev.get("source_id"), 80),
                        relation_ref=relation_id,
                    )
            elif to_ref in assumption_ids:
                counter_checked = True
            else:
                add_hard("counter_relation_missing_target", relation_ref=relation_id)

    citation_present = False
    required_claims = 0
    supported_required = 0
    for claim_id, claim in claims.items():
        section = identifier(claim.get("claim_section"), 80)
        citations = _positive_ints(claim.get("citation_numbers"))
        evidence_refs = set(bounded_refs(claim.get("evidence_refs", ()), limit=48))
        assumption_refs = set(bounded_refs(claim.get("assumption_refs", ()), limit=48))
        status = identifier(claim.get("status"), 40) or "unsupported"
        if citations:
            citation_present = True
        missing_evidence_refs = evidence_refs - evidence_ids
        missing_assumption_refs = assumption_refs - assumption_ids
        if missing_evidence_refs:
            add_hard("claim_missing_evidence_ref", claim_ref=claim_id)
        if missing_assumption_refs:
            add_hard("claim_missing_assumption_ref", claim_ref=claim_id)
        if section in {"conclusion", "evidence"}:
            required_claims += 1
            if not citations:
                add_hard("claim_missing_citation", claim_ref=claim_id)
            if status == "assumption":
                add_hard("assumption_used_as_answer", claim_ref=claim_id)
            if status != "evidence_backed":
                add_hard("claim_not_evidence_backed", claim_ref=claim_id)
            if not evidence_refs:
                add_hard("claim_missing_evidence_ref", claim_ref=claim_id)
            support_refs = support_by_claim.get(claim_id, set())
            claim_support_refs = support_refs & evidence_refs
            if support_refs and not claim_support_refs:
                add_hard("support_relation_not_claim_evidence", claim_ref=claim_id)
            if status == "evidence_backed" and claim_support_refs:
                supported_required += 1
                supported_claim_ids.add(claim_id)
            else:
                add_hard("claim_missing_support_relation", claim_ref=claim_id)
        if section == "counter" and (status == "assumption" or assumption_refs):
            counter_checked = True

    support_relation_verified = bool(required_claims and supported_required == required_claims)
    used_evidence = {
        item
        for claim_id in supported_claim_ids
        for item in support_by_claim.get(claim_id, set())
        if item in set(bounded_refs(claims[claim_id].get("evidence_refs", ()), limit=48))
    }
    locator_verified = bool(used_evidence) and all(
        _evidence_locator_ok(evidence[item], source_ids) for item in used_evidence if item in evidence
    )
    return {
        "citation_present": citation_present,
        "citation_locator_verified": locator_verified,
        "support_relation_verified": support_relation_verified,
        "counterevidence_checked": counter_checked,
        "supported_claim_ids": frozenset(supported_claim_ids),
        "hard_failures": tuple(dict.fromkeys(hard)),
        "missing_evidence": tuple(),
        "diagnostics": tuple(diagnostics),
    }


def _evidence_locator_ok(evidence: Mapping[str, object], source_ids: set[str]) -> bool:
    source_id = identifier(evidence.get("source_id"), 80)
    locator = evidence.get("locator")
    if not source_id or source_id not in source_ids or not isinstance(locator, Mapping):
        return False
    locator_source = identifier(locator.get("source_id"), 80)
    if locator_source != source_id:
        return False
    kind = identifier(locator.get("kind"), 40)
    if not kind or kind == "unknown":
        return False
    start = nonnegative_int(locator.get("char_start"))
    end = nonnegative_int(locator.get("char_end"))
    page = locator.get("page")
    span_ok = end > start
    page_ok = isinstance(page, int) and page > 0
    locator_text_ok = bool(clip(locator.get("locator"), 80))
    return span_ok or page_ok or locator_text_ok


def _answer_coverage(
    question: str,
    claims: Mapping[str, Mapping[str, object]],
    evidence: Mapping[str, Mapping[str, object]],
) -> _CoverageResult:
    terms = _question_terms(question)
    if not terms:
        supported = any(
            identifier(item.get("claim_section"), 80) in {"conclusion", "evidence"}
            and identifier(item.get("status"), 40) == "evidence_backed"
            for item in claims.values()
        )
        return _CoverageResult(1.0 if supported else 0.0, (), ())
    haystack = _normalized_search_text(
        " ".join(
            [
                *(str(item.get("claim_text") or "") for item in claims.values()),
                *(str(item.get("bounded_excerpt") or "") for item in evidence.values()),
            ]
        )
    )
    unmatched = tuple(term for term in terms if _normalized_search_text(term) not in haystack)
    score = _bounded_score((len(terms) - len(unmatched)) / max(1, len(terms)))
    gaps = tuple(
        CoverageGap("missing_question_term", term_ref=_safe_term_ref(term))
        for term in unmatched[:MAX_GAPS]
        if _safe_term_ref(term)
    )
    if unmatched and not gaps:
        gaps = (CoverageGap("missing_question_term"),)
    return _CoverageResult(score, gaps, unmatched)


def _planner_signals(
    *,
    terms: tuple[str, ...],
    missing_evidence: tuple[str, ...],
    source_warnings: tuple[str, ...],
) -> tuple[tuple[PlannerSignal, ...], tuple[PlannerSignal, ...]]:
    safe_terms = tuple(term for term in terms if _is_safe_signal_text(term))[:4]
    phrase = " ".join(safe_terms).strip()
    followups: list[PlannerSignal] = []
    rewrites: list[PlannerSignal] = []
    if phrase:
        followups.append(PlannerSignal(
            "followup_question",
            f"Find opened-source evidence for {phrase}.",
            "coverage_gap",
        ))
        rewrites.append(PlannerSignal(
            "query_rewrite",
            f"{phrase} primary source evidence",
            "coverage_gap",
        ))
    if any(reason in missing_evidence for reason in ("claim_missing_support_relation", "unsupported_claims")):
        followups.append(PlannerSignal(
            "followup_question",
            "Find direct supporting evidence for unsupported conclusion claims.",
            "missing_support",
        ))
    if source_warnings:
        rewrites.append(PlannerSignal(
            "query_rewrite",
            "primary source official report evidence",
            "source_trust_warning",
        ))
    return tuple(followups[:MAX_SIGNALS]), tuple(rewrites[:MAX_SIGNALS])


def _source_trust_warnings(sources: Mapping[str, Mapping[str, object]]) -> tuple[str, ...]:
    warnings: list[str] = []
    if len(sources) < 2:
        warnings.append("single_source")
    freshness = []
    levels = []
    kinds = []
    for source in sources.values():
        quality = source.get("quality")
        if isinstance(quality, Mapping):
            freshness.append(identifier(quality.get("freshness"), 80))
            levels.append(identifier(quality.get("level"), 80))
            kinds.append(identifier(quality.get("kind"), 80))
    if freshness and all(item in {"", "undated", "stale"} for item in freshness):
        warnings.append("sources_stale_or_undated")
    if levels and not any(item == "primary" for item in levels):
        warnings.append("no_primary_source")
    if any(item in {"blog", "forum", "social"} for item in kinds):
        warnings.append("weak_source_kind")
    return tuple(dict.fromkeys(warnings))[:MAX_WARNINGS]


def _overclaim_warnings(
    claims: Mapping[str, Mapping[str, object]],
    supported_claim_ids: object,
) -> tuple[str, ...]:
    supported = set(supported_claim_ids if isinstance(supported_claim_ids, frozenset) else ())
    warnings: list[str] = []
    for claim_id, claim in claims.items():
        if identifier(claim.get("claim_section"), 80) not in {"conclusion", "evidence"}:
            continue
        text = str(claim.get("claim_text") or "")
        if claim_id in supported and not _looks_like_strong_claim(text):
            continue
        if _looks_like_strong_claim(text) and claim_id not in supported:
            warnings.append("strong_claim_without_support")
    return tuple(dict.fromkeys(warnings))[:MAX_WARNINGS]


def _ledger_has_record(
    ledger: Mapping[str, object] | None,
    *,
    record_id: str,
    record_digest: str,
) -> bool:
    if not ledger or not record_id or not record_digest:
        return False
    records = ledger.get("records")
    if not isinstance(records, list):
        return False
    for item in records:
        if not isinstance(item, Mapping):
            continue
        if item.get("record_id") == record_id and item.get("record_digest") == record_digest:
            return True
    return False


def _question_terms(question: str) -> tuple[str, ...]:
    text = str(question or "")
    tokens: list[str] = []
    quoted = re.findall(r"['\"]([^'\"]{2,80})['\"]", text)
    for item in quoted:
        _append_term(tokens, item)
    for item in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|20\d{2}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}", text):
        _append_term(tokens, item)
        if len(tokens) >= 12:
            break
    return tuple(tokens)


def _append_term(tokens: list[str], value: str) -> None:
    term = _clean_term(value)
    if not term or term in tokens or term in _STOP_TERMS:
        return
    if not _is_safe_signal_text(term):
        return
    tokens.append(term)


def _clean_term(value: str) -> str:
    text = str(value or "").strip().strip(".,;:!?()[]{}<>，。；：！？（）【】")
    if len(text) > 80:
        return ""
    return text.casefold()


def _safe_term_ref(term: str) -> str:
    if not _is_safe_signal_text(term):
        return ""
    return identifier(term, 80)


def _is_safe_signal_text(value: str) -> bool:
    text = str(value or "").strip()
    lower = text.casefold()
    if not text or len(text) > 100:
        return False
    if "://" in lower or "\\" in text or "/" in text:
        return False
    if looks_sensitive_signal(text):
        return False
    if re.fullmatch(r"[A-Za-z0-9_./+=-]{24,}", text):
        return False
    return True


def _normalized_search_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _looks_like_strong_claim(value: str) -> bool:
    return bool(_STRONG_CLAIM_RE.search(str(value or "")))


def _positive_ints(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in out:
            out.append(number)
    return tuple(out)


def _answer_status(value: object) -> str:
    text = identifier(value, 40)
    if text in {"answered", "partial", "insufficient_evidence", "not_answered"}:
        return text
    return "not_answered"


def _bounded_score(value: object) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score < 0:
        return 0.0
    if score > 1:
        return 1.0
    return round(score, 3)


def _record_id_or_empty(value: object) -> str:
    text = str(value or "").strip()
    prefix = "research_record:"
    if not text.startswith(prefix):
        return ""
    suffix = text.removeprefix(prefix)
    if len(suffix) == 16 and all(ch in "0123456789abcdef" for ch in suffix):
        return text
    return ""


def _proof_ref_or_empty(value: object) -> str:
    text = str(value or "").strip()
    prefix = "research_proof:"
    if not text.startswith(prefix):
        return ""
    suffix = text.removeprefix(prefix)
    if len(suffix) == 16 and all(ch in "0123456789abcdef" for ch in suffix):
        return text
    return ""


def _question_digest(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return digest_json({"research_proof_question": text})


_STOP_TERMS = frozenset({
    "about",
    "answer",
    "are",
    "can",
    "does",
    "find",
    "for",
    "how",
    "investigate",
    "need",
    "please",
    "question",
    "research",
    "should",
    "study",
    "the",
    "this",
    "what",
    "whether",
    "when",
    "where",
    "which",
    "why",
    "with",
    "研究",
    "调查",
    "查找",
    "问题",
    "什么",
    "如何",
    "是否",
})
_STRONG_CLAIM_RE = re.compile(
    r"(?i)\b(?:always|never|must|guaranteed|certain|proven|will|cannot fail|"
    r"definitely|必然|一定|保证|证明|绝对)\b"
)


__all__ = [
    "CoverageGap",
    "MAX_DIAGNOSTICS",
    "MIN_QUEUE_COVERAGE_SCORE",
    "PlannerSignal",
    "ProofDiagnostic",
    "ResearchProofReview",
    "proof_ref_for_review",
    "proof_review_trace_payload",
    "review_research_proof",
]
