"""Choose host acquisitions before a provider turn and after saved evidence."""
from __future__ import annotations

from typing import TYPE_CHECKING

from codey.runtime.core.models import ToolCall
from codey.types.json_value import decode_json_object

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.operations.kernel.turn_contract import TurnSnapshot
    from codey.operations.task_session import TaskSession
    from codey.research.ledger import ResearchLedger
    from codey.research.tools import ResearchTools


def initial_source_calls(session: TaskSession, snapshot: TurnSnapshot, ledger: ResearchLedger | None,
                         browser: BrowserSession | None) -> list[ToolCall]:
    from codey.operations.browser.browser_detail import initial_query_access
    from codey.operations.browser.browser_task_handoff import initial_navigation
    from codey.operations.browser.browser_upload import upload_scope
    from codey.operations.source_gaps import task_source_coverage
    from codey.policies.tool_requirement import parse_tool_success_check
    from codey.task.web_intent import explicit_web_intent, source_url_key

    coverage = task_source_coverage(session, ledger)
    attempted = {source_url_key(url) for result in session._memory_results.values()
                 if result.call.name == "open_url" and isinstance(url := result.call.args.get("url"), str)}
    deferred = explicit_web_intent(session.task_text).deferred_urls
    urls: list[str] = sorted(set(coverage.missing) - attempted - deferred) if "open_url" in snapshot.inventory_names else []
    upload = upload_scope(session)
    navigation = initial_navigation(session, snapshot)
    # A requested browser navigation owns initial access to its page.
    # Dedicated input bindings and explicit open_url requirements retain
    # their existing initial source observations.
    requires_open = snapshot.input_binding is not None or any(
        (rule := parse_tool_success_check(check)) is not None and rule.tool == "open_url"
        for check in session.policy.required_checks)
    navigation_url = navigation.args.get("url") if navigation is not None else None
    if navigation is not None and navigation.name == "codemode":
        navigation_url = decode_json_object(navigation.args["inputs"]).get("url")
    if isinstance(navigation_url, str) and source_url_key(navigation_url) in deferred:
        navigation = None
    calls = [ToolCall("open_url", {"url": url}, call_id=f"required-source:{index}")
             for index, url in enumerate(urls) if requires_open or not isinstance(navigation_url, str)
             or source_url_key(url) != source_url_key(navigation_url)]
    if navigation is not None:
        calls.append(navigation)
    elif upload is not None and upload.navigate and upload.url in urls and "browser_navigate" in snapshot.inventory_names:
        calls.append(ToolCall("browser_navigate", {"url": upload.url}, call_id="required-source:upload-page"))
    if calls:
        calls[-1] = initial_query_access(session, snapshot, browser, calls[-1])
    return calls


def conflict_followup(session: TaskSession, resources: ResearchTools | None, snapshot: TurnSnapshot,
                      calls: list[ToolCall], turn: int) -> ToolCall | None:
    """Schedule host-selected acquisition only while the research surface is open."""
    if (resources is None or turn >= session.max_turns - 1 or "web_search" not in snapshot.tool_names
            or not session.policy.strict_research
            or not calls
            or not all(call.name in {"save_finding", "save_inference", "open_url", "open_result", "source_search",
                                     "submit_research_answer"}
                       for call in calls)):
        return None
    from codey.research.evidence_gaps import conflict_search
    from codey.research.questions import requires_question_ref

    limits = session.research_acquisition_limits
    used = sum(value == "search" for value in session.research_acquisition_slots.values())
    if (snapshot.research_scope is not None and snapshot.research_scope.primary_passages is not None
            and any(call.name == "submit_research_answer" and call.args.get("state") == "answered" for call in calls)):
        from codey.research.primary_sources import primary_acquisition

        if primary := primary_acquisition(session, resources.ledger,
                missing_primary=not snapshot.research_scope.primary_passages,
                may_search=limits is None or used < limits[0], may_open="open_result" in snapshot.tool_names):
            return primary
    findings = (result.call for ref, result in session._memory_results.items()
                if ref in session.executed and result.ok and result.call.name == "save_finding")
    candidate = conflict_search(resources.ledger, remaining_searches=1 if limits is None else limits[0] - used,
                                findings=findings)
    if candidate is None:
        return None
    args = {"query": candidate["query"]}
    if requires_question_ref(session):
        args["question_ref"] = candidate["question_ref"]
    return ToolCall("web_search", args)
