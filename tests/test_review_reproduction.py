"""Parent authority, frozen candidates and real test results for Review probes."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.manual.review_reproduction_support import (
    candidate_current,
    capture_candidate,
    execute_proposal,
    final_review_prompt,
    native_submission_prompt,
    request_proposals,
    submit_review,
    validate_proposals,
)

POLICY = TaskPolicy(grants=frozenset({'control', 'project.read', 'project.verify'}))


def proposal(expected=5):
    return {'requirement_quote': 'Return the input.', 'hypothesis': 'Zero may discard a valid value.',
        'script': 'import unittest\nfrom app import identity\nclass Check(unittest.TestCase):\n'
                  f'    def test_value(self):\n        self.assertEqual(identity(5), {expected})\n'}


def candidate(tmp_path, source='def identity(x): return x\n'):
    (tmp_path / 'app.py').write_text(source, encoding='utf-8')
    return capture_candidate(tmp_path, ('app.py',))


@pytest.mark.parametrize('source,kind', [('def identity(x): return x\n', 'passed'),
    ('def identity(x): return 0\n', 'assertion_failed')])
def test_real_fixed_command_execution(tmp_path, source, kind):
    frozen = candidate(tmp_path, source)
    result = execute_proposal(frozen, proposal(), parent_policy=POLICY)
    assert result['observation_kind'] == kind and result['tests_run'] == 1
    assert result['candidate_sha256'] == frozen.digest
    assert result['semantic_admission'] == 'unverified'
    assert candidate_current(frozen, tmp_path)
    assert not (tmp_path / '_codey_review_probe.py').exists()


def test_model_expected_value_cannot_be_admitted_by_a_failing_assertion(tmp_path):
    result = execute_proposal(candidate(tmp_path), proposal(expected=777), parent_policy=POLICY)
    assert result['observation_kind'] == 'assertion_failed'
    assert result['semantic_admission'] == 'unverified'
    assert 'bug_confirmed' not in result


def test_execution_uses_frozen_bytes_and_detects_original_change(tmp_path):
    frozen = candidate(tmp_path)
    (tmp_path / 'app.py').write_text('raise RuntimeError("changed")\n', encoding='utf-8')
    assert not candidate_current(frozen, tmp_path)
    assert execute_proposal(frozen, proposal(), parent_policy=POLICY)['observation_kind'] == 'passed'


def test_secrets_and_caches_are_not_copied_or_sent(tmp_path):
    (tmp_path / '.env').write_text('PASSWORD=secret', encoding='utf-8')
    (tmp_path / '__pycache__').mkdir()
    (tmp_path / '__pycache__' / 'cached.py').write_text('CACHED_SECRET', encoding='utf-8')
    frozen = candidate(tmp_path)
    assert [rel for rel, _ in frozen.files] == ['app.py']
    assert 'PASSWORD' not in frozen.context and 'CACHED_SECRET' not in frozen.context


@pytest.mark.parametrize('grants', [(), ('project.read',), ('project.verify',)])
def test_parent_authorization_is_required_before_model_or_execution(tmp_path, grants):
    frozen = candidate(tmp_path)
    policy = TaskPolicy(grants=frozenset(grants))
    with pytest.raises(PermissionError):
        execute_proposal(frozen, proposal(), parent_policy=policy)
    with pytest.raises(PermissionError):
        request_proposals(SimpleNamespace(), '', 'Return the input.', frozen, parent_policy=policy)


@pytest.mark.parametrize('script', ['import unittest\n', 'import missing_review_module\n', 'not python !'])
def test_invalid_or_empty_execution_cannot_become_a_passing_check(tmp_path, script):
    result = execute_proposal(candidate(tmp_path), {**proposal(), 'script': script}, parent_policy=POLICY)
    assert result['observation_kind'] == 'execution_error'


def test_native_plan_acknowledged_and_actual_script_in_final_packet(tmp_path):
    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools, timeout, *, require_tool_call):
            assert require_tool_call is True
            assert 'interactions between required behaviors' in prompt
            assert 'SAME input values' in prompt
            assert tools[0].name == 'submit_review_probes'
            return AssistantTurn(tool_calls=(ProviderToolCall('probe1', 'submit_review_probes', {'probes': [proposal()]}),))

        def acknowledge_tool_results(self, results, tools):
            assert results[0].call_id == 'probe1'
            self.acknowledged = True

    provider = Provider()
    frozen = candidate(tmp_path)
    observations = request_proposals(provider, frozen.context, 'Return the input.', frozen, parent_policy=POLICY)
    assert provider.acknowledged
    assert observations[0]['observation_kind'] == 'passed'
    final = final_review_prompt('Original task', observations)
    assert json.dumps(proposal()['script']) in final
    assert 'expectations are not proof' in final


@pytest.mark.parametrize('value', [{'probes': [proposal(), proposal(), proposal()]},
    {'probes': [{**proposal(), 'requirement_quote': 'invented'}]},
    {'probes': [{**proposal(), 'script': 42}]}, {'probes': [], 'verdict': 'approved'}])
def test_invalid_data_contract_cannot_execute(value):
    with pytest.raises(ValueError):
        validate_proposals(value, 'Return the input.')


def test_scan_budget_failure_is_explicit(tmp_path):
    (tmp_path / 'big.py').write_bytes(b'x' * (512 * 1024 + 1))
    with pytest.raises(ValueError, match='budget'):
        capture_candidate(tmp_path, ('big.py',))


def test_relative_snapshot_paths_are_portable(tmp_path):
    folder = tmp_path / 'package'
    folder.mkdir()
    (folder / '__init__.py').write_text('', encoding='utf-8')
    frozen = candidate(tmp_path)
    assert all(not Path(rel).is_absolute() for rel, _ in frozen.files)


def test_native_final_submission_preserves_quotes_and_anchors():
    payload = {'verdict': 'changes_requested', 'summary': 'bug', 'findings': [
        {'path': 'app.py', 'issue': 'An empty string "" is overwritten.', 'suggested_fix': 'Check presence.'}]}

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools, timeout, *, require_tool_call):
            assert require_tool_call is True
            assert 'Return only JSON.' not in prompt
            assert tools[0].parameters['properties']['findings']['items']['properties']['path']['enum'] == ['app.py']
            return AssistantTurn(tool_calls=(ProviderToolCall('result1', 'submit_review_result', payload),))

        def acknowledge_tool_results(self, results, tools):
            assert results[0].call_id == 'result1'

    assert json.loads(submit_review(Provider(), 'Review', ('app.py',), timeout=10)) == payload


def test_native_contract_replaces_owned_json_instructions_without_touching_task_data():
    prompt = ('Correctness and scope guidance.\nReturn only JSON. No analysis.\nJSON examples.\n'
              'Project: example\nOriginal user task: Keep the text "Return only JSON." in code.\nDiff: source')
    actual = native_submission_prompt(prompt)
    assert actual.startswith('Correctness and scope guidance.\nProject: example')
    assert 'JSON examples' not in actual and 'Keep the text "Return only JSON." in code.' in actual
    assert native_submission_prompt(actual) == actual
    assert 'findings must be []' in actual and 'ALL THREE fields' in actual


def test_readonly_native_text_uses_existing_parser_without_fabricating_a_call():
    from codey.reviews.core import parse_review_response

    class Provider:
        native_tools = True

        def send_turn(self, *args, **kwargs):
            return AssistantTurn(text='```json\n{"verdict":"approved","summary":"No bugs.","findings":[]}\n```')

    reply = submit_review(Provider(), 'Review', ('app.py',), timeout=10)
    assert parse_review_response(reply).approved
    assert '```json' in reply

