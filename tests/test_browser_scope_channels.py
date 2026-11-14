"""Characterize existing source windows separately from a proposed target boundary."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_context import fitting_full_read
from codey.research.ledger import ResearchLedger
from codey.research.passages import restore_passages
from codey.research.source_document import SourceDocument
from codey.research.source_gateway import OpenedSource
from codey.research.source_reading import render_source_read
from codey.research.source_snapshots import document_snapshot
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
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], layout: str,
) -> ToolResult:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall('browser_navigate', {'url': URL}))[1]
    install(browser, CASES[layout])
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
    # Characterization of the current gap, not an assertion of isolation.
    assert any('OTHER_174' in text for _, text in passage_texts(receipt))


@pytest.mark.xfail(strict=True, reason='Existing targeted browser read delivers neighboring record passages.')
@pytest.mark.parametrize('layout', CASES)
def test_targeted_delivery_excludes_the_neighboring_record(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], layout: str,
) -> None:
    receipt = capture_target_read(production_browser, layout)
    assert all('OTHER_174' not in text for _, text in passage_texts(receipt))


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
        assert row['targeted_read_reached'] is True and row['neighbor_in_target_passages'] is True
        assert row['scope_passed'] is False and row['model_calls'] == 1
    saved_validity: object = json.loads((directory / 'validity.json').read_text(encoding='utf-8'))
    validity = decode_json_object(saved_validity)
    assert validity['valid'] is True and validity['scripted_acquisitions'] == 2
