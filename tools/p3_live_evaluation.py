"""Run fixed local-model cases or compare complete production-run artifacts."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession
from codey.durable.sqlite import SQLiteDurableStore
from codey.knowledge.store import KnowledgeStore
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    provider_metrics,
    write_json,
)
from tools.p0_mixed_live_gate import mixed_run
from tools.research_query_delivery_live_gate import QueryCase, run_case

RESEARCH_CASE = QueryCase('/p3-controlled-research',
    'Compare the Cedar and Birch studies on the same 10 L workload. Report each original measurement and whether the two studies agree. '
    'Cite the read source passage, and present their measurements as separate findings.',
    '<h1>Cedar and Birch studies</h1><p>Under the same 10 L workload, the Cedar team measured 24 kg. The Birch team measured 31 kg '
    'under the same 10 L workload. Both reports used the same instruments over the same interval.</p>', ('Cedar', 'Birch', '24', '31'))
ORIGINAL_STUDY = ('<h1>Cedar measurements</h1><p>The Atlas unit consumed 24 kg of fuel in the 10 L workload. '
    'Measurements used the D-47 bench at 20 °C and a 30-minute observation window. This is a study result for those conditions.</p>')
RESEARCH_SOURCES = QueryCase('/p3-atlas-original',
    'Compare the original Atlas measurements under the identical 10 L workload and 20 °C. Include the exact sentence from the Cedar '
    'report that states its fuel result. Cite the original. State how much the two measurements differ. Identify the copy and the '
    'independent replication separately. Include the exact fixture names p3-atlas-copy and p3-atlas-replication so each is unambiguous.',
    ORIGINAL_STUDY, ('24 kg', '31 kg', '7 kg', 'p3-atlas-copy', 'p3-atlas-replication'), additional_pages=(
        ('https://example.org/p3-atlas-copy', ORIGINAL_STUDY),
        ('https://example.net/p3-atlas-replication', '<h1>Birch replication</h1><p>The Atlas unit consumed 31 kg of fuel in the 10 L '
            'workload. Measurements used the D-47 bench at 20 °C and a 30-minute observation window. Birch recorded its own measurement.</p>'),
    ))


def continuity(source: Path, destination: Path, target: Target) -> JsonObject:
    state = source / 'state'
    with closing(DurableSession(SQLiteDurableStore(state / 'durable.sqlite3'))) as durable:
        original = next(task for task in durable.tasks() if task.execution_kind == 'model' and task.phase == 'completed')
    project = original.state.get('project')
    if not isinstance(project, str) or not project:
        raise ValueError('the source run has no original project identity')
    with closing(KnowledgeStore(source / 'knowledge')) as store:
        hits = store.search('web/component.ts', project=project, session_id='fresh-knowledge-session')
        isolated = not store.search('web/component.ts', project=project + '-other', session_id='other-session')
    original_clicks = sum(item.result.ok and item.result.call.name == 'browser_click' for item in tool_receipts(state))
    destination.mkdir(parents=True, exist_ok=False)
    provider = ExperimentProvider(target, destination, fixed_acquisition=False)
    task = ('Use our previous conversation to say what amount and record code we observed, which two verification commands '
            'passed, and in which directories. Answer from those prior results using done. No tools are needed.')
    events: list[JsonObject] = []

    def observe(event: dict[str, object]) -> None:
        row = decode_json_object(event)
        events.append(row)
        append_json(destination / 'events.jsonl', row)

    def connect(provider_id: str, *, port: int) -> ExperimentProvider:
        return provider

    before = production_source_digest()
    write_json(destination / 'input.json', {'task': task, 'source_run': original.task_id, 'seed': target.seed,
        'model': target.model, 'protocol': provider.api_protocol, 'max_turns': 5, 'context_window_tokens': target.context_window_tokens})
    try:
        with (patch('codey.app.provider_services.provider_availability', return_value={'local': True}),
              patch('codey.app.provider_services.provider_tab_availability', return_value={})):
            result = run_headless(HeadlessRequest(None, task, session_id=original.conversation_id, intent='chat',
                provider_id='local', state_home=state, research_store_root=source / 'knowledge', max_turns=5, allow_web=False,
                model_selection={'model': target.model, 'base_url': target.base_url}), emit_jsonl=observe, connect_provider=connect)
        reason = next((row.get('window_open_reason') for row in reversed(provider.context_ledger._records)
                       if row.get('window_open_reason')), None)
    finally:
        provider.close()
    answer = str(next((row.get('summary', '') for row in reversed(events) if row.get('type') == 'task_done'), ''))
    clicks = sum(item.result.ok and item.result.call.name == 'browser_click' for item in tool_receipts(state))
    stable = before == production_source_digest()
    observations: list[JsonValue] = [hit.to_dict() for hit in hits if hit.version.value.kind == 'observation']
    report: JsonObject = {'passed': result.stop_reason == 'done' and bool(observations) and isolated and clicks == original_clicks
        and stable and all(value in answer for value in ('173', 'QUARTZ_851', 'npm test', 'python -m pytest', 'service')),
        'stop_reason': result.stop_reason, 'answer': answer, 'original_clicks': original_clicks, 'final_clicks': clicks,
        'project_observations': observations, 'other_project_isolated': isolated, 'window_reason': str(reason or ''),
        'source_stable': stable, **provider_metrics(destination / 'provider.jsonl')}
    write_json(destination / 'summary.json', report)
    return report


def compare(before: Path, after: Path, output: Path) -> JsonObject:
    runs: list[JsonValue] = []
    for case in (before, after):
        row = parse_json_object((case / 'mixed-summary.json').read_text(encoding='utf-8'))
        state = case / 'state'
        receipts = tool_receipts(state)
        files = parse_json_object((case / 'project-files.json').read_text(encoding='utf-8'))
        runs.append({'path': str(case), 'passed': row['passed'], 'stop_reason': row['stop_reason'], 'elapsed_seconds': row['seconds'],
            'model': row['model'], 'edited_paths': list[JsonValue](sorted({str(receipt.result.call.args.get('path', '')) for receipt in receipts
                if receipt.result.ok and receipt.result.call.name == 'edit' and receipt.result.audit.get('changed') is True})),
            'final_files': list[JsonValue](sorted(files)), 'successful_checks': row['successful_checks'],
            'browser_clicks': sum(receipt.result.ok and receipt.result.call.name == 'browser_click' for receipt in receipts),
            'tool_rejections': sum(not receipt.result.ok for receipt in receipts), **provider_metrics(case / 'provider.jsonl')})
    report: JsonObject = {'same_task': (before / 'task.txt').read_bytes() == (after / 'task.txt').read_bytes(), 'runs': runs}
    write_json(output, report)
    return report


def research_source_checkpoint(directory: Path, target: Target) -> JsonObject:
    result = run_case(directory, RESEARCH_SOURCES, target, max_turns=10, source_access_wording=True)
    receipts = [row.result for row in tool_receipts(directory / 'state')]
    accepted = next((row for row in reversed(receipts) if row.call.name == 'submit_research_answer' and row.ok), None)
    shared_copy = False
    original_quote = False
    cited_independence = False
    if accepted is not None:
        submission = decode_json_object(accepted.canonical['research_submission'])
        validation = decode_json_object(accepted.canonical['research_evidence_check'])
        from codey.research.answer_contract import AnswerScope
        from codey.research.passages import read_passage_refs

        passage_urls = {ref: row.call.args.get('url') for row in receipts if row.ok and row.call.name in {'read', 'open_url'}
                        for ref in read_passage_refs(row.canonical.get('read_passages', []))}
        labels = {label: passage_urls.get(ref) for label, ref in AnswerScope.from_payload(submission['scope']).aliases}
        copy, replication = (url for url, _ in RESEARCH_SOURCES.additional_pages)
        shared = validation.get('shared_source_groups')
        if isinstance(shared, list):
            shared_copy = any({labels.get(key) for key in group if isinstance(key, str)} == {RESEARCH_SOURCES.url, copy}
                              for group in shared if isinstance(group, list))
        quotes = validation.get('verified_quotes')
        if isinstance(quotes, list):
            original_quote = any(isinstance(label := decode_json_object(row).get('label'), str)
                                 and labels.get(label) == RESEARCH_SOURCES.url for row in quotes)
        markdown = str(submission['markdown'])
        cited_independence = any('p3-atlas-replication' in line and any(url == replication and f'[{label}]' in line
                                for label, url in labels.items()) for line in markdown.splitlines())
    result.update(passed=result['passed'] is True and shared_copy and original_quote and cited_independence,
                  exact_repost_group=shared_copy, exact_original_quote=original_quote, replication_citation=cited_independence)
    write_json(directory / 'summary.json', result)
    return result


class Options(argparse.Namespace):
    mode: str
    directory: Path
    source: Path | None
    baseline: Path | None
    base_url: str
    context_window: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('mixed', 'research', 'research-sources', 'continuity', 'compare'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--base-url', default='http://127.0.0.1:5001/v1')
    parser.add_argument('--context-window', default=65536, type=int)
    args = parser.parse_args(namespace=Options())
    if args.mode == 'compare':
        if args.source is None or args.baseline is None:
            parser.error('compare requires --source and --baseline')
        return 0 if compare(args.baseline, args.source, args.directory)['same_task'] is True else 1
    target = replace(select_target(180, args.base_url), context_window_tokens=args.context_window)
    if args.mode == 'continuity':
        if args.source is None:
            parser.error('continuity requires --source')
        result = continuity(args.source, args.directory, target)
    elif args.mode == 'research':
        result = run_case(args.directory, RESEARCH_CASE, target, max_turns=10, source_access_wording=True)
    elif args.mode == 'research-sources':
        result = research_source_checkpoint(args.directory, target)
    else:
        return 0 if mixed_run(args.directory, target).get('passed') is True else 1
    return 0 if result.get('passed') is True else 1


if __name__ == '__main__':
    raise SystemExit(main())
