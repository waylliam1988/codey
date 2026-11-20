"""Real project-entry fixture for bounded recovery and current completion evidence."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import TypedDict
from unittest import mock

from codey.agents.request import AgentRequest
from codey.providers.diagnostics import ProviderFailure
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.events import RunEvent
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object
from codey.workspace.changes import ChangeTracker


class ReviewObservation(TypedDict):
    source: str
    facts: str


class ToolObservation(TypedDict):
    tool: str
    command: JsonValue
    ok: bool
    exit_code: int | None
    error_code: str
    source_sha256: str


class ScenarioOutcome(TypedDict):
    terminal: JsonObject
    calls: list[AgentRequest]
    reviews: list[ReviewObservation]
    events: list[ToolObservation]
    ledger: list[JsonObject]
    project: Path
    proofs: list[JsonObject]

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


def action(name: str, **args: JsonValue) -> JsonObject:
    return {'tool': name, 'args': args}


def edit(old: str, new: str) -> JsonObject:
    return action('edit', path='names.py', replacements=[{'old_string': old, 'new_string': new}])


class ScriptedProvider:
    name = 'offline scripted choices'
    location = 'scripted:offline'

    def __init__(self, steps: Sequence[JsonObject]) -> None:
        self.steps = list(steps)

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        # A withheld check must be rejected by the real gate, not by a fixture crash.
        step = self.steps.pop(0) if self.steps else action('done', summary='Candidate ready')
        return json.dumps(step)


def scenario(tmp_path: Path, *, kind: str = 'stagnant', source: str = GOOD, fault: str = '', max_turns: int = 20,
             task: str = TASK, initial: str = INITIAL, visible: str = VISIBLE,
             repair_stagnates: bool = False, denied_probe: bool = False) -> ScenarioOutcome:
    # Import application stores only after pytest's isolated-home conftest.
    from codey.app.context import AppContext
    from codey.operations import project_adapter
    from codey.operations.task_entry import run_task_submission
    from codey.operations.task_run import TaskRunDeps
    from codey.providers.diagnostics import capture_provider_failure
    from codey.reviews.core import ReviewResult
    from codey.reviews.identity import ReviewIdentity, capture_snapshot
    from codey.task.model import TaskSubmission

    project = tmp_path / 'project'
    project.mkdir()
    (project / 'names.py').write_text(initial, encoding='utf-8')
    (project / 'test_names.py').write_text(visible, encoding='utf-8')
    state_home = Path(tempfile.gettempdir()) / (
        'cc-' + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    )
    state_home.mkdir(parents=True, exist_ok=False)
    state = AppContext(state_home)
    calls: list[AgentRequest] = []
    reviews: list[ReviewObservation] = []
    events: list[ToolObservation] = []

    def observe(event: RunEvent, downstream: Callable[[RunEvent], None], stop_flag: Event | None) -> None:
        call = event.call
        outcome = event.outcome
        if call is not None and outcome is not None:
            events.append({'tool': call.name, 'command': call.args.get('command'),
                           'ok': outcome.ok, 'exit_code': outcome.exit_code,
                           'error_code': outcome.error_code,
                           'source_sha256': hashlib.sha256((project / 'names.py').read_bytes()).hexdigest()})
            if fault == 'cancel' and call.name == 'edit' and outcome.changed and stop_flag is not None:
                stop_flag.set()
            if (fault == 'cancel_repair' and len(calls) == 2 and call.name == 'edit'
                    and outcome.changed and stop_flag is not None):
                stop_flag.set()
        downstream(event)

    def writer(request: AgentRequest) -> RunResult:
        calls.append(request)
        if (request.task == VALIDATE_TASK and fault != 'missing_verification'
                and (fault in {'runtime_validation', 'missing_tests'} or repair_stagnates or denied_probe)):
            return project_adapter.run(replace(request,
                on_event=lambda ev: observe(ev, request.on_event, request.stop_flag)))
        if request.task == VALIDATE_TASK:
            # A missing-verification fault must withhold the check from both
            # the model repair and the runtime's bounded validation attempt.
            steps = [] if fault == 'missing_verification' else [action('run', command=COMMAND, path='.')]
            steps.append(action('done', summary='Validation finished'))
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
            if fault not in {'missing_tests', 'missing_verification'}:
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

    first_identity: ReviewIdentity | None = None

    def reviewer(**kwargs: object) -> tuple[str, ReviewResult] | None:
        nonlocal first_identity
        snapshot = capture_snapshot(project, ('names.py',))
        identity = ReviewIdentity('scope', 'prompt', 'snapshot', str(project), 'fixture-review', '', 1,
                                  'reviewer', False, snapshot_root=snapshot.root, snapshot_files=snapshot.files,
                                  snapshot_inventory_digest=snapshot.inventory_digest)
        first_identity = first_identity or identity
        reviews.append({'source': (project / 'names.py').read_text(encoding='utf-8'),
                        'facts': str(kwargs['execution_evidence'])})
        if len(reviews) == 2 and fault == 'missing_review':
            return None
        if len(reviews) == 2 and fault == 'stale_review':
            identity = first_identity
        if len(reviews) == 2 and fault == 'rejected_review':
            return 'fixture-review', ReviewResult('changes_requested', 'Not approved', [], identity=identity)
        return 'fixture-review', ReviewResult('approved', 'Scripted approval', [], identity=identity)

    def collect_changes(project: str | Path, tracker: ChangeTracker | None) -> dict[str, object]:
        return {
            'ok': True, 'changed_count': 1, 'files': [{'path': 'names.py', 'status': 'modified'}],
            'diff': 'diff --git a/names.py b/names.py\n--- a/names.py\n+++ b/names.py\n'
                    '@@ -1 +1 @@\n-before\n+after\n', 'mode': 'git'}

    def capture_failure(*, model: str, action: str, page: object, error: BaseException) -> ProviderFailure:
        return capture_provider_failure(model=model, action=action, page=page, error=error)

    runner = TaskRunDeps.from_submission_stores(state=state, stores=state.task_submission_stores,
        agent_run=writer, collect_changes=collect_changes,
        run_review=reviewer, capture_provider_failure=capture_failure,
        is_git_repository=lambda project: True)
    denials = (fault.removeprefix('deny_'),) if fault.startswith('deny_') else ()
    if denied_probe:
        # Arbitrary commands now enter the explicit approval workflow. This
        # fixture exercises a pre-execution refusal, so make that boundary
        # explicit instead of leaving the approval capability available.
        denials = (*denials, 'shell.approval')
    with mock.patch.object(state, 'get_provider', return_value=ScriptedProvider([])):
        run_task_submission(runner, TaskSubmission('s', str(project), task, max_turns, False, 'deepseek',
                                                  intent='project', denied_capabilities=denials))
    rows = [parse_json_object(line) for path in (state_home / 'run_ledgers').rglob('*.jsonl')
            for line in path.read_text(encoding='utf-8').splitlines()]
    terminal = state.run_registry.last_terminal_event()
    assert terminal is not None
    assert state.run_traces is not None
    trace = parse_json_object(state.run_traces.path_for('s', str(terminal['run_id'])).read_text(encoding='utf-8'))
    proofs = trace['completion_proofs']
    assert isinstance(proofs, list)
    state.close()
    shutil.rmtree(state_home, ignore_errors=True)
    return {'terminal': terminal, 'calls': calls, 'reviews': reviews,
            'events': events, 'ledger': rows, 'project': project, 'proofs': [decode_json_object(proof) for proof in proofs]}


def assert_complete(outcome: ScenarioOutcome) -> None:
    event = outcome['terminal']
    assert event['stop_reason'] == 'done', event.get('summary', event)
    receipt = decode_json_object(event['receipt'])
    assert decode_json_object(receipt['verification'])['checks_passed'] is True
    checks = [ev for ev in outcome['events'] if ev['tool'] == 'run' and ev['exit_code'] is not None]
    assert checks[0]['ok'] is False
    assert checks[-1]['exit_code'] == 0
    final_hash = hashlib.sha256((outcome['project'] / 'names.py').read_bytes()).hexdigest()
    assert checks[-1]['source_sha256'] == final_hash
    assert outcome['reviews'][-1]['source'] == GOOD
    assert (outcome['project'] / 'test_names.py').read_text(encoding='utf-8') == VISIBLE
    assert outcome['proofs'][-1]['satisfied'] is True
    assert outcome['proofs'][-1]['status'] == 'complete'
