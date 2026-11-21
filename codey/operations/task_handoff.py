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


def report_completes_task(session: TaskSession) -> bool:
    """Only report-only work can propose completion directly from submission."""
    from codey.operations.project_completion_checks import _task_requires_modification

    return (not _task_requires_modification(session)
            and not session.edited_files)


def task_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                 browser: BrowserSession | None, *,
                 delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
    from codey.operations.browser_detail import offer_page_details, offer_query_controls
    from codey.operations.browser_task_handoff import bind_browser_references, browser_task_handoff
    from codey.operations.kernel_protocol import offer_browser_call, offer_browser_tools
    from codey.operations.research_submission_handoff import research_browser_handoff, research_submission_handoff

    generation = browser.generation if browser is not None else None
    context = replace(context, browser_document=browser.document_identity() if browser is not None else None)
    snapshot = offer_browser_tools(snapshot, session)
    snapshot = offer_browser_call(snapshot, session, "browser_navigate")
    snapshot = bind_browser_references(session, snapshot, context, generation)
    from codey.operations.browser_followups import DIRECT_TOOLS

    hints: list[str] = []
    from codey.codemode.verification import project_check_call

    run = next((spec for spec in snapshot.model_specs if spec.name == "run"), None)
    check = project_check_call(run) if run is not None else None
    if check is not None:
        hints.append("The existing project check can be selected without copying its command or working directory: "
            + canonical_json(check) + ". Follow the user's workflow when deciding whether and when to run it. "
            "Reuse completed receipts instead of rerunning a check merely to retrieve its output.")
    if context.browser_document is None:
        from codey.task.web_intent import explicit_web_intent

        deferred = explicit_web_intent(session.task_text).deferred_urls
        if deferred:
            hints.append("The user makes source access conditional: " + ", ".join(sorted(deferred)) + ". "
                "Complete the user's stated prerequisite first using its actual receipts, "
                "then decide whether source access is needed. "
                "The outstanding source requirements below describe later evidence obligations; "
                "they are not an instruction to navigate before that prerequisite.")
    if snapshot.browser_followups and set(DIRECT_TOOLS) <= set(snapshot.tool_names):
        hints.append("For browser steps, prefer the offered direct browser tools. "
                     "Use CodeMode for operations that need programming or composition.")
    snapshot, hint = browser_task_handoff(session, snapshot, context, generation,
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
    return snapshot, "\n\n".join(dict.fromkeys(hint for hint in hints if hint))
