"""Child-only experimental reviewer; Writer repair and completion stay production-owned."""
import argparse
import json
from dataclasses import asdict, replace
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--state-home', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--arm', choices=('codey', 'ocr', 'generate'), required=True)
    parser.add_argument('--max-turns', type=int, default=14)
    args = parser.parse_args()
    spec = json.loads(args.manifest.read_text(encoding='utf-8'))
    from codey.app import headless_runner, review_service
    from codey.operations import project_adapter
    from codey.reviews.core import review_result_payload
    from tests.manual.agent_stability_codey_worker import ControlledContext, sampling_provider
    from tests.manual.candidate_recovery_koboldcpp_worker import NativeSteps
    from tests.manual.open_code_review_adapter import OCRReviewer

    args.state_home.mkdir(parents=True, exist_ok=True)
    log = args.state_home / 'review-observations.jsonl'
    actual_review = review_service.run_review_attempt

    def observed_review(*a, **kw):
        row = {'error': 'review interrupted before a result'}
        try:
            result = actual_review(*a, **kw)
        except Exception as exc:
            row = {'error': type(exc).__name__ + ': ' + str(exc)}
            raise
        else:
            row = {'review': {**review_result_payload(result[1]),
                              'summary': result[1].summary,
                              'findings': [asdict(f) for f in result[1].findings]}}
            return result
        finally:
            with log.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')

    review_service.run_review_attempt = observed_review
    headless_runner.HeadlessAppContext = ControlledContext
    if args.arm == 'generate':
        build = headless_runner.build_task_deps

        def without_actual_review(*a, **kw):
            return replace(build(*a, **kw), run_review=lambda **kw: None)

        headless_runner.build_task_deps = without_actual_review
    calls = 0

    def writer(request):
        nonlocal calls
        calls += 1
        if args.arm != 'generate' and calls == 1:
            old = spec['files']['app.py']
            steps = [('read_file', {'path': 'app.py'}), ('read_file', {'path': 'test_app.py'}),
                     ('edit', {'path': 'app.py', 'replacements': [{'old_string': old, 'new_string': spec['candidate']}]}),
                     ('run', {'command': 'python -m unittest discover -v', 'path': '.'}),
                     ('done', {'summary': 'Frozen candidate submitted after actual visible verification.'})]
            request = replace(request, provider=NativeSteps(steps), fresh_chat=True)
        return project_adapter.run(request)

    def reviewer(provider_id, **kwargs):
        provider = sampling_provider(provider_id, **kwargs)
        return (OCRReviewer(provider, args.project, args.reference, args.state_home / 'ocr-read-tools.jsonl')
                if args.arm == 'ocr' else provider)

    def emit(payload):
        headless_runner.emit_jsonl(payload)
        import sys
        sys.stdout.flush()

    request = headless_runner.HeadlessRequest(project=args.project, state_home=args.state_home,
        session_id='review_quality', provider_id='local', task=spec['task'], max_turns=args.max_turns)
    return headless_runner.run_headless(request, agent_run=writer, emit_jsonl=emit,
        connect_provider=sampling_provider, connect_reviewer=reviewer).exit_code


if __name__ == '__main__':
    raise SystemExit(main())
