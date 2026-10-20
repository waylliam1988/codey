"""Steering is consumed once by a generation; follow-ups remain separate tasks."""
from __future__ import annotations

import pytest

from codey.app.input_queue import DurableConversationInputQueue
from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore, SubmissionStatus


@pytest.fixture(params=['memory', 'sqlite'])
def session(request, tmp_path):
    session = DurableSession(MemoryDurableStore() if request.param == 'memory' else SQLiteDurableStore(tmp_path / 'durable.sqlite3'))
    session.admit_input(conversation_id='chat', request_id='active', task_id='active', text='current', provider_snapshot={})
    session.start_task('active', state={})
    yield session
    session.close()


def admit(session, identity, mode):
    return session.admit_input(conversation_id='chat', request_id=identity, task_id=identity, text=identity,
                               provider_snapshot={}, placement='queued', mode=mode,
                               task_state={'input_identity': identity, 'input_request': {'session_id': 'chat', 'run_id': identity, 'task': identity, 'input_mode': mode}})


def test_boundary_consumes_steering_receipt_without_leaving_a_second_execution(session):
    steering = admit(session, 'steer', 'steer')
    follow_up = admit(session, 'next', 'follow_up')
    before = session.commit_sequence()
    placed = session.commit_generation_boundary('chat', task_id='active', at='post_tools')
    assert session.commit_sequence() == before + 1
    assert [item.request_id for item in placed] == ['steer']
    receipt = session.task('steer')
    assert receipt.status.value == 'completed'
    assert receipt.state['consumed_by_task_id'] == 'active'
    assert session.store.submission(steering.submission_id).status is SubmissionStatus.DONE
    assert session.store.submission(follow_up.submission_id).placement.value == 'queued'
    queue = DurableConversationInputQueue(session, emit=lambda event: None)
    assert queue.get('steer')['state'] == 'consumed'
    assert queue.get('steer')['consumed_by_run_id'] == 'active'
    assert session.commit_generation_boundary('chat', task_id='active', at='final') == ()
    assert session.task('next').state['input_state'] == 'queued'
    assert not any(task.task_id == 'steer' for task in session.pending_tasks())
    assert [entry.payload.get('text') for entry in session.context('chat')] == ['current', 'steer']


def test_without_a_live_consumer_steering_becomes_one_ordinary_task(session):
    admit(session, 'steer', 'steer')
    session.complete_task('active', 'done', state={})
    placed = session.place_boundary_inputs('chat', at='final')
    assert [item.request_id for item in placed] == ['steer']
    assert session.task('steer').status.value == 'pending'
    assert session.task('steer').state['input_state'] == 'placed'
    assert session.place_boundary_inputs('chat', at='final') == ()


def test_failed_boundary_does_not_lose_or_acknowledge_the_steer(session, monkeypatch):
    admitted = admit(session, 'steer', 'steer')
    original = session.task('steer')
    def fail(mutation):
        raise OSError('failed commit')
    monkeypatch.setattr(session.store, 'commit', fail)
    with pytest.raises(OSError, match='failed commit'):
        session.commit_generation_boundary('chat', task_id='active', at='final')
    assert session.store.task('steer') == original
    assert session.store.submission(admitted.submission_id).placement.value == 'queued'


@pytest.mark.parametrize('boundary', ['approval', 'settle'])
def test_non_generating_boundaries_do_not_consume_steering(session, boundary):
    admit(session, 'steer', 'steer')
    assert session.commit_generation_boundary('chat', task_id='active', at=boundary) == ()
    assert session.task('steer').state['input_state'] == 'queued'
    assert session.task('steer').status.value == 'pending'
