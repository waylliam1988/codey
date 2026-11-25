"""Validate experiment admission, passive tracing and metrics before live sampling."""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_protocol import normalize_turn, refresh_snapshot_contract
from codey.operations.kernel_facts import project_execution_record
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_fill_history_audit as history
from tools import browser_result_adoption_ab as ab
from tools.browser_production_live_gate import URL
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target, append_json, production_source_digest

GRANTS = frozenset({'control', 'web.read', 'browser.observe', 'browser.interact', 'browser.effect'})
DOCUMENT: JsonObject = {'target_id': 'tab', 'loader_id': 'document', 'url': URL}


def session_with_snapshot(*, checks: tuple[str, ...] = (ab.WAIT,), multiple: bool = False) -> TaskSession:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks), task_kind='chat', task_text='Click Details for the requested record.')
    elements: list[JsonObject] = [{'role': 'button', 'name': 'Details', 'ref': 'current-one',
        'disabled': False, 'context_id': 'one'}]
    if multiple:
        elements.append({'role': 'button', 'name': 'Details', 'ref': 'current-two',
            'disabled': False, 'context_id': 'two'})
    result = ToolResult(ToolCall('browser_snapshot', {}), 'Complete controls', ok=True,
        canonical={'value': {'generation': 7, 'complete': True, 'elements_truncated': False,
            'elements': elements, 'contexts': [{'id': 'one', 'label': 'Order A-173'},
                                             {'id': 'two', 'label': 'Order A-174'}]}},
        audit={'browser_document': DOCUMENT})
    session._memory_results['snapshot'] = result
    project_execution_record(session, 'snapshot', result.call, result, ok=True)
    return session


def test_complete_result_handoff_uses_the_public_composed_contract() -> None:
    session = session_with_snapshot()
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    action = ab.result_call(snapshot, {'ref': 'current-one', 'purpose': 'result'})
    assert action is not None
    plan = normalize_turn(decode_text_turn(json.dumps(action)), snapshot=snapshot)
    assert not plan.protocol_error, plan.protocol_error
    assert plan.calls[0].name == 'codemode'


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('args,available', [({'ref': 'current-one', 'purpose': 'result'}, True),
    ({'ref': 'current-one', 'purpose': 'invented'}, False), ({'purpose': 'result'}, False),
    ({'ref': 'current-one', 'purpose': 'result', 'extra': True}, False)])
def test_handoff_keeps_each_frozen_interface_validation(nested: bool, args: JsonObject, available: bool) -> None:
    session = session_with_snapshot()
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    if nested:
        snapshot = refresh_snapshot_contract(replace(snapshot,
            tool_names=tuple(name for name in snapshot.tool_names if name != 'browser_click')))
    action = ab.result_call(snapshot, args)
    assert (action is not None) is available
    if available:
        assert action is not None
        assert action['tool'] == ('codemode' if nested else 'browser_click')
        assert not normalize_turn(decode_text_turn(json.dumps(action)), snapshot=snapshot).protocol_error


def test_handoff_does_not_offer_result_for_a_bare_wait() -> None:
    session = session_with_snapshot(checks=(require_tool_success('browser_wait'),))
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    assert ab.result_call(snapshot, {'ref': 'real', 'purpose': 'result'}) is None


def test_hint_has_real_candidates_without_selecting_between_equal_names() -> None:
    session = session_with_snapshot(multiple=True)
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    hint = ab.result_hint(session, snapshot, CompletionContext(browser_document=DOCUMENT), 7)
    lines = [parse_json_object(line) for line in hint.splitlines()[1:]]
    assert len(lines) == 2
    assert [decode_json_object(row['context'])['label'] for row in lines] == ['Order A-173', 'Order A-174']
    for row in lines:
        action = decode_json_object(row['call'])
        assert not normalize_turn(decode_text_turn(json.dumps(action)), snapshot=snapshot).protocol_error
    assert 'QUARTZ_851' not in hint and '173 克' not in hint


@pytest.mark.parametrize('guard', ['bare_wait', 'generation', 'document', 'truncated', 'incomplete',
                                  'delivered', 'uncertain', 'missing_fill', 'denied'])
def test_hint_requires_a_current_complete_executable_first_query(guard: str) -> None:
    checks = ((require_tool_success('browser_wait'),) if guard == 'bare_wait' else
              (ab.WAIT, require_tool_success('browser_fill')) if guard == 'missing_fill' else (ab.WAIT,))
    session = session_with_snapshot(checks=checks)
    generation = 8 if guard == 'generation' else 7
    document = {**DOCUMENT, 'loader_id': 'other'} if guard == 'document' else DOCUMENT
    if guard in {'truncated', 'incomplete'}:
        old = session._memory_results['snapshot']
        value = decode_json_object(old.canonical['value'])
        if guard == 'incomplete':
            value['complete'] = False
        session._memory_results['snapshot'] = replace(old, truncated=guard == 'truncated', canonical={'value': value})
    if guard in {'delivered', 'uncertain'}:
        click = ToolResult(ToolCall('browser_click', {'ref': 'previous'}), 'Sent', ok=guard == 'delivered',
            canonical={'value': {'delivery': guard}}, audit={'browser_document': DOCUMENT})
        session._memory_results['click'] = click
        project_execution_record(session, 'click', click.call, click, ok=click.ok)
    if guard == 'denied':
        session.controller_denied = ('browser_read',)
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    assert ab.result_hint(session, snapshot, CompletionContext(browser_document=document), generation) == ''


