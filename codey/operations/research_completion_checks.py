"""Research completion checks for the unified gate (operations layer).

Owns source-open, ledger, and strict-report checks plus research text
finalization. Never mints a completion proof and never imports the gate.
"""

from __future__ import annotations

from typing import Any

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_NOT_RUN,
    CHECK_PASS,
    CompletionCheck,
    completion_check,
)


def _context_get(context: Any, key: str) -> Any:
    if isinstance(context, dict):
        return context.get(key)
    return getattr(context, key, None)


def knowledge_reference_checks(session: Any, context: Any = None) -> list[CompletionCheck]:
    raw = getattr(session, "last_done_args", {}).get("knowledge_refs")
    if not raw:
        return []
    from codey.knowledge.answer_refs import answer_references

    try:
        answer_references(raw, _context_get(context, "knowledge_store"),
                          project=str(_context_get(context, "knowledge_project") or ""),
                          session_id=str(_context_get(context, "knowledge_session_id") or ""))
    except (ValueError, TypeError, KeyError):
        return [CompletionCheck("knowledge_versions", CHECK_FAIL, "knowledge_reference_invalid")]
    # A locator/version check cannot prove an interpretation or applicability.
    return [CompletionCheck("knowledge_versions", CHECK_PASS)]


def source_requirement_checks(session: Any, context: Any = None) -> list[CompletionCheck]:
    """普通联网任务的来源要求：只检查任务要求的来源是否实际打开。"""
    policy = getattr(session, "policy", None)
    if not bool(getattr(policy, "sources_open_required", False)):
        return []
    ledger = _context_get(context, "research_ledger")
    if ledger is None:
        return [CompletionCheck("research_sources_opened", CHECK_FAIL, "research_ledger_missing")]
    opened = ledger.final_url_set()
    row = completion_check(
        "research_sources_opened",
        CHECK_PASS if opened else CHECK_NOT_RUN,
        "" if opened else "research_source_not_opened",
    )
    return [row] if row is not None else []


def investigation_attempt_checks(session: Any) -> list[CompletionCheck]:
    """An evidence-only phase must try acquisition, not recite its parent.

    Failure receipts count: an unavailable source can legitimately leave a
    gap. This is an execution check, never a claim of semantic coverage.
    """
    if (getattr(session, "task_kind", "") != "evidence_followup"
            or getattr(session, "research_acquisition_limits", None) is None):
        return []
    acquisition = {"web_search", "open_url", "open_result", "open_hit", "reopen_source", "source_search"}
    attempted = any(result.call.name in acquisition
                    or (result.call.name == "knowledge_write" and result.ok)
                    for result in session._memory_results.values())
    return [CompletionCheck("research_investigation_attempted", CHECK_PASS if attempted else CHECK_NOT_RUN,
                            "" if attempted else "research_investigation_not_attempted")]


def citation_requirement_checks(session: Any, text: str, context: Any) -> list[CompletionCheck]:
    policy = getattr(session, "policy", None)
    required = "research_citations" in getattr(policy, "required_checks", ())
    strict = bool(getattr(policy, "strict_research", False))
    if not (required or strict or getattr(policy, "sources_open_required", False)):
        return []
    ledger = _context_get(context, "research_ledger")
    if ledger is None:
        reason = "research_ledger_missing"
    else:
        from codey.research.provenance import citation_problem

        reason = citation_problem(text, ledger=ledger, source_ids=dict(session.source_ids),
                                  require_citation=required or strict) or ""
    return [CompletionCheck("research_citations", CHECK_FAIL if reason else CHECK_PASS, reason)]


def _missing_ledger_rows() -> list[CompletionCheck]:
    """Strict Research without a ledger is blocked, never downgraded."""
    rows: list[CompletionCheck] = []
    for check_id, reason in (
        ("research_ledger", "research_ledger_missing"),
        ("research_sources_opened", "research_ledger_missing"),
        ("research_evidence_saved", "research_ledger_missing"),
        ("research_report_sections", "research_ledger_missing"),
    ):
        row = completion_check(check_id, CHECK_FAIL, reason)
        if row is not None:
            rows.append(row)
    return rows


