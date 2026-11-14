"""Explicit target identities survive reference refresh but reject replacement nodes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.sync_api import Route

from codey.automation.browser_query import QueryObservationError, bind_query_target, observe_query_target
from codey.automation.browser_session import BrowserSession, _browser_call
from codey.operations.browser_followups import pending_click
from codey.operations.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import _browser_completion_block
from codey.operations.kernel_protocol import build_turn_snapshot, offer_browser_tools
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.research.browser_search import BrowserSearchProvider
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.support.real_cdp import cdp_browser
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser


@pytest.mark.parametrize('relation', ['aria', 'native'])
def test_original_result_node_is_bound_without_requiring_a_global_single_region(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], relation: str,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    html = ('<button aria-controls="result">Details</button><article id="result">Original target</article>'
            if relation == 'aria' else '<details><summary>Details</summary>Original target</details>')
    install(browser, html + '<article id="other">Unrelated result</article>')

    def probe() -> None:
        page = browser._page_on_browser_thread()
        control = page.query_selector('button,summary')
        assert control is not None
        binding = bind_query_target(page, control)
        assert binding is not None
        before = observe_query_target(page, binding)
        page.locator('#other').evaluate("el => el.textContent='Unrelated update'")
        assert observe_query_target(page, binding) == before
        selector = '#result' if relation == 'aria' else 'details'
        page.locator(selector).evaluate("el => el.outerHTML=el.outerHTML")
        with pytest.raises(QueryObservationError, match='original query node'):
            observe_query_target(page, binding)
    _browser_call(probe)


def test_ambiguous_control_relationship_has_no_target_binding(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<button aria-controls="same">Details</button><div id="same">One</div><div id="same">Two</div>')

    def probe() -> JsonObject | None:
        page = browser._page_on_browser_thread()
        control = page.query_selector('button')
        assert control is not None
        return bind_query_target(page, control)
    assert _browser_call(probe) is None


@pytest.mark.parametrize('relation', ['aria', 'native'])
def test_query_read_uses_explicit_target_and_preserves_the_formal_source(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], relation: str,
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL}))[1]
    html = ('''<button aria-controls="result" onclick="window.clicks=(window.clicks||0)+1;
        document.querySelector('#result').textContent='Target value CORRECT_173';">Details</button>
        <article id="result">Target not loaded yet</article>''' if relation == 'aria' else
        '<details><summary>Details</summary>Target value CORRECT_173</details>')
    install(browser, html + '<article>Neighbor value WRONG_174</article>')
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    click = browser.click(ref, query=True)
    assert click.ok
    baseline = click.observation['query_baseline']
    assert isinstance(baseline, dict) and isinstance(baseline.get('target'), dict)
    session._memory_results['original-click'] = ToolResult(ToolCall('browser_click', {'ref': ref}), click.model_text,
        ok=True, canonical={'value': {'delivery': 'delivered'}}, audit={'query_baseline': baseline})
    read, ok, _ = delegate.execute(ToolCall('browser_read', {}))
    assert ok, read.model_text
    assert read.audit['query_click'] == 'original-click'
    assert decode_json_object(read.audit['query_region'])['text']
    assert 'CORRECT_173' in read.model_text
    observation = decode_json_object(read.canonical['research_observation'])
    assert 'WRONG_174' in str(observation['text'])  # Full source is still preserved.


def test_pending_click_does_not_lock_a_new_document_but_fresh_refs_do_not_replay_in_same_document(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL}))[1]
    html = '<button onclick="window.clicks=(window.clicks||0)+1">Query</button><article>Current result</article>'
    install(browser, html)
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    click = browser.click(ref, query=True)
    old_document = browser.document_identity()
    session._memory_results['click'] = ToolResult(ToolCall('browser_click', {'ref': ref}), click.model_text, ok=True,
        canonical={'value': {'delivery': 'delivered'}}, audit={'query_baseline': click.observation['query_baseline']})
    fresh_ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    denied, ok, _ = delegate.execute(ToolCall('browser_click', {'ref': fresh_ref}))
    assert 'value' in denied.canonical, denied.model_text
    assert not ok and decode_json_object(denied.canonical['value'])['delivery'] == 'not_delivered'
    assert pending_click(session._memory_results, old_document) is not None
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL + '?new-document'}))[1]
    document = browser.document_identity()
    assert document != old_document and pending_click(session._memory_results, document) is None
    install(browser, html)
    fresh_ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    assert delegate.execute(ToolCall('browser_click', {'ref': fresh_ref}))[1]
    base = offer_browser_tools(build_turn_snapshot(session), session)
    _, text = browser_task_handoff(session, base, CompletionContext(browser_document=document))
    assert 'unverified' in text and 'Execute this next step' not in text
    session.policy = TaskPolicy(session.policy.grants, required_checks=(require_tool_success('browser_read'),))
    _, declared_text = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=document))
    assert 'unverified' in declared_text and 'without repeating the click' not in declared_text
    session.task_text = ''
    assert _browser_completion_block(session, 'Previous query was not verified.',
                                     CompletionContext(browser_document=document)) is None
    assert session._memory_results['click'].ok  # Historical delivery was not rewritten.


def test_changed_observed_label_is_rejected_before_click(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<section aria-label="Order A-173"><button onclick="window.clicked=true">Details</button></section>')
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    browser.evaluate("document.querySelector('section').setAttribute('aria-label','Order A-174')")
    rejected = browser.click(ref)
    assert not rejected.ok and rejected.observation['delivery'] == 'not_delivered'


@pytest.mark.parametrize('update', ['other', 'same', 'error'])
def test_target_update_is_not_inferred_from_other_regions_or_semantic_success(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], update: str,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    script = {"other": "document.querySelector('#other').textContent='Other updated'",
              "same": "document.querySelector('#result').textContent='Original target'",
              "error": "document.querySelector('#result').textContent='Query failed: denied'"}[update]
    install(browser, f'''<button aria-controls="result" onclick="{script}">Details</button>
        <article id="result">Original target</article><article id="other">Other result</article>''')
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    click = browser.click(ref, query=True)
    baseline = decode_json_object(click.observation['query_baseline'])
    if update == 'error':
        page = browser.capture_query_result(baseline, pending='Unconfirmed')
        assert 'Query failed' in str(page['text'])
        assert 'query_success' not in page
    else:
        with pytest.raises(QueryObservationError) as caught:
            browser.capture_query_result(baseline, pending='Unconfirmed')
        assert caught.value.reason == 'query_update_unconfirmed'


class BoundBrowser(BrowserSearchProvider):
    def _guard_request(self, route: Route) -> None:
        route.fulfill(status=200, content_type='text/html; charset=utf-8', body='''
            <button aria-controls="result" onclick="this.dataset.clicks=Number(this.dataset.clicks||0)+1;
            document.querySelector('#result').textContent='Current target CORRECT_173';">Details</button>
            <article id="result">Original target</article><article>Other result WRONG_174</article>''')


def restart_probe(state: Path, port: int, resume: bool) -> None:
    provider = BoundBrowser(headless=False, isolated=False, launch=False, require_existing_session=True, cdp_port=port)
    browser = BrowserSession(provider)
    if not resume:
        assert browser.navigate(URL).ok
        ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
        click = browser.click(ref, query=True)
        assert click.ok
        receipt: JsonObject = {'baseline': click.observation['query_baseline'],
                               'browser_resource': browser.resource_binding()}
        state.write_text(json.dumps(receipt), encoding='utf-8')
        os._exit(73)
    saved = parse_json_object(state.read_text(encoding='utf-8'))
    browser.prepare_from_receipts([ToolResult(ToolCall('browser_click', {'ref': 'historical'}), 'delivered', ok=True,
        audit={'browser_resource': saved['browser_resource']})])
    page = browser.capture_query_result(decode_json_object(saved['baseline']), pending='Unconfirmed')
    assert 'CORRECT_173' in str(page['text'])
    def count() -> str | None:
        return browser._page_on_browser_thread().locator('button').get_attribute('data-clicks')
    assert _browser_call(count) == '1'
    provider.close()


def test_explicit_query_node_reconnects_after_process_exit_without_another_click(tmp_path: Path) -> None:
    state = tmp_path / 'original-receipt.json'
    with cdp_browser(tmp_path / 'profile') as port:
        source = ('from pathlib import Path; import sys; from tests.test_browser_query_binding import restart_probe; '
                  'restart_probe(Path(sys.argv[1]),int(sys.argv[2]),sys.argv[3]=="resume")')
        command = [sys.executable, '-c', source, str(state), str(port)]
        initial = subprocess.run([*command, 'initial'], capture_output=True, text=True, timeout=60)
        assert initial.returncode == 73, initial.stdout + initial.stderr
        resumed = subprocess.run([*command, 'resume'], capture_output=True, text=True, timeout=60)
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr


def test_completed_query_is_not_relabelled_interrupted_after_navigation() -> None:
    session = TaskSession(TaskPolicy(frozenset({'control', 'web.read', 'browser.observe', 'browser.interact'})),
                          task_kind='chat', task_text=f'Navigate to {URL}.')
    session._memory_results['click'] = ToolResult(ToolCall('browser_click', {'ref': 'observed'}), 'delivered', ok=True,
        canonical={'value': {'delivery': 'delivered'}}, audit={'query_baseline': {'document': {'loader_id': 'old'}}})
    session._memory_results['read'] = ToolResult(ToolCall('browser_read', {}), 'Observed result', ok=True,
        audit={'query_click': 'click'})
    _, text = browser_task_handoff(session, build_turn_snapshot(session),
                                    CompletionContext(browser_document={'loader_id': 'new'}))
    assert text == ''


def test_query_read_never_confirms_when_document_identity_is_unavailable(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<article id="record">Current target CORRECT_173 is now available.</article>')

    def unavailable() -> JsonObject | None:
        return None
    monkeypatch.setattr(browser, '_document_identity_on_browser_thread', unavailable)
    baseline: JsonObject = {'document': None, 'regions': [{'key': 'record', 'text': 'Original target', 'busy': False}]}
    with pytest.raises(QueryObservationError):
        browser.capture_query_result(baseline, pending='Unconfirmed')
