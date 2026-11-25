"""Scope diagnostics distinguish source bytes, delivered passages and answer contamination."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.research.passages import passage_ref
from codey.research.source_observation import source_observation
from codey.runtime.core.models import ToolCall, ToolResult
from tools import browser_query_scope_probe as probe
from tools.browser_leaf_exposure_ab import evidence_checks
from tools.completion_artifact_audit import EffectReceipt


@pytest.mark.parametrize('show_neighbor', [False, True])
def test_neighbor_in_source_is_not_automatically_neighbor_in_delivered_passage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, show_neighbor: bool,
) -> None:
    target = 'Target 18:20 TARGET_173.'
    text = target + '\nNeighbor OTHER_174.'
    ledger = ResearchLedger()
    ledger.record_open('https://example.com/scope', 'https://example.com/scope', 'Scope', text)
    observation = ledger_observation(ledger, 'browser_read', url='https://example.com/scope')
    source, _, _ = source_observation(observation)
    end = len(text) if show_neighbor else len(target)
    ref = passage_ref(source.final_url, source.text_hash, 0, end)
    result = ToolResult(ToolCall('browser_read', {}), 'window', ok=True,
        canonical={'research_observation': observation,
                   'read_passages': [{'ref': ref, 'start': 0, 'end': end}]},
        audit={'query_region': {'text': target}})

    def receipts(_state: Path) -> list[EffectReceipt]:
        return [EffectReceipt('original-read', result, None)]
    monkeypatch.setattr(probe, 'tool_receipts', receipts)
    row = probe.audit(tmp_path, {'answer': 'TARGET_173'})
    assert row['targeted_read_reached'] is True
    assert row['target_read_correct'] is True
    assert row['neighbor_in_target_passages'] is show_neighbor
    assert row['neighbor_in_answer'] is False


def test_ordinary_read_is_not_counted_as_targeted_readback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = ToolResult(ToolCall('browser_read', {}), 'TARGET_173', ok=True)

    def receipts(_state: Path) -> list[EffectReceipt]:
        return [EffectReceipt('ordinary-read', result, None)]
    monkeypatch.setattr(probe, 'tool_receipts', receipts)
    row = probe.audit(tmp_path, {'answer': 'OTHER_174'})
    assert row['targeted_read_reached'] is False
    assert row['neighbor_in_answer'] is True


def test_target_marker_in_region_metadata_cannot_replace_actual_target_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = ResearchLedger()
    url = 'https://example.com/scope'
    ledger.record_open(url, url, 'Scope', 'Target 18:20 TARGET_173.')
    result = ToolResult(ToolCall('browser_read', {}), 'Read', ok=True,
        canonical={'research_observation': ledger_observation(ledger, 'browser_read', url=url), 'read_passages': []},
        audit={'query_region': {'label': '18:20 TARGET_173', 'text': 'Not loaded yet.'}})

    def receipts(_state: Path) -> list[EffectReceipt]:
        return [EffectReceipt('read', result, None)]
    monkeypatch.setattr(probe, 'tool_receipts', receipts)
    assert probe.audit(tmp_path, {})['target_read_correct'] is False


@pytest.mark.parametrize('citation', ['target', 'neighbor', 'source'])
def test_scope_score_recomputes_cited_ranges_instead_of_trusting_cached_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, citation: str,
) -> None:
    target = 'Target 18:20 TARGET_173.\n'
    text = target + 'Neighbor 12:10 OTHER_174.'
    ledger = ResearchLedger()
    url = 'https://example.com/scope'
    ledger.record_open(url, url, 'Scope', text)
    observation = ledger_observation(ledger, 'browser_read', url=url)
    source, _, _ = source_observation(observation)
    target_ref = passage_ref(url, source.text_hash, 0, len(target))
    neighbor_ref = passage_ref(url, source.text_hash, len(target), len(text))
    result = ToolResult(ToolCall('browser_read', {}), 'Delivered', ok=True,
        canonical={'source_id': 's1', 'research_observation': observation, 'read_passages': [
            {'ref': target_ref, 'start': 0, 'end': len(target)},
            {'ref': neighbor_ref, 'start': len(target), 'end': len(text)}]}, audit={'query_region': {'text': target}})

    def receipts(_state: Path) -> list[EffectReceipt]:
        return [EffectReceipt('original-read', result, None)]
    monkeypatch.setattr(probe, 'tool_receipts', receipts)
    ref = {'target': target_ref, 'neighbor': neighbor_ref, 'source': 's1'}[citation]
    row = probe.audit(tmp_path, {'answer': f'18:20 TARGET_173 [{ref}]', 'passed': True,
                                'evidence': {'correct_evidence_cited': True, 'exact_passage_cited': True}})
    evidence = row['evidence']
    assert isinstance(evidence, dict)
    assert row['passed'] is (citation != 'neighbor')
    assert evidence['correct_evidence_cited'] is (citation != 'neighbor')
    assert evidence['exact_passage_cited'] is (citation == 'target')


@pytest.mark.parametrize('wrapper', ['`{ref}`', '``{ref}``', '```text\n{ref}\n```', '~~~\n{ref}\n~~~', '    {ref}'])
@pytest.mark.parametrize('source_label', [False, True])
def test_code_only_labels_do_not_count_as_answer_citations(wrapper: str, source_label: bool) -> None:
    url, text = 'https://example.com/scope', 'Target 18:20 TARGET_173.'
    ledger = ResearchLedger()
    ledger.record_open(url, url, 'Scope', text)
    observation = ledger_observation(ledger, 'browser_read', url=url)
    source, _, _ = source_observation(observation)
    ref = passage_ref(url, source.text_hash, 0, len(text))
    result = ToolResult(ToolCall('browser_read', {}), 'Delivered', ok=True, canonical={
        'source_id': 's1', 'research_observation': observation,
        'read_passages': [{'ref': ref, 'start': 0, 'end': len(text)}]})
    label = '[s1]' if source_label else f'[{ref}]'
    answer = f'18:20 TARGET_173\n\n{wrapper.format(ref=label)}'
    score = evidence_checks([result], answer, ('18:20', 'TARGET_173'))
    assert score['correct_evidence_cited'] is False and score['exact_passage_cited'] is False
    prose = evidence_checks([result], answer + f'\n\nThe actual evidence is {label}.', ('18:20', 'TARGET_173'))
    assert prose['correct_evidence_cited'] is True
    assert prose['exact_passage_cited'] is (not source_label)


@pytest.mark.parametrize('mixed,source_only', [(False, False), (True, False), (False, True)])
def test_scope_delivery_and_actually_cited_passage_are_scored_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mixed: bool, source_only: bool,
) -> None:
    url = 'https://example.com/record-scope'
    target = '18:20 TARGET_173.\n'
    text = target + '12:10 OTHER_174.'
    ledger = ResearchLedger()
    ledger.record_open(url, url, 'Orders', text)
    observation = ledger_observation(ledger, 'browser_read', url=url)
    source, _, _ = source_observation(observation)
    end = len(text) if mixed else len(target)
    ref = passage_ref(url, source.text_hash, 0, end)
    result = ToolResult(ToolCall('browser_read', {}), 'Window', ok=True,
        canonical={'source_id': 's1', 'research_observation': observation,
                   'read_passages': [{'ref': ref, 'start': 0, 'end': end}]},
        audit={'query_region': {'text': target}})

    def receipts(_state: Path) -> list[EffectReceipt]:
        return [EffectReceipt('read', result, None)]
    monkeypatch.setattr(probe, 'tool_receipts', receipts)
    row = probe.audit(tmp_path, {'passed': True, 'answer': f'18:20 TARGET_173 [{"s1" if source_only else ref}]'})
    assert row['passed'] is True  # Existing task/source success remains a separate metric.
    assert row['scope_passed'] is (not mixed and not source_only)
    assert row['cited_target_passage_contains_neighbor'] is (mixed and not source_only)
    assert row['exact_target_passage_supported'] is (not source_only)
