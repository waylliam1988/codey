"""Deterministic Research object projection.

This module turns the existing per-run Research ledger and final report review
into a bounded object model. It is not a planner, proof gate, connector layer,
or UI surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Mapping

from codey.research.ledger import (
    EvidenceItem,
    OpenedSource,
    ResearchLedger,
    normalize_evidence_stance,
)
from codey.research.identity import (
    bounded_refs as _bounded_refs,
    clip as _clip,
    digest_json as _digest_json,
    digest_ref as _digest_ref,
    digest_text as _digest_text,
    identifier as _identifier,
    nonnegative_int as _nonnegative_int,
    normalize_text as _normalize_text,
    path_ref,
    project_ref,
    sanitize_research_url_ref,
    stable_ref as _stable_ref,
)
from codey.research.report_quality import ReportQualityReview, citation_ref_items, parse_sections


MAX_RECORD_SOURCES = 24
MAX_RECORD_EVIDENCE = 48
MAX_RECORD_CLAIMS = 32
MAX_RECORD_ASSUMPTIONS = 16
MAX_RECORD_RELATIONS = 96
MAX_CLAIM_TEXT_CHARS = 260
MAX_EXCERPT_CHARS = 360
MAX_REF_VALUES = 12
RESEARCH_RECORD_SCHEMA_VERSION = 1
RESEARCH_RECORD_KIND = "research_record"
ANSWER_STATUSES = frozenset({
    "answered",
    "partial",
    "insufficient_evidence",
    "not_answered",
})
CLAIM_STATUSES = frozenset({
    "evidence_backed",
    "unsupported",
    "assumption",
})
CLAIM_RELATION_KINDS = frozenset({
    "supports",
    "refutes",
    "updates",
    "supersedes",
    "conflicts_with",
    "limits",
})
EXTRACTED_RELATION_KINDS = frozenset({"supports", "refutes", "limits"})
_ASSUMPTION_MARKERS = (
    "assume",
    "assumption",
    "assuming",
    "uncertain",
    "uncertainty",
    "likely",
    "may",
    "might",
    "could",
    "if ",
    "假设",
    "可能",
    "不确定",
    "限制",
    "未找到",
    "没有找到",
    "需要进一步",
)


@dataclass(frozen=True)
class ResearchQuestion:
    question_id: str
    question_text_digest: str
    chars: int = 0

    def to_jsonable(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "question_text_digest": self.question_text_digest,
            "chars": max(0, int(self.chars or 0)),
        }


@dataclass(frozen=True)
class ResearchSource:
    source_id: str
    requested_url_ref: Mapping[str, object] = field(default_factory=dict)
    final_url_ref: Mapping[str, object] = field(default_factory=dict)
    host: str = ""
    title_digest: str = ""
    content_hash: str = ""
    retrieved_at: str = ""
    content_kind: str = "html"
    page_count: int = 0
    pages_read: tuple[int, ...] = ()
    truncated: bool = False
    quality: Mapping[str, object] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "requested_url_ref": dict(self.requested_url_ref),
            "final_url_ref": dict(self.final_url_ref),
            "host": _clip(self.host, 120),
            "title_digest": _digest_ref(self.title_digest),
            "content_hash": _clip(self.content_hash, 80),
            "retrieved_at": _clip(self.retrieved_at, 80),
            "content_kind": _identifier(self.content_kind, 40) or "html",
            "page_count": max(0, int(self.page_count or 0)),
            "pages_read": [int(page) for page in self.pages_read if int(page) > 0][:MAX_REF_VALUES],
            "truncated": bool(self.truncated),
            "quality": dict(self.quality),
        }


@dataclass(frozen=True)
class EvidenceLocator:
    kind: str
    source_id: str
    page: int | None = None
    char_start: int = 0
    char_end: int = 0
    locator: str = ""

    def to_jsonable(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": _identifier(self.kind, 40) or "unknown",
            "source_id": _identifier(self.source_id, 80),
            "char_start": max(0, int(self.char_start or 0)),
            "char_end": max(0, int(self.char_end or 0)),
        }
        if self.page is not None and int(self.page) > 0:
            payload["page"] = int(self.page)
        if self.locator:
            payload["locator"] = _clip(self.locator, 80)
        return payload


@dataclass(frozen=True)
class ResearchEvidence:
    evidence_id: str
    source_id: str
    excerpt_digest: str
    bounded_excerpt: str
    locator: EvidenceLocator
    stance: str = "supports"
    note_id: str = ""
    claim_text_digest: str = ""

    def to_jsonable(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "excerpt_digest": _digest_ref(self.excerpt_digest),
            "bounded_excerpt": _clip(self.bounded_excerpt, MAX_EXCERPT_CHARS),
            "locator": self.locator.to_jsonable(),
            "stance": _identifier(self.stance, 40) or "supports",
            "note_id": _clip(self.note_id, 120),
            "claim_text_digest": _digest_ref(self.claim_text_digest),
        }


@dataclass(frozen=True)
class ResearchAssumption:
    assumption_id: str
    assumption_text: str
    reason: str = ""
    claim_ref: str = ""

    def to_jsonable(self) -> dict[str, object]:
        return {
            "assumption_id": self.assumption_id,
            "assumption_text": _clip(self.assumption_text, MAX_CLAIM_TEXT_CHARS),
            "reason": _identifier(self.reason, 80),
            "claim_ref": _identifier(self.claim_ref, 80),
        }


@dataclass(frozen=True)
class ResearchClaim:
    claim_id: str
    claim_text: str
    claim_section: str
    citation_numbers: tuple[int, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    assumption_refs: tuple[str, ...] = ()
    status: str = "unsupported"

    def to_jsonable(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "claim_text": _clip(self.claim_text, MAX_CLAIM_TEXT_CHARS),
            "claim_section": _identifier(self.claim_section, 80),
            "citation_numbers": [int(value) for value in self.citation_numbers][:MAX_REF_VALUES],
            "evidence_refs": list(_bounded_refs(self.evidence_refs)),
            "assumption_refs": list(_bounded_refs(self.assumption_refs)),
            "status": _claim_status(self.status),
        }


@dataclass(frozen=True)
class ResearchClaimRelation:
    relation_id: str
    relation_kind: str
    from_ref: str
    to_ref: str
    citation_numbers: tuple[int, ...] = ()

    def to_jsonable(self) -> dict[str, object]:
        return {
            "relation_id": self.relation_id,
            "relation_kind": _relation_kind(self.relation_kind),
            "from_ref": _identifier(self.from_ref, 80),
            "to_ref": _identifier(self.to_ref, 80),
            "citation_numbers": [int(value) for value in self.citation_numbers][:MAX_REF_VALUES],
        }


@dataclass(frozen=True)
class ResearchRecord:
    record_id: str
    record_digest: str
    question: ResearchQuestion
    answer_status: str
    sources: tuple[ResearchSource, ...] = ()
    evidence: tuple[ResearchEvidence, ...] = ()
    claims: tuple[ResearchClaim, ...] = ()
    assumptions: tuple[ResearchAssumption, ...] = ()
    relations: tuple[ResearchClaimRelation, ...] = ()
    unsupported_claim_count: int = 0
    run_id: str = ""
    session_id: str = ""
    project_ref: Mapping[str, object] = field(default_factory=dict)
    synthesis_id: str = ""
    stop_reason: str = ""

    def to_jsonable(self) -> dict[str, object]:
        return {
            "schema_version": RESEARCH_RECORD_SCHEMA_VERSION,
            "kind": RESEARCH_RECORD_KIND,
            "record_id": self.record_id,
            "record_digest": _digest_ref(self.record_digest),
            "question": self.question.to_jsonable(),
            "answer_status": _answer_status(self.answer_status),
            "sources": [item.to_jsonable() for item in self.sources[:MAX_RECORD_SOURCES]],
            "evidence": [item.to_jsonable() for item in self.evidence[:MAX_RECORD_EVIDENCE]],
            "claims": [item.to_jsonable() for item in self.claims[:MAX_RECORD_CLAIMS]],
            "assumptions": [
                item.to_jsonable() for item in self.assumptions[:MAX_RECORD_ASSUMPTIONS]
            ],
            "relations": [item.to_jsonable() for item in self.relations[:MAX_RECORD_RELATIONS]],
            "unsupported_claim_count": max(0, int(self.unsupported_claim_count or 0)),
            "run_id": _clip(self.run_id, 120),
            "session_id": _clip(self.session_id, 120),
            "project_ref": dict(self.project_ref),
            "synthesis_id": _clip(self.synthesis_id, 120),
            "stop_reason": _identifier(self.stop_reason, 80),
        }

    def to_summary_payload(self) -> dict[str, object]:
        return research_record_summary(self)


def build_research_record(
    *,
    question: str,
    summary: str,
    ledger: ResearchLedger,
    review: ReportQualityReview | None = None,
    run_id: str = "",
    session_id: str = "",
    project: str | Path | None = None,
    synthesis_id: str = "",
    stop_reason: str = "",
) -> ResearchRecord:
    sources = _build_sources(ledger)
    source_ids_by_url = _source_ids_by_url(ledger, sources)
    evidence = _build_evidence(ledger, source_ids_by_url)
    evidence_by_source = _evidence_by_source(evidence)
    sections = parse_sections(summary)
    citations = tuple(getattr(review, "citation_map", ()) or ())
    citation_urls = {int(item.number): str(item.url or "") for item in citations}
    claims, assumptions, relations, unsupported = extract_claim_candidates(
        sections=sections,
        citation_urls=citation_urls,
        evidence_by_source=evidence_by_source,
        source_ids_by_url=source_ids_by_url,
    )
    status = _derive_answer_status(
        summary=summary,
        review=review,
        evidence=evidence,
        unsupported_claim_count=unsupported,
        stop_reason=stop_reason,
    )
    question_obj = ResearchQuestion(
        question_id=_stable_ref("question", _normalize_text(question)),
        question_text_digest=_digest_text(question),
        chars=len(str(question or "")),
    )
    base = {
        "schema_version": RESEARCH_RECORD_SCHEMA_VERSION,
        "kind": RESEARCH_RECORD_KIND,
        "question": question_obj.to_jsonable(),
        "answer_status": status,
        "sources": [item.to_jsonable() for item in sources],
        "evidence": [item.to_jsonable() for item in evidence],
        "claims": [item.to_jsonable() for item in claims],
        "assumptions": [item.to_jsonable() for item in assumptions],
        "relations": [item.to_jsonable() for item in relations],
        "unsupported_claim_count": unsupported,
        "run_id": _clip(run_id, 120),
        "session_id": _clip(session_id, 120),
        "project_ref": project_ref(project),
        "synthesis_id": _clip(synthesis_id, 120),
        "stop_reason": _identifier(stop_reason, 80),
    }
    digest = _digest_json(base)
    return ResearchRecord(
        record_id="research_record:" + digest.removeprefix("sha256:")[:16],
        record_digest=digest,
        question=question_obj,
        answer_status=status,
        sources=sources,
        evidence=evidence,
        claims=claims,
        assumptions=assumptions,
        relations=relations,
        unsupported_claim_count=unsupported,
        run_id=_clip(run_id, 120),
        session_id=_clip(session_id, 120),
        project_ref=project_ref(project),
        synthesis_id=_clip(synthesis_id, 120),
        stop_reason=_identifier(stop_reason, 80),
    )


def extract_claim_candidates(
    *,
    sections: Mapping[str, str],
    citation_urls: Mapping[int, str],
    evidence_by_source: Mapping[str, tuple[ResearchEvidence, ...]],
    source_ids_by_url: Mapping[str, str],
) -> tuple[
    tuple[ResearchClaim, ...],
    tuple[ResearchAssumption, ...],
    tuple[ResearchClaimRelation, ...],
    int,
]:
    claims: list[ResearchClaim] = []
    assumptions: list[ResearchAssumption] = []
    relations: list[ResearchClaimRelation] = []
    evidence_by_id = _evidence_by_id(evidence_by_source)
    unsupported = 0
    for section in ("conclusion", "evidence", "counter"):
        for line in _claim_lines(sections.get(section, "")):
            if len(claims) >= MAX_RECORD_CLAIMS:
                break
            refs = tuple(sorted({item.number for item in citation_ref_items(line)}))
            clean = _strip_citations(line)
            evidence_refs = _evidence_refs_for_citations(
                refs,
                citation_urls=citation_urls,
                claim_text=clean,
                section=section,
                evidence_by_source=evidence_by_source,
                source_ids_by_url=source_ids_by_url,
            )
            claim_id = _stable_ref("claim", section, clean, refs)
            assumption_refs: tuple[str, ...] = ()
            status = "evidence_backed" if evidence_refs else "unsupported"
            if not evidence_refs and _looks_like_assumption(clean, section):
                assumption = ResearchAssumption(
                    assumption_id=_stable_ref("assumption", section, clean),
                    assumption_text=clean,
                    reason="declared_uncertainty" if section == "counter" else "unverified_assumption",
                    claim_ref=claim_id,
                )
                assumptions.append(assumption)
                assumption_refs = (assumption.assumption_id,)
                status = "assumption"
            elif not evidence_refs:
                unsupported += 1
            claim = ResearchClaim(
                claim_id=claim_id,
                claim_text=clean,
                claim_section=section,
                citation_numbers=refs,
                evidence_refs=evidence_refs,
                assumption_refs=assumption_refs,
                status=status,
            )
            claims.append(claim)
            if evidence_refs:
                for target in evidence_refs:
                    relation_kind = _relation_kind_for_evidence(section, evidence_by_id.get(target))
                    if not relation_kind:
                        continue
                    if len(relations) >= MAX_RECORD_RELATIONS:
                        break
                    relations.append(ResearchClaimRelation(
                        relation_id=_stable_ref("relation", relation_kind, claim_id, target),
                        relation_kind=relation_kind,
                        from_ref=claim_id,
                        to_ref=target,
                        citation_numbers=refs,
                    ))
            elif assumption_refs:
                relation_kind = _assumption_relation_kind(section)
                if relation_kind:
                    for target in assumption_refs:
                        if len(relations) >= MAX_RECORD_RELATIONS:
                            break
                        relations.append(ResearchClaimRelation(
                            relation_id=_stable_ref("relation", relation_kind, claim_id, target),
                            relation_kind=relation_kind,
                            from_ref=claim_id,
                            to_ref=target,
                            citation_numbers=refs,
                        ))
        if len(claims) >= MAX_RECORD_CLAIMS:
            break
    return _close_claim_graph(claims, assumptions, relations, unsupported)


def research_record_summary(record: ResearchRecord | Mapping[str, object]) -> dict[str, object]:
    if isinstance(record, ResearchRecord):
        return {
            "record_id": record.record_id,
            "answer_status": _answer_status(record.answer_status),
            "source_count": len(record.sources),
            "evidence_count": len(record.evidence),
            "claim_count": len(record.claims),
            "assumption_count": len(record.assumptions),
            "unsupported_claim_count": max(0, int(record.unsupported_claim_count or 0)),
            "record_digest": _digest_ref(record.record_digest),
        }
    return {
        "record_id": _identifier(record.get("record_id"), 80),
        "answer_status": _answer_status(record.get("answer_status")),
        "source_count": _nonnegative_int(record.get("source_count")),
        "evidence_count": _nonnegative_int(record.get("evidence_count")),
        "claim_count": _nonnegative_int(record.get("claim_count")),
        "assumption_count": _nonnegative_int(record.get("assumption_count")),
        "unsupported_claim_count": _nonnegative_int(record.get("unsupported_claim_count")),
        "record_digest": _digest_ref(record.get("record_digest")),
    }


def _build_sources(ledger: ResearchLedger) -> tuple[ResearchSource, ...]:
    rows: list[ResearchSource] = []
    for opened in list(getattr(ledger, "opened_sources", ()))[:MAX_RECORD_SOURCES]:
        rows.append(_source_from_opened(opened))
    return tuple(rows)


def _source_from_opened(opened: OpenedSource) -> ResearchSource:
    final_ref = sanitize_research_url_ref(opened.final_url)
    requested_ref = sanitize_research_url_ref(opened.requested_url)
    host = str(final_ref.get("host") or requested_ref.get("host") or "")
    source_id = _stable_ref(
        "source",
        final_ref.get("url_digest") or requested_ref.get("url_digest") or "",
        opened.text_hash,
        opened.content_kind,
    )
    return ResearchSource(
        source_id=source_id,
        requested_url_ref=requested_ref,
        final_url_ref=final_ref,
        host=host,
        title_digest=_digest_text(opened.title),
        content_hash=_clip(opened.text_hash, 80),
        retrieved_at=_clip(opened.retrieved_at, 80),
        content_kind=_identifier(opened.content_kind, 40) or "html",
        page_count=max(0, int(opened.page_count or 0)),
        pages_read=tuple(int(page) for page in opened.pages_read if int(page) > 0),
        truncated=bool(opened.truncated),
        quality=opened.quality.to_dict(),
    )


def _source_ids_by_url(
    ledger: ResearchLedger,
    sources: Iterable[ResearchSource],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for opened, source in zip(getattr(ledger, "opened_sources", ()), sources, strict=False):
        for url in (opened.final_url, opened.requested_url):
            text = str(url or "").strip()
            if text:
                mapping[text] = source.source_id
    return mapping


def _build_evidence(
    ledger: ResearchLedger,
    source_ids_by_url: Mapping[str, str],
) -> tuple[ResearchEvidence, ...]:
    rows: list[ResearchEvidence] = []
    for item in list(getattr(ledger, "evidence_items", ()))[:MAX_RECORD_EVIDENCE]:
        final_url = ledger.canonical_opened_url(item.source_url) or str(item.source_url or "").strip()
        source_id = source_ids_by_url.get(final_url) or source_ids_by_url.get(str(item.source_url or ""))
        if not source_id:
            continue
        locator = _locator_for_evidence(ledger, item, source_id=source_id, final_url=final_url)
        excerpt_digest = _digest_text(item.excerpt)
        stance = normalize_evidence_stance(item.stance)
        rows.append(ResearchEvidence(
            evidence_id=_stable_ref(
                "evidence",
                source_id,
                excerpt_digest,
                item.page or "",
                stance,
            ),
            source_id=source_id,
            excerpt_digest=excerpt_digest,
            bounded_excerpt=_clip(item.excerpt, MAX_EXCERPT_CHARS),
            locator=locator,
            stance=stance,
            note_id=_clip(item.note_id, 120),
            claim_text_digest=_digest_text(_normalize_text(item.claim)),
        ))
    return tuple(rows)


def _locator_for_evidence(
    ledger: ResearchLedger,
    item: EvidenceItem,
    *,
    source_id: str,
    final_url: str,
) -> EvidenceLocator:
    page = int(item.page) if item.page is not None and int(item.page) > 0 else None
    source = ledger.source_record_for_url(final_url)
    kind = _identifier(getattr(source, "content_kind", ""), 40) or "html"
    if page is not None and kind == "pdf":
        text = ledger.source_pages_for_url(final_url).get(page, "")
    else:
        text = ledger.source_text_for_url(final_url)
    start, end = _excerpt_offsets(text, item.excerpt)
    return EvidenceLocator(
        kind=kind,
        source_id=source_id,
        page=page,
        char_start=start,
        char_end=end,
        locator=item.locator or (f"p.{page}" if page is not None else ""),
    )


def _evidence_by_source(evidence: Iterable[ResearchEvidence]) -> dict[str, tuple[ResearchEvidence, ...]]:
    buckets: dict[str, list[ResearchEvidence]] = {}
    for item in evidence:
        buckets.setdefault(item.source_id, []).append(item)
    return {key: tuple(value[:MAX_REF_VALUES]) for key, value in buckets.items()}


def _evidence_by_id(
    evidence_by_source: Mapping[str, tuple[ResearchEvidence, ...]],
) -> dict[str, ResearchEvidence]:
    return {
        item.evidence_id: item
        for items in evidence_by_source.values()
        for item in items
    }


def _evidence_refs_for_citations(
    citation_numbers: Iterable[int],
    *,
    claim_text: str,
    section: str,
    citation_urls: Mapping[int, str],
    evidence_by_source: Mapping[str, tuple[ResearchEvidence, ...]],
    source_ids_by_url: Mapping[str, str],
) -> tuple[str, ...]:
    refs: list[str] = []
    clean = _normalize_text(claim_text)
    claim_digest = _digest_text(clean)
    for number in citation_numbers:
        url = str(citation_urls.get(int(number)) or "")
        source_id = source_ids_by_url.get(url)
        if not source_id:
            continue
        for evidence in evidence_by_source.get(source_id, ()):
            if not _stance_allowed_for_section(evidence.stance, section):
                continue
            if not _evidence_matches_claim(evidence, clean, claim_digest):
                continue
            if evidence.evidence_id not in refs:
                refs.append(evidence.evidence_id)
            if len(refs) >= MAX_REF_VALUES:
                return tuple(refs)
    return tuple(refs)


def _evidence_matches_claim(
    evidence: ResearchEvidence,
    claim_text: str,
    claim_digest: str,
) -> bool:
    if evidence.claim_text_digest == claim_digest:
        return True
    claim_norm = _normalize_for_match(claim_text)
    excerpt_norm = _normalize_for_match(evidence.bounded_excerpt)
    if len(excerpt_norm) < 24 or len(claim_norm) < 24:
        return False
    return excerpt_norm in claim_norm or claim_norm in excerpt_norm


def _derive_answer_status(
    *,
    summary: str,
    review: ReportQualityReview | None,
    evidence: tuple[ResearchEvidence, ...],
    unsupported_claim_count: int,
    stop_reason: str,
) -> str:
    if not str(summary or "").strip():
        return "not_answered"
    if not evidence:
        if getattr(review, "ok", False) or stop_reason == "done":
            return "insufficient_evidence"
        return "not_answered"
    if getattr(review, "ok", False) and unsupported_claim_count == 0:
        return "answered"
    return "partial"


def _claim_lines(text: str) -> tuple[str, ...]:
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+[\.)、]\s+", "", line)
        line = line.strip()
        if not line:
            continue
        lines.append(_clip(line, MAX_CLAIM_TEXT_CHARS))
        if len(lines) >= MAX_RECORD_CLAIMS:
            break
    return tuple(lines)


def _strip_citations(text: str) -> str:
    return _clip(re.sub(r"\[\d+(?:[^\]]*)?\]", "", str(text or "")).strip(), MAX_CLAIM_TEXT_CHARS)


def _looks_like_assumption(text: str, section: str) -> bool:
    lower = str(text or "").casefold()
    return section == "counter" or any(marker in lower for marker in _ASSUMPTION_MARKERS)


def _assumption_relation_kind(section: str) -> str:
    return "limits" if section == "counter" else ""


def _relation_kind_for_evidence(section: str, evidence: ResearchEvidence | None) -> str:
    if evidence is None:
        return ""
    stance = normalize_evidence_stance(evidence.stance)
    if section in {"conclusion", "evidence"} and stance == "supports":
        return "supports"
    if section == "counter" and stance == "contradicts":
        return "refutes"
    if section == "counter" and stance == "context":
        return "limits"
    return ""


def _stance_allowed_for_section(stance: str, section: str) -> bool:
    normalized = normalize_evidence_stance(stance)
    if section in {"conclusion", "evidence"}:
        return normalized == "supports"
    if section == "counter":
        return normalized in {"contradicts", "context"}
    return False


def _dedupe_assumptions(items: Iterable[ResearchAssumption]) -> list[ResearchAssumption]:
    rows: list[ResearchAssumption] = []
    seen: set[str] = set()
    for item in items:
        if item.assumption_id in seen:
            continue
        seen.add(item.assumption_id)
        rows.append(item)
    return rows


def _close_claim_graph(
    claims: list[ResearchClaim],
    assumptions: list[ResearchAssumption],
    relations: list[ResearchClaimRelation],
    fallback_unsupported_count: int,
) -> tuple[
    tuple[ResearchClaim, ...],
    tuple[ResearchAssumption, ...],
    tuple[ResearchClaimRelation, ...],
    int,
]:
    kept_assumptions = tuple(_dedupe_assumptions(assumptions)[:MAX_RECORD_ASSUMPTIONS])
    assumption_ids = {item.assumption_id for item in kept_assumptions}
    evidence_ids = {
        ref
        for claim in claims
        for ref in claim.evidence_refs
    }
    closed_claims: list[ResearchClaim] = []
    claim_ids: set[str] = set()
    for claim in claims[:MAX_RECORD_CLAIMS]:
        assumption_refs = tuple(ref for ref in claim.assumption_refs if ref in assumption_ids)
        status = _claim_status(claim.status)
        if status == "assumption" and not assumption_refs and not claim.evidence_refs:
            status = "unsupported"
        closed = replace(claim, assumption_refs=assumption_refs, status=status)
        closed_claims.append(closed)
        claim_ids.add(closed.claim_id)
    closed_relations = tuple(
        relation
        for relation in relations
        if relation.from_ref in claim_ids
        and (relation.to_ref in assumption_ids or relation.to_ref in evidence_ids)
    )[:MAX_RECORD_RELATIONS]
    unsupported = max(
        int(fallback_unsupported_count or 0),
        sum(1 for claim in closed_claims if claim.status == "unsupported"),
    )
    return tuple(closed_claims), kept_assumptions, closed_relations, unsupported


def _excerpt_offsets(text: str, excerpt: str) -> tuple[int, int]:
    source = str(text or "")
    needle = str(excerpt or "").strip()
    if not source or not needle:
        return 0, 0
    index = source.find(needle)
    if index < 0:
        return 0, 0
    return index, index + len(needle)


def _answer_status(value: object) -> str:
    text = _identifier(value, 40)
    return text if text in ANSWER_STATUSES else "not_answered"


def _relation_kind(value: object) -> str:
    text = _identifier(value, 40)
    return text if text in CLAIM_RELATION_KINDS else "limits"


def _claim_status(value: object) -> str:
    text = _identifier(value, 40)
    return text if text in CLAIM_STATUSES else "unsupported"


def _normalize_for_match(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


__all__ = [
    "ANSWER_STATUSES",
    "CLAIM_STATUSES",
    "CLAIM_RELATION_KINDS",
    "EXTRACTED_RELATION_KINDS",
    "EvidenceLocator",
    "RESEARCH_RECORD_KIND",
    "RESEARCH_RECORD_SCHEMA_VERSION",
    "ResearchAssumption",
    "ResearchClaim",
    "ResearchClaimRelation",
    "ResearchEvidence",
    "ResearchQuestion",
    "ResearchRecord",
    "ResearchSource",
    "build_research_record",
    "extract_claim_candidates",
    "path_ref",
    "project_ref",
    "research_record_summary",
    "sanitize_research_url_ref",
]
