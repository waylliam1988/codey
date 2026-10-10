"""Stopped-candidate validation has one durable, budget-preserving admission."""
from dataclasses import replace

import pytest

from codey.runtime.core import operation_state as states
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from tests.test_runtime_operation_state import _state


def stopped(**overrides):
    return _state(states.LEAF_WRITER_SETTLED, writer_attempt=1, turns_used=3,
                  stop_reason='no_progress', **overrides)


def admission(state, provider='deepseek'):
    transition = getattr(states, 'mark_candidate_validation_running', None)
    assert callable(transition), 'A stopped candidate needs explicit durable validation admission'
    return transition(state, provider_id=provider, writer_attempt=state.writer_attempt + 1)


def test_admission_preserves_used_budget_and_cannot_be_repeated():
    state = admission(stopped())
    assert state.leaf == states.LEAF_WRITER_RUNNING
    assert state.writer_attempt == 2 and state.turns_used == 3
    settled = states.mark_writer_settled(state, provider_id='deepseek', turns_used=5,
                                        stop_reason='no_progress')
    with pytest.raises(states.RuntimeOperationTransitionError):
        admission(settled)


@pytest.mark.parametrize('changes', [
    {'stop_reason': 'done'}, {'stop_reason': 'approval'}, {'turns_used': 4},
    {'candidate_validation_attempted': True}, {'completion_proof_ref': 'completion_proof:0123456789abcdef'},
    {'leaf': states.LEAF_TOOL_EFFECT_PENDING}, {'task_kind': 'research'},
])
def test_admission_rejects_wrong_phase_reason_budget_proof_and_reentry(changes):
    with pytest.raises(states.RuntimeOperationTransitionError):
        admission(replace(stopped(), **changes))


def test_admission_cannot_switch_provider():
    with pytest.raises(states.RuntimeOperationTransitionError):
        admission(stopped(), 'local')


def test_admission_is_committed_through_existing_session_log(tmp_path):
    log = RuntimeSessionLog(tmp_path)
    line = RuntimeMutationLine(log)
    line.accept_operation(session_id='s1', run_id='run-1', project='.', provider_id='deepseek',
                          turn_budget=5, max_repair_rounds=1, task_kind='project')
    line.mark_writer_running('s1', 'run-1', provider_id='deepseek')
    line.mark_writer_settled('s1', 'run-1', provider_id='deepseek', turns_used=3,
                            stop_reason='no_progress')
    commit = getattr(line, 'mark_candidate_validation_running', None)
    assert callable(commit), 'Validation admission must persist, not just mutate an in-memory flag'
    result = commit('s1', 'run-1', provider_id='deepseek', writer_attempt=2)
    restored = states.operation_state_from_entries(log.read('s1'), session_id='s1', run_id='run-1')
    assert restored is not None
    assert restored == result
    assert restored.turns_used == 3 and restored.writer_attempt == 2


def test_repair_admission_preserves_the_failed_proof_and_existing_repair_round():
    state = _state(states.LEAF_REPAIR_SETTLED, stop_reason='no_progress', turns_used=3,
        writer_attempt=1, repair_rounds=1, repair_context_ref='sha256:' + 'a' * 64,
        completion_proof_ref='completion_proof:0123456789abcdef', completion_proof_status='failed')
    resumed = admission(state)
    assert resumed.leaf == states.LEAF_REPAIR_RUNNING
    assert resumed.repair_rounds == 1 and resumed.turns_used == 3
    assert resumed.completion_proof_ref == state.completion_proof_ref
    assert resumed.to_payload()['candidate_validation_attempted'] is True
    assert states.RuntimeOperationState.from_payload(resumed.to_payload()) == resumed


def test_candidate_validation_is_once_for_the_whole_operation_not_once_per_phase():
    resumed = admission(stopped())
    assert resumed.to_payload().get('candidate_validation_attempted') is True
    repair = replace(resumed, leaf=states.LEAF_REPAIR_SETTLED, stop_reason='no_progress',
        repair_rounds=1, repair_context_ref='sha256:' + 'a' * 64,
        completion_proof_ref='completion_proof:0123456789abcdef', completion_proof_status='failed')
    with pytest.raises(states.RuntimeOperationTransitionError):
        admission(repair)


@pytest.mark.parametrize('value', [None, 0, 1, 'true'])
def test_candidate_validation_consumption_is_a_strict_durable_boolean(value):
    payload = stopped().to_payload()
    payload['candidate_validation_attempted'] = value
    assert states.RuntimeOperationState.from_payload(payload) is None


def test_pre_consumption_state_schema_cannot_claim_current_admission_facts():
    payload = stopped().to_payload()
    payload.pop('candidate_validation_attempted')
    payload['schema_version'] = 1
    assert states.RuntimeOperationState.from_payload(payload) is None
