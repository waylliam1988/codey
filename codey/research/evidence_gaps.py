"""Question coverage from settled findings and immutable source passages."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from codey.research.ledger import ResearchLedger
from codey.research.passages import ReadPassage
from codey.research.query_planner import build_research_plan
from codey.research.source_document import normalized_doi
from codey.research.urls import full_key, parsed_url
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject, canonical_json_object


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
    for gap in evidence_gaps(ledger, ledger.questions):
        candidate = _search_candidate(ledger, gap)
        if candidate is None:
            continue
        rows.append(candidate)
        if len(rows) == max_queries:
            break
    return rows


def _search_candidate(ledger: ResearchLedger, gap: ResearchGap) -> JsonObject | None:
    attempted = {" ".join(row.query.casefold().split()) for row in ledger.searches}
    attempted.update(" ".join(query.casefold().split()) for row in ledger.investigations.values()
                     if row["tool"] == "web_search" and isinstance(query := row["args"].get("query"), str))
    question = gap.question + (" replication contradictory result" if gap.reason == "conflicting_findings" else "")
    plan = build_research_plan(None, question=question, max_queries=1, max_sources=1)
    candidate = next(iter(plan.query_candidates), None)
    if candidate is None or " ".join(candidate.query_preview.casefold().split()) in attempted:
        return None
    return {"question_ref": gap.question_ref, "reason": gap.reason,
            "passage_refs": list(gap.passage_refs), "source_type": gap.source_type, "query": candidate.query_preview}


def _comparison_gaps(ledger: ResearchLedger, findings: Iterable[ToolCall]) -> Iterable[ResearchGap]:
    """Compare explicit directions only for the same reported study conditions."""
    from codey.knowledge.model import canonical_json

    directional: dict[tuple[str, tuple[str, ...], tuple[str, ...]], dict[int, set[str]]] = {}
    for call in findings:
        comparison = canonical_json_object(call.args.get("comparison", {}))
        fields = tuple(" ".join(str(comparison.get(key, "")).casefold().split())
                       for key in ("subject", "factor", "comparator", "endpoint"))
        if any(field in {"", "unknown"} for field in fields):
            continue
        direction = " ".join(str(comparison.get("direction", "")).casefold().split())
        direction_value = {"higher": 1, "increase": 1, "increased": 1,
                           "lower": -1, "decrease": -1, "decreased": -1}.get(direction)
        passage = call.args.get("passage_ref")
        question = call.args.get("question_ref", "")
        conditions = call.args.get("conditions", [])
        if (direction_value is None or not isinstance(passage, str) or passage not in ledger.read_passages
                or not isinstance(question, str) or not isinstance(conditions, list)):
            continue
        cohort = (question, fields, tuple(sorted(canonical_json(condition) for condition in conditions)))
        directional.setdefault(cohort, {}).setdefault(direction_value, set()).add(passage)
    for (question, fields, _conditions), reports in directional.items():
        if set(reports) != {-1, 1}:
            continue
        refs = sorted(reports[-1] | reports[1])
        groups = same_source_groups(ledger.read_passages, tuple((ref, ref) for ref in refs))
        source = {ref: group[0] for group in groups for ref in group}
        # A singleton's passage ref is its source key. Compare only independent pairs.
        higher = sorted(reports[1])
        for lower in sorted(reports[-1]):
            other = next((ref for ref in higher if source.get(ref, ref) != source.get(lower, lower)), None)
            if other is not None:
                yield ResearchGap(question, " ".join(fields), "conflicting_findings", (lower, other),
                                  "independent evidence under the same reported conditions")
                break


def conflict_search(ledger: ResearchLedger, *, remaining_searches: int,
                    findings: Iterable[ToolCall] = ()) -> JsonObject | None:
    """Pick one unresolved, untried contradiction with primary evidence."""
    if remaining_searches <= 0:
        return None
    gaps = (*evidence_gaps(ledger, ledger.questions), *_comparison_gaps(ledger, findings))
    return next((row for gap in gaps if gap.reason == "conflicting_findings" and gap.passage_refs
                 if (row := _search_candidate(ledger, gap)) is not None), None)


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
