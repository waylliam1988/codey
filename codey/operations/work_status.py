"""Ephemeral required-work projection shared by handoff and completion."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from codey.completion.contract import CHECK_NOT_APPLICABLE, CHECK_PASS, CompletionCheck
from codey.operations.browser_completion import evaluate_page_rounds, research_page_evidence
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_output import tool_facts
from codey.operations.explicit_execution_requirements import requested_output_read_check
from codey.operations.file_field_binding import validate_field
from codey.operations.project_completion_checks import collect_completion_workspace, project_evidence_checks
from codey.operations.research_completion_checks import source_requirement_checks
from codey.operations.task_session import TaskSession
from codey.policies.browser_requirement import BrowserPageRounds, parse_browser_requirement
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import ToolObservation, ToolSuccessRequirement, parse_tool_success_check
from codey.toolchain.runtime import WRITE_MAX_FILE_BYTES, safe_join
from codey.workspace.paths import read_text_bounded


def tool_work_outcome(session: TaskSession, rule: ToolSuccessRequirement, observation: ToolObservation,
                      context: CompletionContext) -> RequirementEvaluation:
    if not rule.file_path:
        return observation.evaluation
    try:
        root = session.project or context.project
        if not root:
            raise ValueError("File binding requires a project")
        content = read_text_bounded(safe_join(Path(root), rule.file_path), max_bytes=WRITE_MAX_FILE_BYTES)
        return validate_field(rule, observation, content)
    except (OSError, ValueError) as exc:
        return RequirementEvaluation("file_field_unavailable", str(exc), observation.evaluation.evidence_refs)


@dataclass(frozen=True)
class ToolWork:
    rule: ToolSuccessRequirement
    observation: ToolObservation
    outcome: RequirementEvaluation


@dataclass(frozen=True)
class WorkStatus:
    tools: tuple[ToolWork, ...]
    browser: tuple[RequirementEvaluation, ...]
    project: tuple[CompletionCheck, ...]
    other: tuple[CompletionCheck, ...]
    before_edit: tuple[CompletionCheck, ...] = ()

    @property
    def ready(self) -> bool:
        return (all(row.outcome.passed for row in self.tools) and all(row.passed for row in self.browser)
                and all(row.status in {CHECK_PASS, CHECK_NOT_APPLICABLE} for row in (*self.project, *self.other)))


def work_status(session: TaskSession, context: CompletionContext, *,
                observations: Mapping[str, ToolObservation] | None = None) -> WorkStatus:
    records, results = tool_facts(session, context)
    tools: list[ToolWork] = []
    browser: list[RequirementEvaluation] = []
    for check in session.policy.required_checks:
        if (rule := parse_tool_success_check(check)) is not None:
            observed = (rule.observe(records, results=results, browser_document=context.browser_document)
                        if observations is None else observations[rule.check_id])
            tools.append(ToolWork(rule, observed, tool_work_outcome(session, rule, observed, context)))
        elif (page := parse_browser_requirement(check)) is not None:
            outcome = (evaluate_page_rounds(page, records=records, results=results) if isinstance(page, BrowserPageRounds)
                       else research_page_evidence(page, records=records, results=results,
                                                   document=context.browser_document)[0])
            browser.append(outcome)
    collect_completion_workspace(session, context)
    other = source_requirement_checks(session, context)
    before_edit = list(other)
    if (requested := requested_output_read_check(session, context.task or session.task_text)) is not None:
        other.append(requested)
        before_edit.append(requested)
    return WorkStatus(tuple(tools), tuple(browser), tuple(project_evidence_checks(session, context)),
                      tuple(other), tuple(before_edit))
