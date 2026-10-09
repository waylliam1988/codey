"""Keep the stagnation stop; express a proven readonly fix obstruction honestly."""
import json
from threading import Event

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_progress import KernelProgress
from codey.operations.task_loop import _stop_no_progress
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.managed_outputs import ManagedOutputStore, run_command_with_managed_output
from codey.workspace.revision import WorkspaceRevisionStore

TASK = ('Diagnose the failure, but must not change any file. The task requires fixing the defect; '
        'if forbidden changes prevent fixing it, finish with JSON {"status":"blocked","reason":"..."}.')


def stopped_session(tmp_path, *, task=TASK, writable=False, passing=False, stale=False, cancelled=False):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.py').write_text('value = 1\n')
    (project / 'test_app.py').write_text('import unittest\nfrom app import value\nclass Values(unittest.TestCase):\n'
        f'    def test_value(self): self.assertEqual(value, {1 if passing else 2})\n')
    store = ManagedOutputStore(tmp_path / 'outputs')
    revisions = WorkspaceRevisionStore(tmp_path / 'state')
    identity = revisions.current_state(project)
    grants = {'control', 'project.read', 'project.verify'} | ({'project.write'} if writable else set())
    session = TaskSession(policy=TaskPolicy(frozenset(grants)), project=str(project), task_text=task)
    session.set_workspace_state(identity.revision, identity.fingerprint)
    command = 'python -m unittest discover -v'

    def run(call):
        observed = run_command_with_managed_output(project, '.', command, store=store,
            session_id='s', run_id='r', permission_profile='coding_writer')
        return ToolResult(call, observed.model_text, ok=observed.ok,
                          audit={**observed.audit, 'exit_code': observed.exit_code},
                          canonical=observed.canonical, presentation=observed.presentation)

    result = execute_turn(session, [ToolCall('run', {'command': command, 'path': '.'})],
        executors={'run': run}, project_path=project, workspace_revision_store=revisions, run_id='r', turn=1)[0]
    assert result.ok is passing
    if stale:
        (project / 'app.py').write_text('value = 3\n')
    progress = KernelProgress(stagnant_turns=1)
    repeated = ToolResult(ToolCall('read', {'path': 'app.py'}), 'same actual source', ok=True)
    assert not progress.observe([repeated], session)
    flag = Event()
    if cancelled:
        flag.set()
    stopped = _stop_no_progress(progress, [repeated], session, None, None, 7,
                               propagate=False, stop_flag=flag)
    return stopped, session, project


def test_current_failed_check_and_forbidden_required_fix_produce_blocked_json_without_false_success(tmp_path):
    result, session, project = stopped_session(tmp_path)
    assert result.stop_reason == 'blocked' and result.completed is False
    data = json.loads(result.summary)
    assert data['status'] == 'blocked' and 'permission' in data['reason']
    assert 'python -m unittest discover -v' in data['reason']
    assert not session.edited_files and (project / 'app.py').read_text() == 'value = 1\n'
    assert len(session.verifications) == 1 and session.verifications[0]['passed'] is False


@pytest.mark.parametrize('kwargs', [
    {'writable': True}, {'passing': True}, {'stale': True},
    {'task': 'Diagnose the tests and report findings. Do not change files.'},
])
def test_unknown_or_authorized_work_is_not_relabeled_as_forbidden_fix(kwargs, tmp_path):
    result, _, _ = stopped_session(tmp_path, **kwargs)
    assert result.stop_reason == 'no_progress' and not result.completed


def test_cancellation_keeps_priority_over_readonly_failure_block(tmp_path):
    result, _, _ = stopped_session(tmp_path, cancelled=True)
    assert result.stop_reason == 'stopped'
