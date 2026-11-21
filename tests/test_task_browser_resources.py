"""Task leases survive source cleanup and waits; terminal commits release once."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.browser_resources import build_task_browser
from codey.operations.research_resources import close_research_tools
from codey.operations.task_session import TaskSession
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.tools import ResearchTools
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core.tool_resources import ToolResources
from codey.types.json_value import JsonObject
from tests.test_browser_context_delivery import URL
from tests.test_browser_context_delivery import production_browser as production_browser


class LeaseBrowser(BrowserSession):
    def __init__(self) -> None:
        self.retains = 0
        self.releases = 0
        self.closes = 0

    def retain(self) -> None:
        self.retains += 1

    def release(self) -> None:
        self.releases += 1

    def close(self) -> None:
        self.closes += 1


def test_parent_and_wait_leases_have_distinct_lifetimes() -> None:
    resources = ToolResources()
    browser = LeaseBrowser()
    other = LeaseBrowser()
    assert resources.acquire_task_browser("parent", lambda: browser) is browser
    assert resources.acquire_task_browser("parent", lambda: other) is browser
    assert browser.retains == 1 and other.retains == 0
    binding: JsonObject = {"original": "target"}
    resources.bind_browser("wait-child", browser, binding)
    assert resources.browser("wait-child", binding) is browser
    resources.release_browser("wait-child")
    assert browser.retains == 2 and browser.releases == 1 and browser.closes == 0
    assert resources.task_browser("parent") is browser
    resources.close()
    resources.close()
    assert browser.releases == 2 and browser.closes == 1
    with pytest.raises(ValueError, match="unavailable"):
        resources.task_browser("parent")


def test_durable_wait_keeps_parent_and_cancellation_releases_it(tmp_path: Path) -> None:
    with closing(DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))) as durable:
        durable.admit_input(conversation_id="conversation", request_id="input", task_id="parent",
                            text="inspect the page", provider_snapshot={})
        durable.claim_task("parent")
        durable.start_task("parent", state={})
        browser = LeaseBrowser()
        assert durable.tool_resources.acquire_task_browser("parent", lambda: browser) is browser
        durable.wait_task("parent", state={})
        assert durable.tool_resources.task_browser("parent") is browser
        assert browser.releases == 0
        durable.cancel_task("parent")
        assert browser.releases == 1 and browser.closes == 1
    assert browser.releases == 1


def test_source_cleanup_and_new_adapter_reuse_original_browser(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    _, first_tools, browser = production_browser
    resources = ToolResources()
    try:
        assert build_task_browser("task", resources, first_tools) is browser
        assert browser.navigate(URL).ok
        original = browser.document_identity()
        assert original is not None
        close_research_tools(first_tools)
        assert browser.document_identity() == original
        replacement = ResearchTools(WebSourceProvider(), None, None)
        acquired = build_task_browser("task", resources, replacement)
        assert acquired is browser and replacement.browser_session is browser
        assert isinstance(replacement.search, WebSourceProvider)
        assert isinstance(replacement.search.browser_source, TaskBrowserSource)
        assert replacement.search.browser_source.browser is browser
        # A resumed task may register a wait after the previous source adapter closed.
        binding = browser.capture_wait_binding()
        resources.bind_browser("wait", browser, binding)
        close_research_tools(replacement)
        assert browser.document_identity() == original
        resources.release_browser("wait")
        assert browser.document_identity() == original
    finally:
        resources.close()
