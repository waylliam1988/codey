"""An explicit once requirement limits executions, not result reads or fresh tests."""
from codey.operations.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.workspace.revision import WorkspaceRevisionStore

COMMAND = 'python -m unittest discover -v'


def setup_session(tmp_path, task):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'app.py').write_text('value = 1\n', encoding='utf-8')
    revisions = WorkspaceRevisionStore(tmp_path / 'state')
    identity = revisions.current_state(project)
    session = TaskSession(policy=TaskPolicy(frozenset({'control', 'project.read', 'project.write', 'project.verify'})),
                          project=str(project), task_text=task)
    session.set_workspace_state(identity.revision, identity.fingerprint)
    counts = []

    def run(call):
        counts.append(call)
        return ToolResult(call, 'actual command result', ok=True, audit={'exit_code': 0})

    def execute(call, turn):
        return execute_turn(session, [call], executors={'run': run}, project_path=project,
                            workspace_revision_store=revisions, run_id='r', turn=turn)[0]

    return session, counts, execute


def test_explicit_once_returns_prior_reference_without_execution_or_new_verification_then_allows_after_edit(tmp_path):
    task = f'Run {COMMAND} once for diagnostics. Read its stored output. Run tests again after editing.'
    session, counts, execute = setup_session(tmp_path, task)
    call = ToolCall('run', {'command': COMMAND, 'path': '.'})
    assert execute(call, 1).ok
    observations = list(session.verifications)
    rejected = execute(call, 2)
    assert not rejected.ok and len(counts) == 1
    assert 'read_tool_result' in rejected.model_text and 'task_turn_effect:' in rejected.model_text
    assert session.verifications == observations
    session.read_files.add('app.py')
    edited = execute(ToolCall('edit', {'path': 'app.py', 'replacements': [
        {'old_string': 'value = 1', 'new_string': 'value = 2'}]}), 3)
    assert edited.ok
    assert execute(call, 4).ok and len(counts) == 2
    assert len(session.verifications) == 2


def test_ordinary_repetition_is_allowed_when_original_task_does_not_limit_it(tmp_path):
    _, counts, execute = setup_session(tmp_path, f'Run {COMMAND} repeatedly to check stability.')
    call = ToolCall('run', {'command': COMMAND, 'path': '.'})
    assert execute(call, 1).ok and execute(call, 2).ok
    assert len(counts) == 2


def test_once_limit_is_bound_to_the_exact_command_and_cwd(tmp_path):
    _, counts, execute = setup_session(tmp_path, f'Run {COMMAND} once for diagnostics.')
    assert execute(ToolCall('run', {'command': COMMAND, 'path': '.'}), 1).ok
    assert execute(ToolCall('run', {'command': 'python -m unittest', 'path': '.'}), 2).ok
    assert len(counts) == 2


def test_negated_or_quoted_once_phrase_does_not_invent_an_execution_limit(tmp_path):
    for task in (f'Do not run {COMMAND} once; run it twice.',
                 f'The old requirement said "Run {COMMAND} once". Repeat it now.'):
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        root.mkdir()
        _, counts, execute = setup_session(root, task)
        call = ToolCall('run', {'command': COMMAND, 'path': '.'})
        assert execute(call, 1).ok and execute(call, 2).ok
        assert len(counts) == 2


def test_once_limit_does_not_confuse_another_cwd_with_the_same_command(tmp_path):
    session, counts, execute = setup_session(tmp_path, f'Run {COMMAND} once for diagnostics.')
    from pathlib import Path
    (Path(session.project) / 'subdir').mkdir()
    assert execute(ToolCall('run', {'command': COMMAND, 'path': '.'}), 1).ok
    assert execute(ToolCall('run', {'command': COMMAND, 'path': 'subdir'}), 2).ok
    assert len(counts) == 2


def test_new_user_requirement_can_remove_the_once_limit(tmp_path):
    session, counts, execute = setup_session(tmp_path, f'Run {COMMAND} once for diagnostics.')
    call = ToolCall('run', {'command': COMMAND, 'path': '.'})
    assert execute(call, 1).ok
    session.task_text = f'Run {COMMAND} repeatedly now.'
    assert execute(call, 2).ok and len(counts) == 2
