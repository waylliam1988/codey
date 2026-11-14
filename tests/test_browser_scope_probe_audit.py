"""Scope diagnostics distinguish source bytes, delivered passages and answer contamination."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.runtime.core.models import ToolCall, ToolResult
from tools import browser_query_scope_probe as probe
from tools.completion_artifact_audit import EffectReceipt


@pytest.mark.parametrize('show_neighbor', [False, True])
def test_neighbor_in_source_is_not_automatically_neighbor_in_delivered_passage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, show_neighbor: bool,
) -> None:
    target = 'Target TARGET_173.'
    text = target + '\nNeighbor OTHER_174.'
    result = ToolResult(ToolCall('browser_read', {}), 'window', ok=True,
        canonical={'research_observation': {'text': text},
                   'read_passages': [{'start': 0, 'end': len(text) if show_neighbor else len(target)}]},
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
