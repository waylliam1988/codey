"""Browser identity, observed relationships and visible disclosure regression cases."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import NoReturn

import pytest

from codey.automation.browser_document import document_changed, same_document
from codey.automation.browser_query import QueryObservationError
from codey.automation.browser_session import BrowserSession
from codey.operations import kernel_control
from codey.operations.browser.browser_followups import pending_click, query_click
from codey.operations.browser.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import _browser_completion_block
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.kernel_facts import project_execution_record
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.tool_requirement import require_tool_success
from codey.research.passages import passage_ref
from codey.research.source_observation import source_observation
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.support.browser_reconnect_process import NoModel, resources
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser
from tools.browser_leaf_exposure_ab import evidence_checks


def mutate(browser: BrowserSession, code: str) -> None:
    """Simulate website changes through the same production worker."""
    observed = browser.evaluate(code)
    assert observed.ok, observed.model_text


def start_query(session: TaskSession, tools: ResearchTools, browser: BrowserSession, html: str) -> JsonObject:
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, html)
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    token = query_click.set(True)
    try:
        click, ok, _ = delegate.execute(ToolCall('browser_click', {'ref': ref}))
    finally:
        query_click.reset(token)
    assert ok, click.model_text
    session._memory_results['query'] = click
    project_execution_record(session, 'query', click.call, click, ok=click.ok)
    return decode_json_object(click.audit['query_baseline'])


def test_push_state_keeps_pending_and_blocks_a_second_click_with_a_fresh_ref(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    baseline = start_query(session, tools, browser, '''<button aria-controls="result" onclick="
        window.clicks=(window.clicks||0)+1;history.pushState({},'', '?query=1')">Query</button>
        <article id="result">Previous result</article>''')
    current = browser.document_identity()
    assert current is not None and current['loader_id'] == decode_json_object(baseline['document'])['loader_id']
    assert pending_click(session._memory_results, current) is not None
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    denied, ok, _ = ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_click', {'ref': ref}))
    assert not ok and decode_json_object(denied.canonical['value'])['delivery'] == 'not_delivered'
    assert browser.evaluate('window.clicks').observation['value'] == 1
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=current))
    assert 'do not click again' in hint
    assert _browser_completion_block(session, 'Finished', CompletionContext(browser_document=current)) is not None


def test_push_state_does_not_prevent_readback_of_the_original_target(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    baseline = start_query(session, tools, browser, '''<button aria-controls="result" onclick="
        history.pushState({},'', '?query=1');document.querySelector('#result').textContent='Updated CORRECT_173';">
        Query</button><article id="result">Previous result</article>''')
    page = browser.capture_query_result(baseline, pending='Unconfirmed')
    assert 'CORRECT_173' in str(page['text']) and page['url'] == URL + '?query=1'


def test_shadow_contexts_are_distinct_and_choose_the_corresponding_actual_control(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<div id="first"></div><div id="second"></div>')
    assert browser.evaluate('''for (const [id,label] of [['first','A-173'],['second','A-174']]) {
        document.getElementById(id).attachShadow({mode:'open'}).innerHTML=
            `<section aria-label="${label}"><button onclick="window.chosen='${label}'">Details</button></section>`;
    }''').ok
    snapshot = browser.snapshot().observation
    contexts = snapshot['contexts']
    assert isinstance(contexts, list) and len(contexts) == 2
    target = next(decode_json_object(row) for row in contexts if decode_json_object(row)['label'] == 'A-174')
    control = next(row for row in elements(snapshot) if row['context_id'] == target['id'])
    scope = require_json_string(target['id'], 'context id')
    scoped = browser.snapshot(scope_ref=scope)
    assert scoped.ok and [row["name"] for row in elements(scoped.observation)] == ["Details"]
    assert "box" in elements(scoped.observation)[0]
    display = elements(parse_json_object(scoped.model_text))[0]
    assert "box" not in display and display["ref"] == elements(scoped.observation)[0]["ref"]
    assert "A-173" not in str(scoped.observation["text"])
    assert not browser.click(scope).ok
    assert browser.click(require_json_string(control['ref'], 'ref')).ok
    assert browser.evaluate('window.chosen').observation['value'] == 'A-174'
    mutate(browser, "document.getElementById('second').shadowRoot.querySelector('section').remove()")
    assert not browser.snapshot(scope_ref=scope).ok


def test_unrelated_region_insertion_does_not_invalidate_a_control(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<section aria-label="A-173"><button onclick="window.clicked=true">Details</button></section>')
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    mutate(browser, "document.body.insertAdjacentHTML('afterbegin','<section>Unrelated</section>')")
    clicked = browser.click(ref)
    assert clicked.ok, clicked.model_text
    assert browser.evaluate('window.clicked').observation['value'] is True


def test_same_named_replacement_context_does_not_preserve_the_old_relationship(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<section aria-label="A-173"><button onclick="window.clicked=true">Details</button></section>')
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    mutate(browser, '''const old=document.querySelector('section'), replacement=old.cloneNode(false);
        old.replaceWith(replacement);replacement.append(old.querySelector('button'));''')
    rejected = browser.click(ref)
    assert not rejected.ok and rejected.observation['delivery'] == 'not_delivered'
    assert browser.evaluate('window.clicked===true').observation['value'] is False


@pytest.mark.parametrize('hidden', ['hidden', 'style="display:none"', 'style="visibility:hidden"'])
def test_background_changes_in_a_hidden_target_do_not_confirm_a_visible_result(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], hidden: str,
) -> None:
    session, tools, browser = production_browser
    baseline = start_query(session, tools, browser, f'''<button aria-controls="result" onclick="
        document.querySelector('#result').textContent='Updated CORRECT_173';">Query</button>
        <article id="result" {hidden}>Previous result</article><article>Visible copy: Updated CORRECT_173</article>''')
    with pytest.raises(QueryObservationError, match="has not shown a confirmed update"):
        browser.capture_query_result(baseline, pending='Unconfirmed')
    read, ok, _ = ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_read', {}))
    assert not ok and not read.canonical.get('read_passages')
    assert read.canonical.get('query_read') == {"reason": "query_update_unconfirmed"}
    assert read.audit.get('query_click') == 'query'
    assert pending_click(session._memory_results, browser.document_identity()) is not None


@pytest.mark.parametrize('native', [False, True])
def test_revealing_existing_text_is_a_disclosure_update_and_is_formally_read(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], native: bool,
) -> None:
    session, tools, browser = production_browser
    html = ('<details><summary>Query</summary>Existing CORRECT_173</details>' if native else '''<button aria-controls="result" onclick="
        document.querySelector('#result').hidden=false;">Query</button>
        <article id="result" hidden>Existing CORRECT_173</article>''')
    baseline = start_query(session, tools, browser, html)
    page = browser.capture_query_result(baseline, pending='Unconfirmed')
    assert 'CORRECT_173' in str(page['text'])
    read, ok, _ = ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_read', {}))
    assert ok and read.canonical['read_passages'] and read.audit['query_click'] == 'query'


def test_declared_wait_after_push_state_still_settles_the_original_query(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    session.policy = type(session.policy)(session.policy.grants, required_checks=(require_tool_success(
        'browser_wait', arguments={'condition': 'text', 'value': 'CORRECT_173'}),))
    start_query(session, tools, browser, '''<button aria-controls="result" onclick="
        history.pushState({},'', '?query=1');document.querySelector('#result').textContent='Updated CORRECT_173';">
        Query</button><article id="result">Previous result</article>''')
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    wait, ok, _ = delegate.execute(ToolCall('browser_wait', {'condition': 'text', 'value': 'CORRECT_173', 'timeout_ms': 5000}))
    assert ok
    session._memory_results['wait'] = wait
    project_execution_record(session, 'wait', wait.call, wait, ok=wait.ok)
    _, hint = browser_task_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=browser.document_identity()))
    assert parse_json_object(hint.partition('Execute this next step: ')[2]) == {'tool': 'browser_read', 'args': {}}
    read, ok, _ = delegate.execute(ToolCall('browser_read', {}))
    assert ok, read.model_text
    session._memory_results['read'] = read
    assert pending_click(session._memory_results, browser.document_identity()) is None


def test_citing_only_the_neighbor_passage_cannot_support_target_facts(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<article>Target CORRECT_173. Neighbor WRONG_174.</article>')
    read, ok, _ = delegate.execute(ToolCall('browser_read', {}))
    assert ok
    observation = decode_json_object(read.canonical['research_observation'])
    text = require_json_string(observation['text'], 'text')
    split = text.index('Neighbor')
    source, _, _ = source_observation(observation)
    target_ref = passage_ref(source.final_url, source.text_hash, 0, split)
    neighbor_ref = passage_ref(source.final_url, source.text_hash, split, len(text))
    read = ToolResult(read.call, read.model_text, ok=True, canonical={**read.canonical, 'read_passages': [
        {'ref': target_ref, 'start': 0, 'end': split},
        {'ref': neighbor_ref, 'start': split, 'end': len(text)}]})
    score = evidence_checks([read], f'CORRECT_173 [{neighbor_ref}]', ('CORRECT_173',))
    assert score['facts_in_read_receipts'] is True
    assert score['correct_evidence_cited'] is False and score['exact_passage_cited'] is False
    correct = evidence_checks([read], f'CORRECT_173 [{target_ref}]', ('CORRECT_173',))
    assert correct['correct_evidence_cited'] is True and correct['exact_passage_cited'] is True
    forged = ToolResult(read.call, read.model_text, ok=True, canonical={**read.canonical, 'read_passages': [
        {'ref': target_ref, 'start': split, 'end': len(text)}]})
    assert evidence_checks([forged], f'CORRECT_173 [{target_ref}]', ('CORRECT_173',))['correct_evidence_cited'] is False


def test_pending_identity_uses_targets_and_loaders_and_preserves_unknown_history() -> None:
    original: JsonObject = {'frame_id': 'main', 'target_id': 'tab-1', 'loader_id': 'document-1', 'url': URL}
    pushed: JsonObject = {**original, 'url': URL + '?query=1'}
    other_target: JsonObject = {**original, 'target_id': 'tab-2'}
    reload: JsonObject = {**original, 'loader_id': 'document-2'}
    assert same_document(original, pushed) and not document_changed(original, pushed)
    assert document_changed(original, other_target) and document_changed(original, reload)
    assert not same_document(None, None) and not document_changed(original, None)
    assert not same_document({'loader_id': 'document-1', 'url': URL}, pushed)


def test_chat_completion_refreshes_live_identity_for_an_unconfirmed_query(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, tools, browser = production_browser
    baseline = start_query(session, tools, browser, '<button>Query</button><article>Previous result</article>')
    assert browser.navigate(URL + '?new-document').ok  # Website navigation, no new tool receipt yet.
    sink = resources(tmp_path / 'state', initialize=True)
    with closing(sink.durable):
        context = KernelContext(session, KernelRunRequest(
            transport=KernelTransportDeps(provider=NoModel(), run_id='r', effect_scope='task', provider_id='local'),
            execution=KernelExecutionDeps(intent_sink=sink, research_tools=tools)))
        state = context.load_view().state
        assert state is not None
        captured: list[CompletionContext] = []

        class Inspected(BaseException):
            pass

        def plan(_state: RuntimeOperationState) -> ToolPlan:
            return ToolPlan([], None)

        def inspect(_session: TaskSession, _text: str, observed: CompletionContext) -> NoReturn:
            captured.append(observed)
            raise Inspected

        monkeypatch.setattr(context, 'plan', plan)
        monkeypatch.setattr(kernel_control, 'prepare_completion_output', inspect)
        with pytest.raises(Inspected):
            kernel_control.evaluate_completion(context, state)
        assert captured[0].browser_document == browser.document_identity()
        assert captured[0].browser_document != baseline['document']
        assert _browser_completion_block(session, 'Previous result unverified.', captured[0]) is None
