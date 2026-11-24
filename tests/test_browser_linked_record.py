"""Record choice and evidence remain bound through the real Chromium runtime."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from codey.automation.browser_contracts import RouteDecision
from codey.automation.browser_session import BrowserSession
from codey.operations.browser_linked_record import (
    linked_record_progress,
    observed_first_record,
    requested_linked_record,
)
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.kernel_facts import project_execution_record
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.result_schema import validate_output
from codey.toolchain.tool_spec import tool_specs
from codey.types.json_value import JsonObject, require_json_string
from tests.support.task_browser import browser_for, install

ENTRY = "https://example.com/catalog"
DETAIL = "https://example.com/profile/north"
OTHER = "https://example.com/profile/south"
REGISTER = "https://example.com/catalog?item=register"
TASK = (f"What is the first sentence of the first quote at {ENTRY}, who wrote it, and when and where was that person born? "
        "Open the linked profile to verify those details and include its link.")


@pytest.fixture()
def driver() -> Iterator[BrowserSession]:
    def route(request: RouteDecision) -> None:
        html = {
            ENTRY: "<main>Reading list</main>",
            DETAIL: ("<h1>Rosa North</h1><p><strong>Born:</strong> <span>May 1, 1940</span> <span>in Port Heath</span></p>"
                     "<h2>Guest writer</h2><p>Born: April 10, 1950 in Pine Rock</p>"),
            OTHER: "<h1>Sam South</h1><p><strong>Born:</strong> <span>March 2, 1970</span> <span>in Lake Point</span></p>",
            REGISTER: "<title>Opening register | Library</title><main>Available during opening hours.</main>",
        }.get(request.request.url, "<h1>Wrong source</h1>")
        request.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    browser = browser_for(route, owner_task_id="r")
    try:
        yield browser
    finally:
        browser.close()


def session() -> TaskSession:
    policy = TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"}))
    return TaskSession(policy, task_kind="chat", task_text=TASK)


def card(author: str, link: str, extra: str = "") -> str:
    return (f'<section itemscope itemtype="https://schema.org/CreativeWork"><span itemprop="text">'
            '“Dr. Grey found the trail. Keep looking.”</span>'
            f'<p>By <small itemprop="author">{author}</small> <a href="{link}">More</a>{extra}</p>'
            '<a href="/topics/general">Topics</a></section>')


def committed(delegate: ExecutionDelegate, task: TaskSession, ref: str, tool: str, args: JsonObject | None = None) -> ToolResult:
    call = ToolCall(tool, args or {}, call_id=ref)
    result, ok, _ = delegate.execute(call)
    task._memory_results[ref] = result
    project_execution_record(task, ref, call, result, ok=ok)
    return result


@pytest.mark.parametrize("reverse", [False, True])
def test_repeated_record_owns_same_named_control(driver: BrowserSession, reverse: bool) -> None:
    task = session()
    request = requested_linked_record(task)
    assert request is not None and driver.navigate(ENTRY).ok
    cards = [card("Rosa North", DETAIL), card("Sam South", OTHER)]
    install(driver, '<main>' + ''.join(reversed(cards) if reverse else cards) + '</main>')
    result = driver.snapshot()
    document = driver.document_identity()
    assert result.ok and document is not None
    validate_output("browser_snapshot", tool_specs()["browser_snapshot"].output_schema, result.observation)
    record = observed_first_record(request, "snapshot", result.observation, document)
    assert record is not None and record.region["subject"] == ("Sam South" if reverse else "Rosa North")
    assert len(record.links) == 1 and record.links[0]["name"] == "More"
    assert driver.click(record.link_ref).ok
    opened = driver.document_identity()
    assert opened is not None and opened["url"] == (OTHER if reverse else DETAIL)


@pytest.mark.parametrize("reordered", [False, True])
def test_ambiguous_link_or_unconfirmed_order_does_not_bind(driver: BrowserSession, reordered: bool) -> None:
    request = requested_linked_record(session())
    assert request is not None and driver.navigate(ENTRY).ok
    cards = card("Rosa North", DETAIL, '<a href="/alternate">More</a>') + card("Sam South", OTHER)
    layout = ' style="display:flex;flex-direction:column-reverse"' if reordered else ''
    install(driver, f"<main{layout}>{cards}</main>")
    snapshot = driver.snapshot()
    document = driver.document_identity()
    assert snapshot.ok and document is not None
    record = observed_first_record(request, "snapshot", snapshot.observation, document)
    if reordered:
        assert record is None
    else:
        assert record is not None and not record.link_ref and len(record.links) == 2


@pytest.mark.parametrize("bypass_link", [False, True])
def test_completion_uses_matched_link_receipts_and_actual_detail_fields(driver: BrowserSession, bypass_link: bool) -> None:
    task = session()
    tools = ResearchTools(search=TaskBrowserSource(driver), store=None, changes=None, browser_session=driver)
    delegate = ExecutionDelegate(session=task, research_tools=tools)
    assert committed(delegate, task, "navigate", "browser_navigate", {"url": ENTRY}).ok
    install(driver, '<main>' + card("Rosa North", DETAIL) + card("Sam South", OTHER) + '</main>')
    assert committed(delegate, task, "snapshot", "browser_snapshot").ok
    request = requested_linked_record(task)
    assert request is not None
    record = linked_record_progress(task, request).entry
    assert record is not None and record.link_ref
    assert not evaluate(task, "Rosa North was born in 1999.").complete
    assert not committed(delegate, task, "wrong-click", "browser_click", {"ref": "wrong-ref"}).ok
    if bypass_link:
        # A direct visit to the right URL cannot substitute for the requested link.
        assert committed(delegate, task, "direct", "browser_navigate", {"url": DETAIL}).ok
        assert committed(delegate, task, "read", "browser_read").ok
        assert not evaluate(task, f"Rosa North was born on May 1, 1940 in Port Heath. {DETAIL}").complete
        return
    assert committed(delegate, task, "click", "browser_click", {"ref": record.link_ref}).ok
    args: JsonObject = {"condition": "new_document", "value": "click", "timeout_ms": 3000}
    assert committed(delegate, task, "wait", "browser_wait", args).ok
    assert not evaluate(task, "Rosa North was born in 1999.").complete
    assert committed(delegate, task, "read", "browser_read").ok
    document = driver.document_identity()
    assert document is not None and require_json_string(document["url"], "URL") == DETAIL
    verdict = evaluate(task, "The year was 1999.", context=CompletionContext(browser_document=document))
    assert verdict.complete and verdict.final_text is not None
    assert "Dr. Grey found the trail." in verdict.final_text and "Keep looking." not in verdict.final_text
    assert "May 1, 1940" in verdict.final_text and "Port Heath" in verdict.final_text and "1999" not in verdict.final_text
    assert "Pine Rock" not in verdict.final_text
    assert DETAIL in verdict.final_text


@pytest.mark.parametrize("conditional", ["If authorized, ", "Do not "])
def test_linked_record_intent_does_not_override_an_access_condition(conditional: str) -> None:
    task = session()
    task.task_text = f"The first article at {ENTRY} is relevant. {conditional}open the linked page to verify the title."
    assert requested_linked_record(task) is None


def test_grid_record_title_and_query_link_are_bound_without_model_copying(driver: BrowserSession) -> None:
    task = session()
    task.task_text = (f"Take the first item at {ENTRY}. Open the linked page to verify its title. "
                      "Give the title and its link.")
    tools = ResearchTools(search=TaskBrowserSource(driver), store=None, changes=None, browser_session=driver)
    delegate = ExecutionDelegate(session=task, research_tools=tools)
    assert committed(delegate, task, "navigate", "browser_navigate", {"url": ENTRY}).ok
    install(driver, '''<main style="display:grid;grid-template-columns:1fr 1fr">
        <div class="card"><h3><a href="?item=register">Opening register</a></h3></div>
        <div class="card"><h3><a href="/later">Later register</a></h3></div></main>''')
    assert committed(delegate, task, "snapshot", "browser_snapshot").ok
    request = requested_linked_record(task)
    assert request is not None
    record = linked_record_progress(task, request).entry
    assert record is not None and record.link_ref
    assert committed(delegate, task, "click", "browser_click", {"ref": record.link_ref}).ok
    assert committed(delegate, task, "wait", "browser_wait", {
        "condition": "new_document", "value": "click", "timeout_ms": 3000}).ok
    assert committed(delegate, task, "read", "browser_read").ok
    result = evaluate(task, "Checked.", context=CompletionContext(browser_document=driver.document_identity()))
    assert result.complete and result.final_text is not None
    assert "Opening register" in result.final_text and REGISTER in result.final_text
