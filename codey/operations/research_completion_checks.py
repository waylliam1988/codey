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
    ledger = _context_get(context, "research_execution_ledger") or _context_get(context, "research_ledger")
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
                    or (result.call.name in {"knowledge_write", "save_finding"} and result.ok)
                    for result in session._memory_results.values())
    return [CompletionCheck("research_investigation_attempted", CHECK_PASS if attempted else CHECK_NOT_RUN,
                            "" if attempted else "research_investigation_not_attempted")]


def citation_requirement_checks(session: Any, text: str, context: Any) -> list[CompletionCheck]:
    policy = getattr(session, "policy", None)
    required = "research_citations" in getattr(policy, "required_checks", ())
    strict = bool(getattr(policy, "strict_research", False))
    if not (required or strict or getattr(policy, "sources_open_required", False)):
        return []
    compiled = _context_get(context, "research_report")
    if compiled is not None:
        # Sources/citations were resolved from exact selected versions before rendering.
        return [CompletionCheck("research_citations", CHECK_PASS)]
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
    if "research_investigation_attempted" in failed:
        return ("This evidence investigation has not attempted acquisition. Parent findings were saved in an "
                "earlier phase, not here. Next action: use source_search on a previously opened URL or "
                "web_search for the first missing comparison or condition, then inspect the passage and save "
                "any supported new finding. If acquisition fails, report the actual failed attempt and remaining "
                "gap in a brief done.summary; do not invent evidence or repeat the parent report.")
    if "knowledge_versions" in failed:
        return "done.knowledge_refs must use the exact object id and revision actually read; cite original premises, not an old answer."
    if "research_report_sections" in failed:
        return "The strict report requires its validated structured proposal and exact reference compilation."
    return ""


def _ledger_checks(session: Any, done_text: str, context: Any) -> list[CompletionCheck]:
    compiled = _context_get(context, "research_report")
    if compiled is None or compiled.text != done_text:
        return [CompletionCheck("research_report_sections", CHECK_FAIL, "structured_report_missing")]
    # These checks confirm structure and exact evidence chains, not semantics.
    # Historical evidence does not become a fresh source-open execution receipt.
    return [CompletionCheck(check, CHECK_PASS) for check in (
        "research_sources_opened", "research_evidence_saved", "research_report_sections",
        "research_claim_bindings", "research_question_coverage")]


def finalize_research_text(session: Any, text: str, context: Any) -> str:
    # Strict reports are already rendered from the accepted structured proposal.
    # Ordinary text/import finalizers remain available to their own callers.
    return text


__all__ = ["finalize_research_text", "source_requirement_checks", "strict_research_checks"]
