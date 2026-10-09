"""Behavioral archive refs are readable without synthetic kernel tool calls."""
import json
from types import SimpleNamespace

import pytest

from codey.operations.task_session import TaskSession
from codey.operations.tool_result_reader import read_tool_result
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall
from tests.test_python_behavioral_probe_observes_properties_and_failures import BAD, execute


def delegate(store, *, session_id='session', run_id='run'):
    return SimpleNamespace(session=TaskSession(policy=TaskPolicy(frozenset({'control'}))),
                           managed_outputs=store, session_id=session_id, run_id=run_id)


def test_current_task_behavioral_archive_can_be_read_and_searched_after_session_reconstruction(tmp_path):
    observation, _, store = execute(tmp_path, BAD)
    reader = delegate(store)
    assert not reader.session._memory_results
    result = read_tool_result(reader, ToolCall('read_tool_result',
        {'result_ref': observation.output_ref, 'query': 'Q.r7t', 'limit': 4000}))
    assert result.ok
    data = json.loads(result.model_text)
    assert data['ok'] is False and data['match_offset'] is not None
    assert 'Q.r7t' in data['text']


@pytest.mark.parametrize('scope', ['session', 'run', 'digest'])
def test_behavioral_receipt_cannot_escape_task_ownership_or_bypass_store_integrity(tmp_path, scope):
    observation, _, store = execute(tmp_path, BAD)
    reader = delegate(store, session_id='another' if scope == 'session' else 'session',
                      run_id='another' if scope == 'run' else 'run')
    if scope == 'digest':
        store.path_for('session', 'run', observation.output_ref).write_text('tampered', encoding='utf-8')
    result = read_tool_result(reader, ToolCall('read_tool_result', {'result_ref': observation.output_ref}))
    assert not result.ok and 'does not authorize re-execution' in result.model_text


def test_an_unindexed_ordinary_output_is_not_promoted_to_a_behavioral_receipt(tmp_path):
    _, _, store = execute(tmp_path, BAD)
    ref = store.write_tool_output(session_id='session', run_id='run', tool_id='ordinary',
        permission_profile='coding_writer', tool_name='run', display_ref='.', text='ordinary output')
    assert ref is not None
    result = read_tool_result(delegate(store), ToolCall('read_tool_result', {'result_ref': ref.handle}))
    assert not result.ok


def test_invalid_behavioral_archive_shape_returns_read_failure_without_throwing(tmp_path):
    _, _, store = execute(tmp_path, BAD)
    ref = store.write_tool_output(session_id='session', run_id='run', tool_id='invalid',
        permission_profile='coding_writer', tool_name='behavioral_verification', display_ref='.', text='[]')
    assert ref is not None
    assert not read_tool_result(delegate(store), ToolCall('read_tool_result', {'result_ref': ref.handle})).ok
