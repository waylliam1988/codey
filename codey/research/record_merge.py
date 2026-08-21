"""Deterministic patch applier for Research records and reports.

This module combines initial ResearchRunResult with newly written evidence
from bounded follow-up, re-indexing citations and re-computing immutable
ResearchRecord graph objects deterministically.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Sequence

from codey.research.done_finalizer import (
    finalize_done_answer,
    render_research_report_sections,
)
from codey.research.identity import clip, digest_text as _digest_text
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.object_model import (
    ResearchRecord,
    _source_from_opened,
    build_research_record,
)
from codey.research.plan_executor import PlanExecutionResult
from codey.research.report_quality import (
    ReportQualityReview,
    parse_sections,
    review_report_quality,
)
from codey.research.runner import ResearchRunResult
from codey.research.tools import ResearchTools


def merge_evidence_patch(
    initial: ResearchRunResult,
    tools: ResearchTools,
    material: PlanExecutionResult | None = None,
) -> ResearchRunResult:
    """Merge newly collected evidence items into initial ResearchRunResult deterministically."""
    if not hasattr(tools, "ledger") or tools.ledger is None:
        return initial
    initial_record = getattr(initial, "research_record", None)
    ledger = tools.ledger
    fresh_urls = set(material.fresh_source_urls) if material is not None else set()
    new_evidence = _find_new_evidence_items(initial_record, ledger, fresh_urls)
    if not new_evidence:
        return initial

    sections = parse_sections(initial.summary)
    if not sections:
        initial_body = initial.summary.strip()
        if not initial_body or initial_body.startswith("ERROR:") or "protocol" in str(initial.stop_reason or ""):
            initial_body = f"Research findings for: {initial.question}"
        sections = {
            "conclusion": initial_body,
            "evidence": "",
            "counter": "",
            "quality": "",
            "coverage": "",
            "sources": "",
        }

    merged_sections = _inject_new_evidence_into_sections(sections, new_evidence, ledger)
    preliminary_text = render_research_report_sections(merged_sections)

    finalized = finalize_done_answer(preliminary_text, ledger)
    final_text = finalized.text if finalized.text.strip() else preliminary_text

    quality_review: ReportQualityReview | None = None
    try:
        quality_review = review_report_quality(
            final_text,
            ledger=ledger,
            opened_sources=ledger.final_url_set(),
            search_result_urls=ledger.search_result_urls(),
        )
    except Exception:
        quality_review = None

    project_val = (
        getattr(initial_record, "project_ref", {}).get("path")
        if isinstance(getattr(initial_record, "project_ref", None), dict)
        else getattr(initial_record, "project", "") or getattr(tools, "project", "")
    )
    merge_synthesis_id = f"synthesis:merge:{hashlib.sha256(final_text.encode('utf-8')).hexdigest()[:12]}"

    new_record = build_research_record(
        summary=final_text,
        question=initial.question,
        session_id=getattr(initial_record, "session_id", "") or getattr(tools, "session_id", ""),
        project=project_val,
        run_id=getattr(initial_record, "run_id", ""),
        ledger=ledger,
        review=quality_review,
        synthesis_id=merge_synthesis_id,
        stop_reason="done",
    )

    seen_final: set[str] = set()
    final_urls: list[str] = []
    for opened in getattr(ledger, "opened_sources", ()):
        c_url = ledger.canonical_opened_url(opened.final_url or opened.requested_url) or str(opened.final_url or opened.requested_url or "").strip()
        if c_url and c_url not in seen_final:
            seen_final.add(c_url)
            final_urls.append(c_url)
    for u in sorted(ledger.final_url_set()):
        if u and u not in seen_final:
            seen_final.add(u)
            final_urls.append(u)

    opened_list = [item.to_dict() for item in getattr(ledger, "opened_sources", ())]
    evidence_list = [item.to_dict() for item in getattr(ledger, "evidence_items", ())]
    citation_map = [
        item.to_dict()
        for item in (getattr(quality_review, "citation_map", ()) or ())
    ]
    coverage = getattr(quality_review, "coverage", {}) if quality_review is not None else initial.coverage
    warnings = list(getattr(quality_review, "warnings", ()) or ())

    merged_queries = list(
        dict.fromkeys(
            list(initial.queries or [])
            + list(getattr(material, "queries_executed", ()) or [])
        )
    )

    # Use complete search_results_payload shape from ledger
    ledger_search_results = ledger.search_results_payload()
    seen_search_keys = {(r.get("query"), r.get("url")) for r in ledger_search_results if isinstance(r, dict)}
    extra_initial = [
        r for r in (initial.search_results or [])
        if isinstance(r, dict) and (r.get("query"), r.get("url")) not in seen_search_keys
    ]
    merged_search_results = ledger_search_results + extra_initial

    merged_notes_created = list(
        dict.fromkeys(
            list(initial.notes_created or [])
            + list(getattr(tools, "created_ids", ()) or [])
        )
    )
    merged_notes_updated = list(
        dict.fromkeys(
            list(initial.notes_updated or [])
            + list(getattr(tools, "updated_ids", ()) or [])
        )
    )
    staged_counterpoints = [
        str(item.claim or item.excerpt or "").strip()
        for item in getattr(ledger, "evidence_items", ())
        if item.stance in {"contradicts", "context"} and str(item.claim or item.excerpt or "").strip()
    ]
    merged_counterpoints = list(
        dict.fromkeys(
            list(initial.counterpoints or []) + staged_counterpoints
        )
    )
    merged_links_created = max(
        int(getattr(initial, "links_created", 0) or 0),
        int(getattr(tools, "links_created", 0) or 0),
    )

    return replace(
        initial,
        summary=final_text,
        research_record=new_record,
        opened_sources=opened_list or initial.opened_sources,
        source_urls=final_urls or initial.source_urls,
        sources_read=len(final_urls) or initial.sources_read,
        evidence_items=evidence_list or initial.evidence_items,
        citation_map=citation_map or initial.citation_map,
        coverage=coverage or initial.coverage,
        quality_warnings=warnings or initial.quality_warnings,
        queries=merged_queries,
        search_results=merged_search_results,
        notes_created=merged_notes_created,
        notes_updated=merged_notes_updated,
        links_created=merged_links_created,
        counterpoints=merged_counterpoints,
        synthesis_id=merge_synthesis_id,
        turns=initial.turns + 1,
        max_turns_used=max(initial.max_turns_used, initial.turns + 1),
        stop_reason="done",
    )


def _find_new_evidence_items(
    initial_record: ResearchRecord | None,
    ledger: ResearchLedger,
    fresh_urls: set[str],
) -> list[EvidenceItem]:
    existing_pairs: set[tuple[str, str]] = set()
    if initial_record is not None:
        source_id_to_canonical_url: dict[str, str] = {}
        for opened in getattr(ledger, "opened_sources", ()):
            src_obj = _source_from_opened(opened)
            c_url = ledger.canonical_opened_url(opened.final_url or opened.requested_url)
            if c_url:
                source_id_to_canonical_url[src_obj.source_id] = c_url

        for ev in getattr(initial_record, "evidence", ()):
            s_url = source_id_to_canonical_url.get(ev.source_id, "")
            dig = str(getattr(ev, "excerpt_digest", "") or _digest_text(getattr(ev, "bounded_excerpt", "") or ""))
            if dig:
                if s_url:
                    existing_pairs.add((s_url, dig))
                else:
                    existing_pairs.add(("", dig))

    new_items: list[EvidenceItem] = []
    final_urls = ledger.final_url_set()
    for item in getattr(ledger, "evidence_items", ()):
        url = ledger.canonical_opened_url(item.source_url) or str(item.source_url or "").strip()
        excerpt = str(item.excerpt or "").strip()
        if not url or not excerpt:
            continue
        if url not in final_urls:
            continue
        if fresh_urls and url not in fresh_urls:
            continue
        item_digest = _digest_text(excerpt)
        pair = (url, item_digest)
        if pair in existing_pairs or ("", item_digest) in existing_pairs:
            continue
        existing_pairs.add(pair)
        new_items.append(item)
    return new_items


def _inject_new_evidence_into_sections(
    sections: dict[str, str],
    new_evidence: Sequence[EvidenceItem],
    ledger: ResearchLedger,
) -> dict[str, str]:
    updated = dict(sections)
    conclusion_lines: list[str] = []
    evidence_lines: list[str] = []
    counter_lines: list[str] = []
    source_lines: list[str] = []
    existing_source_text = updated.get("sources", "").strip()
    if existing_source_text:
        source_lines.extend(existing_source_text.splitlines())

    url_to_num: dict[str, int] = {}
    for line in source_lines:
        match = re.match(r"^\[(\d+)\]\s+(.*?)\s+-\s+(https?://\S+)", line.strip())
        if match:
            num, _, url = match.groups()
            canonical = ledger.canonical_opened_url(url) or url
            url_to_num[canonical] = int(num)

    next_num = max(url_to_num.values(), default=0) + 1

    # Pre-populate url_to_num and source_lines for all new evidence items so valid_numbers is comprehensive
    for item in new_evidence:
        url = ledger.canonical_opened_url(item.source_url) or str(item.source_url or "").strip()
        if not url:
            continue
        if url not in url_to_num:
            url_to_num[url] = next_num
            title = ledger.source_title(url).strip() or "Source"
            source_lines.append(f"[{next_num}] {title} - {url}")
            next_num += 1

    valid_numbers = set(url_to_num.values())

    def _is_evidence_backed(line: str) -> bool:
        refs = [int(m) for m in re.findall(r"\[(\d+)\]", line)]
        if not refs:
            return False
        return all(r in valid_numbers for r in refs)

    def _is_valid_counter(line: str) -> bool:
        refs = [int(m) for m in re.findall(r"\[(\d+)\]", line)]
        if refs:
            return all(r in valid_numbers for r in refs)
        return any(m in line for m in ("未找到", "没有找到", "限制", "持续追踪", "no strong counter", "limitation"))

    # Prune un-cited or unmapped/unsupported raw lines from conclusion
    existing_conclusion_text = updated.get("conclusion", "").strip()
    if existing_conclusion_text:
        for line in existing_conclusion_text.splitlines():
            sline = line.strip()
            if not sline:
                continue
            if _is_evidence_backed(sline):
                conclusion_lines.append(sline)

    # Prune un-cited or unmapped/unsupported raw lines from initial evidence
    existing_evidence_text = updated.get("evidence", "").strip()
    if existing_evidence_text:
        for line in existing_evidence_text.splitlines():
            sline = line.strip()
            if not sline:
                continue
            if _is_evidence_backed(sline):
                evidence_lines.append(sline)

    # Prune un-cited or unmapped/unsupported raw lines from initial counter
    existing_counter_text = updated.get("counter", "").strip()
    if existing_counter_text:
        for line in existing_counter_text.splitlines():
            sline = line.strip()
            if not sline:
                continue
            if _is_valid_counter(sline):
                counter_lines.append(sline)

    for item in new_evidence:
        url = ledger.canonical_opened_url(item.source_url) or str(item.source_url or "").strip()
        claim = str(item.claim or "").strip()
        excerpt = str(item.excerpt or "").strip()
        text = claim or excerpt
        if not text or not url or url not in url_to_num:
            continue
        num = url_to_num[url]
        line = f"- [{num}] {clip(text, 240)}"
        if item.stance in {"contradicts", "context"}:
            counter_lines.append(line)
        else:
            evidence_lines.append(line)

    if not conclusion_lines and evidence_lines:
        first_ev = evidence_lines[0]
        match = re.search(r"\[(\d+)\]\s*(.*)", first_ev)
        if match:
            c_num, c_text = match.groups()
            conclusion_lines.append(f"- {c_text} [{c_num}]")
        else:
            c_num = next(iter(url_to_num.values()), 1)
            conclusion_lines.append(f"- Research findings supported by verified sources. [{c_num}]")
    elif not conclusion_lines and url_to_num:
        c_num = next(iter(url_to_num.values()), 1)
        conclusion_lines.append(f"- Research findings supported by verified sources. [{c_num}]")

    updated["conclusion"] = "\n".join(conclusion_lines).strip()
    updated["evidence"] = "\n".join(evidence_lines).strip()
    updated["counter"] = "\n".join(counter_lines).strip()
    updated["sources"] = "\n".join(source_lines).strip()

    return updated





__all__ = [
    "merge_evidence_patch",
]
