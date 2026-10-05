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


def source_requirement_checks(session: Any, context: Any = None) -> list[CompletionCheck]:
    """普通联网任务的来源要求：只检查任务要求的来源是否实际打开。"""
    policy = getattr(session, "policy", None)
    if not bool(getattr(policy, "sources_open_required", False)):
        return []
    if context is not None:
        single = _ledger_source_only(session, context)
        if single is not None:
            return single
    opened = set(getattr(session, "opened_sources", set()) or set())
    row = completion_check(
        "research_sources_opened",
        CHECK_PASS if opened else CHECK_NOT_RUN,
        "" if opened else "research_source_not_opened",
    )
    return [row] if row is not None else []


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


def _ledger_source_only(session: Any, context: Any) -> list[CompletionCheck] | None:
    def get(key: str) -> Any:
        return _context_get(context, key)
    ledger = get("research_ledger")
    if ledger is None:
        return None
    try:
        finals = set(ledger.final_url_set())
    except Exception as exc:
        row = completion_check("research_ledger", CHECK_FAIL, f"ledger_error:{type(exc).__name__}")
        return [row] if row is not None else []
    row = completion_check(
        "research_sources_opened",
        CHECK_PASS if finals else CHECK_NOT_RUN,
        "" if finals else "research_source_not_opened",
    )
    return [row] if row is not None else []


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
            str(done_text or ""), ledger,
            source_ids=dict(get("source_ids") or {}),
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
    elif "结论" in str(done_text or "") and "来源" in str(done_text or ""):
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
                str(done_text or ""),
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
                question=str(get("question") or ""), summary=str(done_text or ""),
                ledger=ledger, review=report_review,
                run_id=str(get("run_id") or ""), project=get("project"),
                stop_reason="done",
            )
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
        try:
            from codey.research.urls import opened_url

            canonical = opened_url(ledger, url)
        except Exception:
            canonical = url
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

    finalized = finalize_done_answer(text, ledger, source_ids=dict(get("source_ids") or {}),
                                    question=str(get("question") or ""), enforce_claim_support=True)
    return finalized.text.strip()


__all__ = [
    "finalize_research_text",
    "source_requirement_checks",
    "strict_research_checks",
]
