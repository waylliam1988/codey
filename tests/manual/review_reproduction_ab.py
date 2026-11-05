"""Three-arm independent/executable Review comparison on identical candidates."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.providers import controls
from codey.providers.registry import connect_provider
from tests.manual.coding_pipeline_cases import BY_NAME, CodingCase
from tests.manual.coding_pipeline_value_ab import EvidenceProvider, save, source_metadata
from tests.manual.coding_review_value_ab import run_arm, seed_files
from tests.manual.reproducible_review_adapter import ReproductionReviewer

DEFAULTS = CodingCase('preserve-defaults', 'Implement apply_defaults(settings, defaults) in settings.py. '
    'Return a new dictionary containing settings plus defaults for absent keys. '
    'Existing values including 0, False, empty strings and None take precedence. '
    'Preserve both input dictionaries. Keep the tests unchanged.',
    {'settings.py': 'def apply_defaults(settings, defaults):\n    raise NotImplementedError\n',
     'test_settings.py': 'from settings import apply_defaults\ndef test_happy():\n    assert apply_defaults({"port":8080},{"port":80,"mode":"safe"})=={"port":8080,"mode":"safe"}\n',
     'pytest.ini': '[pytest]\n'}, ('settings.py',), ('settings.py',),
    (('falsy', 'from settings import apply_defaults as f\ns={"x":0,"y":False,"z":None,"w":""};d={"x":9,"y":True,"z":1,"w":"default","a":1}\nassert f(s,d)=={**d,**s}'),
     ('unchanged', 'from settings import apply_defaults as f\ns={"a":0};d={"b":1};f(s,d);assert s=={"a":0} and d=={"b":1}'),
     ('new', 'from settings import apply_defaults as f\ns={"a":1};assert f(s,{})==s and f(s,{}) is not s'),
     ('empty', 'from settings import apply_defaults as f\nassert f({},{})=={} and f({},{"a":1})=={"a":1}')))

CASES = {name: BY_NAME[name] for name in ('discount-bug', 'tag-feature', 'cross-file-rename')}
CASES[DEFAULTS.name] = DEFAULTS


def candidates(name, correct):
    if name == 'preserve-defaults':
        source = ('    return {**defaults, **settings}\n' if correct else
                  '    result = dict(settings)\n    for key,value in defaults.items():\n        if not result.get(key): result[key]=value\n    return result\n')
        return {'settings.py': 'def apply_defaults(settings, defaults):\n' + source}
    if name == 'cross-file-rename':
        files = {'totals.py': 'def total(values):\n    return sum(values)\n',
                 'test_totals.py': 'from totals import total\n\ndef test_total():\n    assert total([2,3])==5\n'}
        if correct:
            files['report.py'] = 'from totals import total\n\ndef render(values):\n    return str(total(values))\n'
        return files
    return seed_files(name, correct)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', default='discount-bug,tag-feature')
    parser.add_argument('--arms', default='baseline,independent,reproduce')
    parser.add_argument('--reverse', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    arms = args.arms.split(',')
    if args.reverse:
        arms.reverse()
    controls.begin_task_context('review-reproduction-ab')
    provider = EvidenceProvider(connect_provider('local'))
    payload = {'model': provider.model, 'seeded_candidates': True, 'rows': [], **source_metadata()}
    payload['experiment_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [Path(__file__), Path(__file__).with_name('reproducible_review_adapter.py')]}
    try:
        for name in args.cases.split(','):
            for correct in (False, True):
                for arm in arms:
                    if arm not in {'baseline', 'independent', 'reproduce'}:
                        raise ValueError(arm)
                    factory = None if arm == 'baseline' else lambda p, root, task, kw, row, arm=arm: ReproductionReviewer(
                        p, root, task, kw, row, execute=arm == 'reproduce')
                    row = run_arm(provider, CASES[name], correct, 'repair', seed_override=candidates,
                                  reviewer_factory=factory)
                    row['arm'] = arm
                    payload['rows'].append(row)
                    save(args.output, payload)
                    print(json.dumps({k: row.get(k) for k in ('case','seed','arm','success','quality_score',
                        'stop_reason','approved_current_snapshot','http_requests','input_context_tokens','error')}), flush=True)
    finally:
        provider.close()
        controls.end_task_context()
    return 0 if all(r['success'] for r in payload['rows']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
