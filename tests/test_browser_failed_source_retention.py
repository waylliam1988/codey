"""Source rejection preserves a real, observable page without minting passages."""
from __future__ import annotations

import pytest

from codey.automation.browser_contracts import RouteDecision as Route
from codey.automation.browser_routes import route_policy
from codey.automation.browser_session import BrowserSession
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.research.browser_page import validate_browser_capture
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from tests.test_browser_context_delivery import URL
from tests.test_browser_context_delivery import production_browser as production_browser


@pytest.mark.parametrize("status", [200, 403])
def test_failed_source_page_remains_available_for_observation(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    session, tools, browser = production_browser
    body = ('<p>Sign in to view this record.</p><input type="password"><button>Continue</button>'
            if status == 200 else '<p>Access to this record was denied.</p><button>Help</button>')

    def route(request: Route) -> None:
        request.fulfill(status=status, content_type="text/html", body=body)

    browser._process.network_policy = route_policy(route)
    receipt, ok, _ = ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall("open_url", {"url": URL}))
    assert not ok and "source_failure" in receipt.canonical
    assert "read_passages" not in receipt.canonical
    assert receipt.audit["browser_document"] == browser.document_identity()
    observed = browser.snapshot()
    assert observed.ok and ("Sign in" if status == 200 else "denied") in observed.model_text
    assert not tools.ledger.read_passages


def test_response_status_does_not_survive_replacement_document_on_same_page(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, browser = production_browser
    status = 403

    def route(request: Route) -> None:
        request.fulfill(status=status, content_type="text/html", body=(
            "<article>Original access restriction.</article>" if status == 403 else
            "<article>The replacement document contains readable current source text.</article>"))

    browser._process.network_policy = route_policy(route)
    assert browser.navigate(URL).ok
    assert validate_browser_capture(browser.capture())["status"] == "error"
    previous = browser.document_identity()
    status = 200

    assert browser.navigate(URL).ok
    assert browser.document_identity() != previous
    current = browser.capture()
    assert current["status"] == "ok", current
    assert "replacement document" in str(current["text"])
