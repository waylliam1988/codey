"""Deterministic Research follow-up query planner.

Planner v1 consumes proof-review gaps plus connector metadata and returns a
bounded plan. The planner itself does not execute searches, fetch sources, or
patch prompts; ResearchPipeline may use its bounded output as follow-up behavior
input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from codey.research.connector_domains import preferred_connector_ids
from codey.utils.refs import (
    bounded_refs,
    clip,
    digest_text,
    identifier,
    stable_ref,
)
from codey.research.domain_profiles import EvidenceProfile
from codey.research.proof_quality import ResearchProofReview
from codey.policies.redaction import looks_prompt_visible_secret, looks_sensitive_code
from codey.research.shape import (
    connector_id as _connector_id,
    valid_digest_ref,
    generated_ref as _generated_ref,
)
from codey.research.source_connectors import (
    CONNECTOR_AVAILABLE_STATUSES,
    SourceConnectorRegistry,
    built_in_connector_registry,
    safe_connector_query_terms,
    safe_connector_signal_text,
)


MAX_PLAN_QUERIES = 8
MAX_PLAN_SOURCES = 12
MAX_PLAN_REASON_CODES = 12
MAX_QUERY_PREVIEW_CHARS = 180
MAX_PLAN_WARNINGS = 12
DEFAULT_MAX_QUERIES = 4
DEFAULT_MAX_SOURCES = 6
PLAN_MAX_DEPTH = 1
_CONNECTOR_REASON_SCORES = {
    "pubmed": (1.0, ("medical_life_science_source",)),
    "arxiv": (0.95, ("paper_preprint_source",)),
    "local_file": (0.9, ("local_file_source",)),
    "csv_tsv": (0.88, ("table_data_source",)),
    "json_file": (0.86, ("structured_data_source",)),
}
PROFILE_PREFERENCE_SCORE = 0.92
# Evidence-profile connector kinds map onto shipped connectors only; unknown
# kinds yield a bounded warning instead of guesses.
_PROFILE_CONNECTOR_KINDS = {
    "paper": ("arxiv", "pubmed"),
    "data": ("csv_tsv", "json_file"),
    "local": ("local_file",),
}


@dataclass(frozen=True)
class QueryCandidate:
    query_id: str
    query_preview: str
    connector_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    score: float = 0.0

    def to_payload(self) -> dict[str, object]:
        return {
            "query_id": _generated_ref(self.query_id, "research_query"),
            "query_digest": digest_text(self.query_preview),
            "query_preview": clip(self.query_preview, MAX_QUERY_PREVIEW_CHARS),
            "connector_ids": list(bounded_refs(self.connector_ids, limit=6)),
            "reason_codes": list(bounded_refs(self.reason_codes, limit=MAX_PLAN_REASON_CODES)),
            "score": _unit_float(self.score),
        }

    def to_trace_payload(self) -> dict[str, object]:
        return {
            "query_id": _generated_ref(self.query_id, "research_query"),
            "query_digest": digest_text(self.query_preview),
            "query_chars": len(self.query_preview),
            "connector_ids": list(bounded_refs(self.connector_ids, limit=6)),
            "reason_codes": list(bounded_refs(self.reason_codes, limit=MAX_PLAN_REASON_CODES)),
            "score": _unit_float(self.score),
        }


@dataclass(frozen=True)
class SourcePreference:
    connector_id: str
    reason_codes: tuple[str, ...] = ()
    score: float = 0.0

    def to_payload(self) -> dict[str, object]:
        return {
            "connector_id": _connector_id(self.connector_id),
            "reason_codes": list(bounded_refs(self.reason_codes, limit=MAX_PLAN_REASON_CODES)),
            "score": _unit_float(self.score),
        }


@dataclass(frozen=True)
class ResearchPlan:
    plan_ref: str
    question_digest: str = ""
    proof_ref: str = ""
    query_candidates: tuple[QueryCandidate, ...] = ()
    source_preferences: tuple[SourcePreference, ...] = ()
    max_depth: int = PLAN_MAX_DEPTH
    max_queries: int = DEFAULT_MAX_QUERIES
    max_sources: int = DEFAULT_MAX_SOURCES
    reason_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "plan_ref": _generated_ref(self.plan_ref, "research_plan"),
            "question_digest": valid_digest_ref(self.question_digest),
            "proof_ref": _generated_ref(self.proof_ref, "research_proof"),
            "query_candidates": [item.to_payload() for item in self.query_candidates[:MAX_PLAN_QUERIES]],
            "source_preferences": [item.to_payload() for item in self.source_preferences[:MAX_PLAN_SOURCES]],
            "max_depth": max(1, min(PLAN_MAX_DEPTH, int(self.max_depth or PLAN_MAX_DEPTH))),
            "max_queries": _bounded_int(self.max_queries, 1, MAX_PLAN_QUERIES),
            "max_sources": _bounded_int(self.max_sources, 1, MAX_PLAN_SOURCES),
            "reason_codes": list(bounded_refs(self.reason_codes, limit=MAX_PLAN_REASON_CODES)),
            "warnings": list(bounded_refs(self.warnings, limit=MAX_PLAN_WARNINGS)),
            "dry_run": True,
        }

    def to_trace_payload(self) -> dict[str, object]:
        return research_plan_trace_payload(self)


def build_research_plan(
    review: ResearchProofReview | Mapping[str, object] | None,
    *,
    question: str = "",
    registry: SourceConnectorRegistry | None = None,
    max_queries: int = DEFAULT_MAX_QUERIES,
    max_sources: int = DEFAULT_MAX_SOURCES,
    evidence_profile: EvidenceProfile | Mapping[str, object] | None = None,
) -> ResearchPlan:
    payload = _review_payload(review)
    registry = registry or built_in_connector_registry()
    question_digest = valid_digest_ref(payload.get("question_digest")) or (
        digest_text(question) if question else ""
    )
    proof_ref = _generated_ref(payload.get("proof_ref"), "research_proof")
    max_query_count = _bounded_int(max_queries, 1, MAX_PLAN_QUERIES)
    max_source_count = _bounded_int(max_sources, 1, MAX_PLAN_SOURCES)
    if _proof_ok_without_required_gap(payload):
        reason_codes = ("proof_ok_no_required_followup",)
        warnings: tuple[str, ...] = ()
        plan_ref = stable_ref(
            "research_plan",
            question_digest,
            proof_ref,
            (),
            (),
            max_query_count,
            max_source_count,
            reason_codes,
            warnings,
        )
        return ResearchPlan(
            plan_ref=plan_ref,
            question_digest=question_digest,
            proof_ref=proof_ref,
            query_candidates=(),
            source_preferences=(),
            max_depth=PLAN_MAX_DEPTH,
            max_queries=max_query_count,
            max_sources=max_source_count,
            reason_codes=reason_codes,
            warnings=warnings,
        )
    signals = _signals_from_review(payload)
    terms = _safe_terms(" ".join([question, *signals]))
    preferences, pref_reasons = _source_preferences(
        terms,
        registry,
        evidence_profile=evidence_profile,
    )
    query_candidates = _query_candidates(
        signals=signals,
        terms=terms,
        preferences=preferences,
        max_queries=max_query_count,
    )
    reason_codes = list(bounded_refs([
        *_reason_codes_from_review(payload),
        *pref_reasons,
        *(
            ("proof_ok_no_required_followup",)
            if bool(payload.get("ok")) and not query_candidates
            else ()
        ),
    ], limit=MAX_PLAN_REASON_CODES))
    warnings = list(_planner_warnings(payload, registry, preferences))
    plan_ref = stable_ref(
        "research_plan",
        question_digest,
        proof_ref,
        tuple(item.query_id for item in query_candidates),
        tuple(item.connector_id for item in preferences),
        max_query_count,
        max_source_count,
        tuple(reason_codes),
        tuple(warnings),
    )
    return ResearchPlan(
        plan_ref=plan_ref,
        question_digest=question_digest,
        proof_ref=proof_ref,
        query_candidates=query_candidates,
        source_preferences=preferences,
        max_depth=PLAN_MAX_DEPTH,
        max_queries=max_query_count,
        max_sources=max_source_count,
        reason_codes=tuple(reason_codes),
        warnings=tuple(warnings),
    )


def research_plan_trace_payload(plan: ResearchPlan | Mapping[str, object]) -> dict[str, object]:
    payload = plan.to_payload() if isinstance(plan, ResearchPlan) else dict(plan)
    source_preferences = payload.get("source_preferences")
    if not isinstance(source_preferences, list):
        source_preferences = []
    query_candidates = payload.get("query_candidates")
    if not isinstance(query_candidates, list):
        query_candidates = []
    return {
        "plan_ref": _generated_ref(payload.get("plan_ref"), "research_plan"),
        "question_digest": valid_digest_ref(payload.get("question_digest")),
        "proof_ref": _generated_ref(payload.get("proof_ref"), "research_proof"),
        "dry_run": True,
        "max_depth": _bounded_int(payload.get("max_depth"), 1, PLAN_MAX_DEPTH),
        "max_queries": _bounded_int(payload.get("max_queries"), 1, MAX_PLAN_QUERIES),
        "max_sources": _bounded_int(payload.get("max_sources"), 1, MAX_PLAN_SOURCES),
        "query_count": min(MAX_PLAN_QUERIES, len(query_candidates)),
        "source_preferences": [
            _safe_trace_connector_id(item.get("connector_id"))
            for item in source_preferences
            if isinstance(item, Mapping) and _safe_trace_connector_id(item.get("connector_id"))
        ][:MAX_PLAN_SOURCES],
        "reason_codes": [
            _safe_trace_code(item, 80)
            for item in _trace_list_items(payload.get("reason_codes"))
            if _safe_trace_code(item, 80)
        ][:MAX_PLAN_REASON_CODES],
        "warnings": [
            _safe_trace_code(item, 120)
            for item in _trace_list_items(payload.get("warnings"))
            if _safe_trace_code(item, 120)
        ][:MAX_PLAN_WARNINGS],
    }


def _trace_list_items(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    return ()


def _review_payload(review: ResearchProofReview | Mapping[str, object] | None) -> dict[str, object]:
    if review is None:
        return {}
    if isinstance(review, ResearchProofReview):
        try:
            return review.to_payload()
        except Exception:
            return {}
    if isinstance(review, Mapping):
        return dict(review)
    return {}


def _signals_from_review(payload: Mapping[str, object]) -> tuple[str, ...]:
    signals: list[str] = []
    for key in ("coverage_gaps", "followup_questions", "query_rewrite_candidates"):
        value = payload.get(key)
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            if isinstance(item, Mapping):
                text = str(item.get("text") or item.get("term_ref") or item.get("detail") or "")
            else:
                text = str(item or "")
            safe = _safe_signal_text(text)
            if safe and safe not in signals:
                signals.append(safe)
            if len(signals) >= MAX_PLAN_QUERIES * 2:
                return tuple(signals)
    return tuple(signals)


def _proof_ok_without_required_gap(payload: Mapping[str, object]) -> bool:
    if not bool(payload.get("ok")):
        return False
    if _has_items(payload.get("coverage_gaps")) or _has_items(payload.get("missing_evidence")):
        return False
    answer_status = identifier(payload.get("answer_status"), 40)
    if answer_status and answer_status != "answered":
        return False
    if payload.get("answers_question") is False:
        return False
    return True


def _has_items(value: object) -> bool:
    return bool(value)


def _source_preferences(
    terms: tuple[str, ...],
    registry: SourceConnectorRegistry,
    *,
    evidence_profile: EvidenceProfile | Mapping[str, object] | None = None,
) -> tuple[tuple[SourcePreference, ...], tuple[str, ...]]:
    available = {
        spec.id: spec
        for spec in registry.all()
        if spec.status in CONNECTOR_AVAILABLE_STATUSES and spec.shipped
    }
    preferences: list[SourcePreference] = []
    reasons: list[str] = []

    def add(connector_id: str, score: float, *reason_codes: str) -> None:
        if connector_id not in available:
            return
        if any(item.connector_id == connector_id for item in preferences):
            return
        preferences.append(SourcePreference(connector_id, reason_codes, score))
        reasons.extend(reason_codes)

    if evidence_profile is not None:
        for _kind, connector_ids in _profile_connector_kinds(evidence_profile):
            added = 0
            for connector_id in connector_ids:
                before = len(preferences)
                add(connector_id, PROFILE_PREFERENCE_SCORE, "domain_profile_source_preference")
                if len(preferences) > before:
                    added += 1
                if len(preferences) >= MAX_PLAN_SOURCES:
                    break
            if not added:
                reasons.append("domain_profile_kind_unavailable")
    for connector_id in preferred_connector_ids(terms, available_ids=tuple(available)):
        score, reason_codes = _CONNECTOR_REASON_SCORES.get(connector_id, (0.0, ()))
        add(connector_id, score, *reason_codes)
    return tuple(preferences[:MAX_PLAN_SOURCES]), tuple(dict.fromkeys(reasons))


def _profile_connector_kinds(
    evidence_profile: EvidenceProfile | Mapping[str, object],
) -> list[tuple[str, tuple[str, ...]]]:
    raw = (
        evidence_profile.get("preferred_connector_kinds")
        if isinstance(evidence_profile, Mapping)
        else getattr(evidence_profile, "preferred_connector_kinds", ())
    ) or ()
    rows: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for kind in raw:
        token = identifier(kind, 40).lower()
        if not token or token in seen:
            continue
        seen.add(token)
        # Unknown kinds keep an empty mapping so callers can warn instead of
        # silently guessing what the profile meant.
        rows.append((token, _PROFILE_CONNECTOR_KINDS.get(token, ())))
        if len(rows) >= 4:
            break
    return rows


def _query_candidates(
    *,
    signals: tuple[str, ...],
    terms: tuple[str, ...],
    preferences: tuple[SourcePreference, ...],
    max_queries: int,
) -> tuple[QueryCandidate, ...]:
    connector_ids = tuple(item.connector_id for item in preferences)
    candidates: list[QueryCandidate] = []
    for signal in signals:
        candidate = _candidate_query(signal, terms, connector_ids)
        if candidate:
            candidates.append(candidate)
        if len(candidates) >= max_queries:
            break
    if len(candidates) < max_queries and terms:
        phrase = " ".join(terms[:6])
        suffix = _domain_suffix(connector_ids)
        text = _safe_query_preview(f"{phrase} {suffix}".strip())
        if text and not any(item.query_preview == text for item in candidates):
            candidates.append(_make_query_candidate(text, connector_ids, ("question_terms",), 0.74))
    return tuple(candidates[:max_queries])


def _candidate_query(
    signal: str,
    terms: tuple[str, ...],
    connector_ids: tuple[str, ...],
) -> QueryCandidate | None:
    text = _safe_query_preview(signal)
    if not text and terms:
        text = _safe_query_preview(" ".join(terms[:6]))
    if not text:
        return None
    suffix = _domain_suffix(connector_ids)
    if suffix and suffix.casefold() not in text.casefold():
        text = _safe_query_preview(f"{text} {suffix}")
    return _make_query_candidate(text, connector_ids, ("proof_review_signal",), 0.82)


def _make_query_candidate(
    query_preview: str,
    connector_ids: tuple[str, ...],
    reason_codes: tuple[str, ...],
    score: float,
) -> QueryCandidate:
    query = clip(query_preview, MAX_QUERY_PREVIEW_CHARS)
    return QueryCandidate(
        query_id=stable_ref("research_query", digest_text(query), connector_ids, reason_codes),
        query_preview=query,
        connector_ids=connector_ids,
        reason_codes=reason_codes,
        score=score,
    )


def _domain_suffix(connector_ids: tuple[str, ...]) -> str:
    if "pubmed" in connector_ids:
        return "PubMed evidence"
    if "arxiv" in connector_ids:
        return "arXiv preprint"
    if "csv_tsv" in connector_ids:
        return "table evidence"
    if "json_file" in connector_ids:
        return "structured data"
    if "local_file" in connector_ids:
        return "local source"
    return "primary source evidence"


def _safe_terms(text: str) -> tuple[str, ...]:
    return safe_connector_query_terms(text, limit=16)


def _safe_query_preview(text: str) -> str:
    tokens = _safe_terms(text)
    if not tokens:
        return ""
    return clip(" ".join(tokens[:10]), MAX_QUERY_PREVIEW_CHARS)


def _safe_signal_text(value: object) -> str:
    return safe_connector_signal_text(value, limit=MAX_QUERY_PREVIEW_CHARS)


def _safe_trace_code(value: object, limit: int) -> str:
    raw = clip(value, limit)
    if not raw:
        return ""
    if looks_sensitive_code(raw):
        return ""
    text = identifier(raw, limit)
    if looks_sensitive_code(text):
        return ""
    return text


def _safe_trace_connector_id(value: object) -> str:
    raw = clip(value, 80)
    if looks_prompt_visible_secret(raw):
        return ""
    return _connector_id(raw)


def _reason_codes_from_review(payload: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("missing_evidence", "reason_codes"):
        raw = payload.get(key)
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            code = identifier(item, 80)
            if code and code not in values:
                values.append(code)
    answer_status = identifier(payload.get("answer_status"), 40)
    if answer_status and answer_status != "answered":
        values.append("answer_status_" + answer_status)
    if payload and not bool(payload.get("ok")):
        values.append("proof_review_failed")
    return tuple(values)


def _planner_warnings(
    payload: Mapping[str, object],
    registry: SourceConnectorRegistry,
    preferences: tuple[SourcePreference, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if not payload:
        warnings.append("missing_proof_review")
    unavailable = {
        spec.id
        for spec in registry.all()
        if spec.status not in CONNECTOR_AVAILABLE_STATUSES or not spec.shipped
    }
    if "openalex" in unavailable:
        warnings.append("openalex_deferred")
    if "rss" in unavailable:
        warnings.append("rss_optional")
    if not preferences:
        warnings.append("no_connector_preference")
    return tuple(dict.fromkeys(warnings))[:MAX_PLAN_WARNINGS]


def _bounded_int(value: object, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = lower
    return max(lower, min(upper, parsed))


def _unit_float(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(number, 3)))


__all__ = [
    "DEFAULT_MAX_QUERIES",
    "DEFAULT_MAX_SOURCES",
    "MAX_PLAN_QUERIES",
    "MAX_PLAN_SOURCES",
    "PLAN_MAX_DEPTH",
    "QueryCandidate",
    "ResearchPlan",
    "SourcePreference",
    "build_research_plan",
    "research_plan_trace_payload",
]
