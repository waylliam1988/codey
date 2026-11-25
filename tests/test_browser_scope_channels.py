"""Target passages stay inside a verified range of the complete captured source."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.source_execution import open_source
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_context import fitting_full_read
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import restore_ledger_observation
from codey.research.passages import passage_ref, restore_passages
from codey.research.source_document import SourceDocument, SourceReadScope
from codey.research.source_gateway import OpenedSource
from codey.research.source_observation import source_observation
from codey.research.source_reading import render_source_read
from codey.research.source_snapshots import captured_source, document_snapshot, snapshot_document
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_query_scope_probe as probe
from tools.browser_leaf_exposure_ab import passage_texts
from tools.browser_query_scope_probe import CASES
from tools.completion_explanation_ab import Target


def capture_target_read(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], layout: str, *, duplicate: bool = False,
) -> ToolResult:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL}))[1]
    html = CASES[layout]
    if duplicate:
        html += '<article>预计周五 18:20 送达，回执 TARGET_173。</article>'
    install(browser, html)
    observed = browser.snapshot().observation
    contexts = observed['contexts']
    assert isinstance(contexts, list)
    target = next(row['id'] for row in contexts if isinstance(row, dict) and row['label'] == '订单 A-173')
    ref = require_json_string(next(row['ref'] for row in elements(observed) if row['context_id'] == target), 'ref')
    click = browser.click(ref, query=True)
    assert click.ok
    baseline = decode_json_object(click.observation['query_baseline'])
    assert require_json_integer(decode_json_object(baseline['target'])['backend_node_id'], 'backend_node_id') > 0
    session._memory_results['click'] = ToolResult(ToolCall('browser_click', {'ref': ref}), click.model_text, ok=True,
        canonical={'value': {'delivery': 'delivered'}}, audit={'query_baseline': baseline})
    receipt, ok, _ = delegate.execute(ToolCall('browser_read', {}))
    assert ok, receipt.model_text
    assert 'TARGET_173' in str(receipt.audit['query_region'])
    assert 'OTHER_174' not in str(receipt.audit['query_region'])
    assert 'OTHER_174' in str(decode_json_object(receipt.canonical['research_observation'])['text'])
    return receipt


@pytest.mark.parametrize('layout', CASES)
def test_real_target_capture_and_delivered_passages_are_separate_channels(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], layout: str,
) -> None:
    receipt = capture_target_read(production_browser, layout)
    observation = decode_json_object(receipt.canonical['research_observation'])
    source, text, _ = source_observation(observation)
    scope = source.read_scope
    assert scope is not None and scope.start is not None and scope.end is not None
    assert ' '.join(text[scope.start:scope.end].split()) == decode_json_object(receipt.audit['query_region'])['text']
    assert 'OTHER_174' in text
    assert 'Target range: confirmed' in receipt.model_text


@pytest.mark.parametrize('layout', CASES)
def test_targeted_delivery_excludes_the_neighboring_record(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], layout: str,
) -> None:
    receipt = capture_target_read(production_browser, layout)
    passages = passage_texts(receipt)
    assert passages and any('TARGET_173' in text for _, text in passages)
    assert all('OTHER_174' not in text for _, text in passages)
    assert 'OTHER_174' not in receipt.model_text


@pytest.mark.parametrize('layout', CASES)
def test_duplicate_target_text_falls_back_without_claiming_a_confirmed_range(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], layout: str,
) -> None:
    receipt = capture_target_read(production_browser, layout, duplicate=True)
    source, _, _ = source_observation(receipt.canonical['research_observation'])
    assert source.read_scope is not None and source.read_scope.start is None and source.read_scope.end is None
    assert 'Target range: unconfirmed' in receipt.model_text
    assert any('OTHER_174' in text for _, text in passage_texts(receipt))


def test_target_range_survives_expansion_continuation_and_historical_restore() -> None:
    prefix = 'Neighbor before OTHER_174.\n\n'
    target = 'Target 18:20 TARGET_173.\n\n' + ('Target details.\n\n' * 110) + 'Target end END_173.'
    text = prefix + target + '\n\nNeighbor after OTHER_174.'
    scope = SourceReadScope.locate(text, target)
    document = SourceDocument.html(requested_url=URL, final_url=URL, title='Orders', text=text, read_scope=scope)
    observation = document_snapshot(document)
    ledger = ResearchLedger()
    restore_ledger_observation(ledger, observation)
    assert 'read_scope' not in ledger.opened_sources_payload()[0]
    outcome = OpenedSource('ok', document=document, observation=observation)
    initial = render_source_read(outcome, ledger, offset=0, limit=500)
    assert initial.ok and 'TARGET_173' in initial.model_text and 'END_173' not in initial.model_text
    assert initial.read_passages[0]['start'] == len(prefix)
    assert '[more text available:' in initial.model_text
    session = TaskSession(TaskPolicy(frozenset({'control', 'web.read'})), task_kind='chat')
    extended = fitting_full_read(session, ledger, ToolCall('browser_read', {}), initial)
    assert extended.ok and 'END_173' in extended.model_text and 'OTHER_174' not in extended.model_text
    assert extended.read_passages[-1]['end'] == len(prefix) + len(target)
    assert '[End of captured target range;' in extended.model_text
    assert '[more text available:' not in extended.model_text
    tools = ResearchTools(None, None, None, ledger=ledger)
    continued = open_source(ToolCall('reopen_source', {'offset': initial.read_passages[-1]['end']}), tools, ledger, URL)
    assert continued.ok and 'END_173' in continued.model_text and 'OTHER_174' not in continued.model_text
    exhausted = render_source_read(outcome, ledger, offset=len(prefix) + len(target), limit=6000)
    assert not exhausted.ok and not exhausted.read_passages
    restore_passages(ledger, observation, list(extended.read_passages))
    ledger.record_open(URL, URL, 'New page', 'Current source NEW_999.')
    original, _, _ = source_observation(observation)
    historical = captured_source(ledger, URL, original.text_hash)
    assert snapshot_document(historical).read_scope == scope
    reread = tools.read_source(historical)
    assert reread.ok and 'TARGET_173' in reread.model_text and 'OTHER_174' not in reread.model_text
    restored = ResearchLedger()
    restore_ledger_observation(restored, historical)
    restore_passages(restored, historical, list(extended.read_passages))
    assert restored.read_passages and all('OTHER_174' not in p.excerpt and 'NEW_999' not in p.excerpt
                                         for p in restored.read_passages.values())
    forged = [{'ref': passage_ref(URL, original.text_hash, 0, len(text)), 'start': 0, 'end': len(text)}]
    with pytest.raises(ValueError, match='outside captured target range'):
        restore_passages(ResearchLedger(), observation, forged)


@pytest.mark.parametrize('text,target,confirmed', [
    ('Before\nTarget\t18:20  TARGET_173.\nAfter', 'Target 18:20 TARGET_173.', True),
    ('Same target. Same target.', 'Same target.', False),
    ('a a a', 'a a', False),
    ('Some other body.', 'Missing target.', False),
])
def test_range_mapping_is_unique_and_verified_in_the_source_receipt(text: str, target: str, confirmed: bool) -> None:
    scope = SourceReadScope.locate(text, target)
    assert (scope.start is not None) is confirmed
    document = SourceDocument.html(requested_url=URL, final_url=URL, title='Orders', text=text, read_scope=scope)
    observation = document_snapshot(document)
    assert snapshot_document(observation).read_scope == scope
    result = render_source_read(OpenedSource('ok', document=document, observation=observation),
                                ResearchLedger(), offset=0, limit=6000)
    assert result.ok
    assert ('Target range: confirmed' in result.model_text) is confirmed
    assert ('Target range: unconfirmed' in result.model_text) is (not confirmed)
    if not confirmed:
        assert text in result.model_text
    raw_source = decode_json_object(observation['source'])
    raw_scope = decode_json_object(raw_source['read_scope'])
    raw_source['read_scope'] = {**raw_scope, 'start': 0, 'end': len(text)}
    with pytest.raises(ValueError, match='scope differs'):
        source_observation({**observation, 'source': raw_source})


def test_pre_scope_source_receipts_remain_readable() -> None:
    document = SourceDocument.html(requested_url=URL, final_url=URL, title='Old receipt', text='Original body.')
    observation = document_snapshot(document)
    source = decode_json_object(observation['source'])
    del source['read_scope']
    legacy: JsonObject = {**observation, 'source': source}
    assert snapshot_document(legacy).read_scope is None
    restored = ResearchLedger()
    restore_ledger_observation(restored, legacy)
    assert restored.source_text_for_url(URL) == document.text


def test_initial_extension_continuation_and_recovery_use_the_actual_source_version() -> None:
    target = 'Target 18:20 TARGET_173.\n\n'
    text = target + ('Background paragraph with additional details.\n\n' * 110) + 'Neighbor 12:10 OTHER_174.'
    document = SourceDocument.html(requested_url=URL, final_url=URL, title='Captured orders', text=text)
    observation = document_snapshot(document)
    outcome = OpenedSource('ok', document=document, observation=observation)
    ledger = ResearchLedger()
    initial = render_source_read(outcome, ledger, offset=0, limit=500)
    assert initial.ok and 'TARGET_173' in initial.model_text and 'OTHER_174' not in initial.model_text
    session = TaskSession(TaskPolicy(frozenset({'control', 'web.read'})), task_kind='chat')
    extended = fitting_full_read(session, ledger, ToolCall('browser_read', {}), initial)
    assert 'OTHER_174' in extended.model_text
    continued = render_source_read(outcome, ledger, offset=initial.read_passages[-1]['end'], limit=6000)
    assert continued.ok and 'OTHER_174' in continued.model_text
    # A new live/source version cannot change the slices restored from this receipt.
    ledger.record_open(URL, URL, 'New page', 'Completely different current page NEW_999.')
    restored = ResearchLedger()
    restore_passages(restored, observation, list(extended.read_passages))
    assert any('OTHER_174' in passage.excerpt for passage in restored.read_passages.values())
    assert all('NEW_999' not in passage.excerpt for passage in restored.read_passages.values())
    damaged = [row.copy() for row in extended.read_passages]
    damaged[0]['start'] += 1
    with pytest.raises(ValueError, match='identity differs'):
        restore_passages(ResearchLedger(), observation, damaged)


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
def test_scope_runner_keeps_seeded_acquisition_separate_from_actual_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'scope'

    def replies() -> Iterator[JsonObject]:
        for _ in CASES:
            yield {'tool': 'done', 'args': {'summary': '预计 18:20 送达，回执 TARGET_173。[s1]'}}

    install_http(monkeypatch, replies())
    def target(_timeout: float, _url: str) -> Target:
        return Target('http://scripted.test/v1', 'fixture', 1, 15)
    monkeypatch.setattr(probe, 'select_target', target)
    monkeypatch.setattr(sys, 'argv', ['scope-probe', '--run-dir', str(directory), '--seeded-only'])
    assert probe.main() == 0
    raw: object = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
    assert isinstance(raw, list) and len(raw) == 2
    for value in raw:
        row = decode_json_object(value)
        assert row['targeted_read_reached'] is True and row['neighbor_in_target_passages'] is False
        # This scripted answer cites the source, so exact passage attribution remains unproven.
        assert row['scope_passed'] is False and row['model_calls'] == 1
    saved_validity: object = json.loads((directory / 'validity.json').read_text(encoding='utf-8'))
    validity = decode_json_object(saved_validity)
    assert validity['valid'] is True and validity['scripted_acquisitions'] == 2
