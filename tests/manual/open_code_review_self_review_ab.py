"""Paired Local study: production Codey review vs OCR prompts/rules/read tools.

Fixed candidates isolate review; a separately labelled native cohort uses the
same 12B to author and review. No hidden oracle enters an agent's workspace.
Run with python -m tests.manual.open_code_review_self_review_ab --run-dir PATH.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from tests.manual.agent_stability_cases import execution_rows, snapshot_files
from tests.manual.agent_stability_measurements import terminal_state, usage_totals
from tests.manual.agent_stability_proxy import Proxy, local_url
from tests.manual.codey_vs_pi_agent_stability_ab import _new_project_root, _run_process, _wait_for_backend_idle
from tests.manual.open_code_review_quality_cases import cases

ROOT = Path(__file__).resolve().parents[2]


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def worker_manifest(case, candidate):
    # Deliberately exclude oracle, buggy/control labels and expected verdict.
    return {'task': case.task, 'files': case.files, 'candidate': candidate}


def json_lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def first_review(observations):
    if not observations:
        return {'status': 'not_observed', 'verdict': None, 'findings': []}
    row = observations[0]
    review = row.get('review', {})
    verdict = review.get('verdict')
    status = ('available' if verdict in {'approved', 'changes_requested'} and review.get('status') == 'complete'
              else 'unavailable')
    return {'status': status, 'verdict': verdict, 'findings': review.get('findings', []),
            'detail': row}


def native_candidate_ready(result):
    """Freeze candidates without selecting only those passing the hidden oracle."""
    terminal = result['terminal']
    review_disabled_block = (terminal.get('reason') == 'blocked' and
        (terminal.get('event') or {}).get('summary') == 'Current candidate requires a fresh approved review.')
    return ((terminal['completed'] or review_disabled_block)
            and result['process_status'] == 'completed'
            and all(result['verification'][k] for k in
                    ('visible_passed', 'scope_ok', 'agent_verification_fresh')))


def verify(case, project, trace, baseline):
    env = dict(os.environ, AGENT_AB_ORACLE='1', PYTHONDONTWRITEBYTECODE='1')
    visible = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-v'],
        cwd=project, env=env, capture_output=True, text=True, timeout=30, check=False)
    hidden = subprocess.run([sys.executable, '-B', '-c', 'import app as m\n' + case.oracle],
        cwd=project, env=env, capture_output=True, text=True, timeout=30, check=False)
    current = snapshot_files(project)
    changed = [p for p in set(baseline) | set(current) if baseline.get(p) != current.get(p)]
    checks = [r for r in execution_rows(trace) if r.get('kind') == 'verification_finished']
    sources = {p: h for p, h in current.items() if p.endswith('.py') and '/' not in p}
    fresh = bool(checks and checks[-1].get('passed') is True and checks[-1].get('tests_run', 0) > 0
                 and checks[-1].get('files') == sources)
    return {'visible_passed': visible.returncode == 0 and 'Ran 0 tests' not in visible.stderr,
            'hidden_passed': hidden.returncode == 0, 'scope_ok': set(changed) <= {'app.py'},
            'agent_verification_fresh': fresh, 'agent_verifications': checks,
            'hidden_stderr': hidden.stderr, 'visible_stderr': visible.stderr, 'changed_paths': changed}


def summarize(rows):
    result = {}
    for arm in ('codey', 'ocr'):
        fixed = [r for r in rows if r['arm'] == arm and r['cohort'] == 'fixed']
        bugs = [r for r in fixed if r['buggy']]
        controls = [r for r in fixed if not r['buggy']]
        native = [r for r in rows if r['arm'] == arm and r['cohort'] == 'native']
        result[arm] = {'runs': len(fixed) + len(native),
            'fixed_bug_cases': len(bugs),
            'initial_bug_rejections': sum(r['first_review']['verdict'] == 'changes_requested' for r in bugs),
            'fixed_bug_final_successes': sum(r['success'] for r in bugs),
            'correct_controls': len(controls),
            'correct_control_rejections': sum(r['first_review']['verdict'] == 'changes_requested' for r in controls),
            'correct_control_successes': sum(r['success'] for r in controls),
            'unavailable_reviews': sum(r['first_review']['status'] != 'available' for r in fixed + native),
            'false_completions': sum(r['false_completion'] for r in fixed + native),
            'native_successes': sum(r['success'] for r in native), 'native_runs': len(native),
            'seconds': sum(r['seconds'] for r in fixed + native),
            'known_tokens': sum(r['usage']['known_token_usage'] or 0 for r in fixed + native),
            'usage_complete': bool(fixed + native) and all(r['usage']['usage_complete'] for r in fixed + native)}
    return result


def run_arm(case, seed, arm, candidate, directory, proxy, options, model):
    directory.mkdir(parents=True)
    trace, state, home = directory / 'trace', directory / 'state', directory / 'home'
    for path in (trace, state, home):
        path.mkdir()
    project = _new_project_root()
    for name, text in case.files.items():
        (project / name).write_text(text, encoding='utf-8')
    baseline = snapshot_files(project)
    manifest = directory / 'worker-manifest.json'
    manifest.write_text(json.dumps(worker_manifest(case, candidate)), encoding='utf-8')
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
    proxy.active_arm = f'{case.name}/{seed}/{arm}'
    proxy.generation_requests = 0
    begin = len(proxy.records)
    command = [sys.executable, '-B', '-m', 'tests.manual.open_code_review_self_review_worker',
        '--project', str(project), '--state-home', str(state), '--reference', str(options.reference),
        '--manifest', str(manifest), '--arm', arm, '--max-turns', str(options.max_turns)]
    process = _run_process(command, project, env, directory, deadline=time.monotonic() + options.timeout)
    _wait_for_backend_idle(options.upstream, timeout=30)
    # Forwarding threads can publish their final usage record just after the child exits.
    with proxy.record_lock:
        records = json.loads(json.dumps(proxy.records[begin:]))
    (directory / 'requests.json').write_text(json.dumps(records, ensure_ascii=False), encoding='utf-8')
    verification = verify(case, project, trace, baseline)
    terminal = terminal_state(process['rows'])
    observations = json_lines(state / 'review-observations.jsonl')
    proofs = []
    for path in (state / 'run_traces').rglob('*.json'):
        proofs.extend(json.loads(path.read_text(encoding='utf-8')).get('completion_proofs', []))
    source = (project / 'app.py').read_text(encoding='utf-8')
    (directory / 'final-app.py').write_text(source, encoding='utf-8')
    result = {'case': case.name, 'seed': seed, 'arm': arm,
        'cohort': 'native' if case.candidate is None else 'fixed', 'buggy': case.buggy,
        'project': str(project), 'candidate_sha256': digest(candidate) if candidate is not None else None,
        'final_sha256': digest(source), 'process_status': process['status'], 'returncode': process['returncode'],
        'seconds': process['wall_time_seconds'], 'terminal': terminal,
        'verification': verification, 'first_review': first_review(observations), 'reviews': observations,
        'completion_proofs': proofs, 'read_tools': json_lines(state / 'ocr-read-tools.jsonl'),
        'usage': usage_totals(records)}
    valid = all(verification[k] for k in ('visible_passed', 'hidden_passed', 'scope_ok', 'agent_verification_fresh'))
    receipt = (terminal.get('event') or {}).get('receipt', {}).get('verification', {})
    result['completion_proof_present'] = bool(receipt.get('proof_refs') and receipt.get('trust') == 'trusted'
                                             and any(p.get('satisfied') is True for p in proofs))
    result['success'] = bool(valid and terminal['completed'] and process['returncode'] == 0
        and process['status'] == 'completed' and not process['forced_cleanup'] and result['completion_proof_present'])
    result['false_completion'] = bool(terminal['completed'] and not valid)
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result, source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--reference', type=Path, default=ROOT / 'reference-projects/open-code-review')
    parser.add_argument('--upstream', default='http://127.0.0.1:5001')
    parser.add_argument('--seeds', nargs='+', type=int, default=[51, 52])
    parser.add_argument('--cases', nargs='+', choices=[c.name for c in cases()])
    parser.add_argument('--reuse-generation', nargs='*', type=Path, default=[])
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--max-turns', type=int, default=14)
    parser.add_argument('--max-tokens', type=int, default=1536)
    parser.add_argument('--request-limit', type=int, default=20)
    parser.add_argument('--temperature', type=float, default=0.2)
    options = parser.parse_args()
    options.upstream = local_url(options.upstream)
    selected = tuple(c for c in cases() if not options.cases or c.name in options.cases)
    from codey.task.entry_auth import derive_entry_auth

    for case in selected:
        auth = derive_entry_auth({'task': case.task, 'intent': 'project'}, project='fixture')
        if 'project.write' in auth.denied_capabilities:
            raise ValueError('Review fixture does not authorize its candidate edit: ' + case.name)
    reusable = {}
    for path in options.reuse_generation:
        result = json.loads(path.read_text(encoding='utf-8'))
        if result['arm'] != 'generate' or not native_candidate_ready(result):
            raise ValueError('Reused generation is not an eligible real verified Writer candidate')
        source = (path.parent / 'final-app.py').read_text(encoding='utf-8')
        if digest(source) != result['final_sha256']:
            raise ValueError('Reused generation source identity changed')
        reusable[(result['case'], result['seed'])] = (result, source, str(path))
    options.run_dir.mkdir(parents=True, exist_ok=False)
    with urlopen(options.upstream + '/v1/models', timeout=5) as response:
        model = json.load(response)['data'][0]['id']
    with urlopen(options.upstream + '/api/extra/version', timeout=5) as response:
        version = json.load(response)
    def revision(root):
        return subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    plan = {'codey_commit': revision(ROOT), 'reference_commit': revision(options.reference),
        'model': model, 'backend': version, 'seeds': options.seeds,
        'cases': [{'name': c.name, 'task': c.task, 'buggy': c.buggy, 'candidate': c.candidate,
                   'oracle_sha256': digest(c.oracle)} for c in selected],
        'comparison_arms': len(selected) * len(options.seeds) * 2,
        'native_generation_runs': sum(c.candidate is None for c in selected) * len(options.seeds) - len(reusable),
        'reused_generation_sources': [value[2] for value in reusable.values()],
        'controls': {'temperature': options.temperature, 'max_tokens': options.max_tokens,
                     'max_turns': options.max_turns, 'request_limit': options.request_limit,
                     'timeout': options.timeout, 'read_generations_per_review': 4},
        'script_sha256': {p.name: digest(p.read_text(encoding='utf-8')) for p in
            [ROOT / 'tests/manual' / name for name in ('open_code_review_adapter.py',
                'open_code_review_quality_cases.py', 'open_code_review_self_review_worker.py',
                'open_code_review_self_review_ab.py')]},
        'limitations': ['Not native OCR CLI: templates, Python rules and read tools only.',
            'No grouping, planning, reflection, comment filter or relocation emulated.',
            'Fixed candidates are not model-authored; native cohort is reported separately.',
            'Small preregistered sample; same-model correlated mistakes remain possible.']}
    (options.run_dir / 'preregistered.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    proxy = Proxy(('127.0.0.1', 0), options.upstream, options.timeout,
        journal=options.run_dir / 'http.jsonl', request_limit=options.request_limit)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    rows, generations = [], []
    output = {'plan': plan, 'runs': rows, 'generations': generations}

    def save():
        output['summary'] = summarize(rows)
        (options.run_dir / 'result.json').write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')

    try:
        index = 0
        for case in selected:
            for seed in options.seeds:
                candidate = case.candidate
                if candidate is None:
                    prior = reusable.get((case.name, seed))
                    if prior is None:
                        generated, candidate = run_arm(case, seed, 'generate', None,
                            options.run_dir / f'{case.name}-{seed}-generate', proxy, options, model)
                    else:
                        generated, candidate, _ = prior
                    generations.append(generated)
                    save()
                    if not native_candidate_ready(generated):
                        output.setdefault('ineligible_generations', []).append({'case': case.name, 'seed': seed})
                        save()
                        continue
                order = ('codey', 'ocr') if index % 2 == 0 else ('ocr', 'codey')
                index += 1
                for arm in order:
                    result, _ = run_arm(case, seed, arm, candidate,
                        options.run_dir / f'{case.name}-{seed}-{arm}', proxy, options, model)
                    rows.append(result)
                    save()
                    print(json.dumps({'case': case.name, 'seed': seed, 'arm': arm, 'success': result['success'],
                        'review': result['first_review']['verdict'], 'seconds': round(result['seconds'], 2),
                        'tokens': result['usage']['token_usage']}), flush=True)
    except Exception as exc:
        output['halt_reason'] = type(exc).__name__ + ': ' + str(exc)
        save()
        raise
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
