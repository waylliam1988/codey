"""Parent-task browser ownership; source acquisition borrows the same live page."""
from __future__ import annotations

from codey.automation.browser_session import BrowserSession
from codey.operations.research_resources import bind_browser_session
from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.tools import ResearchTools
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core.tool_resources import ToolResources


def bind_source_browser(tools: ResearchTools, browser: BrowserSession) -> None:
    search = tools.search
    base = search.base_provider if isinstance(search, ConnectorAwareSearchProvider) else search
    if isinstance(base, WebSourceProvider):
        # Source acceptance and its ledger stay with the existing gateway.
        # Only the live browser connection is shared across attempts.
        base.bind_browser_source(browser.provider)
    elif isinstance(base, BrowserSearchProvider) and base is not browser.provider:
        base.close()
        if isinstance(search, ConnectorAwareSearchProvider):
            search.base_provider = browser.provider
        else:
            tools.search = browser.provider
    tools.browser_session = browser


def build_task_browser(owner_task_id: str, resources: ToolResources, tools: ResearchTools | None = None,
                       *, browser: BrowserSession | None = None) -> BrowserSession:
    def create() -> BrowserSession:
        if browser is not None:
            return browser
        if tools is None:
            return BrowserSession(BrowserSearchProvider(headless=False, isolated=False))
        bind_browser_session(tools)
        assert tools.browser_session is not None
        return tools.browser_session

    acquired = resources.acquire_task_browser(owner_task_id, create)
    if tools is not None:
        bind_source_browser(tools, acquired)
    return acquired
