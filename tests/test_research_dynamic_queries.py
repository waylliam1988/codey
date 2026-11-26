"""Ordinary Research can choose observed queries without declaring a workflow."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession, BrowserToolOutput
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object, require_json_string
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_detail_production import run_case, submit
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tools import research_natural_handoff_ab as natural
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

DYNAMIC_CASE = natural.Case('/deliveries/ZX-581', 'ZX-581 现在到哪了，大概几点能到？', '''
<section aria-label="Parcel ZX-581"><h2>Parcel ZX-581</h2><p>Status: In transit.</p>
<button aria-controls="eta" onclick="window.clicks=(window.clicks||0)+1;setTimeout(() => {
document.getElementById('eta').textContent='ZX-581 arrival: Friday 18:37.';},350)">View itinerary</button>
<article id="eta">Arrival not loaded.</article></section>
<section aria-label="Parcel ZX-582"><h2>Parcel ZX-582</h2><p>Arrival: Thursday 12:10.</p>
<button aria-controls="neighbor">View itinerary</button><article id="neighbor">Neighbor record.</article></section>
''', ('In transit', 'Friday 18:37'))


def arrival_answer(directory: Path) -> str:
    messages = '\n'.join(text_values(requests(directory)[-1]['messages']))
    headings = list(re.finditer(r'(?m)^\[(e[0-9]+)\] [^\n]+\n', messages))
    label = next(match[1] for index, match in enumerate(headings)
                 if 'Friday 18:37' in messages[match.end():headings[index + 1].start()
                     if index + 1 < len(headings) else len(messages)])
    return f'ZX-581: In transit [e1]; arrival Friday 18:37. [{label}]'


def text_values(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from text_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from text_values(item)


def query_choices(directory: Path) -> list[JsonObject]:
    result: list[JsonObject] = []
    for text in text_values(requests(directory)[-1]['messages']):
        for line in text.splitlines():
            if line.startswith('{"name":'):
                row = parse_json_object(line)
                if 'call' in row:
                    result.append(row)
    return result


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection')
@pytest.mark.parametrize('native', [False, True])
def test_ordinary_research_selects_a_record_bound_query_and_receives_new_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / 'run'
    if native:
        monkeypatch.setattr(natural, 'ExperimentProvider', NativeExperimentProvider)

    def replies() -> Iterator[JsonObject]:
        initial = requests(directory)[-1]
        schema = json.dumps(initial.get('response_format', initial.get('tools')))
        assert 'browser_snapshot' in schema and 'submit_research_answer' in schema
        choices = query_choices(directory)
        assert len(choices) == 2, choices
        chosen = next(row for row in choices if decode_json_object(row['context'])['label'] == 'Parcel ZX-581')
        action = decode_json_object(chosen['call'])
        args = decode_json_object(action['args'])
        assert args['purpose'] == 'result'
        assert decode_json_object(args['ref'])['context'] == 'Parcel ZX-581'
        yield action
        messages = '\n'.join(text_values(requests(directory)[-1]['messages']))
        assert 'Friday 18:37' in messages
        assert 'Do not repeat' in messages or 'without repeating' in messages
        headings = list(re.finditer(r'(?m)^\[(e[0-9]+)\] [^\n]+\n', messages))
        label = next(match[1] for index, match in enumerate(headings)
                     if 'Friday 18:37' in messages[match.end():headings[index + 1].start()
                                                  if index + 1 < len(headings) else len(messages)])
        yield submit(f'ZX-581: In transit [e1]; arrival Friday 18:37. [{label}]')

    install_http(monkeypatch, replies(), native=native)
    row = run_case(directory, DYNAMIC_CASE, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    # Query reads can publish a targeted passage rather than the whole page;
    # the experiment helper's full-page coverage metric does not apply here.
    assert row['stop_reason'] == 'done' and row['source_stable'], row
    assert all(fact in require_json_string(row['answer'], 'answer') for fact in DYNAMIC_CASE.facts)
    receipts = [item.result for item in tool_receipts(directory / 'state')]
    clicks = [result for result in receipts if result.call.name == 'browser_click']
    reads = [result for result in receipts if result.call.name == 'browser_read']
    assert len(clicks) == len(reads) == 1
    assert clicks[0].ok and reads[0].ok and reads[0].audit.get('query_click')
    assert decode_json_object(reads[0].audit['query_region'])['text'] == 'ZX-581 arrival: Friday 18:37.'


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection')
def test_ordinary_query_entry_does_not_force_a_click_when_current_text_is_sufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'run'
    case = natural.Case('/inventory/K-039', 'K-039 还有多少？',
        '<h1>K-039</h1><p>Available: 29 units.</p><button aria-controls="info">Details</button><div id="info">More.</div>',
        ('29 units',))
    def replies() -> Iterator[JsonObject]:
        messages = '\n'.join(text_values(requests(directory)[-1]['messages']))
        assert 'Execute this next step:' not in messages
        yield submit('K-039 currently has 29 units available. [e1]')
    install_http(monkeypatch, replies(), native=False)
    row = run_case(directory, case, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert row['passed'], row
    receipts = [item.result for item in tool_receipts(directory / 'state')]
    assert sum(row.call.name == 'browser_snapshot' for row in receipts) == 1
    assert not any(row.call.name == 'browser_click' for row in receipts)


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection')
def test_failed_initial_observation_is_settled_once_and_normal_observation_can_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'run'
    original = BrowserSession.snapshot
    calls = 0

    def observe(self: BrowserSession, *, max_elements: int = 80, max_text: int = 12000,
                file_inputs_only: bool = False, scope_ref: str = "", selector: str = "",
                base_revision: int | None = None) -> BrowserToolOutput:
        nonlocal calls
        calls += 1
        if calls == 1:
            return BrowserToolOutput('Observation temporarily unavailable.', False, {})
        return original(self, max_elements=max_elements, max_text=max_text, file_inputs_only=file_inputs_only,
                        scope_ref=scope_ref, selector=selector, base_revision=base_revision)

    monkeypatch.setattr(BrowserSession, 'snapshot', observe)

    def replies() -> Iterator[JsonObject]:
        assert not query_choices(directory)
        yield {'tool': 'browser_snapshot', 'args': {}}
        chosen = next(row for row in query_choices(directory) if decode_json_object(row['context'])['label'] == 'Parcel ZX-581')
        yield decode_json_object(chosen['call'])
        yield submit(arrival_answer(directory))

    install_http(monkeypatch, replies(), native=False)
    row = run_case(directory, DYNAMIC_CASE, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert row['stop_reason'] == 'done', row
    receipts = [item.result for item in tool_receipts(directory / 'state')]
    assert [item.ok for item in receipts if item.call.name == 'browser_snapshot'] == [False, True]
    assert sum(item.call.name == 'browser_click' for item in receipts) == 1


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection')
def test_slow_query_continues_from_the_delivered_click_after_first_capture_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'run'
    case = replace(DYNAMIC_CASE, html=DYNAMIC_CASE.html.replace('},350)', '},6000)'))

    def replies() -> Iterator[JsonObject]:
        chosen = next(row for row in query_choices(directory) if decode_json_object(row['context'])['label'] == 'Parcel ZX-581')
        yield decode_json_object(chosen['call'])
        rows = tool_receipts(directory / 'state')
        first = next(item.result for item in reversed(rows) if item.result.call.name == 'browser_read')
        assert not first.ok and first.audit.get('query_click')
        assert decode_json_object(first.canonical['query_read'])['reason'] == 'query_update_unconfirmed'
        assert all(item.result.call.name != 'browser_click' or item.result.ok for item in rows)
        yield {'tool': 'browser_wait', 'args': {'condition': 'text', 'value': 'Friday 18:37',
                                               'timeout_ms': 10000, 'run_in_background': False}}
        yield {'tool': 'browser_read', 'args': {}}
        yield submit(arrival_answer(directory))

    install_http(monkeypatch, replies(), native=False)
    row = run_case(directory, case, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert row['stop_reason'] == 'done', row
    receipts = [item.result for item in tool_receipts(directory / 'state')]
    assert sum(item.call.name == 'browser_click' for item in receipts) == 1
    assert [item.ok for item in receipts if item.call.name == 'browser_read'] == [False, True]


@pytest.mark.parametrize('change', ['replace', 'relationship'])
def test_query_target_observed_before_selection_cannot_be_rebound_before_click(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], change: str,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, '<button aria-controls="result" onclick="window.clicked=true">Open</button>'
                     '<article id="result">Before</article><article id="other">Other</article>')
    control = elements(browser.snapshot().observation)[0]
    assert control['query_relation'] == 'aria-controls'
    script = ("document.getElementById('result').outerHTML='<article id=\"result\">Replacement</article>'"
              if change == 'replace' else "document.querySelector('button').setAttribute('aria-controls','other')")
    assert browser.evaluate(script).ok
    rejected = browser.click(require_json_string(control['ref'], 'ref'), query=True)
    assert not rejected.ok and rejected.observation['delivery'] == 'not_delivered'
    assert rejected.observation['reason'] in {'query_target_changed', 'stale_ref'}
    clicked = browser.inspect_state('Boolean(window.clicked)')
    assert clicked is False
