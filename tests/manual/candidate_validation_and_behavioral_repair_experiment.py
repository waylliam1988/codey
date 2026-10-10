"""Offline project-entry experiment; never installed or imported by production.

Run with ``python -m tests.manual.candidate_validation_and_behavioral_repair_experiment``.
The current variant asserts the desired contract and deliberately exposes gaps.
The candidate variant injects a test-only proposal. Both run real project tools,
unittest, the behavioral worker, review identities and the existing completion gate.
Scripted choices cannot establish real-model reliability or a 20/20 A/B score.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

COMMAND = 'python -m unittest discover -v'
TASK = ('First run the existing failing tests before changing any file. Fix names.py so clean_name '
        'removes ASCII punctuation and preserves digits. Do not modify tests. Run ' + COMMAND + '.')
INITIAL = 'def clean_name(text): return text\n'
GOOD = "import string\ndef clean_name(text):\n    return text.translate(str.maketrans('', '', string.punctuation)).lower().strip()\n"
BAD = "import string\ndef clean_name(text):\n    return '-'.join(''.join(' ' if c in string.punctuation else c for c in text).lower().split())\n"
VISIBLE = ("import unittest\nfrom names import clean_name\nclass Names(unittest.TestCase):\n"
           "    def test_punctuation(self): self.assertEqual(clean_name(' A! '), 'a')\n"
           "    def test_digits(self): self.assertEqual(clean_name('Ab42'), 'ab42')\n")
VALIDATE_TASK = 'Validate the stopped candidate using the original authorized check; do not edit files.'
REUSE_FACTS = 'Use the recorded actual counterexample; do not create a new probe command.'


def action(name, **args):
    return {'tool': name, 'args': args}


def edit(old, new):
    return action('edit', path='names.py', replacements=[{'old_string': old, 'new_string': new}])


class ScriptedProvider:
    name = 'offline scripted choices'

    def __init__(self, steps):
        self.steps = list(steps)

    def new_chat(self, timeout=None):
        pass

    def close(self):
        pass

    def send(self, text, timeout=None):
        # A withheld check must be rejected by the real gate, not by a fixture crash.
        step = self.steps.pop(0) if self.steps else action('done', summary='Candidate ready')
        return json.dumps(step)


def scenario(tmp_path, *, kind='stagnant', source=GOOD, fault='', max_turns=20,
             task=TASK, initial=INITIAL, visible=VISIBLE, repair_stagnates=False, denied_probe=False,
             review_regression=False):
    # Import application stores only after pytest's isolated-home conftest.
    from codey.app import server
    from codey.operations import behavioral_verification, project_adapter
    from codey.operations.task_entry import run_task_submission
    from codey.reviews.core import ReviewResult
    from codey.reviews.identity import ReviewIdentity, capture_snapshot
    from codey.task.model import TaskSubmission
    from tests.test_project_completion_flow_enforcement import _changes, _runner

    project = tmp_path / 'project'
    project.mkdir()
    (project / 'names.py').write_text(initial, encoding='utf-8')
    (project / 'test_names.py').write_text(visible, encoding='utf-8')
    state = server.AppContext(tmp_path / 'state')
    calls, reviews, events = [], [], []
    probe_calls = 0
    failed_snapshot = None
    review_regression = review_regression or fault == 'review_regression'

    def observe(event, downstream, stop_flag):
        call = getattr(event, 'call', None)
        outcome = getattr(event, 'outcome', None)
        if call is not None and outcome is not None:
            events.append({'tool': call.name, 'command': call.args.get('command'),
                           'ok': outcome.ok, 'exit_code': outcome.exit_code,
                           'error_code': outcome.error_code,
                           'source_sha256': hashlib.sha256((project / 'names.py').read_bytes()).hexdigest()})
            if fault == 'cancel' and call.name == 'edit' and outcome.changed:
                stop_flag.set()
            if fault == 'cancel_repair' and len(calls) == 2 and call.name == 'edit' and outcome.changed:
                stop_flag.set()
        downstream(event)

    def writer(request):
        calls.append(request)
        if request.task == VALIDATE_TASK and (fault == 'runtime_validation' or repair_stagnates or denied_probe):
            return project_adapter.run(replace(request,
                on_event=lambda ev: observe(ev, request.on_event, request.stop_flag)))
        if request.task == VALIDATE_TASK:
            steps = [action('run', command=COMMAND, path='.'), action('done', summary='Validation finished')]
        elif len(calls) == 1:
            steps = [action('run', command=COMMAND, path='.'), action('read_file', path='names.py'),
                     action('read_file', path='test_names.py'),
                     edit(initial, GOOD if review_regression else source if kind == 'stagnant' else BAD)]
            if kind == 'stagnant':
                if denied_probe:
                    steps.append(action('run', command='python -c "print(12345)"', path='.'))
                steps += [edit(initial, source)] * 3
            else:
                steps += [action('run', command=COMMAND, path='.'), action('done', summary='Tests passed')]
        elif review_regression and 'Review findings:' in request.task:
            steps = [action('read_file', path='names.py'), edit(GOOD, BAD),
                     action('run', command=COMMAND, path='.'), action('done', summary='Review repair checked')]
        else:
            assert 'Qr7t' in request.completion_repair_context
            assert 'Q!r7t' in request.completion_repair_context
            assert 'qr7t' in request.completion_repair_context and 'q-r7t' in request.completion_repair_context
            # This is a declared scripted strategy, not evidence of LLM compliance.
            if fault == 'ignore_guidance' or (fault == 'needs_guidance' and REUSE_FACTS not in request.task):
                steps = [action('shell', command='python3 -c "from names import clean_name; print(clean_name(\'Qr7t\'))"', path='.')]
            else:
                steps = [action('read_file', path='names.py'), edit(BAD, source)]
                if fault != 'missing_tests':
                    steps.append(action('run', command=COMMAND, path='.'))
                if repair_stagnates:
                    if denied_probe:
                        steps.append(action('run', command='python -c "print(12345)"', path='.'))
                    steps += [edit(BAD, source)] * 3
                elif fault == 'await_behavior_refresh' and 'call done to submit the repaired candidate' not in request.task:
                    steps += [action('run', command=COMMAND, path='.')] * 3
                else:
                    steps.append(action('done', summary='Repaired candidate'))
        wrapped = replace(request, provider=ScriptedProvider(steps), fresh_chat=True,
                          on_event=lambda ev: observe(ev, request.on_event, request.stop_flag))
        return project_adapter.run(wrapped)

    first_identity = None

    def reviewer(**kwargs):
        nonlocal first_identity
        snapshot = capture_snapshot(project, ('names.py',))
        identity = ReviewIdentity('scope', 'prompt', 'snapshot', str(project), 'fixture-review', '', 1,
                                  'reviewer', False, snapshot_root=snapshot.root, snapshot_files=snapshot.files,
                                  snapshot_inventory_digest=snapshot.inventory_digest)
        first_identity = first_identity or identity
        reviews.append({'source': (project / 'names.py').read_text(encoding='utf-8'),
                        'facts': kwargs['execution_evidence']})
        if review_regression and len(reviews) == 1:
            from codey.reviews.core import ReviewFinding

            return 'fixture-review', ReviewResult('changes_requested', 'Replace punctuation with spaces',
                [ReviewFinding(path='names.py', issue='Use punctuation as separators',
                               suggested_fix='Replace punctuation with spaces')], identity=identity)
        if len(reviews) == 2 and fault == 'missing_review':
            return None
        if len(reviews) == 2 and fault == 'stale_review':
            identity = first_identity
            if review_regression and failed_snapshot is not None:
                identity = replace(identity, snapshot_root=failed_snapshot.root, snapshot_files=failed_snapshot.files,
                                   snapshot_inventory_digest=failed_snapshot.inventory_digest)
        if len(reviews) == 2 and fault == 'rejected_review':
            return 'fixture-review', ReviewResult('changes_requested', 'Not approved', [], identity=identity)
        return 'fixture-review', ReviewResult('approved', 'Scripted approval', [], identity=identity)

    refresh = behavioral_verification.refresh_behavioral_observation

    def observe_behavior(ctx):
        nonlocal probe_calls, failed_snapshot
        probe_calls += 1
        if fault == 'missing_behavior' and probe_calls == 2:
            return  # Keep the old failure: it must not become evidence for repaired code.
        refresh(ctx)
        if review_regression and ctx.behavioral_observation is not None and ctx.behavioral_observation.status == 'fail':
            failed_snapshot = capture_snapshot(project, ('names.py',))

    runner = replace(_runner(state, writer),
                     collect_changes=lambda *a, **k: _changes('names.py'), run_review=reviewer)
    denials = (fault.removeprefix('deny_'),) if fault.startswith('deny_') else ()
    with (mock.patch.object(state, 'get_provider', return_value=ScriptedProvider([])),
          mock.patch.object(behavioral_verification, 'refresh_behavioral_observation', observe_behavior)):
        run_task_submission(runner, TaskSubmission('s', str(project), task, max_turns, False, 'deepseek',
                                                  intent='project', denied_capabilities=denials))
    rows = [json.loads(line) for path in (tmp_path / 'state' / 'run_ledgers').rglob('*.jsonl')
            for line in path.read_text(encoding='utf-8').splitlines()]
    terminal = state.run_registry.last_terminal_event()
    trace = json.loads(state.run_traces.path_for('s', terminal['run_id']).read_text(encoding='utf-8'))
    return {'terminal': state.run_registry.last_terminal_event(), 'calls': calls, 'reviews': reviews,
            'events': events, 'ledger': rows, 'project': project, 'proofs': trace['completion_proofs']}


@pytest.fixture(autouse=True)
def variant(request, monkeypatch, no_external_advisor_models):
    if request.config.getoption('experiment_variant', default='current') == 'candidate':
        from tests.manual.candidate_recovery_test_proposal import install
        install(monkeypatch)


def record(outcome, record_property):
    record_property('observations', {
        'stop_reason': outcome['terminal']['stop_reason'],
        'writer_attempts': len(outcome['calls']), 'reviews': len(outcome['reviews']),
        'tools': outcome['events'],
        'proofs': [{'status': p['status'], 'satisfied': p['satisfied']} for p in outcome['proofs']],
        'behavior': [{k: r.get(k) for k in ('status', 'reason', 'output_ref', 'workspace_fingerprint')}
                     for r in outcome['ledger'] if r['type'] == 'behavioral_observed']})


def assert_complete(outcome):
    event = outcome['terminal']
    assert event['stop_reason'] == 'done', event.get('summary', event)
    assert event['receipt']['verification']['checks_passed'] is True
    checks = [ev for ev in outcome['events'] if ev['tool'] == 'run' and ev['exit_code'] is not None]
    assert checks[0]['ok'] is False
    assert checks[-1]['exit_code'] == 0
    final_hash = hashlib.sha256((outcome['project'] / 'names.py').read_bytes()).hexdigest()
    assert checks[-1]['source_sha256'] == final_hash
    assert outcome['reviews'][-1]['source'] == GOOD
    assert (outcome['project'] / 'test_names.py').read_text(encoding='utf-8') == VISIBLE
    observed = [r for r in outcome['ledger'] if r['type'] == 'behavioral_observed']
    assert observed[-1]['status'] == 'pass' and observed[-1]['output_ref']
    assert outcome['proofs'][-1]['satisfied'] is True
    assert outcome['proofs'][-1]['status'] == 'complete'


def test_correct_candidate_after_three_stale_searches_gets_real_check_review_and_proof(tmp_path, record_property):
    result = scenario(tmp_path)
    record(result, record_property)
    misses = [ev for ev in result['events'] if ev['tool'] == 'edit' and not ev['ok']]
    assert len(misses) == 3
    assert_complete(result)
    assert len(result['calls']) == 2 and len(result['reviews']) == 1


@pytest.mark.parametrize('fault,max_turns', [('cancel', 20), ('deny_project.verify', 20),
                                            ('deny_project.write', 20), ('', 7)])
def test_cancellation_denied_permissions_and_exhausted_budget_never_promote_stopped_candidate(
        tmp_path, record_property, fault, max_turns):
    result = scenario(tmp_path, fault=fault, max_turns=max_turns)
    record(result, record_property)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert len(result['calls']) == 1 and not result['reviews']
    if fault == 'deny_project.verify':
        assert not any(ev['tool'] == 'run' and ev['exit_code'] is not None for ev in result['events'])
    if fault == 'deny_project.write':
        assert (result['project'] / 'names.py').read_text() == INITIAL


def test_incorrect_stopped_candidate_cannot_be_completed_by_false_approval(tmp_path, record_property):
    result = scenario(tmp_path, source="def clean_name(text): return ''\n")
    record(result, record_property)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert result['terminal']['receipt']['verification']['checks_passed'] is False


def test_recorded_counterexample_guides_repair_without_a_new_unapproved_probe(tmp_path, record_property):
    result = scenario(tmp_path, kind='behavioral', fault='needs_guidance')
    record(result, record_property)
    assert_complete(result)
    assert not any(ev['tool'] == 'shell' for ev in result['events'])
    assert REUSE_FACTS in result['calls'][1].task and COMMAND in result['calls'][1].task


def test_existing_behavioral_repair_keeps_actual_counterexample_and_refreshes_all_evidence(tmp_path, record_property):
    result = scenario(tmp_path, kind='behavioral')
    record(result, record_property)
    assert_complete(result)
    assert len(result['calls']) == 2
    assert [r['source'] for r in result['reviews']] == [BAD, GOOD]
    observed = [r for r in result['ledger'] if r['type'] == 'behavioral_observed']
    assert [r['status'] for r in observed] == ['fail', 'pass']
    assert observed[0]['workspace_fingerprint'] != observed[1]['workspace_fingerprint']


def test_repair_submission_refreshes_previous_candidate_observation_without_repeating_green_tests(tmp_path, record_property):
    result = scenario(tmp_path, kind='behavioral', fault='await_behavior_refresh')
    record(result, record_property)
    assert_complete(result)
    assert len([ev for ev in result['events'] if ev['tool'] == 'run' and ev['ok']]) == 2


def test_behavioral_repair_relates_required_output_to_actual_baseline_and_distinguishes_test_receipts(tmp_path):
    result = scenario(tmp_path, kind='behavioral')
    assert_complete(result)
    projection = result['calls'][1].completion_repair_context
    assert 'required_right_value_given_left' in projection
    assert 'ordinary test results do not contain' in result['calls'][1].task


@pytest.mark.parametrize('fault', ['missing_tests', 'missing_behavior', 'missing_review', 'stale_review',
                                  'rejected_review', 'ignore_guidance'])
def test_missing_repaired_tests_or_fresh_review_and_unapproved_probe_cannot_complete(
        tmp_path, record_property, fault):
    result = scenario(tmp_path, kind='behavioral', fault=fault)
    record(result, record_property)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    if fault == 'ignore_guidance':
        assert result['terminal']['stop_reason'] == 'approval'


def test_new_visible_pass_and_false_approval_cannot_override_repeated_behavior_failure(tmp_path, record_property):
    result = scenario(tmp_path, kind='behavioral', source=BAD)
    record(result, record_property)
    assert result['terminal']['stop_reason'] != 'done'
    assert len(result['calls']) == 2
    assert [r['status'] for r in result['ledger'] if r['type'] == 'behavioral_observed'] == ['fail', 'fail']


def test_stopped_semantically_wrong_candidate_cannot_pass_with_green_ordinary_tests(tmp_path, record_property):
    result = scenario(tmp_path, source=BAD)
    record(result, record_property)
    assert result['terminal']['stop_reason'] not in {'done', 'error', 'provider_failure'}
    assert len(result['calls']) <= 3  # One candidate validation and at most one existing completion repair.
    assert result['terminal']['receipt']['verification']['checks_passed'] is False


class ReportPlugin:
    def __init__(self, report, variant):
        self.path, self.variant, self.results = report, variant, []

    def pytest_addoption(self, parser):
        parser.addoption('--experiment-variant', choices=('current', 'candidate'), default='current')

    def pytest_runtest_logreport(self, report):
        if report.when == 'call' or (report.when == 'setup' and report.failed):
            self.results.append({'test': report.nodeid, 'phase': report.when, 'outcome': report.outcome,
                                 'observations': dict(report.user_properties).get('observations'),
                                 'failure': str(report.longrepr) if report.failed else ''})

    def pytest_sessionfinish(self, session, exitstatus):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        script = Path(__file__)
        self.path.write_text(json.dumps({'variant': self.variant, 'exit_code': int(exitstatus),
            'script_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
            'test_only_proposal_applied': self.variant == 'candidate', 'production_files_written': False, 'model_calls': 0,
            'limitations': ['Scripted choices; no claim about real-model reliability.',
                            'Candidate is a process-local test proposal, not a production implementation.'],
            'results': self.results}, ensure_ascii=False, indent=2), encoding='utf-8')


class ProposalPlugin:
    """Use pytest's fixture lifecycle when evaluating production-contract files."""

    @pytest.fixture(autouse=True)
    def proposal(self, monkeypatch, no_external_advisor_models):
        from tests.manual.candidate_recovery_test_proposal import install
        install(monkeypatch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=('current', 'candidate'), default='current')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    return pytest.main([str(Path(__file__).resolve()), '-q', '--tb=short',
                        '--experiment-variant=' + args.variant], plugins=[ReportPlugin(args.report, args.variant)])


if __name__ == '__main__':
    raise SystemExit(main())
