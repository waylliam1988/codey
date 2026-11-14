"""Separate autonomous target choice from seeded readback and cross-record answer errors."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

from codey.types.json_value import JsonObject, decode_json_object, decode_json_value, require_json_string
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


def audit(directory: Path, row: JsonObject) -> JsonObject:
    receipts = [item.result for item in tool_receipts(directory / 'state')]
    reads = [receipt for receipt in receipts if receipt.ok and receipt.call.name == 'browser_read']
    linked = [receipt for receipt in reads if 'query_region' in receipt.audit]
    excerpts: list[str] = []
    for receipt in linked:
        excerpts.extend(text for _, text in passage_texts(receipt))
    answer = str(row.get('answer') or '')
    evidence = evidence_checks(receipts, answer, FACTS)
    return {**row, 'evidence': evidence,
            'passed': row.get('passed') is True and evidence['correct_evidence_cited'] is True,
            'targeted_read_reached': bool(linked),
            'target_read_correct': any('TARGET_173' in str(receipt.audit['query_region']) for receipt in linked),
            'neighbor_in_target_passages': any('OTHER_174' in excerpt for excerpt in excerpts),
            'neighbor_in_answer': 'OTHER_174' in answer or '12:10' in answer}


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
    args = parser.parse_args(namespace=Options())
    if args.audit_run is not None:
        print(json.dumps(reaudit(args.audit_run)), flush=True)
        return 0
    assert args.run_dir is not None
    args.run_dir.mkdir(parents=True, exist_ok=False)
    target = select_target(args.timeout, 'http://127.0.0.1:5001/v1')
    rows: list[JsonObject] = []
    for name, html in CASES.items():
        for seeded in (False, True):
            directory = args.run_dir / (name + ('-seeded' if seeded else '-autonomous'))
            case = Case('请查订单 A-173 的配送详情，报告预计送达时间和回执号，附对应证据。',
                        html, FACTS, ('browser_click', 'browser_read'))
            if seeded:
                with patch('tools.browser_leaf_exposure_ab.ExperimentProvider', SeededProvider):
                    result = run_arm(directory, case, 'A', target)
            else:
                result = run_arm(directory, case, 'A', target)
            row = audit(directory, result)
            row.update(layout=name, seeded=seeded)
            rows.append(row)
            write_json(directory / 'scope-audit.json', row)
            status: JsonObject = {key: row[key] for key in ('layout', 'seeded', 'passed', 'targeted_read_reached',
                'target_read_correct', 'neighbor_in_target_passages', 'neighbor_in_answer')}
            print(json.dumps(status), flush=True)
    write_json(args.run_dir / 'summary.json', rows)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
