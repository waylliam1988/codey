"""Expose submission once the declared browser Research evidence is ready."""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from codey.operations.browser_completion import (
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
    from codey.types.json_value import JsonObject


SUBMISSION_TOOLS = ("submit_research_answer",)
_EVIDENCE_MUTATIONS = frozenset({"browser_fill", "browser_click", "browser_eval", "browser_scroll",
                                 "browser_upload", "browser_download"})
_DOCUMENT_TOOLS = PAGE_EVIDENCE_TOOLS | _EVIDENCE_MUTATIONS | {"browser_navigate", "browser_snapshot", "browser_tab_list"}


def submission_sequence(session: TaskSession) -> BrowserResearchSequence | None:
    if not whole_research(session):
        return None
    rules = [rule for check in session.policy.required_checks if (rule := parse_browser_requirement(check)) is not None]
    if (len(rules) == 1 and isinstance(rule := rules[0], BrowserResearchSequence)
            and session.required_source_urls <= {source_url_key(rule.url)}):
        return rule
    return None


def declared_workflow_url(session: TaskSession) -> str:
    """Recognize a single named source and an explicit wait/read tool workflow."""
    if not whole_research(session) or len(session.required_source_urls) != 1:
        return ""
    tools: set[str] = set()
    for check in session.policy.required_checks:
        if check in RESEARCH_ANSWER_REQUIRED_CHECKS or check in STRICT_RESEARCH_REQUIRED_CHECKS:
            continue
        rule = parse_tool_success_check(check)
        if rule is None or rule.tool not in _DOCUMENT_TOOLS or rule.answer_pointer is not None:
            return ""
        if rule.tool in PAGE_EVIDENCE_TOOLS and rule.tool in tools:
            return ""
        tools.add(rule.tool)
    return next(iter(session.required_source_urls)) if tools >= PAGE_EVIDENCE_TOOLS else ""


def traces_workflow_document(session: TaskSession, name: str) -> bool:
    return name in _DOCUMENT_TOOLS and bool(declared_workflow_url(session))


def _workflow_passages(session: TaskSession, context: CompletionContext, document: JsonObject, url: str) -> tuple[set[str], str]:
    records, results = tool_facts(session, context)
    # Keep parent receipts for via=codemode checks, but require page tools to
    # have run on this document. An earlier load of the same URL cannot count.
    current = {ref: row for ref, row in records.items() if row["name"] not in _DOCUMENT_TOOLS
               or ((result := results.get(ref)) is not None and result.audit.get("browser_document") == document)}
    witnesses: dict[str, frozenset[str]] = {}
    for check in session.policy.required_checks:
        rule = parse_tool_success_check(check)
        if rule is None:
            continue
        evaluation = rule.observe(current, results=results, browser_document=document).evaluation
        if not evaluation.passed:
            return set(), evaluation.feedback
        if rule.tool in PAGE_EVIDENCE_TOOLS:
            witnesses[rule.tool] = frozenset(ref for ref in evaluation.evidence_refs if current[ref]["name"] == rule.tool)
    wait, passages = ordered_page_passages(url, records=current, results=results, document=document,
        wait_matches=lambda ref, result: ref in witnesses["browser_wait"] and wait_matched_on_page(result, url),
        read_refs=witnesses["browser_read"], invalidated_by=_EVIDENCE_MUTATIONS)
    if not wait:
        return set(), "Complete the declared browser_wait on the required page after the page actions."
    if not passages:
        return set(), "The required wait succeeded. Use browser_read on the same document to obtain complete text."
    return set(passages), ""


def research_submission_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                                browser: BrowserSession | None) -> tuple[TurnSnapshot, str]:
    from codey.operations.kernel_protocol import refresh_snapshot_contract

    scope = snapshot.research_scope
    if browser is None or scope is None or "submit_research_answer" not in snapshot.tool_names:
        return snapshot, ""
    rule = submission_sequence(session)
    url = rule.url if rule is not None else declared_workflow_url(session)
    if not url:
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
    specs = tuple(spec for spec in snapshot.frozen_specs if spec.name in SUBMISSION_TOOLS)
    return refresh_snapshot_contract(replace(snapshot, tool_names=SUBMISSION_TOOLS, nested_tool_names=(), frozen_specs=specs,
                   custom_executors=tuple(row for row in snapshot.custom_executors if row[0] in SUBMISSION_TOOLS))), (
        "The required post-wait page evidence is ready. Use submit_research_answer and cite "
        f"{labels} for the required page."
    )
