"""Explicit saved-output evidence must be read, not guessed from a preview."""
import json
from unittest import mock

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.kernel_execution import execute_turn
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_explicit_once_command_blocks_repeat_until_workspace_changes import COMMAND, setup_session

TASK = f'Run {COMMAND} once to obtain diagnostics, then find BUILD_ID in its saved output. Implement app.py with that value.'


@pytest.mark.parametrize('query', ['BUILD_ID', None])
def test_mutation_waits_for_actual_owned_keyword_excerpt_not_a_correct_guess(tmp_path, query):
    session, _, execute = setup_session(tmp_path, TASK)
    assert execute(ToolCall('run', {'command': COMMAND, 'path': '.'}), 1).ok
    ref = next(iter(session._memory_results))
    original = session._memory_results[ref]
    from codey.operations.kernel_provenance import (
        _kernel_workspace_identity_of,
        _trusted_workspace_proof,
        attach_trusted_workspace,
    )
    # Keep the real kernel's provenance; this fixture substitutes only command output.
    session._memory_results[ref] = attach_trusted_workspace(
        ToolResult(original.call, 'preamble\nBUILD_ID=84\nend', ok=True, audit={'exit_code': 0}),
        _trusted_workspace_proof(_kernel_workspace_identity_of(original), 'in_memory_kernel_result'))
    session.read_files.add('app.py')
    edit = ToolCall('edit', {'path': 'app.py', 'replacements': [{'old_string': 'value = 1', 'new_string': 'value = 84'}]})
    denied = execute(edit, 2)
    assert not denied.ok and 'read_tool_result' in denied.model_text and ref in denied.model_text
    assert not session.edited_files
    args = {'result_ref': ref, 'limit': 200}
    if query:
        args['query'] = query
    result = execute_turn(session, [ToolCall('read_tool_result', args)],
                          project_path=session.project, run_id='r', turn=3)[0]
    assert result.ok and 'BUILD_ID=84' in json.loads(result.model_text)['text']
    from codey.operations.explicit_execution_requirements import requested_output_read_check
    assert requested_output_read_check(session, TASK).status == 'pass'
    assert execute(edit, 4).ok


def test_reading_absent_keyword_does_not_authorize_the_requested_edit(tmp_path):
    session, _, execute = setup_session(tmp_path, TASK)
    assert execute(ToolCall('run', {'command': COMMAND, 'path': '.'}), 1).ok
    ref = next(iter(session._memory_results))
    assert execute_turn(session, [ToolCall('read_tool_result', {'result_ref': ref, 'query': 'BUILD_ID'})],
                        project_path=session.project, run_id='r', turn=2)[0].ok
    session.read_files.add('app.py')
    assert not execute(ToolCall('edit', {'path': 'app.py', 'replacements': [
        {'old_string': 'value = 1', 'new_string': 'value = 84'}]}), 3).ok


def test_model_done_claim_and_other_passed_checks_do_not_substitute_for_requested_read(tmp_path):
    session, _, _ = setup_session(tmp_path, TASK)
    from codey.completion.contract import CompletionCheck
    with mock.patch('codey.operations.project_completion_checks._engine_checks',
                    return_value=[CompletionCheck('relevant_verification', 'pass')]):
        verdict = evaluate(session, 'I found BUILD_ID and tests passed.', context={'task': TASK, 'project': session.project})
    assert not verdict.complete and verdict.proof is not None
    assert any(c.check_id == 'requested_output_read' and c.status == 'not_run' for c in verdict.proof.checks)


def test_generic_output_mentions_and_quoted_old_instructions_do_not_invent_a_read_requirement(tmp_path):
    task = f'The previous instruction was "Run {COMMAND} once, then find BUILD_ID in its saved output". Now edit app.py.'
    session, _, execute = setup_session(tmp_path, task)
    session.read_files.add('app.py')
    assert execute(ToolCall('edit', {'path': 'app.py', 'replacements': [
        {'old_string': 'value = 1', 'new_string': 'value = 84'}]}), 1).ok