def strict_research_checks(session: Any, done_text: str, context: Any = None) -> list[CompletionCheck]:
    """严格 Research 专属：证据归档、报告与研究质量（普通任务不调用）。

    Strict Research always requires a valid evidence ledger. A missing
    ledger blocks instead of falling back to weak session/text checks.
    """
    policy = getattr(session, "policy", None)
    if not bool(getattr(policy, "strict_research", False)):
        return []
    if context is None:
        return _missing_ledger_rows()
    return _ledger_checks(session, done_text, context)


def research_completion_feedback(session: Any, text: str, context: Any, checks: list[CompletionCheck]) -> str:
    """Explain failed report checks without rewriting the proposed answer."""
    failed = {row.check_id for row in checks if row.status != CHECK_PASS}
    parts: list[str] = []
    if "research_investigation_attempted" in failed:
        return ("This evidence investigation has not attempted acquisition. Parent findings were saved in an "
                "earlier phase, not here. Next action: use source_search on a previously opened URL or "
                "web_search for the first missing comparison or condition, then inspect the passage and save "
                "any supported new finding. If acquisition fails, report the actual failed attempt and remaining "
                "gap in a brief done.summary; do not invent evidence or repeat the parent report.")
    if "knowledge_versions" in failed:
        parts.append("done.knowledge_refs must use the structured object id and revision from "
                     "Structured knowledge versions or knowledge_read, not a note_id, Markdown path, "
                     "source id, tool result_ref or old answer. Read the cited object before resubmitting; "
                     "do not guess an id or a revision.")
        from codey.knowledge.model import canonical_json

        raw_refs = getattr(session, "last_done_args", {}).get("knowledge_refs", ())
        if isinstance(raw_refs, (tuple, list)):
            requests = [canonical_json({"tool": "knowledge_read", "args": {"id": row["id"]}})
                        for row in raw_refs[:3] if isinstance(row, dict) and isinstance(row.get("id"), str)]
            if requests:
                parts.append("Next action: execute one knowledge_read request below to inspect the cited object "
                             "or legacy note. Copy the returned structured version, not its document id. "
                             "Do not repeat done with the rejected reference.\n" + "\n".join(requests))
        if "research_evidence_saved" not in failed:
            parts.append("Fix this reference defect first; the remaining report/evidence checks will run again "
                         "on the next proposed answer.")
            return "\n\n".join(parts)
    if "research_evidence_saved" in failed:
        parts.append("No citable evidence has been saved. Use knowledge_write with type=fact, "
                     "sources=[an opened URL], and evidence=[{claim:your conclusion,source_url:the same URL,"
                     "excerpt:exact text read from that page,stance:supports}]. "
                     "A hypothesis without source excerpts does not satisfy this requirement. "
                     "The excerpt must support the actual predicate and conditions of the claim; "
                     "use source_search or an offset read if the relevant passage was omitted.")
        parts.append("Save a scoped finding and wait for a successful knowledge_write result before another done. "
                     "Previously proposed or displayed calls are not saved evidence. Report formatting and "
                     "citation repairs can follow after evidence is actually saved.")
        return "\n\n".join(parts)
    ledger = _context_get(context, "research_ledger")
    if ledger is not None and failed.intersection({"research_citations", "research_report_sections", "research_report_quality"}):
        prerequisite = _citation_prerequisite_feedback(session, text, ledger)
        if prerequisite:
            parts.append(prerequisite)
            return "\n\n".join(parts)
    if (ledger is not None and "research_report_quality" in failed
            and "research_report_sections" not in failed):
        from codey.research.report_quality import review_report_quality

        review = review_report_quality(text, ledger=ledger, opened_sources=ledger.final_url_set(),
                                      search_result_urls=set(getattr(session, "search_results", {}).values()))
        if not review.ok:
            # Source/format defects can make otherwise saved statements appear
            # unbound. Give one repair, then rerun all checks on the next draft.
            parts.append(review.message)
            parts.append("Repair this citation/report defect first, preserving the other sections and saved "
                         "source wording. This alone does not require another search or knowledge_write. "
                         "Claim support and coverage checks will run again on the corrected report.")
            return "\n\n".join(parts)
    if "research_claim_bindings" in failed:
        parts.append("Some conclusion/evidence statements could not be linked to saved claim excerpts. "
                     "This is a text-binding check, not a verdict that the statement is false. "
                     "Keep supported wording from evidence[].claim, or save a separate exact excerpt for the missing claim. "
                     "Putting two findings in a note body with only one evidence item does not support both. "
                     "Do not relabel a sourced finding as a hypothesis to bypass the missing evidence. "
                     "The report has been preserved, not silently reduced to its supported subset.")
        parts.append("Every factual evidence paragraph needs its own inline [n] citation to the numbered source table. "
                     "Citations in summary or a source list do not cite an uncited evidence paragraph. "
                     "A wording match alone is insufficient without the source citation.")
        ledger = _context_get(context, "research_ledger")
        if ledger is not None:
            from codey.research.object_model import build_research_record

            record = build_research_record(question=str(_context_get(context, "question") or ""),
                                           summary=text, ledger=ledger, stop_reason="done")
            unbound = [claim.claim_text[:240] for claim in record.claims
                       if claim.claim_section in {"conclusion", "evidence"} and claim.status == "unsupported"]
            parts.append("Unbound report statements:\n" + "\n".join("- " + line for line in unbound[:4]))
            saved = list(dict.fromkeys(item.claim for item in ledger.evidence_items if item.stance == "supports"))
            parts.append("Saved claim wording available:\n" + "\n".join("- " + line[:240] for line in saved[:6]))
            policy = getattr(session, "policy", None)
            if policy is not None and policy.allows("web.read") and policy.allows("knowledge.write"):
                parts.append("Next action: investigate the first missing factual finding using source_search "
                             "and open_hit on its cited source, then knowledge_write its own scoped claim and "
                             "exact passage. Do not substitute an unrelated saved finding or repeat the rejected "
                             "done. If the saved finding already expresses the SAME predicate and conditions, "
                             "reuse that wording with its own citation instead. If evidence remains unavailable, "
                             "state the specific unresolved question and search limit without claiming coverage.")
            else:
                parts.append("Use available saved wording only for the same predicate and conditions. "
                             "Report any unresolved evidence requirement and authorization limit; "
                             "do not substitute an unrelated saved finding.")
            parts.append("Keep the saved conditions and limits; do not add unsupported elaborations. "
                         "With report_sections, summary is conclusion text only, without Markdown headings.")
    if "research_report_sections" in failed:
        from codey.reviews.report_sections import REQUIRED_SECTIONS, parse_sections, section_title

        sections = parse_sections(text)
        missing = [section_title(key) for key in REQUIRED_SECTIONS if not sections.get(key, "").strip()]
        parts.append("Repair report formatting using the evidence already saved; this alone does not require another search.")
        if missing:
            parts.append("Missing sections: " + ", ".join(missing) + ". Send done.summary as only the cited "
                         "conclusion and done.report_sections with five nonempty text arrays: evidence, counter, "
                         "source_quality, coverage, sources. The application adds the required headings.")
        else:
            parts.append("All required sections are present; repair the specific citation/report defect below. "
                         'With report_sections, sources must contain numbered entries such as '
                         '"[1] Title - https://final-url", not bare URLs. Preserve the other sections.')
        if "\\n" in text and "\n" not in text:
            parts.append("The summary contains literal backslash+n text, not line breaks. "
                         "Use done.report_sections: put only the conclusion in summary, and supply "
                         "evidence, counter, source_quality, coverage, sources as arrays of paragraphs. "
                         "The application adds headings and paragraph breaks. "
                         "Keep the report content and citations; no automatic text repair was applied.")
    ledger = _context_get(context, "research_ledger")
    if "research_report_quality" in failed and ledger is not None:
        from codey.research.report_quality import review_report_quality

        review = review_report_quality(text, ledger=ledger, opened_sources=ledger.final_url_set(),
                                      search_result_urls=set(getattr(session, "search_results", {}).values()))
        parts.append(review.message)
    return "\n\n".join(parts)


