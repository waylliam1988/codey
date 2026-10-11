"""Preregistered Local discovery then three-arm production-lifecycle repair study.

python -m tests.manual.counterexample_review_quality_ab --run-dir artifacts/NAME
Only experimental child-process reviewer adapters are replaced, never production files.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from tests.manual.agent_stability_cases import snapshot_files
from tests.manual.agent_stability_measurements import terminal_state, usage_totals
from tests.manual.agent_stability_proxy import Proxy, local_url
from tests.manual.codey_vs_pi_agent_stability_ab import _new_project_root, _run_process, _wait_for_backend_idle
from tests.manual.counterexample_review_adapter import online_manifest
from tests.manual.counterexample_review_cases import cases, score_probe
from tests.manual.open_code_review_self_review_ab import digest, first_review, json_lines, verify

ROOT = Path(__file__).resolve().parents[2]


def score_evidence(case, evidence, scorer=score_probe):
    scored = []
    for review in evidence:
        actual = (review.get('actual') or {}).get('observations', [])
        for index, hypothesis in enumerate(review.get('hypotheses', [])):
            score = scorer(case, hypothesis['probe'], actual[index]) if index < len(actual) else {
                'valid_domain': None, 'expected_correct': None, 'distinguishes': False}
            scored.append({'probe': hypothesis['probe'],
                           'actual': actual[index] if index < len(actual) else None, **score})
    return scored


def run_arm(case, seed, arm, phase, directory, proxy, options, model, *, worker_module=None,
            worker_phase=None, extra_manifest=None, evidence_scorer=score_probe):
    directory.mkdir(parents=True)
    state, home, trace = (directory / p for p in ('state', 'home', 'trace'))
    for p in (state, home, trace):
        p.mkdir()
    project = _new_project_root()
    for name, source in case.files.items():
        (project / name).write_text(source, encoding='utf-8')
    baseline = snapshot_files(project)
    if phase == 'discovery':
        (project / 'app.py').write_text(case.candidate, encoding='utf-8')
    manifest = directory / 'online.json'
    manifest.write_text(json.dumps(online_manifest(case) | (extra_manifest or {})), encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if not k.endswith(('_API_KEY', '_AUTH_TOKEN', '_OAUTH_TOKEN'))}
    env.update({'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8',
        'PYTHONPATH': str(ROOT), 'HOME': str(home), 'USERPROFILE': str(home),
        'HOMEDRIVE': home.drive, 'HOMEPATH': str(home)[len(home.drive):],
        'NATIVE_TOOLS': '1', 'AGENT_AB_TRACE': str(trace), 'AGENT_AB_ORACLE': '0',
        'AGENT_AB_SEED': str(seed), 'AGENT_AB_TEMPERATURE': str(options.temperature),
        'AGENT_AB_WAIT': '0', 'AGENT_AB_LONG_OUTPUT': '0',
        'LOCAL_OPENAI_BASE_URL': f'http://127.0.0.1:{proxy.server_port}/v1',
        'LOCAL_OPENAI_MODEL': model, 'LOCAL_OPENAI_API_KEY': 'local',
        'LOCAL_OPENAI_CONTEXT_WINDOW': '32768', 'LOCAL_OPENAI_CONTEXT_RESERVE': str(options.max_tokens),
        'LOCAL_OPENAI_CONTEXT_KEEP': '12000'})
    _wait_for_backend_idle(options.upstream, timeout=30)
    proxy.active_arm = f'{phase}/{case.name}/{seed}/{arm}'
    proxy.generation_requests = 0
    begin = len(proxy.records)
    command = [sys.executable, '-B', '-m', worker_module or 'tests.manual.counterexample_review_experiment_worker',
        '--project', str(project), '--state-home', str(state), '--manifest', str(manifest),
        '--phase', worker_phase or phase, '--arm', arm, '--max-turns', str(options.max_turns)]
    process = _run_process(command, project, env, directory, deadline=time.monotonic() + options.timeout)
    _wait_for_backend_idle(options.upstream, timeout=30)
    with proxy.record_lock:
        records = json.loads(json.dumps(proxy.records[begin:]))
    (directory / 'requests.json').write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    verification = verify(case, project, trace, baseline)
    observations = json_lines(state / 'review-observations.jsonl')
    discovery_path = state / 'discovery.json'
    if discovery_path.exists():
        review = json.loads(discovery_path.read_text(encoding='utf-8'))
        observations = [{'review': review}]
    evidence = json_lines(state / 'counterexamples.jsonl')
    terminal = terminal_state(process['rows'])
    proofs = []
    for path in (state / 'run_traces').rglob('*.json'):
        proofs.extend(json.loads(path.read_text(encoding='utf-8')).get('completion_proofs', []))
    source = (project / 'app.py').read_text(encoding='utf-8')
    (directory / 'final-app.py').write_text(source, encoding='utf-8')
    valid = all(verification[k] for k in ('visible_passed', 'hidden_passed', 'scope_ok', 'agent_verification_fresh'))
    receipt = (terminal.get('event') or {}).get('receipt', {}).get('verification', {})
    proof = bool(receipt.get('proof_refs') and receipt.get('trust') == 'trusted'
                 and any(p.get('satisfied') is True for p in proofs))
    result = {'case': case.name, 'cohort': case.cohort, 'buggy': case.buggy, 'seed': seed, 'arm': arm, 'phase': phase,
        'candidate_sha256': digest(case.candidate), 'final_sha256': digest(source), 'process_status': process['status'],
        'seconds': process['wall_time_seconds'], 'returncode': process['returncode'], 'terminal': terminal,
        'verification': verification, 'first_review': first_review(observations), 'reviews': observations,
        'evidence': evidence, 'offline_probe_scores': score_evidence(case, evidence, evidence_scorer),
        'usage': usage_totals(records),
        'completion_proofs': proofs, 'qualified_patch': valid, 'completion_proof_present': proof,
        'success': phase == 'repair' and valid and terminal['completed'] and proof and process['returncode'] == 0,
        'false_completion': phase == 'repair' and terminal['completed'] and not valid,
        'harness_error': phase == 'discovery' and not discovery_path.exists(), 'project': str(project)}
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def summarize(rows):
    result = {}
    for phase in ('discovery', 'repair'):
        result[phase] = {}
        for arm in ('no-review', 'codey', 'counterexample'):
            selected = [r for r in rows if r['phase'] == phase and r['arm'] == arm]
            if not selected:
                continue
            bugs, controls = ([r for r in selected if r['buggy']], [r for r in selected if not r['buggy']])
            probes = [p for r in selected for p in r['offline_probe_scores']]
            result[phase][arm] = {'runs': len(selected), 'bug_cases': len(bugs), 'correct_controls': len(controls),
                'bug_rejections': sum(r['first_review']['verdict'] == 'changes_requested' for r in bugs),
                'false_rejections': sum(r['first_review']['verdict'] == 'changes_requested' for r in controls),
                'unavailable_reviews': sum(r['first_review']['status'] != 'available' for r in selected),
                'qualified_patches': sum(r['qualified_patch'] for r in selected),
                'full_successes': sum(r['success'] for r in selected),
                'bug_repair_successes': sum(r['success'] for r in bugs),
                'correct_control_successes': sum(r['success'] for r in controls),
                'correct_control_regressions': sum(not r['verification']['hidden_passed'] for r in controls),
                'false_completions': sum(r['false_completion'] for r in selected),
                'probes': len(probes), 'invalid_domain': sum(p['valid_domain'] is False for p in probes),
                'wrong_expectations': sum(p['expected_correct'] is False for p in probes),
                'valid_counterexamples': sum(p['expected_correct'] is True and p['distinguishes'] for p in probes),
                'known_tokens': sum(r['usage']['known_token_usage'] or 0 for r in selected),
                'usage_complete': all(r['usage']['usage_complete'] for r in selected),
                'seconds': sum(r['seconds'] for r in selected), 'harness_errors': sum(r['harness_error'] for r in selected)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--upstream', default='http://127.0.0.1:5001')
    parser.add_argument('--seeds', nargs='+', type=int, default=[71, 72])
    parser.add_argument('--timeout', type=int, default=240)
    parser.add_argument('--max-turns', type=int, default=12)
    parser.add_argument('--max-tokens', type=int, default=1536)
    parser.add_argument('--request-limit', type=int, default=24)
    parser.add_argument('--temperature', type=float, default=.2)
    options = parser.parse_args()
    options.upstream = local_url(options.upstream)
    options.run_dir = options.run_dir.resolve()
    selected = cases()
    from codey.task.entry_auth import derive_entry_auth
    for case in selected:
        if 'project.write' in derive_entry_auth({'task': case.task, 'intent': 'project'}, project='fixture').denied_capabilities:
            raise ValueError('candidate edit unexpectedly denied: ' + case.name)
    options.run_dir.mkdir(parents=True, exist_ok=False)
    with urlopen(options.upstream + '/v1/models', timeout=5) as response:
        model = json.load(response)['data'][0]['id']
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    production_hashes = {p.relative_to(ROOT).as_posix(): digest(p.read_text(encoding='utf-8'))
                         for p in (ROOT / 'codey').rglob('*.py')}
    plan = {'commit': commit, 'model': model, 'seeds': options.seeds,
        'discovery_arms': ['codey', 'counterexample'], 'repair_arms': ['no-review', 'codey', 'counterexample'],
        'discovery_runs': len(selected)*len(options.seeds)*2,
        'repair_runs': sum(c.cohort == 'holdout' for c in selected)*len(options.seeds)*3,
        'cases': [{'name': c.name, 'cohort': c.cohort, 'candidate_sha256': digest(c.candidate),
                   'oracle_sha256': digest(c.oracle), 'task': c.task} for c in selected],
        'controls': vars(options) | {'run_dir': str(options.run_dir)},
        'production_hashes': production_hashes,
        'limitations': ['Frozen authored candidates, not natural Writer self-review.',
            'JSON synchronous return observations only; alias/mutation properties deferred.',
            'Online semantic judgment remains model inference; offline reference is never injected.',
            'No-review removes actual review call only; mandatory completion requirements remain.',
            'No prompt adjustment or outcome-based reruns after preregistration.']}
    (options.run_dir / 'preregistered.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    proxy = Proxy(('127.0.0.1', 0), options.upstream, options.timeout,
                  journal=options.run_dir / 'http.jsonl', request_limit=options.request_limit)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    rows = []
    def save():
        (options.run_dir / 'result.json').write_text(json.dumps({'plan': plan, 'runs': rows,
            'summary': summarize(rows)}, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        for phase in ('discovery', 'repair'):
            subset = selected if phase == 'discovery' else [c for c in selected if c.cohort == 'holdout']
            for index, case in enumerate(subset):
                for seed in options.seeds:
                    arms = ['codey', 'counterexample'] if phase == 'discovery' else ['no-review', 'codey', 'counterexample']
                    shift = (index + options.seeds.index(seed)) % len(arms)
                    arms = arms[shift:] + arms[:shift]
                    for arm in arms:
                        row = run_arm(case, seed, arm, phase,
                            options.run_dir / f'{phase}-{case.name}-{seed}-{arm}', proxy, options, model)
                        rows.append(row)
                        save()
                        print(json.dumps({'phase': phase, 'case': case.name, 'seed': seed, 'arm': arm,
                            'review': row['first_review']['verdict'], 'success': row['success'],
                            'seconds': round(row['seconds'], 2)}), flush=True)
                        if row['harness_error']:
                            raise RuntimeError('discovery worker did not produce a result; experiment halted')
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)
        current = {p.relative_to(ROOT).as_posix(): digest(p.read_text(encoding='utf-8'))
                   for p in (ROOT / 'codey').rglob('*.py')}
        if current != production_hashes:
            raise RuntimeError('production source changed during the experiment')
        save()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
