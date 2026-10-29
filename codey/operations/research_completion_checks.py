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
    if "knowledge_versions" in failed:
        parts.append("done.knowledge_refs must use the structured object id and revision from "
                     "Structured knowledge versions or knowledge_read, not a note_id, Markdown path, "
                     "source id, tool result_ref or old answer. Read the cited object before resubmitting; "
                     "do not guess an id or a revision.")
    if "research_evidence_saved" in failed:
        parts.append("No citable evidence has been saved. Use knowledge_write with type=fact, "
                     "sources=[an opened URL], and evidence=[{claim:your conclusion,source_url:the same URL,"
                     "excerpt:exact text read from that page,stance:supports}]. "
                     "A hypothesis without source excerpts does not satisfy this requirement. "
                     "The excerpt must support the actual predicate and conditions of the claim; "
                     "use source_search or an offset read if the relevant passage was omitted.")
    if "research_claim_bindings" in failed:
        parts.append("Some conclusion/evidence statements could not be linked to saved claim excerpts. "
                     "This is a text-binding check, not a verdict that the statement is false. "
                     "Keep supported wording from evidence[].claim, or save a separate exact excerpt for the missing claim. "
                     "Putting two findings in a note body with only one evidence item does not support both. "
                     "Do not relabel a sourced finding as a hypothesis to bypass the missing evidence. "
                     "The report has been preserved, not silently reduced to its supported subset.")
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
    if "research_report_sections" in failed:
        from codey.reviews.report_sections import REQUIRED_SECTIONS, parse_sections, section_title

        sections = parse_sections(text)
        missing = [section_title(key) for key in REQUIRED_SECTIONS if not sections.get(key, "").strip()]
        parts.append("Repair report formatting using the evidence already saved; this alone does not require another search.")
        if missing:
            parts.append("Each required heading must be on its own line, followed by its content. "
                         "Missing sections: " + ", ".join(missing) + ". Required headings:\n" +
                         "\n".join("## " + section_title(key) for key in REQUIRED_SECTIONS))
        else:
            parts.append("All required sections are present; repair the specific citation/report defect below. "
                         'With report_sections, sources must contain numbered entries such as '
                         '"[1] Title - https://final-url", not bare URLs. Preserve the other sections.')
        if "\\n" in text and "\n" not in text:
            parts.append("The summary contains literal backslash+n text, not line breaks. "
                         "Resend summary with actual paragraph newlines after argument decoding. "
                         "Prefer done.report_sections: put the conclusion in summary, and supply "
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
