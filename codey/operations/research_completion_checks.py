"""Research completion checks for the unified gate (operations layer).

Owns source-open, ledger, and strict-report checks. Never mints a completion
proof and never imports the gate.
"""

from __future__ import annotations

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_NOT_RUN,
    CHECK_PASS,
    CompletionCheck,
    completion_check,
)
from codey.operations.completion_context import CompletionContext
from codey.operations.task_session import TaskSession
from codey.research.finding_report import ResearchReport, compile_finding_report


def prepare_research_completion(session: TaskSession, text: str, context: object) -> tuple[str, ResearchReport | None]:
    """Derive this gate call's report without replacing the original proposal."""
    context = CompletionContext.from_value(context)
    if not session.policy.strict_research or session.task_kind == "evidence_followup":
        return text, None
    ledger = context.research_ledger
    store = context.knowledge_store
    if ledger is None or store is None:
        raise ValueError("strict report preparation requires its research ledger and knowledge store")
    from codey.research.answer_contract import whole_research

    if whole_research(session):
        from codey.research.answer_report import compile_answer_report

        answer = compile_answer_report(session, ledger, store,
            project=str(context.knowledge_project or ""),
            session_id=str(context.knowledge_session_id or ""))
        return answer.text, answer
    try:
        report = compile_finding_report(session.last_done_args, ledger, store,
            project=str(context.knowledge_project or ""),
            session_id=str(context.knowledge_session_id or ""), task=session.task_text)
    except ValueError as exc:
        from codey.research.issues import ResearchIssue

        if isinstance(exc, ResearchIssue):
            raise
        raise ValueError(f"Report reference fields: {exc}") from exc
    return report.text, report


def knowledge_reference_checks(session: TaskSession, context: object = None) -> list[CompletionCheck]:
    context = CompletionContext.from_value(context)
    raw = session.last_done_args.get("knowledge_refs")
    if not raw:
        return []
    from codey.knowledge.answer_refs import answer_references

    try:
        answer_references(raw, context.knowledge_store,
                          project=str(context.knowledge_project or ""),
                          session_id=str(context.knowledge_session_id or ""))
    except (ValueError, TypeError, KeyError):
        return [CompletionCheck("knowledge_versions", CHECK_FAIL, "knowledge_reference_invalid")]
    # A locator/version check cannot prove an interpretation or applicability.
    return [CompletionCheck("knowledge_versions", CHECK_PASS)]


def source_requirement_checks(session: TaskSession, context: object = None) -> list[CompletionCheck]:
    """普通联网任务的来源要求：只检查任务要求的来源是否实际打开。"""
    context = CompletionContext.from_value(context)
    policy = session.policy
    if not policy.sources_open_required:
        return []
    ledger = context.research_execution_ledger or context.research_ledger
    if ledger is None and not session.required_source_urls:
        return [CompletionCheck("research_sources_opened", CHECK_FAIL, "research_ledger_missing")]
    from codey.operations.source_gaps import task_source_coverage

    coverage = task_source_coverage(session, ledger)
    row = completion_check(
        "research_sources_opened",
        CHECK_PASS if coverage.satisfied else CHECK_NOT_RUN,
        "" if coverage.satisfied else "research_source_not_opened",
    )
    return [row] if row is not None else []


def investigation_attempt_checks(session: TaskSession) -> list[CompletionCheck]:
    """An evidence-only phase must try acquisition, not recite its parent.

    Failure receipts count: an unavailable source can legitimately leave a
    gap. This is an execution check, never a claim of semantic coverage.
    """
    if (session.task_kind != "evidence_followup"
            or session.research_acquisition_limits is None):
        return []
    acquisition = {"web_search", "open_url", "open_result", "open_hit", "reopen_source", "source_search"}
    attempted = any(result.call.name in acquisition
                    or (result.call.name in {"knowledge_write", "save_finding"} and result.ok)
                    for result in session._memory_results.values())
    return [CompletionCheck("research_investigation_attempted", CHECK_PASS if attempted else CHECK_NOT_RUN,
                            "" if attempted else "research_investigation_not_attempted")]


def citation_requirement_checks(session: TaskSession, text: str, context: object, *, report: ResearchReport | None = None) -> list[CompletionCheck]:
    context = CompletionContext.from_value(context)
    policy = session.policy
    required = "research_citations" in policy.required_checks
    strict = policy.strict_research
    if not (required or strict or policy.sources_open_required):
        return []
    if report is not None:
        # Sources/citations were resolved from exact selected versions before rendering.
        return [CompletionCheck("research_citations", CHECK_PASS)]
    ledger = context.research_ledger
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


def strict_research_checks(session: TaskSession, done_text: str, context: object = None, *,
                           report: ResearchReport | None = None) -> list[CompletionCheck]:
    """严格 Research 专属：证据归档、报告与研究质量（普通任务不调用）。

    Strict Research always requires a valid evidence ledger. A missing
    ledger blocks instead of falling back to weak session/text checks.
    """
    context = CompletionContext.from_value(context)
    policy = session.policy
    if not policy.strict_research:
        return []
    if context.research_ledger is None:
        return _missing_ledger_rows()
    return _ledger_checks(session, done_text, report)


def research_completion_feedback(session: TaskSession, text: str, context: object, checks: list[CompletionCheck]) -> str:
    """Explain failed report checks without rewriting the proposed answer."""
    failed = {row.check_id for row in checks if row.status != CHECK_PASS}
    if "research_sources_opened" in failed and session.task_kind == "chat" and not session.policy.strict_research:
        receipts = [ref for ref, result in session._memory_results.items()
                    if not result.ok and "source_failure" in result.canonical]
        if receipts:
            return ("A requested source was attempted but unavailable. Do not repeat the same failing fetch just to finish. "
                    "To report this limitation honestly, submit done with your summary and source_gaps "
                    "[{result_ref: <actual failed receipt>, reason: <observed failure>}]. "
                    "This closes as blocked, not verified. Available failed acquisition receipts: " + ", ".join(receipts))
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


def _ledger_checks(session: TaskSession, done_text: str, compiled: ResearchReport | None) -> list[CompletionCheck]:
    from codey.research.answer_report import DELIVERY_CHECKS, AnswerReport

    if isinstance(compiled, AnswerReport):
        AnswerReport.from_payload(compiled.to_payload())
        if done_text != compiled.text:
            return [CompletionCheck("research_report_sections", CHECK_FAIL, "answer_text_changed")]
        return [CompletionCheck(check, CHECK_PASS, "whole_answer_delivery_not_semantic_verification")
                for check in DELIVERY_CHECKS]
    if compiled is None or compiled.text != done_text:
        return [CompletionCheck("research_report_sections", CHECK_FAIL, "structured_report_missing")]
    # These checks confirm structure and exact evidence chains, not semantics.
    # Historical evidence does not become a fresh source-open execution receipt.
    rows = [CompletionCheck(check, CHECK_PASS) for check in (
        "research_sources_opened", "research_evidence_saved", "research_report_sections",
        "research_claim_bindings", "research_question_coverage")]
    from codey.research.task_requirements import validate_task_accounting

    try:
        validate_task_accounting(list(compiled.coverage), session.task_text)
    except ValueError:
        rows.append(CompletionCheck("research_task_accounting", CHECK_FAIL, "task_accounting_missing"))
    else:
        rows.append(CompletionCheck("research_task_accounting", CHECK_PASS, "model_reported_not_semantically_verified"))
    return rows



__all__ = ["source_requirement_checks", "strict_research_checks"]
