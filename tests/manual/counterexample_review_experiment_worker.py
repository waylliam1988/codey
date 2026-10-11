"""Isolated reviewer intervention; production repair and completion are unchanged."""
import argparse
import difflib
import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from tests.manual.counterexample_review_adapter import CounterexampleReviewer


def discovery(spec, args):
    from codey.app.review_service import _request_review_result
    from codey.reviews.core import render_review_prompt
    from codey.reviews.identity import capture_snapshot
    from tests.manual.agent_stability_codey_worker import sampling_provider

    source = spec['candidate']
    changes = {'ok': True, 'changed_count': 1,
               'files': [{'path': 'app.py', 'status': 'M', 'additions': len(source.splitlines()), 'deletions': 2}],
               'diff': ''.join(difflib.unified_diff(spec['files']['app.py'].splitlines(True),
                   source.splitlines(True), fromfile='a/app.py', tofile='b/app.py'))}
    import subprocess
    import sys
    checked = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-v'],
                             cwd=args.project, capture_output=True, text=True, timeout=15, check=False)
    if checked.returncode:
        raise ValueError('frozen candidate fails visible setup tests')
    prompt = render_review_prompt(project=str(args.project), task=spec['task'],
        writer_summary='Frozen candidate submitted after actual visible verification.', changes=changes,
        execution_evidence='Actual current visible verification:\n' + checked.stderr[-2000:],
        project_map='Frozen app.py:\n' + source + '\nUnchanged consumer.py:\n' + spec['files']['consumer.py'])
    delegate = sampling_provider('local')
    reviewer = (CounterexampleReviewer(delegate, args.project, spec['task'],
        args.state_home / 'counterexamples.jsonl', authorized=True) if args.arm == 'counterexample' else delegate)
    reviewer.new_chat()
    try:
        result = _request_review_result(reviewer, None, SimpleNamespace(prompt=prompt, reviewer_view=changes),
                                       capture_snapshot(args.project, ('app.py',)))
        payload = asdict(result)
    except Exception as exc:
        payload = {'status': 'unavailable', 'verdict': None, 'error': type(exc).__name__ + ': ' + str(exc)}
    finally:
        reviewer.close()
    (args.state_home / 'discovery.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return 0


def repair(spec, args):
    from codey.app import headless_runner, review_service
    from codey.operations import project_adapter
    from codey.reviews.core import review_result_payload
    from tests.manual.agent_stability_codey_worker import ControlledContext, sampling_provider
    from tests.manual.candidate_recovery_koboldcpp_worker import NativeSteps

    actual_review = review_service.run_review_attempt
    def observed_review(*a, **kw):
        row = {'error': 'review interrupted'}
        try:
            result = actual_review(*a, **kw)
            row = {'review': {**review_result_payload(result[1]), 'summary': result[1].summary,
                              'findings': [asdict(f) for f in result[1].findings]}}
            return result
        finally:
            with (args.state_home / 'review-observations.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    review_service.run_review_attempt = observed_review
    headless_runner.HeadlessAppContext = ControlledContext
    if args.arm == 'no-review':
        build = headless_runner.build_task_deps
        def without_actual_call(*a, **kw):
            return replace(build(*a, **kw), run_review=lambda **kw: None)
        headless_runner.build_task_deps = without_actual_call
    calls = 0
    def writer(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            steps = [('read_file', {'path': 'app.py'}), ('read_file', {'path': 'test_app.py'}),
                ('edit', {'path': 'app.py', 'replacements': [{'old_string': spec['files']['app.py'],
                                                           'new_string': spec['candidate']}]}),
                ('run', {'command': 'python -m unittest discover -v', 'path': '.'}),
                ('done', {'summary': 'Frozen candidate submitted after actual visible verification.'})]
            request = replace(request, provider=NativeSteps(steps), fresh_chat=True)
        return project_adapter.run(request)
    def reviewer(provider_id, **kw):
        delegate = sampling_provider(provider_id, **kw)
        return (CounterexampleReviewer(delegate, args.project, spec['task'],
            args.state_home / 'counterexamples.jsonl', authorized=True) if args.arm == 'counterexample' else delegate)
    def emit(payload):
        import sys
        headless_runner.emit_jsonl(payload)
        sys.stdout.flush()
    request = headless_runner.HeadlessRequest(project=args.project, state_home=args.state_home,
        session_id='counterexample_review', provider_id='local', task=spec['task'], max_turns=args.max_turns)
    return headless_runner.run_headless(request, agent_run=writer, emit_jsonl=emit,
        connect_provider=sampling_provider, connect_reviewer=reviewer).exit_code


def main():
    parser = argparse.ArgumentParser()
    for key in ('project', 'state-home', 'manifest'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--phase', choices=('discovery', 'repair'), required=True)
    parser.add_argument('--arm', choices=('codey', 'counterexample', 'no-review'), required=True)
    parser.add_argument('--max-turns', type=int, default=12)
    args = parser.parse_args()
    spec = json.loads(args.manifest.read_text(encoding='utf-8'))
    if set(spec) != {'task', 'files', 'candidate'}:
        raise ValueError('worker manifest must not contain hidden evaluation data')
    args.state_home.mkdir(parents=True, exist_ok=True)
    return discovery(spec, args) if args.phase == 'discovery' else repair(spec, args)


if __name__ == '__main__':
    raise SystemExit(main())
