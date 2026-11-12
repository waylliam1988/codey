"""Expose submission once an explicit browser Research prerequisite is satisfied."""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from codey.operations.browser_completion import post_wait_passages
from codey.operations.completion_output import tool_facts
from codey.policies.browser_requirement import BrowserResearchSequence, parse_browser_requirement
from codey.policies.tool_requirement import parse_tool_success_check
from codey.research.answer_contract import whole_research
from codey.task.web_intent import source_url_key

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.operations.completion_context import CompletionContext
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_session import TaskSession


SUBMISSION_TOOLS = ("submit_research_answer",)


def submission_sequence(session: TaskSession) -> BrowserResearchSequence | None:
    if not whole_research(session):
        return None
    rules = [rule for check in session.policy.required_checks if (rule := parse_browser_requirement(check)) is not None]
    if (len(rules) == 1 and isinstance(rule := rules[0], BrowserResearchSequence)
            and session.required_source_urls <= {source_url_key(rule.url)}):
        return rule
    return None


def research_submission_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                                browser: BrowserSession | None) -> tuple[TurnSnapshot, str]:
    from codey.operations.kernel_protocol import refresh_snapshot_contract

    scope = snapshot.research_scope
    if browser is None or scope is None or "submit_research_answer" not in snapshot.tool_names:
        return snapshot, ""
    rule = submission_sequence(session)
    if rule is None:
        return snapshot, ""
    records, results = tool_facts(session, context)
    if not rule.receipt_tools <= {row["name"] for row in records.values() if row["ok"]}:
        return snapshot, ""
    document = browser.document_identity()
    if document is None or document.get("url") != rule.url:
        return snapshot, ""
    _, passages = post_wait_passages(rule, records=records, results=results, document=document)
    labels = ", ".join(f"[{label}]" for label, ref in scope.aliases if ref in passages)
    if not labels:
        return snapshot, ""
    for check in session.policy.required_checks:
        requirement = parse_tool_success_check(check)
        if requirement is not None and not requirement.observe(
            records, results=results, browser_document=document,
        ).evaluation.passed:
            return snapshot, ""
    specs = tuple(spec for spec in snapshot.frozen_specs if spec.name in SUBMISSION_TOOLS)
    return refresh_snapshot_contract(replace(snapshot, tool_names=SUBMISSION_TOOLS, nested_tool_names=(), frozen_specs=specs,
                   custom_executors=tuple(row for row in snapshot.custom_executors if row[0] in SUBMISSION_TOOLS))), (
        "The required post-wait page evidence is ready. Use submit_research_answer and cite "
        f"{labels} for the required page."
    )
