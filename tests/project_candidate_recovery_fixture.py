"""Real project-entry fixture for bounded recovery and current completion evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from unittest import mock

COMMAND = 'python -m unittest discover -v'
TASK = ('First run the existing failing tests before changing any file. Fix names.py so clean_name '
        'removes ASCII punctuation and preserves digits. Do not modify tests. Run ' + COMMAND + '.')
INITIAL = 'def clean_name(text): return text\n'
GOOD = "import string\ndef clean_name(text):\n    return text.translate(str.maketrans('', '', string.punctuation)).lower().strip()\n"
BAD = "def clean_name(text): return ''\n"
VISIBLE = ("import unittest\nfrom names import clean_name\nclass Names(unittest.TestCase):\n"
           "    def test_punctuation(self): self.assertEqual(clean_name(' A! '), 'a')\n"
           "    def test_digits(self): self.assertEqual(clean_name('Ab42'), 'ab42')\n")
VALIDATE_TASK = 'Validate the stopped candidate using the original authorized check; do not edit files.'


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
             task=TASK, initial=INITIAL, visible=VISIBLE, repair_stagnates=False, denied_probe=False):
    # Import application stores only after pytest's isolated-home conftest.
    from codey.app import server
    from codey.operations import project_adapter
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
                     edit(initial, source if kind == 'stagnant' else BAD)]
            if kind == 'stagnant':
                if denied_probe:
                    steps.append(action('run', command='python -c "print(12345)"', path='.'))
                steps += [edit(initial, source)] * 3
            else:
                steps += [action('run', command=COMMAND, path='.'), action('done', summary='Tests passed')]
        else:
            steps = [action('read_file', path='names.py'), edit(BAD, source)]
            if fault != 'missing_tests':
                steps.append(action('run', command=COMMAND, path='.'))
            if repair_stagnates:
                if denied_probe:
                    steps.append(action('run', command='python -c "print(12345)"', path='.'))
                steps += [edit(BAD, source)] * 3
            else:
                steps.append(action('done', summary='Repaired candidate'))
        wrapped = replace(request, provider=ScriptedProvider(steps), fresh_chat=True,
                          on_event=lambda ev: observe(ev, request.on_event, request.stop_flag))
        if kind == 'repair' and len(calls) == 1:
            # Exercise the outer completion-enforcement boundary with a writer
            # submission that claims done despite a real failed registered check.
            # Only the proposal is injected; execution facts remain unchanged.
            result = project_adapter.run(replace(wrapped, max_turns=5))
            return replace(result, stop_reason='done')
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
        if len(reviews) == 2 and fault == 'missing_review':
            return None
        if len(reviews) == 2 and fault == 'stale_review':
            identity = first_identity
        if len(reviews) == 2 and fault == 'rejected_review':
            return 'fixture-review', ReviewResult('changes_requested', 'Not approved', [], identity=identity)
        return 'fixture-review', ReviewResult('approved', 'Scripted approval', [], identity=identity)

    runner = replace(_runner(state, writer),
                     collect_changes=lambda *a, **k: _changes('names.py'), run_review=reviewer)
    denials = (fault.removeprefix('deny_'),) if fault.startswith('deny_') else ()
    if denied_probe:
        # Arbitrary commands now enter the explicit approval workflow. This
        # fixture exercises a pre-execution refusal, so make that boundary
        # explicit instead of leaving the approval capability available.
        denials = (*denials, 'shell.approval')
    with mock.patch.object(state, 'get_provider', return_value=ScriptedProvider([])):
        run_task_submission(runner, TaskSubmission('s', str(project), task, max_turns, False, 'deepseek',
                                                  intent='project', denied_capabilities=denials))
    rows = [json.loads(line) for path in (tmp_path / 'state' / 'run_ledgers').rglob('*.jsonl')
            for line in path.read_text(encoding='utf-8').splitlines()]
    terminal = state.run_registry.last_terminal_event()
    trace = json.loads(state.run_traces.path_for('s', terminal['run_id']).read_text(encoding='utf-8'))
    return {'terminal': state.run_registry.last_terminal_event(), 'calls': calls, 'reviews': reviews,
            'events': events, 'ledger': rows, 'project': project, 'proofs': trace['completion_proofs']}


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
    assert outcome['proofs'][-1]['satisfied'] is True
    assert outcome['proofs'][-1]['status'] == 'complete'