def test_invocation_metrics_do_not_count_failed_or_unsettled_result_proposals() -> None:
    row: JsonObject = {'call_id': 'parent:child:0', 'parent': 'parent', 'name': 'browser_click',
                      'args': {'ref': 'bad', 'purpose': 'result'}, 'ok': False, 'followups': []}
    failed = ToolResult(ToolCall('browser_click', {'ref': 'bad'}, 'parent:child:0'), 'Stale', ok=False,
                        canonical={'value': {'delivery': 'not_delivered'}})
    metrics = ab.invocation_metrics([row], [failed])
    assert metrics['result_invocations'] == 1 and metrics['result_delivered'] == 0
    assert metrics['result_query_baselines'] == 0 and metrics['delivered_clicks'] == 0
    with pytest.raises(ValueError, match='no original committed receipt'):
        ab.invocation_metrics([row], [])


def test_manual_reads_in_a_result_program_remain_manual() -> None:
    names = ['browser_read', 'browser_click', 'browser_wait', 'browser_read', 'browser_read']
    args: list[JsonObject] = [{}, {'ref': 'real', 'purpose': 'result'},
        {'condition': 'text', 'value': 'Ready', 'timeout_ms': 5000}, {}, {}]
    invocations: list[JsonObject] = []
    receipts: list[ToolResult] = []
    for i, name in enumerate(names):
        invocation: JsonObject = {'call_id': f'parent:child:{i}', 'parent': 'parent', 'name': name,
                                  'args': args[i], 'ok': True, 'followups': []}
        if i == 1:
            invocation['followups'] = [{'name': 'browser_wait', 'args': args[2]}, {'name': 'browser_read', 'args': {}}]
        invocations.append(invocation)
        receipts.append(ToolResult(ToolCall(name, args[i], f'parent:child:{i}'), 'Observed', ok=True,
            canonical={'value': {'delivery': 'delivered'}} if name == 'browser_click' else {}))
    counts = ab.invocation_metrics(invocations, receipts)
    assert counts['manual_wait_calls'] == 0 and counts['manual_read_calls'] == 2
    assert counts['manual_read_requests'] == 1
    invocations[2]['name'] = 'browser_snapshot'
    with pytest.raises(ValueError, match='differs from its original committed receipt'):
        ab.invocation_metrics(invocations, receipts)


def test_direct_reads_without_a_host_trace_are_counted_as_manual_requests() -> None:
    # Production outer receipts can retain an empty ToolCall.call_id; their
    # separate durable receipts still represent separate model requests.
    receipts = [ToolResult(ToolCall('browser_read', {}), 'Read', ok=True) for _ in range(2)]
    counts = ab.invocation_metrics([], receipts)
    assert counts['manual_read_calls'] == counts['manual_read_requests'] == 2
    assert counts['manual_wait_calls'] == counts['result_invocations'] == 0


def test_followups_must_match_the_captured_plan_even_with_valid_receipts() -> None:
    click: JsonObject = {'call_id': 'p:child:0', 'parent': 'p', 'name': 'browser_click',
        'args': {'ref': 'real', 'purpose': 'result'}, 'ok': True,
        'followups': [{'name': 'browser_wait', 'args': {'condition': 'text', 'value': 'Ready'}}]}
    read: JsonObject = {'call_id': 'p:child:1', 'parent': 'p', 'name': 'browser_read',
                        'args': {}, 'ok': True}
    receipts = [ToolResult(ToolCall(str(row['name']), {}, str(row['call_id'])), 'OK', ok=True)
                for row in (click, read)]
    with pytest.raises(ValueError, match='follow-up sequence'):
        ab.invocation_metrics([click, read], receipts)


def test_source_or_harness_drift_prevents_live_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ab, 'production_source_digest', lambda: 'changed')
    with pytest.raises(RuntimeError, match='complete paired matrix'):
        ab.assert_stable('original', 'anything')


