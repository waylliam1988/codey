"""Actual browser structure remains observation data, independent of business inference."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from codey.automation.browser_contracts import RouteDecision as Route
from codey.automation.browser_session import BrowserSession
from codey.operations.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import build_turn_snapshot, offer_browser_tools
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.task_browser_source import TaskBrowserSource
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.result_schema import validate_output
from codey.toolchain.tool_spec import tool_specs
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tests.support.task_browser import browser_for
from tests.support.task_browser import install as install

URL = 'https://example.com/context-observation'


class FixtureBrowser:
    def _guard_request(self, route: Route) -> None:
        route.fulfill(status=200, content_type='text/html; charset=utf-8',
                      body='<article>Initial readable browser article.</article>')


@pytest.fixture()
def production_browser() -> Iterator[tuple[TaskSession, ResearchTools, BrowserSession]]:
    provider = FixtureBrowser()
    browser = browser_for(provider._guard_request, owner_task_id="r")
    session = TaskSession(TaskPolicy(frozenset({'control', 'web.read', 'browser.observe', 'browser.interact'})),
                          task_kind='chat', task_text=f'Navigate to {URL} and inspect the page.')
    tools = ResearchTools(search=TaskBrowserSource(browser), store=None, changes=None, browser_session=browser)
    try:
        yield session, tools, browser
    finally:
        browser.close()


def elements(value: JsonObject) -> list[JsonObject]:
    rows = value['elements']
    assert isinstance(rows, list)
    return [decode_json_object(row) for row in rows]


def test_labels_are_observed_unique_contexts_and_unknown_controls_still_work(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '''<section><h2>Order A-173</h2><div><button>Details</button><input aria-label="Note"></div></section>
        <section><h2>Order A-174</h2><button>Details</button></section>
        <section aria-label="Duplicate"><button>Details</button></section>
        <section aria-label="Duplicate"><button>Details</button></section>
        <div><button onclick="window.clicked=true">Unknown</button></div>''')
    snapshot = browser.snapshot()
    value = snapshot.observation
    validate_output('browser_snapshot', tool_specs()['browser_snapshot'].output_schema, value)
    contexts = value['contexts']
    assert isinstance(contexts, list) and len(contexts) == 4
    rows = elements(value)
    assert rows[0]['context_id'] == rows[1]['context_id']
    assert rows[0]['context_id'] != rows[2]['context_id']
    assert rows[3]['context_id'] != rows[4]['context_id']
    assert rows[-1]['context_id'] == ''
    assert 'Order A-173' in snapshot.model_text and 'Order A-174' in snapshot.model_text
    assert browser.click(require_json_string(rows[-1]['ref'], 'ref')).ok


@pytest.mark.parametrize('uncertain', [False, True])
def test_pending_query_has_an_executable_recovery_without_a_tool_checklist(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], uncertain: bool,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    document = browser.document_identity()
    click = ToolResult(ToolCall('browser_click', {'ref': 'old'}), 'sent', ok=not uncertain,
        canonical={'value': {'delivery': 'uncertain' if uncertain else 'delivered'}},
        audit={'browser_document': document, 'query_baseline': {'document': document}})
    session._memory_results['click'] = click
    project_execution_record(session, 'click', click.call, click, ok=click.ok)
    base = offer_browser_tools(build_turn_snapshot(session), session)
    snapshot, text = browser_task_handoff(session, base, CompletionContext(browser_document=document))
    name = 'browser_snapshot' if uncertain else 'browser_read'
    assert name in snapshot.tool_names
    assert '"tool": "' + name + '"' in text
    assert 'do not click again' in text or 'uncertain' in text
