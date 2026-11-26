"""Receipt-bound Markdown facts use the original submission and citation contract."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import _required_tool_checks
from codey.operations.completion_output import prepare_completion_output
from codey.operations.kernel_facts import project_execution_record
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import parse_tool_success_check, require_browser_report_field, require_tool_success
from codey.research.answer_contract import (
    ANSWER_SCOPE_VERSION,
    AnswerScope,
    answer_scope,
    submit_answer,
    validate_submission,
)
from codey.research.answer_report import AnswerReport
from codey.research.source_document import OpenedSource
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.test_declared_work_continuation import declared_session, record
from tests.test_research_dynamic_queries import query_choices
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tools import research_query_delivery_live_gate as live
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import Target

URL = 'https://example.com/record/UN-097'
DOCUMENT: JsonObject = {'target_id': 'tab', 'frame_id': 'main', 'url': URL, 'loader_id': 'original'}


def latest_result_citation(directory: Path) -> str:
    state = directory / 'state'
    receipt = next(row.result for row in reversed(tool_receipts(state))
                   if row.result.ok and row.result.call.name == 'browser_read' and row.result.audit.get('query_click'))
    from codey.research.passages import read_passage_refs

    refs = read_passage_refs(receipt.canonical.get('read_passages'))
    for _, artifact in reversed(artifacts(state)):
        if artifact.get('kind') == 'provider_request':
            request = decode_json_object(artifact['payload'])
            snapshot = decode_json_object(request['snapshot'])
            scope = AnswerScope.from_payload(snapshot['research_scope'])
            return next(f'[{label}]' for label, ref in scope.aliases if ref in refs)
    raise AssertionError('Expected the next frozen request to include the post-action read')


def test_ready_hybrid_handoff_delivers_actual_bound_values_after_the_file_result(tmp_path: Path) -> None:
    session, context = declared_session(tmp_path)
    (tmp_path / 'ledger.data').write_text('{"balance":173.25,"keep":"00017"}', encoding='utf-8')
    session.record_edit('ledger.data')
    _snapshot, handoff = browser_task_handoff(session, build_turn_snapshot(session), context)
    assert 'All required work checks passed' in handoff
    assert '"value": "173.25"' in handoff and '"field": "/balance"' in handoff
    assert '"result_refs": ["receipt-0"]' in handoff


def session_for(*, context: str = '', missing: bool = False, after: str = '') -> TaskSession:
    check = require_browser_report_field(url=URL, prefix='Value: ', suffix=' units.', pointer='/quantity',
                                         label='库存', context=context, allow_missing=missing)
    rule = parse_tool_success_check(check)
    assert rule is not None
    check = replace(rule, after=after).declaration
    return TaskSession(TaskPolicy(frozenset({'control', 'web.read', 'browser.observe', 'browser.interact'}),
                                 strict_research=True, required_checks=(check,)),
                       task_kind='research', task_text='Report the declared quantity from ' + URL)


def read(session: TaskSession, body: str, *, context: str = '', region: str = '', record: str = '',
         url: str = URL, tool: str = 'browser_read', target: int | None = None,
         document: JsonObject | None = None) -> ToolResult:
    identity = 'read-' + str(len(session.executed))
    source = OpenedSource(url, url, 'Record UN-097', '2026-11-16T00:00:00Z', hashlib.sha256(body.encode()).hexdigest()[:16])
    audit: JsonObject = {'browser_document': {**(DOCUMENT if document is None else document), 'url': url}}
    if context:
        audit.update(query_click='click-original', query_region={'context': {'label': context}, 'text': region,
                                                                'visible': True, 'relation': 'aria-controls'})
        if target is not None:
            click_ref = f'click-target-{target}'
            target_identity: JsonObject = {'backend_node_id': target, 'relation': 'aria-controls'}
            session._memory_results[click_ref] = ToolResult(ToolCall('browser_click', {}, click_ref), 'clicked', ok=True,
                audit={'query_baseline': {'document': audit['browser_document'],
                                         'target': target_identity}})
            audit['query_click'] = click_ref
    if record:
        audit['record_regions'] = [{'label': record, 'start': 0, 'end': len(body), 'complete': True}]
    result = ToolResult(ToolCall(tool, {}, identity), body, ok=True,
        canonical={'research_observation': {'source': source.to_dict(), 'text': body, 'pages': []},
                   'read_passages': [{'ref': 'p' + hashlib.sha256(body.encode()).hexdigest()[:12], 'start': 0, 'end': len(body)}]}, audit=audit)
    session._memory_results[identity] = result
    project_execution_record(session, identity, result.call, result, ok=True)
    return result


@pytest.mark.parametrize('value', ['173', '9007199254740993.123456789', '6.02214076e23', '00047', 'QUARTZ_851', 'a`b [e999]'])
def test_expansion_pins_exact_strings_citations_and_final_digest(value: str) -> None:
    session = session_for()
    read(session, 'Value: ' + value + ' units.')
    scope = answer_scope(session)
    args: JsonObject = {'markdown': '已核对指定记录。\n{{field:/quantity}}', 'state': 'answered'}
    submitted = submit_answer(scope, args)
    assert value in submitted['markdown'] and '{{field:' not in submitted['markdown']
    assert [row['label'] for row in submitted['citations']] == ['e1']
    assert validate_submission(submitted, session.task_text) == submitted
    assert submit_answer(AnswerScope.from_payload(parse_json_object(json.dumps(scope.to_payload()))), args) == submitted
    prepared = prepare_completion_output(session, submitted['markdown'], CompletionContext(browser_document=DOCUMENT))
    assert prepared.text == submitted['markdown'] and all(row.evaluation.passed for row in prepared.observations.values())
    # A later source is a new attempt; the original frozen submission remains identical.
    read(session, 'Value: 999 units.')
    assert submit_answer(scope, args) == submitted
    assert submit_answer(answer_scope(session), args)['markdown'] != submitted['markdown']


@pytest.mark.parametrize('answer', ['库存 150 [e1]，另外 ABC123XYZ。', '库存 173，缺少对应引用。', '库存 173 [e2]，引用了旧版本。'])
def test_wrong_values_missing_fields_and_wrong_read_versions_are_rejected(answer: str) -> None:
    session = session_for()
    read(session, 'Value: 99 units.')
    read(session, 'Value: 173 units.')
    with pytest.raises(ValueError, match='missing exact value'):
        submit_answer(answer_scope(session), {'markdown': answer.replace('[e2]', '[e1]'), 'state': 'answered'})
    assert submit_answer(answer_scope(session), {'markdown': '库存为 173 units。 [e2]', 'state': 'answered'})['state'] == 'answered'


def test_all_explicit_fields_must_be_delivered_even_if_one_field_is_correct() -> None:
    session = session_for()
    status = require_browser_report_field(url=URL, prefix='Status: ', suffix='.', pointer='/status', label='状态')
    session.policy = replace(session.policy, required_checks=(*session.policy.required_checks, status))
    read(session, 'Value: 173 units.\nStatus: Ready.')
    scope = answer_scope(session)
    with pytest.raises(ValueError, match='状态: missing'):
        submit_answer(scope, {'markdown': '已经核对。{{field:/quantity}}', 'state': 'answered'})
    submitted = submit_answer(scope, {'markdown': '已经核对。\n{{field:/quantity}}\n{{field:/status}}', 'state': 'answered'})
    assert '173' in submitted['markdown'] and 'Ready' in submitted['markdown']


def test_partial_gap_requires_complete_evidence_and_cannot_bypass_ordered_execution() -> None:
    session = session_for(missing=True)
    read(session, 'This record has no quantity measurement.')
    args: JsonObject = {'markdown': '检查了原始记录。\n{{field:/quantity}}', 'state': 'partial'}
    assert 'Not established' in submit_answer(answer_scope(session), args)['markdown']
    with pytest.raises(ValueError, match='not established'):
        submit_answer(answer_scope(session), {**args, 'state': 'answered'})
    result = session._memory_results['read-0']
    session._memory_results['read-0'] = replace(result, truncated=True)
    with pytest.raises(ValueError, match='incomplete'):
        submit_answer(answer_scope(session), args)
    ordered = session_for(missing=True, after='browser_wait')
    read(ordered, 'No measurement available.')
    with pytest.raises(ValueError, match='after its latest successful declared wait'):
        submit_answer(answer_scope(ordered), args)


def test_region_binding_rejects_neighbor_missing_or_ambiguous_values() -> None:
    session = session_for(context='Record UN-097', missing=True)
    read(session, 'Value: 173 units.\nValue: 174 units.', context='Record UN-098', region='Value: 174 units.')
    args: JsonObject = {'markdown': '核对了原始记录。\n{{field:/quantity}}', 'state': 'partial'}
    with pytest.raises(ValueError, match='formal record ownership'):
        submit_answer(answer_scope(session), args)
    read(session, 'Value: 173 units.\nValue: 174 units.', context='Record UN-097', region='Value: 173 units.')
    assert '173' in submit_answer(answer_scope(session), {**args, 'state': 'answered'})['markdown']
    read(session, 'Value: 173 units. Value: 174 units.', context='Record UN-097', region='Value: 173 units. Value: 174 units.')
    with pytest.raises(ValueError, match='selector'):
        submit_answer(answer_scope(session), args)


def test_same_value_in_another_record_does_not_authorize_its_passage() -> None:
    session = session_for(context='Record UN-097')
    body = 'Value: 173 units. (neighbor)\nValue: 173 units. (selected)'
    result = read(session, body, context='Record UN-097', region='Value: 173 units. (selected)')
    split = body.index('\n') + 1
    session._memory_results['read-0'] = replace(result, canonical={**result.canonical, 'read_passages': [
        {'ref': 'p' + 'a' * 12, 'start': 0, 'end': split}, {'ref': 'p' + 'b' * 12, 'start': split, 'end': len(body)}]})
    scope = answer_scope(session)
    assert scope.fields[0].labels == ('e2',)
    with pytest.raises(ValueError, match='required source version'):
        submit_answer(scope, {'markdown': '库存值是 173。 [e1]', 'state': 'answered'})


@pytest.mark.parametrize('other_url', [URL, 'https://example.com/other-record'])
def test_each_record_and_document_keeps_its_own_latest_field(other_url: str) -> None:
    session = session_for(context='Record A')
    other = require_browser_report_field(url=other_url, prefix='Value: ', suffix=' units.', pointer='/other',
                                         label='另一记录', context='Record B')
    session.policy = replace(session.policy, required_checks=(*session.policy.required_checks, other))
    read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.')
    read(session, 'Value: 47 units.', context='Record B', region='Value: 47 units.', url=other_url)
    read(session, 'Value: 174 units.', context='Record A', region='Value: 174 units.')
    read(session, 'Status: Ready.', context='Record A', region='Status: Ready.')
    submitted = submit_answer(answer_scope(session), {'markdown': '已分别核对两份原始记录。\n{{field:/quantity}}\n{{field:/other}}',
                                                     'state': 'answered'})
    assert '174' in submitted['markdown'] and '47' in submitted['markdown']
    assert {row['label'] for row in submitted['citations']} == {'e2', 'e3'}
    assert all(row.evaluation.passed for row in prepare_completion_output(session, submitted['markdown'],
        CompletionContext(browser_document={**DOCUMENT, 'url': other_url})).observations.values())


def test_each_document_retains_its_original_wait_before_the_record_read() -> None:
    session = session_for(context='Record A', after='browser_wait')
    second = 'https://example.com/independent-source'
    other = parse_tool_success_check(require_browser_report_field(url=second, prefix='Value: ', suffix=' units.',
        pointer='/other', label='另一来源', context='Record B'))
    assert other is not None
    session.policy = replace(session.policy, required_checks=(*session.policy.required_checks,
                                                             replace(other, after='browser_wait').declaration))
    for url, owner, amount in ((URL, 'Record A', '173'), (second, 'Record B', '47')):
        record(session, 'browser_wait', {'condition': 'text', 'value': 'Ready', 'timeout_ms': 5000}, {**DOCUMENT, 'url': url})
        read(session, 'Value: ' + amount + ' units.', context=owner, region='Value: ' + amount + ' units.', url=url)
    submitted = submit_answer(answer_scope(session), {'markdown': '分别核对。\n{{field:/quantity}}\n{{field:/other}}', 'state': 'answered'})
    assert '173' in submitted['markdown'] and '47' in submitted['markdown']


@pytest.mark.parametrize('tool', ['open_url', 'browser_read', 'read_page_detail'])
def test_record_ownership_does_not_require_a_click(tool: str) -> None:
    from codey.toolchain.constants import PAGE_DETAIL_TOOL

    session = session_for(context='Record A')
    read(session, 'Record A\nValue: 173 units.', record='Record A', tool=PAGE_DETAIL_TOOL if tool == 'read_page_detail' else tool)
    submitted = submit_answer(answer_scope(session), {'markdown': '已经核对记录归属。\n{{field:/quantity}}', 'state': 'answered'})
    assert '173' in submitted['markdown'] and '[e1]' in submitted['markdown']


def test_explicit_query_result_is_independent_of_record_ownership() -> None:
    session = session_for(context='Record A')
    original = parse_tool_success_check(session.policy.required_checks[0])
    assert original is not None and original.browser_text is not None
    rule = replace(original, browser_text=replace(original.browser_text, query_result=True))
    session.policy = replace(session.policy, required_checks=(rule.declaration,))
    args: JsonObject = {'markdown': '已经检查指定记录。\n{{field:/quantity}}', 'state': 'answered'}
    read(session, 'Record A\nValue: 173 units.', record='Record A')
    with pytest.raises(ValueError, match='confirmed query result'):
        submit_answer(answer_scope(session), args)
    read(session, 'Value: 174 units.', context='Record A', region='Value: 174 units.')
    assert '174' in submit_answer(answer_scope(session), args)['markdown']
    assert parse_tool_success_check(rule.declaration) == rule


def test_incomplete_new_read_of_the_same_field_cannot_resurrect_an_older_value() -> None:
    session = session_for(context='Record A')
    read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.')
    newer = read(session, 'Value: 174 units.', context='Record A', region='Value: 174 units.')
    session._memory_results['read-1'] = replace(newer, truncated=True)
    with pytest.raises(ValueError, match='incomplete'):
        submit_answer(answer_scope(session), {'markdown': '已核对。\n{{field:/quantity}}', 'state': 'answered'})


@pytest.mark.parametrize('same_target', [False, True])
def test_missing_field_invalidates_only_its_original_query_target(same_target: bool) -> None:
    session = session_for(context='Record A', missing=True)
    read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.', target=17)
    read(session, 'No longer available.', context='Record A', region='No longer available.',
         target=17 if same_target else 29)
    scope = answer_scope(session)
    if same_target:
        assert scope.fields[0].value is None
        with pytest.raises(ValueError, match='not established'):
            submit_answer(scope, {'markdown': '库存仍为 173 units。 [e1]', 'state': 'answered'})
        submitted = submit_answer(scope, {'markdown': '已核对当前记录。\n{{field:/quantity}}', 'state': 'partial'})
        assert [row['label'] for row in submitted['citations']] == ['e2']
    else:
        assert scope.fields[0].value == '173'
        submitted = submit_answer(scope, {'markdown': '已核对当前记录。\n{{field:/quantity}}', 'state': 'answered'})
        assert [row['label'] for row in submitted['citations']] == ['e1']


@pytest.mark.parametrize('context', ['', 'Record A'])
@pytest.mark.parametrize('allow_missing', [False, True])
def test_reloaded_query_cannot_fall_back_to_an_old_field(context: str, allow_missing: bool) -> None:
    session = session_for(context=context, missing=allow_missing)
    original = parse_tool_success_check(session.policy.required_checks[0])
    assert original is not None and original.browser_text is not None
    rule = replace(original, browser_text=replace(original.browser_text, query_result=True))
    session.policy = replace(session.policy, required_checks=(rule.declaration,))
    read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.', target=17)
    read(session, 'No longer available.', context='Record A', region='No longer available.', target=29,
         document={**DOCUMENT, 'loader_id': 'reloaded'})
    scope = answer_scope(session)
    assert scope.fields[0].value is None
    args: JsonObject = {'markdown': 'Checked the latest quantity.\n{{field:/quantity}}', 'state': 'answered'}
    with pytest.raises(ValueError, match='not established|complete formal read.*lacks'):
        submit_answer(scope, args)
    if allow_missing:
        submitted = submit_answer(scope, {**args, 'state': 'partial'})
        assert 'Not established' in submitted['markdown'] and '173' not in submitted['markdown']
        assert [row['label'] for row in submitted['citations']] == ['e2']
    else:
        with pytest.raises(ValueError, match='complete formal read.*lacks'):
            submit_answer(scope, {**args, 'state': 'partial'})


@pytest.mark.parametrize('other_url', [URL, 'https://example.com/independent-record'])
def test_new_document_for_another_record_does_not_invalidate_the_field(other_url: str) -> None:
    session = session_for(context='Record A')
    read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.', target=17)
    read(session, 'No longer available.', context='Record B', region='No longer available.', target=29,
         url=other_url, document={**DOCUMENT, 'loader_id': 'reloaded'})
    scope = answer_scope(session)
    assert scope.fields[0].value == '173' and scope.fields[0].labels == ('e1',)
    assert '173' in submit_answer(scope, {'markdown': 'Record A quantity.\n{{field:/quantity}}',
                                        'state': 'answered'})['markdown']


@pytest.mark.parametrize('missing_prerequisite', ['query', 'wait'])
def test_reloaded_record_requires_new_qualified_evidence_before_reusing_a_field(missing_prerequisite: str) -> None:
    session = session_for(context='Record A', missing=True,
                          after='browser_wait' if missing_prerequisite == 'wait' else '')
    original = parse_tool_success_check(session.policy.required_checks[0])
    assert original is not None and original.browser_text is not None
    rule = replace(original, browser_text=replace(original.browser_text, query_result=True))
    session.policy = replace(session.policy, required_checks=(rule.declaration,))
    wait: JsonObject = {'condition': 'text', 'value': 'Ready', 'timeout_ms': 5000}
    if missing_prerequisite == 'wait':
        record(session, 'browser_wait', wait, DOCUMENT)
    read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.', target=17)
    document = {**DOCUMENT, 'loader_id': 'reloaded'}
    if missing_prerequisite == 'query':
        read(session, 'Record A\nNot loaded.', record='Record A', tool='open_url', document=document)
    else:
        read(session, 'No longer available.', context='Record A', region='No longer available.',
             target=29, document=document)
    scope = answer_scope(session)
    assert scope.fields[0].value is None and scope.fields[0].feedback
    args: JsonObject = {'markdown': 'Checked current quantity.\n{{field:/quantity}}', 'state': 'answered'}
    for state in ('answered', 'partial'):
        with pytest.raises(ValueError, match='confirmed query result'):
            submit_answer(scope, {**args, 'state': state})
    if missing_prerequisite == 'wait':
        record(session, 'browser_wait', wait, document)
    read(session, 'Value: 241 units.', context='Record A', region='Value: 241 units.', target=33, document=document)
    submitted = submit_answer(answer_scope(session), args)
    assert '241' in submitted['markdown'] and '173' not in submitted['markdown']


def test_new_formal_source_invalidates_old_codemode_field_ownership() -> None:
    session = session_for(context='Record A', missing=True)
    original = parse_tool_success_check(session.policy.required_checks[0])
    assert original is not None and original.browser_text is not None
    rule = replace(original, via='codemode', browser_text=replace(original.browser_text, query_result=True))
    session.policy = replace(session.policy, required_checks=(rule.declaration,))
    child = read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.', target=17)
    call = ToolCall('codemode', {'code': 'return unwrap(await tools.browser_read({}));'}, 'parent')
    parent = ToolResult(call, 'Read committed.', ok=True, canonical={'child_effect_ids': [child.call.call_id]})
    session._memory_results['parent'] = parent
    project_execution_record(session, 'parent', call, parent, ok=True)
    assert answer_scope(session).fields[0].value == '173'
    read(session, 'Record A\nNot loaded.', record='Record A', tool='open_url',
         document={**DOCUMENT, 'loader_id': 'reloaded'})
    scope = answer_scope(session)
    assert scope.fields[0].value is None and scope.fields[0].feedback
    with pytest.raises(ValueError, match='confirmed query result'):
        submit_answer(scope, {'markdown': 'Current quantity.\n{{field:/quantity}}', 'state': 'partial'})


@pytest.mark.parametrize('fence', ['```text', '~~~'])
def test_field_citation_inside_multiline_code_is_not_a_published_citation(fence: str) -> None:
    session = session_for()
    read(session, 'Value: 99 units.')
    read(session, 'Value: 173 units.')
    markdown = 'Historical source [e1].\n' + fence + '\nQuantity 173 [e2]\n' + fence[:3]
    with pytest.raises(ValueError, match='missing exact value'):
        submit_answer(answer_scope(session), {'markdown': markdown, 'state': 'answered'})


@pytest.mark.parametrize('query', [False, True])
def test_contextless_partial_read_of_another_region_does_not_replace_the_field(query: bool) -> None:
    session = session_for()
    read(session, 'Value: 173 units.')
    body = 'Value: 173 units.\nNo delivery information.'
    newer = read(session, body, context='Record A' if query else '',
                 region='No delivery information.' if query else '', target=29 if query else None)
    start = body.index('No delivery')
    session._memory_results['read-1'] = replace(newer, canonical={**newer.canonical,
        'read_passages': [{'ref': 'p' + 'd' * 12, 'start': start, 'end': len(body)}]})
    scope = answer_scope(session)
    assert scope.fields[0].value == '173' and scope.fields[0].labels == ('e1',)
    assert '173' in submit_answer(scope, {'markdown': '已核对库存。\n{{field:/quantity}}', 'state': 'answered'})['markdown']


def test_completion_gate_rechecks_report_values_and_formal_citation_positions() -> None:
    session = session_for()
    read(session, 'Value: 99 units.')
    submitted = submit_answer(answer_scope(session), {'markdown': '已核对库存。\n{{field:/quantity}}', 'state': 'answered'})
    report = AnswerReport(text=submitted['markdown'], markdown=submitted['markdown'], state='answered',
        task_digest=submitted['task_digest'], proposal_digest=submitted['submission_digest'],
        citation_bindings=tuple(submitted['citations']), source_versions=(), evidence_bindings=(),
        knowledge_refs=(), knowledge_mutations=(), attempt_refs=(), selection_receipts=(), questions=())
    context = CompletionContext(browser_document=DOCUMENT)
    initial = _required_tool_checks(session, report.markdown, context, None, report)
    assert all(row.passed for row in initial.evaluations.values())
    read(session, 'Value: 173 units.')
    updated = _required_tool_checks(session, report.markdown, context, None, report)
    assert all(row.reason_code == 'answer_field_missing' for row in updated.evaluations.values())


def test_completion_gate_rejects_a_field_answer_frozen_before_reload() -> None:
    session = session_for(context='Record A', missing=True)
    read(session, 'Value: 173 units.', context='Record A', region='Value: 173 units.', target=17)
    submitted = submit_answer(answer_scope(session), {'markdown': 'Checked quantity.\n{{field:/quantity}}',
                                                     'state': 'answered'})
    report = AnswerReport(text=submitted['markdown'], markdown=submitted['markdown'], state='answered',
        task_digest=submitted['task_digest'], proposal_digest=submitted['submission_digest'],
        citation_bindings=tuple(submitted['citations']), source_versions=(), evidence_bindings=(),
        knowledge_refs=(), knowledge_mutations=(), attempt_refs=(), selection_receipts=(), questions=())
    document = {**DOCUMENT, 'loader_id': 'reloaded'}
    read(session, 'No longer available.', context='Record A', region='No longer available.', target=29,
         document=document)
    updated = _required_tool_checks(session, report.markdown, CompletionContext(browser_document=document), None, report)
    assert all(row.reason_code == 'answer_field_missing' for row in updated.evaluations.values())


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
@pytest.mark.parametrize('native', [False, True])
def test_production_reload_rejects_old_quantity_then_publishes_the_current_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / 'run'
    if native:
        monkeypatch.setattr(live, 'ExperimentProvider', NativeExperimentProvider)
    url = 'https://example.com/record/reload-check'
    check = require_browser_report_field(url=url, prefix='Value: ', suffix=' units.', pointer='/quantity',
        label='Quantity', context='Record A', allow_missing=True, query_result=True)
    case = live.QueryCase('/record/reload-check', 'Query the quantity, reload, then query and report its current value.', '''
<section aria-label="Record A"><h2>Record A</h2>
<button aria-controls="quantity" onclick="const n=Number(sessionStorage.getItem('queries')||0)+1;
sessionStorage.setItem('queries',String(n));document.getElementById('quantity').textContent=
n===1?'Value: 173 units.':'No longer available.'">Refresh quantity</button>
<article id="quantity">Not loaded.</article></section>''', (), (check,))

    def replies() -> Iterator[JsonObject]:
        yield decode_json_object(query_choices(directory)[0]['call'])
        yield {'tool': 'codemode', 'args': {'code':
            'unwrap(await tools.browser_navigate(inputs)); unwrap(await tools.browser_read({})); '
            'return unwrap(await tools.browser_snapshot({}));', 'inputs': {'url': url}}}
        yield decode_json_object(query_choices(directory)[0]['call'])
        yield {'tool': 'submit_research_answer', 'args': {
            'markdown': 'Checked the latest quantity.\n{{field:/quantity}}', 'state': 'answered'}}
        message = json.dumps(requests(directory)[-1]['messages'], ensure_ascii=False)
        assert 'not established' in message
        yield {'tool': 'submit_research_answer', 'args': {
            'markdown': 'Checked the latest quantity.\n{{field:/quantity}}', 'state': 'partial'}}

    install_http(monkeypatch, replies(), native=native)
    result = live.run_case(directory, case, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert result['stop_reason'] == 'done' and result['source_stable'], result
    body, _, _ = str(result['answer']).partition('\n\n## 程序回执记录')
    assert 'Not established' in body and '173' not in body
    receipts = [row.result for row in tool_receipts(directory / 'state')]
    queries = [row for row in receipts if row.ok and row.call.name == 'browser_read' and row.audit.get('query_click')]
    assert len(queries) == 2
    assert decode_json_object(queries[0].audit['query_region'])['text'] == 'Value: 173 units.'
    assert decode_json_object(queries[-1].audit['query_region'])['text'] == 'No longer available.'
    assert queries[0].audit['browser_document'] != queries[-1].audit['browser_document']
    submissions = [row for row in receipts if row.call.name == 'submit_research_answer']
    assert [row.ok for row in submissions] == [False, True]
    answer = decode_json_object(submissions[-1].canonical['research_submission'])
    citations = answer['citations']
    passages = queries[-1].canonical['read_passages']
    assert isinstance(citations, list) and isinstance(passages, list)
    assert {decode_json_object(row)['passage_ref'] for row in citations} == {
        decode_json_object(row)['ref'] for row in passages}
    assert sum(row.call.name == 'browser_click' for row in receipts) == 2
    assert sum(row.ok and row.call.name == '_publish_research_answer' for row in receipts) == 1


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
@pytest.mark.parametrize('same_target', [False, True])
@pytest.mark.parametrize('context', ['', 'Record UN-097'])
def test_initial_open_field_is_invalidated_only_by_its_own_observed_dom_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_target: bool, context: str,
) -> None:
    directory = tmp_path / 'run'
    check = require_browser_report_field(url=URL, prefix='Value: ', suffix=' units.', pointer='/quantity',
                                         label='库存', context=context, allow_missing=True)
    case = live.QueryCase('/record/UN-097', 'Refresh the requested region and report the quantity.', '''
<section aria-label="Record UN-097"><h2>Record UN-097</h2>
<button aria-controls="quantity" onclick="document.getElementById('quantity').textContent='No longer available.'">Refresh quantity</button>
<article id="quantity">Value: 173 units.</article>
<button aria-controls="status" onclick="document.getElementById('status').textContent='No longer available.'">Refresh status</button>
<article id="status">Status: Ready.</article></section>''', (), (check,))

    def replies() -> Iterator[JsonObject]:
        chosen = next(row for row in query_choices(directory)
                      if row['name'] == ('Refresh quantity' if same_target else 'Refresh status'))
        initial = next(item.result for item in tool_receipts(directory / 'state') if item.result.call.name == 'open_url')
        assert 'query_click' not in initial.audit
        regions = initial.audit['record_regions']
        assert isinstance(regions, list)
        targets = decode_json_object(regions[0])['query_targets']
        assert isinstance(targets, list) and len(targets) == 2
        yield decode_json_object(chosen['call'])
        result_text = '' if same_target else '\n状态区域现已显示 No longer available. ' + latest_result_citation(directory)
        yield {'tool': 'submit_research_answer', 'args': {'markdown': '已检查指定记录。\n{{field:/quantity}}' + result_text,
                                                       'state': 'partial' if same_target else 'answered'}}

    install_http(monkeypatch, replies(), native=False)
    result = live.run_case(directory, case, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert result['stop_reason'] == 'done', result
    receipts = [item.result for item in tool_receipts(directory / 'state')]
    submission = next(row for row in receipts if row.call.name == 'submit_research_answer')
    answer = decode_json_object(submission.canonical['research_submission'])
    assert ('Not established' if same_target else '173') in str(answer['markdown'])
    assert sum(row.call.name == 'browser_click' for row in receipts) == 1


def test_unrelated_source_tool_without_a_source_receipt_cannot_break_field_selection() -> None:
    session = session_for()
    read(session, 'Value: 173 units.')
    call = ToolCall('knowledge_read', {}, 'knowledge')
    result = ToolResult(call, 'Other saved information.', ok=True, canonical={'value': 'Other saved information.'})
    session._memory_results['knowledge'] = result
    project_execution_record(session, 'knowledge', call, result, ok=True)
    assert answer_scope(session).fields[0].value == '173'


def test_frozen_snapshot_restores_only_with_current_scope() -> None:
    old: JsonObject = {'task_digest': 'a' * 64, 'aliases': [['e1', 'p' + 'b' * 12]],
                       'attempt_refs': ['original'], 'schema_version': 4}
    with pytest.raises(ValueError, match="invalid whole-task research scope"):
        AnswerScope.from_payload(old)
    session = session_for()
    read(session, 'Value: 173 units.')
    snapshot = build_turn_snapshot(session)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    assert restored.research.scope is not None and restored.research.scope.schema_version == ANSWER_SCOPE_VERSION
    declaration = session.policy.required_checks[0]
    rule = parse_tool_success_check(declaration)
    assert rule is not None and rule.declaration == declaration


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('source_access_wording', [False, True])
def test_production_expands_fields_before_one_original_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool, source_access_wording: bool,
) -> None:
    directory = tmp_path / 'run'
    if native:
        monkeypatch.setattr(live, 'ExperimentProvider', NativeExperimentProvider)
    def replies() -> Iterator[JsonObject]:
        chosen = next(row for row in query_choices(directory) if decode_json_object(row['context'])['label'] == 'Parcel ZX-581')
        yield decode_json_object(chosen['call'])
        yield {'tool': 'submit_research_answer', 'args': {'markdown': '已核对 ZX-581 的配送详情。\n{{field:/arrival}}', 'state': 'answered'}}
    install_http(monkeypatch, replies(), native=native)
    summary = live.run_case(directory, live.cases()['explicit'], Target('http://scripted.test/v1', 'fixture', 123456789, 10),
                            source_access_wording=source_access_wording)
    assert summary['passed'], summary
    results = [row.result for row in tool_receipts(directory / 'state')]
    source = next(row for row in results if row.call.name == 'open_url')
    assert source.ok
    assert any(row.call.name == 'codemode' and row.ok and row.call.call_id.startswith('required-source:')
               for row in results)
    assert sum(row.call.name == 'browser_click' for row in results) == 1
    assert sum(row.call.name == '_publish_research_answer' and row.ok for row in results) == 1
    original = next(row for row in results if row.call.name == 'submit_research_answer')
    assert '{{field:' in str(original.call.args['markdown'])
    assert '18:37' in str(original.canonical['research_submission'])


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('detail', [False, True])
def test_production_record_fields_use_initial_or_native_detail_evidence_without_clicks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool, detail: bool,
) -> None:
    import re

    directory = tmp_path / 'run'
    original = live.cases()['explicit']
    contents = '<p>Arrival: Friday 18:37.</p>'
    if detail:
        contents = '<details><summary>View itinerary</summary>' + contents + '</details>'
    case = replace(original, html='<section aria-label="Parcel ZX-581"><h2>Parcel ZX-581</h2>'
                                 + contents + '</section>')
    if native:
        monkeypatch.setattr(live, 'ExperimentProvider', NativeExperimentProvider)

    def replies() -> Iterator[JsonObject]:
        if detail:
            message = json.dumps(requests(directory)[-1]['messages'], ensure_ascii=False)
            choices = {match[0] for match in re.finditer(r'detail-[a-f0-9]{12}', message)}
            assert len(choices) == 1
            yield {'tool': PAGE_DETAIL_TOOL, 'args': {'choice': choices.pop()}}
        yield {'tool': 'submit_research_answer', 'args': {'markdown': '已核对 ZX-581 的记录。\n{{field:/arrival}}', 'state': 'answered'}}

    install_http(monkeypatch, replies(), native=native)
    summary = live.run_case(directory, case, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert summary['passed'], summary
    results = [row.result for row in tool_receipts(directory / 'state')]
    assert sum(row.call.name == 'browser_snapshot' for row in results) == 1
    assert not any(row.call.name == 'browser_click' for row in results)
    source = next(row for row in results if row.call.name == (PAGE_DETAIL_TOOL if detail else 'open_url'))
    assert source.ok and source.audit.get('record_regions')


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
def test_missing_field_and_wrong_old_citation_recover_without_rereading_or_reclicking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / 'run'

    def replies() -> Iterator[JsonObject]:
        yield {'tool': 'submit_research_answer', 'args': {'markdown': 'ZX-581 的时间尚未加载。 [e1]', 'state': 'partial'}}
        request = json.dumps(requests(directory)[-1]['messages'], ensure_ascii=False)
        assert 'rereading the unchanged view cannot load it' in request
        chosen = next(row for row in query_choices(directory) if decode_json_object(row['context'])['label'] == 'Parcel ZX-581')
        yield decode_json_object(chosen['call'])
        yield {'tool': 'submit_research_answer', 'args': {'markdown': 'ZX-581 的预计送达为 Friday 18:37。 [e1]', 'state': 'answered'}}
        current = requests(directory)[-1]
        assert 'required source version' in json.dumps(current['messages'])
        schema = json.dumps(current.get('response_format', current.get('tools')))
        assert 'browser_read' in schema and 'browser_click' in schema
        assert 'no new click or edit is needed' in json.dumps(current['messages'])
        yield {'tool': 'submit_research_answer', 'args': {'markdown': '已核对 ZX-581 的查询结果。\n{{field:/arrival}}', 'state': 'answered'}}

    install_http(monkeypatch, replies(), native=False)
    result = live.run_case(directory, live.cases()['explicit'], Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert result['passed'], result
    receipts = [row.result for row in tool_receipts(directory / 'state')]
    for name in ('open_url', 'browser_snapshot', 'browser_click', 'browser_read'):
        assert sum(row.call.name == name for row in receipts) == 1
    assert [row.ok for row in receipts if row.call.name == 'submit_research_answer'] == [False, False, True]


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
@pytest.mark.parametrize('native', [False, True])
def test_one_satisfied_field_preserves_acquisition_for_other_user_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / 'run'
    original = live.cases()['explicit']
    case = replace(original, question='查 ZX-581 的预计送达时间和延误原因。', facts=('18:37', 'weather'), html='''
<section aria-label="Parcel ZX-581"><h2>Parcel ZX-581</h2><p>Arrival: Friday 18:37.</p>
<button aria-controls="reason" onclick="document.getElementById('reason').textContent=
'Arrival: Friday 18:37. Delay reason: weather.'">Inspect reason</button>
<article id="reason">Reason not loaded.</article>
<details><summary>More background</summary><p>Weather may affect this route.</p></details></section>''')
    if native:
        monkeypatch.setattr(live, 'ExperimentProvider', NativeExperimentProvider)

    def replies() -> Iterator[JsonObject]:
        current = requests(directory)[-1]
        schema = json.dumps(current.get('response_format', current.get('tools')))
        assert 'browser_click' in schema and PAGE_DETAIL_TOOL in schema
        message = json.dumps(current['messages'], ensure_ascii=False)
        assert 'other requirements in the original question' in message
        choices = query_choices(directory)
        assert len(choices) == 1
        yield decode_json_object(choices[0]['call'])
        current = requests(directory)[-1]
        message = json.dumps(current['messages'], ensure_ascii=False)
        assert 'weather' in message and 'Confirmed query:' in message
        yield {'tool': 'submit_research_answer', 'args': {'markdown':
            'ZX-581\n{{field:/arrival}}\nDelay reason: weather. [e2]', 'state': 'answered'}}

    install_http(monkeypatch, replies(), native=native)
    result = live.run_case(directory, case, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert result['passed'], result
    receipts = [row.result for row in tool_receipts(directory / 'state')]
    assert sum(row.call.name == 'browser_click' for row in receipts) == 1


@pytest.mark.usefixtures('public_source_dns', 'scripted_local_api_connection', 'no_external_advisor_models')
@pytest.mark.parametrize('native', [False, True])
def test_inferred_tool_workflow_keeps_real_candidates_and_native_details_after_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / 'run'
    original = live.cases()['ordinary']
    wait_args: JsonObject = {'condition': 'text', 'value': 'In transit', 'timeout_ms': 5000}
    case = replace(original, required_tools=(require_tool_success('browser_wait', arguments=wait_args),
                                           require_tool_success('browser_read')),
                   html=original.html.replace('<article id="eta">Arrival not loaded.</article>',
                   '<article id="eta">Arrival not loaded.</article><details>'
                   '<summary>More background</summary><p>Additional route information.</p></details>'))
    if native:
        monkeypatch.setattr(live, 'ExperimentProvider', NativeExperimentProvider)

    def replies() -> Iterator[JsonObject]:
        yield {'tool': 'browser_wait', 'args': wait_args}
        yield {'tool': 'browser_read', 'args': {}}
        current = requests(directory)[-1]
        schema = json.dumps(current.get('response_format', current.get('tools')))
        assert 'browser_click' in schema and PAGE_DETAIL_TOOL in schema
        message = json.dumps(current['messages'], ensure_ascii=False)
        assert 'tool checklist does not declare the whole investigation complete' in message
        choices = query_choices(directory)
        assert len(choices) == 2
        chosen = next(row for row in choices if decode_json_object(row['context'])['label'] == 'Parcel ZX-581')
        yield decode_json_object(chosen['call'])
        yield {'tool': 'submit_research_answer', 'args': {
            'markdown': 'ZX-581 is In transit. Arrival: Friday 18:37. ' + latest_result_citation(directory), 'state': 'answered'}}

    install_http(monkeypatch, replies(), native=native)
    result = live.run_case(directory, case, Target('http://scripted.test/v1', 'fixture', 123456789, 10))
    assert result['passed'], result
