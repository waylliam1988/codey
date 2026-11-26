"""Existing browser execution accepts omitted timeouts without replacing CodeMode."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from codey.codemode.host import CodeModeHost
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.browser.browser_task_handoff import initial_navigation
from codey.operations.browser.browser_waits import complete_wait_args
from codey.operations.browser.catalog import snapshot_catalog
from codey.operations.browser.turn_surface import offer_browser_call
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.research.research_submission_handoff import research_browser_handoff
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object
from tests.test_declared_work_continuation import GRANTS, URL, record


def wait_result(call: ToolCall) -> ToolResult:
    return ToolResult(call, 'matched', ok=True, canonical={'value': {
        'action': 'wait', 'condition': call.args['condition'], 'matched': True,
        'text': 'Ready', 'page_url': URL}})


@pytest.mark.parametrize('surface', ['direct', 'lowered', 'javascript'])
@pytest.mark.parametrize(('declared', 'explicit', 'expected'), [(None, None, 5000), (8200, None, 8200), (8200, 2300, 2300)])
def test_wait_defaults_are_frozen_before_validation_and_formal_delivery(
    surface: str, declared: int | None, explicit: int | None, expected: int,
) -> None:
    required: JsonObject = {'condition': 'text', 'value': 'Ready'}
    if declared is not None:
        required['timeout_ms'] = declared
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success('browser_wait', arguments=required),)))
    snapshot = restore_snapshot(snapshot_description(offer_browser_call(build_turn_snapshot(session), session, 'browser_wait')))
    args: JsonObject = {'condition': 'text', 'value': 'Ready'}
    if explicit is not None:
        args['timeout_ms'] = explicit
    delivered: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        delivered.append(call)
        return wait_result(call)

    if surface == 'direct':
        calls = [ToolCall('browser_wait', args, 'direct-wait')]
    elif surface == 'lowered':
        import json

        reply: JsonObject = {'tool': 'browser_wait', 'args': args}
        normalized = normalize_turn(decode_text_turn(json.dumps(reply)), snapshot=snapshot)
        assert not normalized.protocol_error
        calls = list(normalized.calls)
    else:
        calls = [ToolCall('codemode', {'code': 'return unwrap(await tools.browser_wait(inputs));', 'inputs': args}, 'js-wait')]
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id='wait', effect_scope='task', turn=1,
                                         executors={'browser_wait': execute})
    result = execute_turn(session, calls, snapshot=snapshot, run_id='wait', turn=1,
                          executors={'browser_wait': execute, 'codemode': parent})[0]
    assert result.ok, result.model_text
    assert len(delivered) == 1 and delivered[0].args == {**args, 'timeout_ms': expected}
    receipt = next(row for row in session._memory_results.values() if row.call.name == 'browser_wait')
    assert receipt.call.args['timeout_ms'] == expected


@pytest.mark.parametrize('timeout', ['5000', True, 99, 30001, None])
def test_invalid_explicit_timeout_is_not_replaced_or_delivered(timeout: object) -> None:
    session = TaskSession(TaskPolicy(GRANTS))
    catalog = snapshot_catalog(build_turn_snapshot(session))
    delivered: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        delivered.append(call)
        return wait_result(call)

    host = CodeModeHost(execution_id='invalid-wait', catalog=catalog.entries, execute=execute, allow_writes=True)
    with pytest.raises(ValueError):
        host.invoke('browser_wait', {'condition': 'text', 'value': 'Ready', 'timeout_ms': timeout})
    assert not delivered


def test_wait_replay_receives_the_same_completed_arguments() -> None:
    session = TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success('browser_wait',
        arguments={'condition': 'text', 'value': 'Ready', 'timeout_ms': 8200}),)))
    catalog = snapshot_catalog(restore_snapshot(snapshot_description(build_turn_snapshot(session))))
    seen: list[JsonObject] = []

    def replay(index: int, name: str, args: JsonObject) -> ToolResult:
        assert index == 0 and name == 'browser_wait'
        seen.append(args)
        return wait_result(ToolCall(name, args))

    host = CodeModeHost(execution_id='replay', catalog=catalog.entries, execute=wait_result, replay=replay, allow_writes=True)
    assert host.invoke('browser_wait', {'condition': 'text', 'value': 'Ready'})
    assert seen == [{'condition': 'text', 'value': 'Ready', 'timeout_ms': 8200}]
    assert complete_wait_args({'value': 'Ready'}) == {'value': 'Ready'}


def test_one_javascript_program_uses_the_stage_reached_by_each_committed_click() -> None:
    checks = tuple(require_tool_success('browser_wait', arguments={
        'condition': 'text', 'value': 'Ready', 'timeout_ms': timeout}, after='page_actions', stage=stage)
        for stage, timeout in ((1, 8200), (2, 13300)))
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {'target_id': 'tab', 'frame_id': 'main', 'url': URL, 'loader_id': 'same-page'}
    record(session, 'browser_navigate', {'url': URL}, document)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    delivered: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        delivered.append(call)
        if call.name == 'browser_wait':
            result = wait_result(call)
        else:
            result = ToolResult(call, 'clicked', ok=True, canonical={'value': {
                'action': 'click', 'delivery': 'delivered', 'text': 'clicked', 'ref': call.args['ref'],
                'observation': 'current_page', 'effect_verified': False, 'page_url': URL}})
        return replace(result, audit={'browser_document': document})

    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id='stages', effect_scope='task', turn=1,
        executors={'browser_click': execute, 'browser_wait': execute})
    code = ('await tools.browser_click({ref:"first"}); await tools.browser_wait({condition:"text",value:"Ready"});'
            'await tools.browser_click({ref:"second"}); return unwrap(await tools.browser_wait({condition:"text",value:"Ready"}));')
    result = execute_turn(session, [ToolCall('codemode', {'code': code}, 'two-stages')], snapshot=snapshot,
                          run_id='stages', effect_scope='task', turn=1, executors={'codemode': parent})[0]
    assert result.ok, result.model_text
    assert [row.args['timeout_ms'] for row in delivered if row.name == 'browser_wait'] == [8200, 13300]


def test_wait_replay_keeps_original_timeout_after_current_stage_changed() -> None:
    checks = tuple(require_tool_success('browser_wait', arguments={
        'condition': 'text', 'value': 'Ready', 'timeout_ms': timeout}, after='page_actions', stage=stage)
        for stage, timeout in ((1, 8200), (2, 13300)))
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    original: JsonObject = {'condition': 'text', 'value': 'Ready', 'timeout_ms': 8200}
    seen: list[JsonObject] = []

    def replay(index: int, name: str, args: JsonObject) -> ToolResult:
        assert index == 0 and name == 'browser_wait'
        seen.append(args)
        return wait_result(ToolCall(name, args))

    host = CodeModeHost(execution_id='original-stage', catalog=snapshot_catalog(build_turn_snapshot(session)).entries,
        execute=wait_result, replay=replay, allow_writes=True, browser_stage=lambda: 2,
        original_browser_wait=lambda _index: original)
    assert host.invoke('browser_wait', {'condition': 'text', 'value': 'Ready'})
    assert seen == [original]


def test_formal_wait_return_repair_keeps_original_timeout_without_redelivery() -> None:
    checks = tuple(require_tool_success('browser_wait', arguments={
        'condition': 'text', 'value': 'Ready', 'timeout_ms': timeout}, after='page_actions', stage=stage)
        for stage, timeout in ((1, 8200), (2, 13300)))
    session = TaskSession(TaskPolicy(GRANTS, required_checks=checks))
    document: JsonObject = {'target_id': 'tab', 'frame_id': 'main', 'url': URL, 'loader_id': 'same-page'}
    record(session, 'browser_navigate', {'url': URL}, document)
    record(session, 'browser_click', {}, document)
    delivered: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        delivered.append(call)
        return replace(wait_result(call), audit={'browser_document': document})

    def run(turn: int, args: JsonObject) -> ToolResult:
        snapshot = build_turn_snapshot(session)
        parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id='return-repair',
            effect_scope='task', turn=turn, executors={'browser_wait': execute})
        return execute_turn(session, [ToolCall('codemode', args, f'parent-{turn}')], snapshot=snapshot,
            run_id='return-repair', effect_scope='task', turn=turn, executors={'codemode': parent})[0]

    failed = run(1, {'code': 'const result = unwrap(await tools.browser_wait({condition:"text",value:"Ready"})); return result.absent;'})
    assert not failed.ok and len(delivered) == 1 and delivered[0].args['timeout_ms'] == 8200
    token = decode_json_object(failed.canonical['repair'])['token']
    assert isinstance(token, str)
    record(session, 'browser_click', {}, document)
    repaired = run(2, {'repair_of': token, 'code': 'return unwrap(await tools.browser_wait({condition:"text",value:"Ready"}));'})
    assert repaired.ok, repaired.model_text
    assert len(delivered) == 1


def test_research_declared_navigation_observes_controls_before_reading() -> None:
    session = TaskSession(TaskPolicy(GRANTS, strict_research=True,
        required_checks=(require_tool_success('browser_navigate'),)), task_kind='research',
        task_text=f'Read the record.\n{URL}', required_source_urls=frozenset({URL}))
    snapshot = build_turn_snapshot(session)
    call = initial_navigation(session, snapshot)
    assert call is not None and call.name == 'codemode'
    code = call.args['code']
    assert isinstance(code, str)
    assert code.index('browser_navigate') < code.index('browser_snapshot') < code.index('browser_read')


def test_reloaded_document_does_not_inherit_old_fill_and_click_progress() -> None:
    checks = tuple(require_tool_success(name) for name in ('browser_navigate', 'browser_fill', 'browser_click', 'browser_read'))
    checks += (require_tool_success('browser_wait', arguments={'condition': 'text', 'value': 'Ready'}),)
    session = TaskSession(TaskPolicy(GRANTS, strict_research=True, required_checks=checks), task_kind='research',
                          task_text=f'Investigate the record.\n{URL}', required_source_urls=frozenset({URL}))
    old: JsonObject = {'target_id': 'tab', 'frame_id': 'main', 'url': URL, 'loader_id': 'old'}
    new: JsonObject = {'target_id': 'tab', 'frame_id': 'main', 'url': URL, 'loader_id': 'new'}
    for name in ('browser_navigate', 'browser_fill', 'browser_click'):
        record(session, name, {}, old)
    record(session, 'browser_navigate', {'url': URL}, new)
    _snapshot, guidance = research_browser_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=new))
    assert 'Complete the declared browser_wait next' not in guidance
    for name in ('browser_fill', 'browser_click'):
        record(session, name, {}, new)
    _snapshot, guidance = research_browser_handoff(session, build_turn_snapshot(session), CompletionContext(browser_document=new))
    assert 'Complete the declared browser_wait next' in guidance


def test_codemode_only_keeps_code_and_other_authorized_source_operations() -> None:
    session = TaskSession(TaskPolicy(GRANTS, strict_research=True), task_kind='research',
                          task_text='Use codemode only. Inspect multiple sources.')
    snapshot = offer_browser_call(build_turn_snapshot(session), session, 'browser_click', research=True)
    assert snapshot.input_binding is None
    assert {'open_url', 'browser_navigate', 'browser_click', 'browser_fill'} <= set(snapshot.nested_tool_names)
    spec = next(row for row in snapshot.model_specs if row.name == 'codemode')
    assert 'code' in spec.required and 'step' not in dict(spec.parameters)
    reply = decode_text_turn('{"tool":"codemode","args":{"code":"return unwrap(await tools.open_url(inputs));",'
                             '"inputs":{"url":"https://example.com/another-source"}}}')
    assert not normalize_turn(reply, snapshot=snapshot).protocol_error
    entries = snapshot_catalog(snapshot).entries
    navigation = next(row for row in entries if row.name == 'browser_navigate')
    assert isinstance(navigation.parameters, Mapping)
    assert decode_json_object(canonical_json_object(navigation.parameters)['properties'])['url']
