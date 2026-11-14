"""Measure result-call presentation without changing the production menu or execution.

Both arms use the production handoff and autonomous model responses. The menu
arm only suppresses the new query hint, preserving the same frozen interfaces.
Host tracing is passive; committed child receipts determine actual adoption.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from codey.codemode.host import CodeModeHost
from codey.codemode.identity import rpc_child_index
from codey.operations import browser_task_handoff as handoff
from codey.operations.browser_followups import delivery
from codey.operations.browser_task_handoff import handoff_call, query_result_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import require_tool_success
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_integer,
)
from tools import browser_production_live_gate as live
from tools.browser_leaf_exposure_ab import response_actions
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)

SCENARIOS = ('basic', 'reread', 'legacy_structured', 'stale_ref')
ARMS = ('menu', 'call_hint')
WAIT = require_tool_success('browser_wait', arguments={'condition': 'text', 'value': '鱼丸'})
ROOT = Path(__file__).resolve().parents[1]
HARNESS = (
    'tools/browser_result_adoption_ab.py', 'tools/browser_fill_history_audit.py',
    'tools/browser_query_scope_probe.py', 'tools/browser_production_live_gate.py',
    'tools/browser_leaf_exposure_ab.py', 'tools/completion_explanation_ab.py',
    'tools/completion_artifact_audit.py',
)


def harness_digest() -> str:
    digest = hashlib.sha256()
    for name in HARNESS:
        digest.update(name.encode() + b'\0' + (ROOT / name).read_bytes())
    return digest.hexdigest()


def assert_stable(source: str, harness: str) -> None:
    if production_source_digest() != source or harness_digest() != harness:
        raise RuntimeError('Production or harness changed; restart the complete paired matrix.')


def result_call(snapshot: TurnSnapshot, args: JsonObject) -> JsonObject | None:
    """Use the production call generator and frozen interface validation."""
    session = TaskSession(snapshot.policy) if snapshot.policy is not None else None
    if session is None or not snapshot.browser_followups:
        return None
    _, call, _ = handoff_call(session, snapshot, ToolCall('browser_click', args))
    return call


def result_hint(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                generation: int | None) -> str:
    return query_result_handoff(session, snapshot, context, generation)[1]


@contextmanager
def intervention(directory: Path, arm: str, source: str, harness: str) -> Iterator[None]:
    original_request = KernelContext.request_snapshot
    original_invoke = CodeModeHost.invoke
    latest: tuple[bool, str] = (False, '')

    def query(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
              generation: int | None) -> tuple[TurnSnapshot, str]:
        nonlocal latest
        offered, hint = query_result_handoff(session, snapshot, context, generation)
        latest = bool(hint), hint if arm == 'call_hint' else ''
        return offered, latest[1]

    def request(context: KernelContext, frozen: TurnSnapshot, *,
                delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        nonlocal latest
        latest = False, ''
        snapshot, text = original_request(context, frozen, delivered_results=delivered_results)
        payload = snapshot_description(snapshot)
        if restore_snapshot(payload) != snapshot:
            raise AssertionError('Frozen snapshot did not roundtrip.')
        append_json(directory / 'presentation.jsonl', {'arm': arm, 'snapshot': payload, 'eligible': latest[0],
            'hint': latest[1], 'baseline_handoff': text.replace(latest[1], '') if latest[1] else text})
        return snapshot, text

    def invoke(host: CodeModeHost, name: str, args: dict[str, object]) -> object:
        index = rpc_child_index.get()
        if index is None:
            index = host._next_child
        # Retain the proposed purpose before the original host strips it.
        proposed = decode_json_object(args)
        result = original_invoke(host, name, args)
        plans = canonical_json_object(host._catalog[name].parameters).get('x-codey-click-plans') if name == 'browser_click' else None
        followups = plans.get('result', []) if isinstance(plans, dict) and proposed.get('purpose') == 'result' else []
        append_json(directory / 'invocations.jsonl', {'call_id': f'{host.execution_id}:child:{index}',
            'parent': host.execution_id, 'name': name, 'args': proposed,
            'followups': followups,
            'ok': result.get('ok') if isinstance(result, dict) else None})
        return result

    class GuardedProvider(ExperimentProvider):
        def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                                  response_bytes: int, seconds: float) -> None:
            if phase == 'request':
                assert_stable(source, harness)
            super()._observe_http_attempt(attempt=attempt, data=data, phase=phase,
                response_bytes=response_bytes, seconds=seconds)

    with (patch.object(handoff, 'query_result_handoff', query),
          patch.object(KernelContext, 'request_snapshot', request), patch.object(CodeModeHost, 'invoke', invoke),
          patch.object(live, 'ExperimentProvider', GuardedProvider)):
        yield


def jsonl(path: Path) -> list[JsonObject]:
    return [parse_json_object(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def invocation_metrics(invocations: list[JsonObject], receipts: list[ToolResult]) -> JsonObject:
    by_id = {row.call.call_id: row for row in receipts}
    executed: list[JsonObject] = []
    for row in invocations:
        receipt = by_id.get(str(row['call_id']))
        if receipt is None:
            raise ValueError('A traced host invocation has no original committed receipt.')
        if row['name'] != receipt.call.name:
            raise ValueError('A traced host invocation differs from its original committed receipt.')
        executed.append(row)
    result_rows = [row for row in executed if row['name'] == 'browser_click'
                   and decode_json_object(row['args']).get('purpose') == 'result']
    automatic: set[str] = set()
    for index, row in enumerate(executed):
        if row not in result_rows or row.get('ok') is not True:
            continue
        raw_steps = row.get('followups')
        if not isinstance(raw_steps, list):
            raise ValueError('A result invocation has no captured frozen follow-up plan.')
        for step_index, raw_step in enumerate(raw_steps, index + 1):
            step = decode_json_object(raw_step)
            if step_index >= len(executed):
                raise ValueError('The result follow-up sequence has no committed trace.')
            child = executed[step_index]
            if child['parent'] != row['parent'] or child['name'] != step['name'] or child['args'] != step['args']:
                raise ValueError('The result follow-up sequence is ambiguous or differs from its frozen plan.')
            automatic.add(str(child['call_id']))
            if child.get('ok') is not True:
                break
    # Direct outer reads do not enter CodeModeHost. Count original receipts,
    # using the traced parent only to group nested calls into model requests.
    parents = {str(row['call_id']): str(row['parent']) for row in executed}
    manual_rows = [row for row in receipts if row.call.call_id not in automatic
                   and row.call.name in {'browser_wait', 'browser_read'}]
    manual = Counter(row.call.name for row in manual_rows)
    clicks = [row for row in receipts if row.call.name == 'browser_click']
    return {'result_invocations': len(result_rows),
        'result_delivered': sum(delivery(by_id[str(row['call_id'])]) == 'delivered' for row in result_rows),
        'result_query_baselines': sum('query_baseline' in by_id[str(row['call_id'])].audit for row in result_rows),
        'manual_wait_calls': manual['browser_wait'], 'manual_read_calls': manual['browser_read'],
        'manual_wait_requests': len({parents.get(row.call.call_id, f'outer:{i}') for i, row in enumerate(manual_rows)
                                     if row.call.name == 'browser_wait'}),
        'manual_read_requests': len({parents.get(row.call.call_id, f'outer:{i}') for i, row in enumerate(manual_rows)
                                     if row.call.name == 'browser_read'}),
        'delivered_clicks': sum(delivery(row) in {'delivered', 'verified'} for row in clicks),
        'uncertain_clicks': sum(delivery(row) == 'uncertain' for row in clicks),
        'failed_clicks': sum(not row.ok for row in clicks),
        'query_linked_reads': sum(row.ok and row.call.name == 'browser_read' and 'query_click' in row.audit
                                  for row in receipts)}


def audit_unit(directory: Path, target: Target) -> JsonObject:
    wire = jsonl(directory / 'provider.jsonl')
    requests = [row for row in wire if row.get('phase') == 'request']
    responses = [row for row in wire if row.get('type') == 'response']
    if not requests or len(requests) != len(responses) or len(response_actions(directory / 'provider.jsonl')) != len(responses):
        raise ValueError('Missing or unmatched real model requests/responses.')
    if any(row.get('type') == 'scripted_acquisition' for row in wire):
        raise ValueError('Scripted model proposals are forbidden in the adoption experiment.')
    for row in requests:
        payload = decode_json_object(row['payload'])
        if row.get('attempt') != 1 or any(payload.get(key) != value for key, value in (
                ('model', target.model), ('seed', target.seed), ('temperature', 0), ('stream', False))):
            raise ValueError('Hidden retry or changed model sampling configuration.')
    usage = provider_metrics(directory / 'provider.jsonl')
    if usage['usage_complete'] is not True:
        raise ValueError('Measured model usage is incomplete.')
    receipts = [item.result for item in tool_receipts(directory / 'state')]
    presentation = jsonl(directory / 'presentation.jsonl')
    return {**usage, **invocation_metrics(jsonl(directory / 'invocations.jsonl'), receipts),
            'eligible_requests': sum(row['eligible'] is True for row in presentation),
            'hinted_requests': sum(bool(row['hint']) for row in presentation)}


def run_unit(directory: Path, scenario: str, wording: str, arm: str, target: Target,
             source: str, harness: str) -> JsonObject:
    def wording_transform(task: str) -> str:
        return task if wording == 'original' else task.replace('先观察页面', '先获取当前页面的控件观察').replace(
            '最后报告实际净重和校验码', '最后给出正式正文中的净重和校验码')

    assert_stable(source, harness)
    with intervention(directory, arm, source, harness):
        row = decode_json_object(live.run_case(directory, 'chat', target, reread=scenario == 'reread',
            restore_snapshots=scenario == 'reread', structured=scenario == 'legacy_structured',
            stale_ref=scenario == 'stale_ref', additional_required_tools=(WAIT,), task_transform=wording_transform))
    assert_stable(source, harness)
    row.update(audit_unit(directory, target))
    row.update(scenario=scenario, wording=wording, arm=arm, source_digest=source, harness_digest=harness)
    write_json(directory / 'experiment.json', row)
    return row


def audit_pairs(root: Path, rows: list[JsonObject]) -> None:
    pairs: dict[tuple[str, str], list[JsonObject]] = {}
    for row in rows:
        pairs.setdefault((str(row['scenario']), str(row['wording'])), []).append(row)
    for group in pairs.values():
        if len(group) != 2 or {row['arm'] for row in group} != set(ARMS):
            raise ValueError('Incomplete paired experiment.')
        directories = [root / str(row['unit']) for row in group]
        if len({(directory / 'task.txt').read_bytes() for directory in directories}) != 1:
            raise ValueError('Paired tasks differ.')
        initial = [decode_json_object(jsonl(directory / 'presentation.jsonl')[0]['snapshot']) for directory in directories]
        for key in ('tool_names', 'nested_tool_names', 'allowed', 'policy', 'specs', 'executors'):
            if initial[0][key] != initial[1][key]:
                raise ValueError(f'Paired authority or menu differs: {key}')


def aggregate(rows: list[JsonObject]) -> JsonObject:
    result: JsonObject = {}
    for arm in ARMS:
        selected = [row for row in rows if row['arm'] == arm]
        total: JsonObject = {'runs': len(selected), 'passed': sum(row['passed'] is True for row in selected)}
        for key in ('model_calls', 'prompt_tokens', 'completion_tokens', 'result_invocations', 'result_delivered',
                    'manual_wait_requests', 'manual_read_requests', 'delivered_clicks', 'uncertain_clicks',
                    'eligible_requests', 'hinted_requests'):
            total[key] = sum(require_json_integer(row[key], key) for row in selected)
        total['eligible_units'] = sum(require_json_integer(row['eligible_requests'], 'eligible_requests') > 0
                                      for row in selected)
        total['hinted_units'] = sum(require_json_integer(row['hinted_requests'], 'hinted_requests') > 0
                                    for row in selected)
        result[arm] = total
    return result


def paired_comparison(rows: list[JsonObject]) -> JsonObject:
    pairs: dict[tuple[str, str], dict[str, JsonObject]] = {}
    for row in rows:
        pairs.setdefault((str(row['scenario']), str(row['wording'])), {})[str(row['arm'])] = row
    complete = [pair for pair in pairs.values() if set(pair) == set(ARMS)]
    common = [pair for pair in complete if all(row['passed'] is True for row in pair.values())]
    costs: JsonObject = {}
    for metric in ('model_calls', 'prompt_tokens', 'completion_tokens'):
        costs[metric] = [sum(require_json_integer(pair[arm][metric], metric) for pair in common) for arm in ARMS]
    return {'complete_pairs': len(complete), 'gained': sum(pair['menu']['passed'] is not True
        and pair['call_hint']['passed'] is True for pair in complete),
        'lost': sum(pair['menu']['passed'] is True and pair['call_hint']['passed'] is not True for pair in complete),
        'common_successes': len(common), 'common_success_costs_menu_then_hint': costs}


class Options(argparse.Namespace):
    run_dir: Path
    base_url: str
    timeout: float
    scenarios: Sequence[str]
    wordings: Sequence[str]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--base-url', default='http://127.0.0.1:5001/v1')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS, default=SCENARIOS)
    parser.add_argument('--wordings', nargs='+', choices=('original', 'paraphrase'), default=('original', 'paraphrase'))
    args = parser.parse_args(namespace=Options())
    if len(set(args.scenarios)) != len(args.scenarios) or len(set(args.wordings)) != len(args.wordings):
        parser.error('Scenario and wording selections must be distinct.')
    target = live.select_target(args.timeout, args.base_url)
    source, harness = production_source_digest(), harness_digest()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    write_json(args.run_dir / 'manifest.json', {'source_digest': source, 'harness_digest': harness,
        'target': {'base_url': target.base_url, 'model': target.model, 'seed': target.seed, 'timeout': target.timeout},
        'scenarios': list(args.scenarios), 'wordings': list(args.wordings),
        'arms': list(ARMS), 'additional_required_tools': [WAIT]})
    rows: list[JsonObject] = []
    try:
        for wi, wording in enumerate(args.wordings):
            for si, scenario in enumerate(args.scenarios):
                for arm in ARMS if (si + wi) % 2 == 0 else tuple(reversed(ARMS)):
                    unit = f'{wording}-{scenario}-{arm}'
                    print('Starting ' + unit, flush=True)
                    row = run_unit(args.run_dir / unit, scenario, wording, arm, target, source, harness)
                    row['unit'] = unit
                    rows.append(row)
                    write_json(args.run_dir / 'summary.json', {'valid': True, 'complete': False,
                        'aggregate': aggregate(rows), 'paired_comparison': paired_comparison(rows), 'runs': rows})
                    status: JsonObject = {key: row[key] for key in ('passed', 'result_invocations', 'model_calls')}
                    print(json.dumps(status), flush=True)
        audit_pairs(args.run_dir, rows)
        assert_stable(source, harness)
    except Exception as exc:
        write_json(args.run_dir / 'summary.json', {'valid': False, 'complete': False,
            'error': f'{type(exc).__name__}: {exc}', 'runs': rows})
        raise
    write_json(args.run_dir / 'summary.json', {'valid': True, 'complete': True, 'aggregate': aggregate(rows),
        'paired_comparison': paired_comparison(rows), 'runs': rows})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
