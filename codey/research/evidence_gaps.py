"""Question coverage from settled findings and immutable source passages."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations

from codey.research.calculation import is_exact_quantity
from codey.research.ledger import ResearchLedger
from codey.research.passages import ReadPassage
from codey.research.query_planner import MAX_QUERY_PREVIEW_CHARS, build_research_plan
from codey.research.source_connectors import safe_connector_query_terms
from codey.research.source_document import normalized_doi
from codey.research.urls import full_key, parsed_url
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object
from codey.utils.refs import stable_ref

MAX_SOURCE_PAIR_COMPARISONS = 120
_NEAR_DUPLICATE_VERSION = "character-7-jaccard-v1"


@dataclass(frozen=True)
class ResearchGap:
    question_ref: str
    question: str
    reason: str
    passage_refs: tuple[str, ...]
    source_type: str
    group_ref: str = ""
    reported_conditions: tuple[tuple[str, str], ...] = ()
    comparison_fields: tuple[str, ...] = ()


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
    for gap in (*evidence_gaps(ledger, ledger.questions), *_similarity_gaps(ledger)):
        candidate = _search_candidate(ledger, gap)
        if candidate is None or any(row["query"] == candidate["query"] for row in rows):
            continue
        rows.append(candidate)
        if len(rows) == max_queries:
            break
    return rows


def _attempted_queries(ledger: ResearchLedger) -> set[str]:
    attempted = {" ".join(row.query.casefold().split()) for row in ledger.searches}
    attempted.update(" ".join(query.casefold().split()) for row in ledger.investigations.values()
                     if row["tool"] == "web_search" and isinstance(query := row["args"].get("query"), str))
    return attempted


def _planned_query(gap: ResearchGap) -> str:
    suffix = {"conflicting_findings": "replication contradictory result",
              "suspected_republication": "independent original study"}.get(gap.reason, "")
    question = " ".join(part for part in (gap.question, suffix) if part)
    plan = build_research_plan(None, question=question, max_queries=1, max_sources=1)
    candidate = next(iter(plan.query_candidates), None)
    if candidate is None:
        return ""
    terms = tuple(term for name, value in gap.reported_conditions for term in (
        *safe_connector_query_terms(name, limit=2),
        value if len(value) <= 30 and is_exact_quantity(value) else " ".join(safe_connector_query_terms(value, limit=3)),
    ) if term)[:8]
    return " ".join((*terms, candidate.query_preview))[:MAX_QUERY_PREVIEW_CHARS].strip()


def _search_candidate(ledger: ResearchLedger, gap: ResearchGap) -> JsonObject | None:
    query = _planned_query(gap)
    if not query or " ".join(query.casefold().split()) in _attempted_queries(ledger):
        return None
    return {"question_ref": gap.question_ref, "reason": gap.reason,
            "passage_refs": list(gap.passage_refs), "source_type": gap.source_type, "query": query,
            **({"comparison_group": gap.group_ref} if gap.group_ref else {})}


def _normalized_conditions(conditions: list[JsonValue]) -> tuple[tuple[str, str, str], ...]:
    return tuple(sorted((" ".join(name.casefold().split()), state, " ".join(value.casefold().split()))
        for row in conditions if isinstance(row, dict)
        and isinstance(name := row.get("name"), str) and isinstance(state := row.get("state"), str)
        and isinstance(value := row.get("value", ""), str)))


def _comparison_gaps(ledger: ResearchLedger, findings: Iterable[ToolCall]) -> Iterable[ResearchGap]:
    """Compare explicit directions only for the same reported study conditions."""
    directional: dict[tuple[str, tuple[str, ...], tuple[tuple[str, str, str], ...]], dict[int, set[str]]] = {}
    question_order = {f"q{idx}": idx for idx, _ in enumerate(ledger.questions, 1)}
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
        if not question and len(question_order) == 1:
            question = "q1"
        raw_conditions = call.args.get("conditions", [])
        if (direction_value is None or not isinstance(passage, str) or passage not in ledger.read_passages
                or not isinstance(question, str) or not isinstance(raw_conditions, list)
                or (question_order and question not in question_order)):
            continue
        cohort = (question, fields, _normalized_conditions(raw_conditions))
        directional.setdefault(cohort, {}).setdefault(direction_value, set()).add(passage)
    for cohort in sorted(directional, key=lambda key: (question_order.get(key[0], 0), key)):
        question, fields, conditions = cohort
        reports = directional[cohort]
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
                                  "independent evidence under the same reported conditions",
                                  stable_ref("comparison", cohort),
                                  tuple((name, value) for name, state, value in conditions if state == "reported"), fields)
                break


def _additional_passages(ledger: ResearchLedger, gap: ResearchGap) -> tuple[str, ...]:
    if not gap.reported_conditions:
        return ()
    aliases = tuple((ref, ref) for ref in ledger.read_passages)
    shared = {member for group in same_source_groups(ledger.read_passages, aliases)
              if any(ref in group for ref in gap.passage_refs) for member in group}
    phrases = (*gap.comparison_fields, *(value for _name, value in gap.reported_conditions))
    return tuple(ref for ref, passage in sorted(ledger.read_passages.items())
        if ref not in gap.passage_refs and ref not in shared
        and all(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ".join(passage.excerpt.casefold().split()))
                for phrase in phrases))


def comparison_gap_observations(ledger: ResearchLedger, findings: Iterable[ToolCall]) -> list[JsonObject]:
    """Keep attempted query text and newly read comparison evidence independent."""
    attempted = _attempted_queries(ledger)
    return [{"comparison_group": gap.group_ref, "question_ref": gap.question_ref,
             "passage_refs": list(gap.passage_refs),
             "reported_conditions": [{"name": name, "value": value} for name, value in gap.reported_conditions],
             "query_attempted": " ".join(_planned_query(gap).casefold().split()) in attempted,
             "additional_passage_refs": list(_additional_passages(ledger, gap))}
            for gap in _comparison_gaps(ledger, findings)]


def conflict_search(ledger: ResearchLedger, *, remaining_searches: int,
                    findings: Iterable[ToolCall] = ()) -> JsonObject | None:
    """Pick one unresolved, untried contradiction with primary evidence."""
    if remaining_searches <= 0:
        return None
    comparisons = tuple(_comparison_gaps(ledger, findings))
    covered_questions = {gap.question_ref for gap in comparisons}
    gaps = (*(gap for gap in comparisons if not _additional_passages(ledger, gap)),
            *(gap for gap in evidence_gaps(ledger, ledger.questions) if gap.question_ref not in covered_questions))
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


@dataclass(frozen=True)
class SuspectedSourcePairs:
    pairs: tuple[JsonObject, ...]
    compared: int
    limit_reached: bool


def _source_shingles(text: str) -> set[str]:
    # Collapsing repeated blocks stops a recurring site template from carrying
    # extra weight. Character shingles also cover text without word spaces.
    parts = dict.fromkeys(" ".join(part.casefold().split()) for part in re.split(r"[\n。！？]+|(?<=[.!?])\s+", text))
    text = " ".join(part for part in parts if len(part) >= 18)[:24000]
    return {text[index:index + 7] for index in range(len(text) - 6)} if len(text) >= 256 else set()


def suspected_source_pairs(passages: Mapping[str, ReadPassage], aliases: tuple[tuple[str, str], ...], *,
                           max_pairs: int = MAX_SOURCE_PAIR_COMPARISONS) -> SuspectedSourcePairs:
    """Find candidates without changing source identity or claiming proven republication."""
    selected: dict[tuple[str, str], tuple[str, str]] = {}
    for label, ref in sorted(aliases):
        passage = passages.get(ref)
        if passage is not None and not passage.source.truncated:
            key = full_key(passage.source.final_url), passage.source.text_hash
            selected.setdefault(key, (label, ref))
    reps = tuple(selected.values())
    shared = {label: group[0] for group in same_source_groups(passages, reps) for label in group}
    shingles = {label: _source_shingles(passages[ref].source_text) for label, ref in reps}
    pairs: list[JsonObject] = []
    compared = 0
    for (left_label, left), (right_label, right) in combinations(reps, 2):
        first, second = shingles[left_label], shingles[right_label]
        if not first or not second or shared.get(left_label, left_label) == shared.get(right_label, right_label):
            continue
        if compared >= min(MAX_SOURCE_PAIR_COMPARISONS, max(0, max_pairs)):
            return SuspectedSourcePairs(tuple(pairs), compared, True)
        compared += 1
        shared_count = len(first & second)
        score = shared_count / (len(first) + len(second) - shared_count)
        if score >= .82:
            pairs.append({"passage_refs": [left, right], "labels": [left_label, right_label],
                          "version": _NEAR_DUPLICATE_VERSION, "score": round(score, 4),
                          "reason": "shared distinct character shingles"})
    return SuspectedSourcePairs(tuple(pairs), compared, False)


def _similarity_gaps(ledger: ResearchLedger) -> Iterable[ResearchGap]:
    candidates = suspected_source_pairs(ledger.read_passages, tuple((ref, ref) for ref in ledger.read_passages))
    for pair in candidates.pairs:
        refs = pair["passage_refs"]
        assert isinstance(refs, list)
        passage_refs = tuple(ref for ref in refs if isinstance(ref, str))
        for index, question in enumerate(ledger.questions, 1):
            question_ref = f"q{index}"
            if any(item.question_ref == question_ref and item.passage_ref in passage_refs
                   for item in ledger.references.values()):
                yield ResearchGap(question_ref, question, "suspected_republication", passage_refs, "independent original study")
