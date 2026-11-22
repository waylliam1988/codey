"""Ordinary Research query/delivery checks through the unchanged production entry."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser_contracts import RouteDecision as Route
from codey.policies.tool_requirement import require_browser_report_field
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools.browser_experiment_sources import ResumableBrowserSearchProvider as BrowserSearchProvider
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


@dataclass(frozen=True)
class QueryCase:
    path: str
    question: str
    html: str
    facts: tuple[str, ...]
    required_tools: tuple[str, ...] = ()
    subject: str = ""
    context: str = ""
    additional_pages: tuple[tuple[str, str], ...] = ()

    @property
    def url(self) -> str:
        return "https://example.com" + self.path


def cases() -> dict[str, QueryCase]:
    html = ('<section aria-label="Parcel ZX-581"><h2>Parcel ZX-581</h2><p>Status: In transit.</p>'
            '<button aria-controls="eta" onclick="setTimeout(() => {'
            "document.getElementById('eta').textContent='Arrival: Friday 18:37.';},350)\">View itinerary</button>"
            '<article id="eta">Arrival not loaded.</article></section>'
            '<section aria-label="Parcel ZX-582"><h2>Parcel ZX-582</h2><p>Arrival: Thursday 12:10.</p>'
            '<button aria-controls="other">View itinerary</button><article id="other">Other record.</article></section>')
    dynamic = QueryCase('/deliveries/ZX-581', 'ZX-581 现在到哪了，大概几点能到？', html, ('In transit', '18:37'),
                        subject='ZX-581', context='Parcel ZX-581')
    explicit = require_browser_report_field(url=dynamic.url, prefix='Arrival: ', suffix='.', pointer='/arrival',
                                            label='预计送达时间', context='Parcel ZX-581')
    return {
        'ordinary': dynamic,
        'explicit': QueryCase(dynamic.path, '查 ZX-581 的预计送达时间。', html, ('18:37',), (explicit,),
                              subject=dynamic.subject, context=dynamic.context),
        'visible': QueryCase('/inventory/K-039', 'K-039 还有多少，几点关门？',
            '<h1>K-039</h1><p>Available: 29 units. Closing time: 19:43.</p>', ('29', '19:43')),
        'slow': QueryCase(dynamic.path, dynamic.question, html.replace('},350)', '},3200)'), dynamic.facts,
                          subject=dynamic.subject, context=dynamic.context),
        'unrelated_controls': QueryCase('/archive/P-917', 'P-917 的存档号是什么？',
            '<h1>P-917</h1><p>Archive code: RQ-047.</p><button>Print</button><button>Delete</button>', ('RQ-047',)),
    }


def fact_covered(answer: str, fact: str) -> bool:
    alternatives = ('in transit', '运输中', '在途', '运送中', '运输途中') if fact == 'In transit' else (fact.casefold(),)
    return any(value in answer.casefold() for value in alternatives)


def run_case(directory: Path, case: QueryCase, target: Target, *, max_turns: int = 10,
             source_access_wording: bool = False) -> JsonObject:
    before = production_source_digest()
    directory.mkdir(parents=True, exist_ok=False)
    browser = BrowserSearchProvider(headless=True)
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    events: list[JsonObject] = []
    pages = {case.url: case.html, **dict(case.additional_pages)}

    def route(request: Route) -> None:
        body = pages.get(request.request.url)
        request.fulfill(status=200 if body is not None else 404,
            content_type='text/html; charset=utf-8', body=body if body is not None else 'Unavailable')

    def observe(event: dict[str, object]) -> None:
        value = decode_json_object(event)
        events.append(value)
        append_json(directory / 'events.jsonl', value)

    def connect(provider_id: str, *, port: int) -> ExperimentProvider:
        return provider

    task = f'帮我看看 {case.url}，{case.question}' if source_access_wording else case.question + '\n' + case.url
    if case.additional_pages:
        task += '\nRead these additional sources once:\n' + '\n'.join(url for url, _ in case.additional_pages)
    availability: dict[str, bool] = {'local': True}
    tabs: dict[str, bool] = {}
    model_selection: dict[str, object] = {'model': target.model, 'base_url': target.base_url}
    write_json(directory / 'input.json', {'task': task, 'required_tools': list(case.required_tools), 'model': target.model,
                                         'seed': target.seed, 'max_turns': max_turns, 'source_access_wording': source_access_wording})
    try:
        with (patch.object(browser, '_guard_request', route),
              patch('codey.research.search_factory.default_research_search_provider', return_value=browser),
              patch('codey.operations.task_phases.dispatch.default_research_search_provider', return_value=browser),
              patch('codey.app.provider_services.provider_availability', return_value=availability),
              patch('codey.app.provider_services.provider_tab_availability', return_value=tabs),
              patch('codey.app.review_service.run_review', return_value=None)):
            result = run_headless(HeadlessRequest(None, task, intent='research', strict_research=True, provider_id='local',
                max_turns=max_turns, state_home=directory / 'state', research_store_root=directory / 'knowledge',
                required_tools=case.required_tools, requested_capabilities=('web.read', 'browser.observe', 'browser.interact'),
                model_selection=model_selection), emit_jsonl=observe, connect_provider=connect)
    finally:
        browser.close()
        provider.close()
    receipts = [row.result for row in tool_receipts(directory / 'state')]
    terminal = next((row for row in reversed(events) if row.get('type') == 'task_done'), None)
    answer = str(terminal.get('summary') or '') if terminal is not None else ''
    reads = [row for row in receipts if row.ok and row.call.name == 'browser_read' and row.audit.get('query_click')]
    submissions = [decode_json_object(row.canonical['research_submission']) for row in receipts
                   if row.ok and row.call.name == 'submit_research_answer']
    evidence_checks: list[JsonValue] = [decode_json_object(row.canonical['research_evidence_check']) for row in receipts
                                      if row.ok and row.call.name == 'submit_research_answer' and 'research_evidence_check' in row.canonical]
    cited: set[str] = set()
    for submission in submissions[-1:]:
        citations = submission.get('citations')
        for raw in citations if isinstance(citations, list) else []:
            ref = decode_json_object(raw).get('passage_ref')
            if isinstance(ref, str):
                cited.add(ref)
    query_cited = any((not case.context or isinstance(region := row.audit.get('query_region'), dict)
                      and isinstance(owner := decode_json_object(region).get('context'), dict) and owner.get('label') == case.context)
                      and isinstance(rows := row.canonical.get('read_passages'), list)
                      and any(decode_json_object(raw).get('ref') in cited for raw in rows) for row in reads)
    stable = before == production_source_digest()
    row: JsonObject = {'passed': result.stop_reason == 'done' and stable and all(fact_covered(answer, fact) for fact in case.facts)
                      and (not case.subject or case.subject in answer)
                      and (query_cited if 'aria-controls' in case.html else True),
        'stop_reason': result.stop_reason, 'answer': answer, 'source_stable': stable, 'query_cited': query_cited,
        'clicks': sum(row.call.name == 'browser_click' and row.ok for row in receipts),
        'query_reads': len(reads), 'citation_checks': evidence_checks, **provider_metrics(directory / 'provider.jsonl')}
    write_json(directory / 'summary.json', row)
    return row


class Options(argparse.Namespace):
    run_dir: Path
    timeout: float
    kobold_base_url: str
    source_access_wording: bool


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--timeout', default=180, type=float)
    parser.add_argument('--kobold-base-url', default='http://127.0.0.1:5001/v1')
    parser.add_argument('--source-access-wording', action='store_true',
                        help='Ask to inspect the URL in the task; leave button selection and query steps autonomous.')
    args = parser.parse_args(namespace=Options())
    target = select_target(args.timeout, args.kobold_base_url)
    before = production_source_digest()
    rows: list[JsonObject] = []
    for name, case in cases().items():
        starting: JsonObject = {'starting': name}
        print(json.dumps(starting), flush=True)
        entry = run_case(args.run_dir / name, case, target, source_access_wording=args.source_access_wording)
        entry['case'] = name
        rows.append(entry)
        write_json(args.run_dir / 'summary.json', {'results': rows, 'source_before': before, 'source_after': production_source_digest()})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    return 0 if all(row['passed'] is True for row in rows) else 1


if __name__ == '__main__':
    raise SystemExit(main())
