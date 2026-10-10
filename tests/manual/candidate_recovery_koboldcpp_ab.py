"""Loopback candidate preflight and frozen-source production recovery A/B.

Two native tasks and two explicitly recorded failure prefixes are scored
separately. Repairs and final reviews use the real loaded Local model. The
outside oracle never supplies agent verification or completion evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from tests.manual import codey_vs_pi_agent_stability_ab as common
from tests.manual.agent_stability_cases import TASK_CASES
from tests.manual.agent_stability_proxy import local_url

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    ('test-first', '', 51),
    ('normalize-name', '', 52),
    ('test-first', 'stale-search-replay', 51),
    ('normalize-name', 'counterexample-repair-replay', 52),
)


def isolated_project(case, variant):
    project = common._new_project_root() / case / variant
    if any((folder / '.git').exists() for folder in (project, *project.parents)):
        raise ValueError('Live fixture must not inherit a parent Git worktree')
    return project


def source_digest(root):
    digest = hashlib.sha256()
    for path in sorted((root / 'codey').rglob('*.py')):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--mode', choices=('preflight', 'production-ab'), required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--upstream', default='http://127.0.0.1:5001')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--scenarios', default=','.join(boundary or case for case, boundary, _ in CASES))
    args = parser.parse_args()
    selected = args.scenarios.split(',')
    if not selected or any(item not in {boundary or case for case, boundary, _ in CASES} for item in selected):
        parser.error('Unknown recovery scenario')
    if args.mode == 'production-ab' and args.baseline is None:
        parser.error('production-ab requires the frozen baseline checkout')
    upstream = local_url(args.upstream)
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    with urlopen(upstream + '/v1/models', timeout=5) as response:
        model = json.load(response)['data'][0]['id']
    with urlopen(upstream + '/api/extra/version', timeout=5) as response:
        backend = json.load(response)
    variants = [('proposal', ROOT)] if args.mode == 'preflight' else [('current', args.baseline.resolve()), ('production', ROOT)]
    report = {'mode': args.mode, 'model': model, 'backend': backend,
              'sampling': {'temperature': 0, 'seeds': [51, 52]},
              'budgets': {'window': 32768, 'output': 2048, 'keep': 12000, 'turns': 30,
                          'generation_requests': 24, 'seconds_per_arm': args.timeout},
              'sources': {v: source_digest(root) for v, root in variants},
              'selected_scenarios': selected,
              'control_sha256': {path: hashlib.sha256((ROOT / 'tests/manual' / path).read_bytes()).hexdigest()
                  for path in ('candidate_recovery_koboldcpp_ab.py', 'candidate_recovery_koboldcpp_worker.py',
                               'candidate_recovery_test_proposal.py')},
              'limitations': ['Recorded failure prefixes are not native task success scores.',
                              'Same seed does not imply identical requests or deterministic model output.',
                              'Only the two targeted task contracts are measured, not the full twenty runs.'],
              'results': []}

    def save():
        (directory / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    original_process = common._run_process
    for index, (case_id, boundary, seed) in enumerate(CASES):
        if (boundary or case_id) not in selected:
            continue
        case = next(c for c in TASK_CASES if c.case_id == case_id)
        ordered = variants if index % 2 == 0 else list(reversed(variants))
        for variant, source in ordered:
            common._wait_for_backend_idle(upstream, timeout=60)
            label = boundary or case_id
            arm_dir = directory / label / variant
            arm_dir.mkdir(parents=True)
            project = isolated_project(label, variant)
            common._fixture(project, case)
            proxy = common._Proxy(('127.0.0.1', 0), upstream, args.timeout,
                                  journal=arm_dir / 'requests.jsonl', request_limit=24)
            proxy.active_arm = f'{label}/{variant}/{seed}'
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()

            def dispatch(command, root, env, run_dir, *, variant=variant, boundary=boundary, **kwargs):
                command = [v.replace('tests.manual.agent_stability_codey_worker',
                                     'tests.manual.candidate_recovery_koboldcpp_worker') for v in command]
                env = {**env, 'CANDIDATE_AB_VARIANT': variant, 'CANDIDATE_AB_BOUNDARY': boundary}
                return original_process(command, root, env, run_dir, **kwargs)

            row = {'variant': variant, 'boundary': boundary, 'case': case_id, 'seed': seed}
            try:
                with patch.object(common, 'ROOT', source), patch.object(common, '_run_process', dispatch):
                    row.update(common._run_arm('codey', project, arm_dir / 'agent',
                        f'http://127.0.0.1:{proxy.server_port}', proxy.records, case=case,
                        baseline_hashes=common._snapshot_files(project), max_turns=30, model_id=model,
                        max_tokens=2048, seed=seed, timeout=args.timeout))
            except Exception as exc:
                row.update(status='harness_error', error=f'{type(exc).__name__}: {exc}')
            finally:
                proxy.shutdown()
                try:
                    row['backend_drain_seconds'] = common._wait_for_backend_idle(upstream, timeout=60)
                except Exception as exc:
                    row['backend_isolation_error'] = str(exc)
                proxy.server_close()
            row['native_full_task'] = not boundary
            report['results'].append(row)
            save()
            print(json.dumps({'case': label, 'variant': variant, 'status': row.get('status'),
                              'success': row.get('metrics', {}).get('scenario_success'),
                              'seconds': row.get('wall_time_seconds')}, ensure_ascii=False), flush=True)
            if row.get('backend_isolation_error') or row.get('status') == 'harness_error':
                return 3
    return 0 if all(r.get('metrics', {}).get('scenario_success') for r in report['results']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
