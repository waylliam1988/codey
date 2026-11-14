"""Saved provider entrances and committed values determine binding failure attribution."""
from __future__ import annotations

import json

import pytest

from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject
from tools import browser_fill_history_audit as audit

ORIGINAL = '用户说"明天到"\n请保留原文'
TASK = '备注请原样输入下面两行，再查询受理状态。\n' + ORIGINAL


@pytest.mark.parametrize('entrance', ['direct', 'nested'])
@pytest.mark.parametrize('frozen', [False, True])
@pytest.mark.parametrize('correct', [False, True])
def test_entrance_binding_availability_and_value_accuracy_are_independent(
    entrance: str, frozen: bool, correct: bool,
) -> None:
    action: JsonObject = ({'tool': 'browser_fill', 'args': {'ref': 'real', 'value': 'partial'}}
        if entrance == 'direct' else {'tool': 'codemode', 'args': {
            'code': 'return unwrap(await tools.browser_fill(inputs));', 'inputs': {'ref': 'real', 'value': 'partial'}}})
    response: JsonObject = {'protocol': 'text', 'text': json.dumps(action)}
    snapshot: JsonObject = {'browser_fill_text': ORIGINAL if frozen else None}
    fill = ToolResult(ToolCall('browser_fill', {'ref': 'real', 'value': ORIGINAL if correct else 'partial'}),
                      'Filled', ok=True)
    row = audit.classify(TASK, snapshot, response, [fill])
    assert row['entrance'] == entrance
    assert row['eligible_under_current_parser'] is True
    assert row['original_frozen_in_that_request'] is frozen
    assert row['mismatched_fills'] == int(not correct)
    assert row['nested_mismatch_with_frozen_original'] is (entrance == 'nested' and frozen and not correct)


def test_historical_binding_is_read_from_its_actual_snapshot_field() -> None:
    assert audit.frozen_original({'experiment_fill_binding': {'version': 1, 'source': 'task_text',
                                                             'text': ORIGINAL}}) == ORIGINAL
    assert audit.frozen_original({'experiment_fill_binding': {'source': 'other', 'text': ORIGINAL}}) is None
    assert audit.frozen_original({}) is None


def test_rejected_nested_program_is_not_a_transcription_failure() -> None:
    action: JsonObject = {'tool': 'codemode', 'args': {'code': 'broken program', 'inputs': {}}}
    response: JsonObject = {'protocol': 'text', 'text': json.dumps(action)}
    row = audit.classify(TASK, {'browser_fill_text': ORIGINAL}, response, [])
    assert row['entrance'] == 'nested'
    assert row['successful_fills'] == row['mismatched_fills'] == 0
    assert row['nested_mismatch_with_frozen_original'] is False


def test_ambiguous_task_is_not_compared_with_a_guessed_original() -> None:
    response: JsonObject = {'protocol': 'text', 'text': '{"tool":"browser_fill","args":{"ref":"real","value":"x"}}'}
    fill = ToolResult(ToolCall('browser_fill', {'ref': 'real', 'value': 'x'}), 'Filled', ok=True)
    row = audit.classify('请在两个字段分别填写不同内容。', {}, response, [fill])
    assert row['values_comparable'] is False and row['mismatched_fills'] == 0
