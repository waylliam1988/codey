"""Expose submission once the declared browser Research evidence is ready."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from codey.operations.browser_completion import (
    PAGE_EVIDENCE_INVALIDATORS,
    PAGE_EVIDENCE_TOOLS,
    ordered_page_passages,
    post_wait_passages,
    wait_matched_on_page,
)
from codey.operations.completion_output import tool_facts
from codey.policies.browser_requirement import BrowserResearchSequence, parse_browser_requirement
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, STRICT_RESEARCH_REQUIRED_CHECKS
from codey.policies.tool_requirement import parse_tool_success_check
from codey.research.answer_contract import whole_research
from codey.task.web_intent import source_url_key

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.operations.completion_context import CompletionContext
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.execution_record import ExecutionRecord
    from codey.runtime.core.models import ToolResult
    from codey.types.json_value import JsonObject


SUBMISSION_TOOLS = ("submit_research_answer",)
_DOCUMENT_TOOLS = PAGE_EVIDENCE_TOOLS | PAGE_EVIDENCE_INVALIDATORS | {"browser_snapshot", "browser_tab_list"}


def submission_sequence(session: TaskSession) -> BrowserResearchSequence | None:
    if not whole_research(session):
        return None
    rules = [rule for check in session.policy.required_checks if (rule := parse_browser_requirement(check)) is not None]
    if (len(rules) == 1 and isinstance(rule := rules[0], BrowserResearchSequence)
            and session.required_source_urls <= {source_url_key(rule.url)}):
        return rule
    return None


def declared_workflow_url(session: TaskSession) -> str:
    """Recognize a named source and distinct declared wait/read stages."""
    if not whole_research(session) or len(session.required_source_urls) != 1:
        return ""
    tools: set[str] = set()
    evidence_steps: set[tuple[str, int]] = set()
    for check in session.policy.required_checks:
        if check in RESEARCH_ANSWER_REQUIRED_CHECKS or check in STRICT_RESEARCH_REQUIRED_CHECKS:
            continue
        rule = parse_tool_success_check(check)
        if rule is None or rule.tool not in _DOCUMENT_TOOLS or rule.answer_pointer is not None:
            return ""
        if rule.tool in PAGE_EVIDENCE_TOOLS:
            step = (rule.tool, rule.stage)
            if step in evidence_steps:
                return ""
            evidence_steps.add(step)
        tools.add(rule.tool)
    return next(iter(session.required_source_urls)) if tools >= PAGE_EVIDENCE_TOOLS else ""


def traces_workflow_document(session: TaskSession, name: str) -> bool:
    return name in _DOCUMENT_TOOLS and bool(declared_workflow_url(session))


def _current_document_records(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                              document: JsonObject | None) -> dict[str, ExecutionRecord]:
    # Keep parent receipts for via=codemode checks, but require page tools to
    # have run on this document. An earlier load of the same URL cannot count.
    return {ref: row for ref, row in records.items() if row["name"] not in _DOCUMENT_TOOLS
               or ((result := results.get(ref)) is not None and result.audit.get("browser_document") == document)}


def _workflow_passages(session: TaskSession, context: CompletionContext, document: JsonObject, url: str) -> tuple[set[str], str]:
    records, results = tool_facts(session, context)
    current = _current_document_records(records, results, document)
    witnesses: dict[str, frozenset[str]] = {}
    for check in session.policy.required_checks:
        rule = parse_tool_success_check(check)
        if rule is None:
            continue
        evaluation = rule.observe(current, results=results, browser_document=document).evaluation
        if not evaluation.passed:
            return set(), evaluation.feedback
        if rule.tool in PAGE_EVIDENCE_TOOLS:
            witnesses[rule.tool] = witnesses.get(rule.tool, frozenset()) | frozenset(
                ref for ref in evaluation.evidence_refs if current[ref]["name"] == rule.tool)
    wait, passages = ordered_page_passages(url, records=current, results=results, document=document,
        wait_matches=lambda ref, result: ref in witnesses["browser_wait"] and wait_matched_on_page(result, url),
        read_refs=witnesses["browser_read"], invalidated_by=PAGE_EVIDENCE_INVALIDATORS)
    if not wait:
        return set(), "Complete the declared browser_wait on the required page after the page actions."
    if not passages:
        return set(), "The required wait succeeded. Use browser_read on the same document to obtain complete text."
    return set(passages), ""


def research_submission_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                                browser: BrowserSession | None) -> tuple[TurnSnapshot, str]:
    scope = snapshot.research_scope
    if browser is None or scope is None or "submit_research_answer" not in snapshot.tool_names:
        return snapshot, ""
    rule = submission_sequence(session)
    url = rule.url if rule is not None else declared_workflow_url(session)
    if not url:
        if scope.fields and all(not field.feedback and field.value is not None for field in scope.fields):
            unique = {f"[{label}]": None for field in scope.fields for label in field.labels}
            labels = ", ".join(unique)
            return snapshot, ("All explicitly declared report fields have formal evidence. "
                f"Use the frozen field slots and their original citations {labels} for those fields. "
                "Continue checking other requirements in the original question. "
                "No new click or source read is needed to repair these fields in the answer.")
        return snapshot, ""
    document = browser.document_identity()
    if document is None or not isinstance(page_url := document.get("url"), str) or (
        page_url != url if rule is not None else source_url_key(page_url) != url
    ):
        return snapshot, f"Use browser_navigate to open the required page {url}." if rule is None else ""
    if rule is None:
        passages, guidance = _workflow_passages(session, context, document, page_url)
        if guidance:
            return snapshot, guidance
    else:
        records, results = tool_facts(session, context)
        _, ordered = post_wait_passages(rule, records=records, results=results, document=document)
        passages = set(ordered)
        for check in session.policy.required_checks:
            requirement = parse_tool_success_check(check)
            if requirement is not None and not requirement.observe(
                records, results=results, browser_document=document,
            ).evaluation.passed:
                return snapshot, ""
    labels = ", ".join(f"[{label}]" for label, ref in scope.aliases if ref in passages)
    if not labels:
        return snapshot, ""
    if rule is None:
        return snapshot, ("The declared wait/read obligations have formal evidence. "
            f"Use {labels} for the required page. Continue checking other requirements in the original question; "
            "the tool checklist does not declare the whole investigation complete.")
    return snapshot, ("The required post-wait page evidence is ready. Use submit_research_answer and cite "
                      f"{labels} for the required page.")


def research_browser_handoff(session: TaskSession, snapshot: TurnSnapshot,
                              context: CompletionContext) -> tuple[TurnSnapshot, str]:
    """Reuse fixed browser leaf lowering inside an explicit Research workflow."""
    from codey.operations.kernel_protocol import offer_browser_call

    sequence = submission_sequence(session)
    if snapshot.research_scope is None or not (sequence or declared_workflow_url(session)):
        return snapshot, ""
    names = {"browser_navigate", "browser_wait", "browser_read"} if sequence else set()
    for check in session.policy.required_checks:
        requirement = parse_tool_success_check(check)
        if requirement is not None:
            names.add(requirement.tool)
    for name in sorted(names):
        snapshot = offer_browser_call(snapshot, session, name, research=True)
    from codey.operations.browser_task_handoff import browser_continuation_handoff

    snapshot, continuation = browser_continuation_handoff(session, snapshot, context, workflow=True)
    if continuation:
        return snapshot, continuation
    return snapshot, ("For declared browser steps, use the offered direct browser tools. "
                      "They execute fixed programs under this Research scope. Supply question_ref when the tool requires it. "
                      "Submit one direct browser action per turn. "
                      "For a query result, use browser_click purpose=result when offered to execute its declared waits and read. "
                      "Use the declared page condition for browser_wait, then browser_read, and submit with citations.")