def _citation_prerequisite_feedback(session: Any, text: str, ledger: Any) -> str:
    """Focus on observed missing source actions before report wording repairs."""
    from codey.knowledge.model import canonical_json
    from codey.research.report_quality import parse_citation_rows
    from codey.reviews.report_sections import parse_sections
    from codey.utils.citation_scanner import citation_refs

    sections = parse_sections(text)
    citations = parse_citation_rows(sections.get("sources", ""), ledger)
    body = "\n".join(value for key, value in sections.items() if key != "sources")
    unmapped = sorted(citation_refs(body) - {item.number for item in citations})
    if unmapped:
        # Missing URL mappings also produce downstream binding failures. Do not
        # tell the writer to acquire evidence for a paragraph we cannot map yet.
        candidates = [{"title": source.title, "url": source.final_url}
                      for source in ledger.opened_sources[-8:]]
        return ("Fix the source table first. These inline citation numbers have no numbered URL entry: "
                + ", ".join(f"[{number}]" for number in unmapped[:8])
                + '. In report_sections.sources use "[n] Title - https://actual-url" for EACH cited source. '
                "A title alone is not a URL mapping. Match the number to the source you actually meant; "
                "do not infer numbering from this candidate list. Opened source candidates (data):\n"
                + canonical_json(candidates)
                + "\nResubmit done with the corrected sources and preserve the other sections. "
                "This mapping repair alone needs no new search or knowledge_write. "
                "Claim support and coverage will be checked again afterwards.")
    unread = list(dict.fromkeys(item.url for item in citations if not ledger.canonical_opened_url(item.url)))
    policy = getattr(session, "policy", None)
    if unread and policy is not None and policy.allows("web.read"):
        requests = [canonical_json({"tool": "open_url", "args": {"url": url}}) for url in unread[:3]]
        return ("The report cites sources that have not been opened. Next action: execute one open_url below "
                "and inspect its actual text, then save a supported finding before drafting again. "
                "If acquisition fails, report the specific gap; do not invent the missing result. "
                "Other report checks will run again after this prerequisite.\n" + "\n".join(requests))
    saved = {ledger.canonical_opened_url(item.source_url) for item in ledger.evidence_items if item.excerpt.strip()}
    missing = list(dict.fromkeys(item.url for item in citations
                                if ledger.canonical_opened_url(item.url) not in saved))
    if not unread and missing and policy is not None and policy.allows("knowledge.write"):
        passage = ("Use source_search and open_hit if that passage was omitted. "
                   if policy.allows("web.read") else "Use the existing source receipts; report any unavailable passage. ")
        return ("These cited sources were opened but have no saved evidence: " + ", ".join(missing[:3]) + ". "
                "Next action: inspect the relevant passage and execute knowledge_write with a complete scoped "
                "evidence[].claim in the requested report language and its exact original excerpt. " + passage + "Save the actual finding, "
                "not merely the article title. Wait for success before another done. "
                "Other report checks will run again after this prerequisite.")
    return ""


