"""Parent-task browser ownership; source acquisition borrows the same live page."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from codey.automation.browser_contracts import BrowserConfig
from codey.automation.browser_session import BrowserSession
from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.tools import ResearchTools
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, decode_json_object

if TYPE_CHECKING:
    from codey.runtime.core.tool_resources import ToolResources


def capture_assistance_binding(browser: BrowserSession | None, results: Mapping[str, ToolResult]) -> JsonObject | None:
    if browser is None or (binding := browser.resource_binding()) is None:
        return None
    observed = next((ref for ref, result in reversed(tuple(results.items()))
                     if result.call.name == "browser_snapshot" and result.ok), "")
    blocked = next((result for result in reversed(tuple(results.values()))
                    if result.call.name.startswith("browser_") and not result.ok), None)
    return {"browser_resource": binding, "observation_ref": observed,
        "blocked_action": {"tool": blocked.call.name, "arguments": blocked.call.args,
                           "observed": decode_json_object(blocked.canonical)} if blocked is not None else None}


def resume_assistance_observation(browser: BrowserSession | None, continuation: object,
                                  results: Mapping[str, ToolResult]) -> str:
    row = decode_json_object(continuation)
    if browser is None or row.get("browser_assistance") is None:
        return ""
    assistance = decode_json_object(row["browser_assistance"])
    previous = assistance["observation_ref"]
    latest = next((ref for ref, result in reversed(tuple(results.items()))
                   if result.call.name == "browser_snapshot" and result.ok), "")
    if latest and latest != previous:
        return ""
    # The live resource or its exact durable binding is already restored by
    # the common resource owner. Reset only transient node/image references.
    browser.discard_references()
    from codey.operations.source_gaps import evaluate_source_coverage

    coverage = evaluate_source_coverage(required_urls=(), results=results)
    pending = [failure.url for failure in coverage.acquisition_failures
               if decode_json_object(results[failure.result_ref].canonical["source_failure"]).get("code") == "login_required"]
    if len(pending) == 1:
        return (f"The user answered the browser assistance question. If they confirmed sign-in, call open_url on {pending[0]} "
                "to acquire a formal passage from the original blocked source. A snapshot or navigation alone does not create a citation. "
                "Reuse previously read evidence. If the user chose to skip this source or it remains blocked, report the actual gap.")
    return ("The user answered the assistance question. Observe the original page again with browser_snapshot "
            "before continuing. The answer does not prove that login or access restrictions are resolved; "
            "check the current page. Earlier control and screenshot references have expired.")


def bind_source_browser(tools: ResearchTools, browser: BrowserSession) -> None:
    search = tools.search
    base = search.base_provider if isinstance(search, ConnectorAwareSearchProvider) else search
    source = TaskBrowserSource(browser)
    if isinstance(base, WebSourceProvider):
        # Source acceptance and its ledger stay with the existing gateway.
        # Only the live browser connection is shared across attempts.
        base.bind_browser_source(source)
    elif isinstance(base, TaskBrowserSource):
        base.browser = browser
    elif isinstance(base, BrowserSearchProvider):
        base.close()
        if isinstance(search, ConnectorAwareSearchProvider):
            search.base_provider = source
        else:
            tools.search = source
    tools.browser_session = browser


def source_browser(tools: ResearchTools, *, owner_task_id: str = "") -> BrowserSession:
    """Translate source configuration at the operations composition boundary."""
    search = tools.search
    base = search.base_provider if isinstance(search, ConnectorAwareSearchProvider) else search
    if isinstance(base, TaskBrowserSource):
        return base.browser
    if isinstance(base, BrowserSearchProvider):
        config = BrowserConfig(browser_path=base.browser_path or "", profile_dir=str(base.profile_dir) if not base.isolated else "",
            cdp_port=base.cdp_port, headless=base.headless, launch=base.launch and not base.require_existing_session,
            isolated=base.isolated and not base.require_existing_session)
    elif isinstance(base, WebSourceProvider) and base.browser_session is not None:
        settings = base.browser_session
        config = BrowserConfig(profile_dir=str(settings.profile_dir) if settings.profile_dir is not None else "",
            cdp_port=settings.cdp_port, headless=False, launch=not settings.require_existing,
            isolated=not bool(settings.cdp_port))
    else:
        config = BrowserConfig(headless=False)
    return BrowserSession(config, owner_task_id=owner_task_id)


def build_task_browser(owner_task_id: str, resources: ToolResources, tools: ResearchTools | None = None,
                       *, browser: BrowserSession | None = None) -> BrowserSession:
    def create() -> BrowserSession:
        selected = browser or (tools.browser_session or source_browser(tools, owner_task_id=owner_task_id)
                              if tools is not None else BrowserSession(owner_task_id=owner_task_id))
        selected.attach_owner(owner_task_id)
        return selected

    acquired = resources.acquire_task_browser(owner_task_id, create)
    if tools is not None:
        bind_source_browser(tools, acquired)
    return acquired