def test_hidden_retries_and_scripted_proposals_cannot_pass_live_audit(tmp_path: Path) -> None:
    target = Target('http://fixture/v1', 'fixture', 1, 10)
    response: JsonObject = {'choices': [{'message': {'content': '{"tool":"browser_read","args":{}}'}}]}
    request: JsonObject = {'type': 'wire_attempt', 'phase': 'request', 'attempt': 2, 'payload': {
        'model': target.model, 'seed': 1, 'temperature': 0, 'stream': False}}
    append_json(tmp_path / 'provider.jsonl', request)
    append_json(tmp_path / 'provider.jsonl', {'type': 'response', 'payload': response})
    with pytest.raises(ValueError, match='Hidden retry'):
        ab.audit_unit(tmp_path, target)
    append_json(tmp_path / 'provider.jsonl', {'type': 'scripted_acquisition'})
    with pytest.raises(ValueError, match='Scripted model proposals'):
        ab.audit_unit(tmp_path, target)


def test_cost_comparison_keeps_failed_pairs_out_of_common_success_costs() -> None:
    rows: list[JsonObject] = []
    for scenario, passed in [('basic', (True, True)), ('stale_ref', (False, True)), ('reread', (True, False))]:
        for arm, success in zip(ab.ARMS, passed, strict=True):
            rows.append({'scenario': scenario, 'wording': 'original', 'arm': arm, 'passed': success,
                         'model_calls': 5, 'prompt_tokens': 100, 'completion_tokens': 20})
    result = ab.paired_comparison(rows)
    assert result['gained'] == result['lost'] == result['common_successes'] == 1
    costs = decode_json_object(result['common_success_costs_menu_then_hint'])
    assert costs['model_calls'] == [5, 5]


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
@pytest.mark.parametrize('arm', ab.ARMS)
@pytest.mark.parametrize('reread', [False, True])
def test_scripted_chromium_proves_presentation_only_and_measured_receipt_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, reread: bool,
) -> None:
    directory = tmp_path / arm

    def replies() -> Iterator[JsonObject]:
        if reread:
            yield {'tool': 'browser_read', 'args': {}}
        yield {'tool': 'codemode', 'args': {'code': '''let s=unwrap(await tools.browser_snapshot({}));
unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));
return unwrap(await tools.browser_snapshot({}));''', 'inputs': {}}}
        receipts = [item.result for item in tool_receipts(directory / 'state')
                    if item.result.call.name == 'browser_snapshot' and item.result.ok]
        raw = decode_json_object(receipts[-1].canonical['value'])['elements']
        assert isinstance(raw, list)
        ref = next(row['ref'] for row in raw if isinstance(row, dict) and row.get('name') == 'Load record')
        if arm == 'call_hint':
            hint = require_json_string(ab.jsonl(directory / 'presentation.jsonl')[-1]['hint'], 'hint')
            candidates = [parse_json_object(line) for line in hint.splitlines()[1:]]
            assert len(candidates) == 1 and candidates[0]['name'] == 'Load record'
            action = decode_json_object(candidates[0]['call'])
            assert decode_json_object(action['args']) == {
                'ref': {'name': 'Load record', 'role': 'button'}, 'purpose': 'result'}
            yield action
        else:
            yield {'tool': 'browser_click', 'args': {'ref': ref, 'purpose': 'result'}}
        if reread:
            yield {'tool': 'browser_read', 'args': {}}
        yield {'tool': 'done', 'args': {'summary': '实际净重 173 克，校验码 QUARTZ_851。'}}

    install_http(monkeypatch, replies())
    row = ab.run_unit(directory, 'reread' if reread else 'basic', 'original', arm,
        Target('http://scripted.test/v1', 'fixture', 123456789, 15),
        production_source_digest(), ab.harness_digest())
    assert row['passed'], row
    assert row['result_invocations'] == row['result_delivered'] == row['result_query_baselines'] == 1
    # Only the first read after the query consumes its pending binding.
    assert row['query_linked_reads'] == 1
    assert row['delivered_clicks'] == 1
    assert row['manual_wait_requests'] == 0
    assert row['manual_read_requests'] == row['manual_read_calls'] == 2 * int(reread)
    reads = [item.result for item in tool_receipts(directory / 'state')
             if item.result.call.name == 'browser_read']
    assert len(reads) == 1 + 2 * int(reread)  # Result follow-up plus optional manual reads.
    assert row['model_calls'] == 3 + 2 * int(reread)
    presentation = ab.jsonl(directory / 'presentation.jsonl')
    hints = [require_json_string(item['hint'], 'hint') for item in presentation]
    assert any(bool(hint) for hint in hints) is (arm == 'call_hint')
    assert any(item['eligible'] is True for item in presentation)
    assert row['eligible_requests'] == 1
    assert row['hinted_requests'] == int(arm == 'call_hint')
    totals = decode_json_object(ab.aggregate([row])[arm])
    assert totals['eligible_units'] == 1
    assert totals['hinted_units'] == int(arm == 'call_hint')
    historical = history.audit_directory(directory)
    assert len(historical) == 1
    assert historical[0]['entrance'] == 'nested'
    assert historical[0]['actual_values'] == ['福州']
    assert historical[0]['successful_fills'] == 1