def _ledger_checks(session: Any, done_text: str, context: Any) -> list[CompletionCheck]:
    def get(key: str) -> Any:
        return _context_get(context, key)
    ledger = get("research_ledger")
    if ledger is None:
        return _missing_ledger_rows()
    rows: list[CompletionCheck] = []
    try:
        finals = set(ledger.final_url_set())
    except Exception as exc:
        row = completion_check("research_ledger", CHECK_FAIL, f"ledger_error:{type(exc).__name__}")
        return [row] if row is not None else []
    try:
        citable = _ledger_citable_urls(ledger, finals)
    except Exception as exc:
        row = completion_check("research_evidence", CHECK_FAIL, f"evidence_error:{type(exc).__name__}")
        return [row] if row is not None else []
    row = completion_check(
        "research_sources_opened",
        CHECK_PASS if finals else CHECK_NOT_RUN,
        "" if finals else "research_source_not_opened",
    )
    if row is not None:
        rows.append(row)
    row = completion_check(
        "research_evidence_saved",
        CHECK_PASS if citable else CHECK_NOT_RUN,
        "" if citable else "research_evidence_missing",
    )
    if row is not None:
        rows.append(row)
    try:
        from codey.research.done_finalizer import finalize_done_answer
    except Exception as exc:
        row = completion_check("research_report", CHECK_FAIL, f"finalizer_unavailable:{type(exc).__name__}")
        rows.append(row) if row is not None else None
        return rows
    try:
        finalized = finalize_done_answer(
            (done_text or ""), ledger,
            source_ids=dict(session.source_ids),
            question=str(get("question") or ""),
        )
    except Exception as exc:
        row = completion_check("research_report", CHECK_FAIL, f"finalizer_error:{type(exc).__name__}")
        if row is not None:
            rows.append(row)
        return rows
    if getattr(finalized, "reason", "") in {"no_report_sections", "no_citable_sources",
                                            "unmapped_numeric_refs", "unmapped_source_id_refs",
                                            "no_referenced_citable_sources"}:
        row = completion_check("research_report_sections", CHECK_FAIL, f"report_{finalized.reason}")
    elif "结论" in (done_text or "") and "来源" in (done_text or ""):
        row = completion_check("research_report_sections", CHECK_PASS)
    else:
        row = completion_check("research_report_sections", CHECK_FAIL, "research_report_incomplete")
    if row is not None:
        rows.append(row)
    try:
        from codey.research.contract import research_completion_checks
        from codey.research.object_model import build_research_record
        from codey.research.proof_quality import review_research_proof
        from codey.research.report_quality import review_report_quality

        record = get("research_record")
        if record is None:
            report_review = review_report_quality(
                (done_text or ""),
                ledger=ledger,
                opened_sources=finals,
                search_result_urls=set(getattr(session, "search_results", {}).values()),
            )
            quality_row = completion_check(
                "research_report_quality",
                CHECK_PASS if report_review.ok else CHECK_FAIL,
                "" if report_review.ok else "research_report_quality_failed",
            )
            if quality_row is not None:
                rows.append(quality_row)
            record = build_research_record(
                question=str(get("question") or ""), summary=(done_text or ""),
                ledger=ledger, review=report_review,
                run_id=str(get("run_id") or ""), project=get("project"),
                stop_reason="done",
            )
        rows.append(CompletionCheck("research_claim_bindings",
            CHECK_FAIL if record.unsupported_claim_count else CHECK_PASS,
            "report_claim_binding_incomplete" if record.unsupported_claim_count else ""))
        if get("require_proof_review"):
            review = review_research_proof(record, question=str(get("question") or ""),
                                           evidence_ledger=get("evidence_ledger_payload"))
            for item in research_completion_checks(review) or ():
                if isinstance(item, CompletionCheck):
                    rows.append(item)
    except Exception as exc:
        row = completion_check("research_proof", CHECK_FAIL, f"proof_error:{type(exc).__name__}")
        if row is not None:
            rows.append(row)
    return rows


