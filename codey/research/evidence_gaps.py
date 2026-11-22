"""Question coverage from settled findings and immutable source passages."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from codey.research.ledger import ResearchLedger
from codey.research.passages import ReadPassage
from codey.research.query_planner import build_research_plan
from codey.research.source_document import normalized_doi
from codey.research.urls import full_key, parsed_url
from codey.types.json_value import JsonObject


@dataclass(frozen=True)
class ResearchGap:
    question_ref: str
    question: str
    reason: str
    passage_refs: tuple[str, ...]
    source_type: str


def evidence_gaps(ledger: ResearchLedger, questions: tuple[str, ...]) -> tuple[ResearchGap, ...]:
    gaps: list[ResearchGap] = []
    for index, question in enumerate(questions, 1):
        question_ref = f"q{index}"
        findings = [ref for ref in ledger.references.values() if ref.question_ref == question_ref
                    and ref.acquisition == "current_run" and ref.kind in {"claim", "observation"}]
        passages = tuple(dict.fromkeys(ref.passage_ref for ref in findings if ref.passage_ref in ledger.read_passages))
        notes = {ref.note_id for ref in findings if ref.note_id}
        claims: dict[str, set[str]] = {}
        for evidence in ledger.evidence_items:
            if evidence.note_id in notes:
                claims.setdefault(" ".join(evidence.claim.casefold().split()), set()).add(evidence.stance)
        if any({"supports", "contradicts"} <= stances for stances in claims.values()):
            gaps.append(ResearchGap(question_ref, question, "conflicting_findings", passages,
                                    "independent evidence under the same reported conditions"))
        elif not findings:
            gaps.append(ResearchGap(question_ref, question, "no_saved_direct_evidence", (), "direct source"))
    return tuple(gaps)


def gap_search_candidates(ledger: ResearchLedger, *, max_queries: int) -> list[JsonObject]:
    if max_queries <= 0:
        return []
    rows: list[JsonObject] = []
    attempted = {" ".join(row.query.casefold().split()) for row in ledger.searches}
    attempted.update(" ".join(query.casefold().split()) for row in ledger.investigations.values()
                     if row["tool"] == "web_search" and isinstance(query := row["args"].get("query"), str))
    for gap in evidence_gaps(ledger, ledger.questions):
        question = gap.question + (" replication contradictory result" if gap.reason == "conflicting_findings" else "")
        plan = build_research_plan(None, question=question, max_queries=1, max_sources=1)
        candidate = next(iter(plan.query_candidates), None)
        if candidate is None or " ".join(candidate.query_preview.casefold().split()) in attempted:
            continue
        rows.append({"question_ref": gap.question_ref, "reason": gap.reason,
                     "passage_refs": list(gap.passage_refs), "source_type": gap.source_type,
                     "query": candidate.query_preview})
        if len(rows) == max_queries:
            break
    return rows


def same_source_groups(passages: Mapping[str, ReadPassage], aliases: tuple[tuple[str, str], ...]) -> list[list[str]]:
    """Group one source identity or exact cross-site republications."""
    groups: list[tuple[set[str], list[str]]] = []
    for label, ref in aliases:
        passage = passages.get(ref)
        if passage is None:
            continue
        keys = {"url:" + full_key(passage.source.final_url)}
        requested = parsed_url(passage.source.requested_url)
        if passage.source.publication and passage.source.publication.doi:
            keys.add("doi:" + passage.source.publication.doi)
        if requested.hostname in {"doi.org", "dx.doi.org"} and (doi := normalized_doi(requested.path.lstrip("/"))):
            keys.add("doi:" + doi)
        if not passage.source.truncated and len(passage.source_text.strip()) >= 120:
            keys.add("text:" + passage.source.text_hash)
        matches = [(old_keys, labels) for old_keys, labels in groups if old_keys & keys]
        combined = [label]
        for old_keys, labels in matches:
            keys.update(old_keys)
            combined = labels + combined
        groups = [row for row in groups if row not in matches]
        groups.append((keys, combined))
    return [labels for _, labels in groups if len(labels) > 1]
