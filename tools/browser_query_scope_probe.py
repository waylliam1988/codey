"""Separate autonomous target choice from seeded readback and cross-record answer errors."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from unittest.mock import patch

from codey.research.source_observation import source_observation
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    decode_json_value,
    require_json_string,
)
from codey.utils.citation_scanner import mask_markdown_code
from tools.browser_leaf_exposure_ab import Case, evidence_checks, passage_texts, run_arm
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target, write_json

CASES = {
    'inline': '''<section aria-label="订单 A-173"><h2>订单 A-173</h2>
      <button aria-controls="target" onclick="window.clicks=(window.clicks||0)+1;
      document.querySelector('#target').textContent='预计周五 18:20 送达，回执 TARGET_173。';">查看详情</button>
      <article id="target">尚未查询配送详情。</article></section>
      <section aria-label="订单 A-174"><h2>订单 A-174</h2>
      <button aria-controls="neighbor">查看详情</button>
      <article id="neighbor">预计周四 12:10 送达，回执 OTHER_174。</article></section>''',
    'external_panel': '''<section aria-label="订单 A-173"><h2>订单 A-173</h2>
      <button aria-controls="target" onclick="window.clicks=(window.clicks||0)+1;
      document.querySelector('#target').textContent='预计周五 18:20 送达，回执 TARGET_173。';">查看详情</button></section>
      <section aria-label="订单 A-174"><h2>订单 A-174</h2><button aria-controls="neighbor">查看详情</button>
      <article id="neighbor">预计周四 12:10 送达，回执 OTHER_174。</article></section>
      <article id="target">尚未查询配送详情。</article>''',
}
ACQUIRE = '''const s=unwrap(await tools.browser_snapshot({}));
const context=s.contexts.find(c=>c.label==='订单 A-173');
const button=s.elements.find(e=>e.context_id===context.id && e.role==='button');
return unwrap(await tools.browser_click({ref:button.ref,purpose:'result'}));'''
FACTS = ('18:20', 'TARGET_173')


class SeededProvider(ExperimentProvider):
    def __init__(self, target: Target, directory: Path, *, fixed_acquisition: bool) -> None:
        super().__init__(target, directory, fixed_acquisition=True)
        self.acquisition_action = {'tool': 'codemode', 'args': {'code': ACQUIRE, 'inputs': {}}}


class Options(argparse.Namespace):
    run_dir: Path | None
    audit_run: Path | None
    timeout: float
    seeded_only: bool


def audit(directory: Path, row: JsonObject) -> JsonObject:
    saved = tool_receipts(directory / 'state')
    receipts = [item.result for item in saved]
    clicks = {item.identity: item.result for item in saved if item.result.call.name == 'browser_click'}
    reads = [receipt for receipt in receipts if receipt.ok and receipt.call.name == 'browser_read']
    linked = [receipt for receipt in reads if 'query_region' in receipt.audit]
    excerpts: list[str] = []
    cited: list[JsonObject] = []
    versions: list[JsonObject] = []
    citations = {match.group(1) for match in re.finditer(r'\[([^\[\]\s]+)\]', mask_markdown_code(str(row.get('answer') or '')))}
    for receipt in linked:
        source, body, _ = source_observation(receipt.canonical.get('research_observation'))
        click = clicks.get(str(receipt.audit.get('query_click')))
        baseline = click.audit.get('query_baseline') if click is not None else None
        target = baseline.get('target') if isinstance(baseline, dict) else None
        versions.append(canonical_json_object({'source_hash': source.text_hash, 'url': source.final_url,
            'formal_source_has_target': all(fact in body for fact in FACTS),
            'formal_source_has_neighbor': 'OTHER_174' in body, 'target_node': target,
            'click_receipt': receipt.audit.get('query_click')}))
        passages = passage_texts(receipt)
        excerpts.extend(text for _, text in passages)
        for ref, text in passages:
            if ref in citations:
                cited.append({'ref': ref, 'text': text, 'target_facts': all(fact in text for fact in FACTS),
                              'neighbor': 'OTHER_174' in text})
    answer = str(row.get('answer') or '')
    evidence = evidence_checks(receipts, answer, FACTS)
    return canonical_json_object({**row, 'evidence': evidence,
            'passed': row.get('passed') is True and evidence['correct_evidence_cited'] is True,
            'targeted_read_reached': bool(linked),
            'target_read_correct': any(isinstance(region := receipt.audit['query_region'], dict)
                and isinstance(region_text := decode_json_object(region).get('text'), str)
                and all(fact in region_text for fact in FACTS)
                for receipt in linked),
            'neighbor_in_target_passages': any('OTHER_174' in excerpt for excerpt in excerpts),
            'neighbor_in_answer': 'OTHER_174' in answer or '12:10' in answer,
            'actually_cited_target_passages': cited,
            'target_read_source_versions': versions,
            'cited_target_passage_contains_neighbor': any(item['neighbor'] is True for item in cited),
            'exact_target_passage_supported': any(item['target_facts'] is True for item in cited),
            'scope_passed': bool(excerpts) and any(all(fact in text for fact in FACTS) for text in excerpts)
                and not any('OTHER_174' in text for text in excerpts)
                and any(item['target_facts'] is True and item['neighbor'] is False for item in cited)})


def reaudit(directory: Path) -> list[JsonObject]:
    raw_summary: object = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
    original = decode_json_value(raw_summary)
    if not isinstance(original, list):
        raise ValueError('Expected saved scope experiment rows.')
    rows: list[JsonObject] = []
    for raw in original:
        row = decode_json_object(raw)
        layout = require_json_string(row['layout'], 'layout')
        seeded = row['seeded']
        if layout not in CASES or type(seeded) is not bool:
            raise ValueError('Invalid saved scope experiment arm.')
        result = audit(directory / (layout + ('-seeded' if seeded else '-autonomous')), row)
        result.update(original_evidence=row['evidence'], original_passed=row['passed'])
        rows.append(result)
    write_json(directory / 'scope-reaudit.json', rows)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run-dir', type=Path)
    mode.add_argument('--audit-run', type=Path, help='Reaudit saved receipts without making model or browser calls.')
    parser.add_argument('--timeout', default=180, type=float)
    parser.add_argument('--seeded-only', action='store_true', help='Fix target acquisition to measure evidence delivery only.')
    args = parser.parse_args(namespace=Options())
    if args.audit_run is not None:
        print(json.dumps(reaudit(args.audit_run)), flush=True)
        return 0
    assert args.run_dir is not None
    args.run_dir.mkdir(parents=True, exist_ok=False)
    target = select_target(args.timeout, 'http://127.0.0.1:5001/v1')
    from tools.browser_result_adoption_ab import assert_stable, harness_digest
    from tools.completion_explanation_ab import production_source_digest
    source, harness = production_source_digest(), harness_digest()
    write_json(args.run_dir / 'manifest.json', {'source_digest': source, 'harness_digest': harness,
        'target': {'base_url': target.base_url, 'model': target.model, 'seed': target.seed, 'timeout': target.timeout},
        'seeded_only': args.seeded_only})
    rows: list[JsonObject] = []
    for name, html in CASES.items():
        for seeded in ((True,) if args.seeded_only else (False, True)):
            directory = args.run_dir / (name + ('-seeded' if seeded else '-autonomous'))
            case = Case('请查订单 A-173 的配送详情，报告预计送达时间和回执号，附对应证据。',
                        html, FACTS, ('browser_click', 'browser_read'))
            assert_stable(source, harness)

            class GuardedProvider(ExperimentProvider):
                def __init__(self, target: Target, directory: Path, *, fixed_acquisition: bool, _seeded: bool = seeded) -> None:
                    super().__init__(target, directory, fixed_acquisition=_seeded,
                        acquisition_action={'tool': 'codemode', 'args': {'code': ACQUIRE, 'inputs': {}}} if _seeded else None)

                def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                                          response_bytes: int, seconds: float) -> None:
                    if phase == 'request':
                        assert_stable(source, harness)
                    super()._observe_http_attempt(attempt=attempt, data=data, phase=phase,
                        response_bytes=response_bytes, seconds=seconds)

            with patch('tools.browser_leaf_exposure_ab.ExperimentProvider', GuardedProvider):
                result = run_arm(directory, case, 'A', target)
            assert_stable(source, harness)
            row = audit(directory, result)
            row.update(layout=name, seeded=seeded)
            rows.append(row)
            write_json(directory / 'scope-audit.json', row)
            status: JsonObject = {key: row[key] for key in ('layout', 'seeded', 'passed', 'targeted_read_reached',
                'target_read_correct', 'neighbor_in_target_passages', 'neighbor_in_answer')}
            print(json.dumps(status), flush=True)
    write_json(args.run_dir / 'summary.json', rows)
    write_json(args.run_dir / 'validity.json', {'valid': True, 'complete': True, 'source_digest': source,
        'harness_digest': harness, 'runs': len(rows), 'scripted_acquisitions': len(rows) if args.seeded_only else len(CASES)})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