def _ledger_citable_urls(ledger: Any, finals: set[str]) -> list[str]:
    urls: list[str] = []
    for item in getattr(ledger, "evidence_items", ()) or ():
        url = str(getattr(item, "source_url", "") or "").strip()
        excerpt = str(getattr(item, "excerpt", "") or "").strip()
        if not url or not excerpt:
            continue
        canonical = ledger.canonical_opened_url(url)
        if canonical in finals and canonical not in urls:
            urls.append(canonical)
    return urls

def finalize_research_text(session: Any, text: str, context: Any) -> str:
    """Check and publish the same evidence-compiled answer, as the old runner did.

    A missing strict ledger never finalizes: the ledger checks already
    block completion, and inventing a report without evidence would hide
    that dependency.
    """
    if not getattr(getattr(session, "policy", None), "strict_research", False):
        return text
    def get(key: str) -> Any:
        return _context_get(context, key)
    ledger = get("research_ledger")
    if ledger is None:
        return text
    from codey.research.done_finalizer import finalize_done_answer

    # Citation compilation may renumber references; it must not silently delete
    # or downgrade the answer before completion checks inspect its claims.
    finalized = finalize_done_answer(text, ledger, source_ids=dict(session.source_ids),
                                    question=str(get("question") or ""), enforce_claim_support=False)
    return finalized.text.strip()


__all__ = [
    "finalize_research_text",
    "source_requirement_checks",
    "strict_research_checks",
]
