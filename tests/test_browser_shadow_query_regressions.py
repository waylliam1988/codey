"""Explicit query relationships resolve within the control's actual DOM root."""
from __future__ import annotations

import pytest
from playwright.sync_api import ElementHandle

from codey.automation import browser_session as session_module
from codey.automation.browser_observation import same_node
from codey.automation.browser_query import QueryObservationError, bind_query_target, observe_query_target
from codey.automation.browser_session import BrowserSession, _browser_call
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser


def install_shadow(browser: BrowserSession, html: str) -> None:
    install(browser, '<article id="result">Outside previous value</article><div id="host"></div>')

    def run() -> None:
        _value: object = browser._page_on_browser_thread().evaluate('''html => {
            document.querySelector('#host').attachShadow({mode:'open'}).innerHTML=html;
        }''', html)
    _browser_call(run)


@pytest.mark.parametrize('relation', ['aria', 'native'])
def test_shadow_query_binds_the_actual_node_and_rejects_same_id_replacements(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], relation: str,
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    html = ('''<section aria-label="A-173"><button aria-controls="result" onclick="
        this.getRootNode().querySelector('#result').textContent='Inside CORRECT_173'">Details</button>
        <article id="result">Inside previous value</article></section>''' if relation == 'aria' else
        '<details><summary>Details</summary>Inside CORRECT_173</details>')
    install_shadow(browser, html)
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    clicked = browser.click(ref, query=True)
    assert clicked.ok, clicked.model_text
    baseline = decode_json_object(clicked.observation['query_baseline'])
    binding = decode_json_object(baseline['target'])

    def inspect() -> None:
        page = browser._page_on_browser_thread()
        current = observe_query_target(page, binding)
        assert 'CORRECT_173' in str(current['text']) and 'Outside' not in str(current['text'])
        selector = '#host >> #result' if relation == 'aria' else '#host >> details'
        page.locator(selector).evaluate('el => el.outerHTML=el.outerHTML')
        with pytest.raises(QueryObservationError):
            observe_query_target(page, binding)
    _browser_call(inspect)


def test_outside_update_never_confirms_an_unchanged_shadow_query(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install_shadow(browser, '''<section aria-label="A-173"><button aria-controls="result" onclick="
        window.clicks=(window.clicks||0)+1;setTimeout(() =>
        document.querySelector('#result').textContent='Neighbor OTHER_174 delivery is tomorrow at 12:10 with the unrelated receipt code.',100)">Details</button>
        <article id="result">Inside previous value</article></section>''')
    ref = require_json_string(elements(browser.snapshot().observation)[0]['ref'], 'ref')
    clicked = browser.click(ref, query=True)
    assert clicked.ok, clicked.model_text
    baseline = decode_json_object(clicked.observation['query_baseline'])
    with pytest.raises(QueryObservationError) as caught:
        browser.capture_query_result(baseline, pending='Unconfirmed')
    assert caught.value.reason == 'query_update_unconfirmed'
    assert browser.evaluate('window.clicks').observation['value'] == 1


@pytest.mark.parametrize('targets', ['', '<article id="result">Inside one</article><article id="result">Inside two</article>'])
def test_unresolved_shadow_relation_does_not_fall_through_to_a_document_id(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], targets: str,
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install_shadow(browser, '<button aria-controls="result">Details</button>' + targets)

    def bind() -> JsonObject | None:
        page = browser._page_on_browser_thread()
        control = page.query_selector('#host >> button')
        assert control is not None
        return bind_query_target(page, control)
    assert _browser_call(bind) is None


@pytest.mark.parametrize('count', [10, 40, 80])
def test_snapshot_grouping_does_not_compare_pairs_across_the_browser_boundary(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, ''.join(f'<section aria-label="Order {i}"><button>Details</button></section>' for i in range(count)))
    comparisons = 0
    original = same_node

    def counted(left: ElementHandle, right: ElementHandle) -> bool:
        nonlocal comparisons
        comparisons += 1
        return original(left, right)
    monkeypatch.setattr(session_module, 'same_node', counted)
    snapshot = browser.snapshot()
    assert snapshot.ok and len(elements(snapshot.observation)) == count
    contexts = snapshot.observation['contexts']
    assert isinstance(contexts, list) and len(contexts) == count
    assert comparisons == 0
