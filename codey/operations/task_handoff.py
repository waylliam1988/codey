"""Compose task handoffs without turning an activity into an authorization mode."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.knowledge.model import canonical_json

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.operations.completion_context import CompletionContext
    from codey.operations.kernel_protocol import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult
    from codey.task.web_intent import WebIntent


_PROJECT_DIRECT = frozenset({
    "list_dir", "read_file", "read_files", "grep", "find_references", "edit", "run",
    "task_output", "task_input", "task_signal", "task_resize", "task_cancel",
})
_SOURCE_DIRECT = frozenset({"open_url", "web_search", "source_search"})


def _select_outer_tools(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                        intent: WebIntent, *, ready_for_page: bool) -> TurnSnapshot:
    from codey.operations.kernel_protocol import offer_browser_call, offer_browser_tools
    from codey.policies.browser_requirement import parse_browser_requirement
    from codey.policies.tool_requirement import parse_tool_success_check

    required = tuple(rule.tool for check in session.policy.required_checks
                     if (rule := parse_tool_success_check(check)) is not None)
    browser_steps = (context.browser_document is not None or intent.browser_navigation_requested
                     or any(name.startswith("browser_") for name in required)
                     or any(parse_browser_requirement(check) is not None for check in session.policy.required_checks))
    if (session.project and not browser_steps and not session.policy.strict_research
            and session.model_tool_names is None and snapshot.research_scope is None and snapshot.input_binding is None
            and snapshot.browser_copy is None and {"codemode", "edit"} <= set(snapshot.tool_names)):
        preferred = _PROJECT_DIRECT | set(required)
        if intent.requested or snapshot.task_source_url:
            preferred |= _SOURCE_DIRECT
        names = tuple(name for name in snapshot.tool_names
                      if name in preferred or name not in snapshot.nested_tool_names)
        snapshot = replace(snapshot, tool_names=names)
    if browser_steps:
        snapshot = offer_browser_tools(snapshot, session)
    if browser_steps or ready_for_page:
        snapshot = offer_browser_call(snapshot, session, "browser_navigate")
    return snapshot


def report_completes_task(session: TaskSession) -> bool:
    """Only report-only work can propose completion directly from submission."""
    from codey.operations.project_completion_checks import _task_requires_modification
    from codey.operations.project_delivery import PAGE_AFTER_EDIT

    return (not _task_requires_modification(session)
            and PAGE_AFTER_EDIT not in session.policy.required_checks
            and not session.edited_files)


def task_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                 browser: BrowserSession | None, *,
                 delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
    from codey.completion.verification_policy import VerificationPlan
    from codey.operations.browser_detail import offer_page_details, offer_query_controls
    from codey.operations.browser_task_handoff import (
        bind_browser_references,
        browser_control_wait_handoff,
        browser_observation_handoff,
        browser_step,
        browser_task_handoff_for_step,
        handoff_call,
    )
    from codey.operations.kernel_protocol import refresh_snapshot_contract
    from codey.operations.project_completion_checks import (
        project_evidence_checks,
        resolve_verification_requirement,
        verification_plan_feedback,
    )
    from codey.operations.research_submission_handoff import research_browser_handoff, research_submission_handoff
    from codey.runtime.core.models import ToolCall
    from codey.task.web_intent import explicit_web_intent

    generation = browser.generation if browser is not None else None
    context = replace(context, browser_document=browser.document_identity() if browser is not None else None)
    from codey.operations.project_delivery import observed_delivery_links

    intent = explicit_web_intent(session.task_text)
    links = observed_delivery_links(session)
    if (links.specification and session.policy.allows("web.read") and "open_url" in snapshot.inventory_names
            and (snapshot.input_binding is None or snapshot.input_binding.kind == "declared_fields")
            and snapshot.browser_copy is None):
        snapshot = replace(snapshot, task_source_url=links.specification,
                           task_navigation_url=snapshot.task_navigation_url or links.application or links.specification)
    ready_for_page = bool(links.application and session.edited_files and (session.verification_forbidden or any(
        row.check_id == "relevant_verification" and row.status in {"pass", "not_applicable"}
        for row in project_evidence_checks(session, context))))
    if ready_for_page and "browser_navigate" in snapshot.nested_tool_names and snapshot.browser_copy is None:
        snapshot = replace(snapshot, task_navigation_url=links.application)
    snapshot = _select_outer_tools(session, snapshot, context, intent, ready_for_page=ready_for_page)
    if (ready_for_page and snapshot.task_navigation_url == links.application and snapshot.navigation_observes
            and "browser_navigate" in snapshot.tool_names and "browser_read" in snapshot.nested_tool_names):
        version = snapshot.input_contract_version if snapshot.input_contract_version >= 19 else 19
        snapshot = replace(snapshot, input_contract_version=version, navigation_verifies=True)
    snapshot = bind_browser_references(session, snapshot, context, generation)
    from codey.operations.browser_followups import DIRECT_TOOLS

    hints: list[str] = []
    from codey.operations.browser_result_evidence import action_summary
    from codey.operations.project_delivery import live_record_subject_call, project_delivery_context

    if delivery := project_delivery_context(session, context.browser_document, links):
        hints.append("User-requested delivery and current evidence:\n" + canonical_json(delivery))
    if snapshot.task_source_url == links.specification and delivery.get("record_specification") == {"state": "unread"}:
        hints.append("The observed project documentation names a record specification. "
                     "Use open_url with target=task to read that specification.")
    if (ready_for_page
            and delivery.get("updated_application_page") == {"state": "read the updated application page after the final edit"}):
        if context.browser_document is not None and context.browser_document.get("url") == links.application:
            snapshot, read, _ = handoff_call(session, snapshot, ToolCall("browser_read", {}))
            if read is not None:
                hints.append("The updated application page is already open. Read its current contents: " + canonical_json(read))
        else:
            hints.append("The observed project documentation names the application page. "
                         "For verification after edits, navigate with target=task and read the opened application page.")
    elif links.application and session.edited_files and not ready_for_page:
        hints.append("Finish the requested project changes and get current project checks before verifying the application page.")
        scope = tuple(dict.fromkeys((*context.scope_files, *session.verification_paths)))
        plan = resolve_verification_requirement(session, context, scope)
        if isinstance(plan, VerificationPlan):
            hints.append(verification_plan_feedback(session, context, plan).strip())
    if subject := live_record_subject_call(session, snapshot.browser_references, context.browser_document):
        snapshot, call, _ = handoff_call(session, snapshot, subject)
        if call is not None:
            hints.append("The current page has one eligible input for the user's record subject. "
                         "Fill that input before requesting the result: " + canonical_json(call))
    actions, _refs = action_summary(session.executed, session._memory_results)
    if actions:
        hints.append("Authoritative execution summary derived from committed receipts; reuse these counts and order:\n" + actions)
    hints.append(browser_observation_handoff(session, snapshot, context, generation))
    next_step = browser_step(session, snapshot, context)
    snapshot, control_wait = browser_control_wait_handoff(session, snapshot, context, generation, next_step=next_step)
    hints.append(control_wait)
    from codey.codemode.verification import project_check_call

    run = next((spec for spec in snapshot.model_specs if spec.name == "run"), None)
    check = project_check_call(run) if run is not None else None
    if check is not None:
        hints.append("The existing project check can be selected without copying its command or working directory: "
            + canonical_json(check) + ". Follow the user's workflow when deciding whether and when to run it. "
            "Reuse completed receipts instead of rerunning a check merely to retrieve its output.")
    if context.browser_document is None:
        deferred = intent.deferred_urls
        if deferred:
            hints.append("The user makes source access conditional: " + ", ".join(sorted(deferred)) + ". "
                "Complete the user's stated prerequisite first using its actual receipts, "
                "then decide whether source access is needed. "
                "The outstanding source requirements below describe later evidence obligations; "
                "they are not an instruction to navigate before that prerequisite.")
    if snapshot.browser_followups and set(DIRECT_TOOLS) <= set(snapshot.tool_names):
        hints.append("For browser steps, prefer the offered direct browser tools. "
                     "Use CodeMode for operations that need programming or composition.")
    snapshot, hint = browser_task_handoff_for_step(session, snapshot, context, generation, step=next_step,
                                                  delivered_results=delivered_results)
    hints.append(hint)
    snapshot, hint = research_submission_handoff(session, snapshot, context, browser)
    hints.append(hint)
    snapshot, hint = research_browser_handoff(session, snapshot, context)
    hints.append(hint)
    snapshot, hint = offer_query_controls(session, snapshot, context, browser)
    hints.append(hint)
    snapshot, hint = offer_page_details(session, snapshot, browser)
    hints.append(hint)
    return refresh_snapshot_contract(snapshot), "\n\n".join(dict.fromkeys((hint for hint in hints if hint), ""))
