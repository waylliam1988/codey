"""Real checks through project entry: false approval, bounded repair, fresh review."""
import json
from dataclasses import replace
from unittest import mock

from codey.app import server
from codey.operations.task_entry import run_task_submission
from codey.reviews.core import ReviewResult
from codey.reviews.identity import ReviewIdentity, capture_snapshot
from codey.runtime.core.models import ToolCall
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.events import RunEvent
from codey.storage.managed_outputs import run_command_with_managed_output
from codey.task.model import TaskSubmission
from codey.toolchain.runtime import ToolOutcome
from tests.test_project_completion_flow_enforcement import _changes, _Provider, _runner
from tests.test_python_behavioral_probe_observes_properties_and_failures import BAD

TASK = 'Fix names.py so clean_name removes ASCII punctuation and preserves digits. Do not modify tests. Run python -m unittest discover -v.'
VISIBLE = "import unittest\nfrom names import clean_name\nclass Names(unittest.TestCase):\n    def test_visible(self): self.assertEqual(clean_name(' A! '), 'a')\n"
GOOD = "import string\ndef clean_name(text):\n    return text.translate(str.maketrans('', '', string.punctuation)).lower().strip()\n"


def run_candidate(tmp_path, *, repair, initial_source=BAD, run_repair_checks=True):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'names.py').write_text('def clean_name(text): return text\n')
    (project / 'test_names.py').write_text(VISIBLE)
    state = server.AppContext(tmp_path / 'state')
    calls, reviews, checks = [], [], []

    def writer(request):
        calls.append(request)
        (project / 'names.py').write_text(GOOD if repair and len(calls) == 2 else initial_source)
        request.on_event(RunEvent.tool_finished(1, ToolCall('edit', {'path': 'names.py'}),
                                               ToolOutcome('edited', True, changed=True)))
        if len(calls) == 1 or run_repair_checks:
            result = run_command_with_managed_output(project, '.', 'python -m unittest discover -v',
                permission_profile='coding_writer', store=state.managed_outputs, session_id='s', run_id='r')
            checks.append(result.ok)
            request.on_event(RunEvent.tool_finished(2, ToolCall('run', {'command': 'python -m unittest discover -v', 'path': '.'}), result))
        return RunResult('Tests passed', 'done', 3, changed=True, checks_ran=True)

    def reviewer(**kwargs):
        reviews.append((project / 'names.py').read_text())
        snapshot = capture_snapshot(project, ('names.py',))
        identity = ReviewIdentity('scope', 'prompt', 'snapshot', str(project), 'fixture-review', '', 1, 'reviewer', False,
            snapshot_root=snapshot.root, snapshot_files=snapshot.files, snapshot_inventory_digest=snapshot.inventory_digest)
        return 'fixture-review', ReviewResult('approved', 'Tests pass', [], identity=identity)

    runner = _runner(state, writer)
    runner = replace(runner, collect_changes=mock.Mock(side_effect=lambda *a, **k: _changes('names.py')),
                                             run_review=reviewer)
    with mock.patch.object(state, 'get_provider', return_value=_Provider()):
        run_task_submission(runner, TaskSubmission('s', str(project), TASK, 12, False, 'deepseek', intent='project'))
    return state.run_registry.last_terminal_event(), calls, reviews, checks, project


def test_approved_wrong_candidate_is_repaired_only_after_actual_counterexample_and_new_review(tmp_path):
    event, calls, reviews, checks, project = run_candidate(tmp_path, repair=True)
    assert len(calls) == 2
    assert 'behavioral_verification' in calls[1].completion_repair_context
    assert 'Qr7t' in calls[1].completion_repair_context
    assert checks == [True, True]
    assert reviews == [BAD, GOOD]
    assert (project / 'test_names.py').read_text() == VISIBLE
    assert event['stop_reason'] == 'done'
    assert event['receipt']['verification']['checks_passed'] is True


def test_repeated_false_approval_cannot_override_failed_property_or_start_unbounded_repairs(tmp_path):
    event, calls, reviews, checks, _ = run_candidate(tmp_path, repair=False)
    assert len(calls) == 2 and checks == [True, True]
    assert len(reviews) == 2
    assert event['stop_reason'] == 'blocked'
    assert event['receipt']['verification']['checks_passed'] is False


def test_frozen_plan_and_each_candidate_observation_are_saved_in_existing_run_ledger(tmp_path):
    run_candidate(tmp_path, repair=True)
    rows = [json.loads(line) for path in (tmp_path / 'state' / 'run_ledgers').rglob('*.jsonl')
            for line in path.read_text(encoding='utf-8').splitlines()]
    plans = [row for row in rows if row['type'] == 'behavioral_plan_admitted']
    assert rows
    observations = [row for row in rows if row['type'] == 'behavioral_observed']
    assert len(plans) == 1
    assert len(plans[0]['pairs']) == 32
    assert [row['status'] for row in observations] == ['fail', 'pass']
    assert all(row['plan_digest'] == plans[0]['plan_digest'] and row['output_ref'] for row in observations)
    assert observations[0]['workspace_fingerprint'] != observations[1]['workspace_fingerprint']


def test_unavailable_behavior_probe_does_not_mask_observed_failure_of_required_original_tests(tmp_path):
    event, calls, reviews, checks, _ = run_candidate(tmp_path, repair=True,
        initial_source='def clean_name(text):\n    this is invalid python\n')
    assert checks == [False, True]
    assert len(calls) == 2 and len(reviews) == 2
    assert event['stop_reason'] == 'done'


def test_correct_behavior_and_new_approval_cannot_replace_missing_tests_for_repaired_code(tmp_path):
    event, calls, reviews, checks, _ = run_candidate(tmp_path, repair=True, run_repair_checks=False)
    assert len(calls) == 2 and reviews == [BAD, GOOD] and checks == [True]
    assert event['stop_reason'] == 'blocked'
    assert event['receipt']['verification']['checks_passed'] is False


def test_constant_output_passes_invariance_but_cannot_override_failed_original_tests(tmp_path):
    event, calls, reviews, checks, _ = run_candidate(tmp_path, repair=False,
        initial_source="def clean_name(text): return ''\n")
    assert len(calls) == 2 and len(reviews) == 2 and checks == [False, False]
    assert event['stop_reason'] == 'blocked'
    assert event['receipt']['verification']['checks_passed'] is False
