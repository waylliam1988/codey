"""Explicit query relationships resolve within the control's actual DOM root."""
from __future__ import annotations

import json

import pytest

from codey.automation.browser_query import QueryObservationError
from codey.automation.browser_session import BrowserSession
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.types.json_value import decode_json_object, require_json_string
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser


def install_shadow(browser: BrowserSession, html: str) -> None:
    install(browser, '<article id="result">Outside previous value</article><div id="host"></div>')

    assert browser.evaluate("document.querySelector('#host').attachShadow({mode:'open'}).innerHTML=" + json.dumps(html)).ok


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
    current = browser.capture_query_result(baseline, pending='Unconfirmed')
    assert current['query_confirmed'] is True and 'CORRECT_173' in str(current['text'])
    region = decode_json_object(current['query_region'])
    assert 'CORRECT_173' in str(region['text']) and 'Outside' not in str(region['text'])
    selector = '#result' if relation == 'aria' else 'details'
    assert browser.evaluate("(() => {const el=document.querySelector('#host').shadowRoot.querySelector("
        + json.dumps(selector) + ");el.outerHTML=el.outerHTML;})()").ok
    with pytest.raises(QueryObservationError):
        browser.capture_query_result(baseline, pending='Unconfirmed')


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
    with pytest.raises(QueryObservationError, match='bound result region has not shown a confirmed update'):
        browser.capture_query_result(baseline, pending='Unconfirmed')
    assert browser.evaluate('window.clicks').observation['value'] == 1


@pytest.mark.parametrize('targets', ['', '<article id="result">Inside one</article><article id="result">Inside two</article>'])
def test_unresolved_shadow_relation_does_not_fall_through_to_a_document_id(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], targets: str,
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install_shadow(browser, '<button aria-controls="result">Details</button>' + targets)

    control = elements(browser.snapshot().observation)[0]
    clicked = browser.click(require_json_string(control['ref'], 'ref'), query=True)
    assert clicked.ok
    assert 'target' not in decode_json_object(clicked.observation['query_baseline'])


@pytest.mark.parametrize('count', [10, 40, 80])
def test_snapshot_grouping_does_not_compare_pairs_across_the_browser_boundary(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], count: int,
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, ''.join(f'<section aria-label="Order {i}"><button>Details</button></section>' for i in range(count)))
    snapshot = browser.snapshot()
    assert snapshot.ok and len(elements(snapshot.observation)) == count
    contexts = snapshot.observation['contexts']
    assert isinstance(contexts, list) and len(contexts) == count
    assert len({row['ref'] for row in elements(snapshot.observation)}) == count
