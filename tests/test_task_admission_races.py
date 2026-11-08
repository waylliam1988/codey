"""HTTP admission must not release a reservation adopted by the scheduler."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.app.active_provider_registry import ActiveProviderRegistry
from codey.app.run_registry import RunRegistry
from codey.app.task_runtime import TaskRuntimeCoordinator
from codey.app.task_submit import _claim_admitted_task
from codey.durable import CommitConflict, DurableSession, MemoryDurableStore


@pytest.fixture
def admission():
    registry = RunRegistry()
    woken = Mock()
    runtime = TaskRuntimeCoordinator(registry, ActiveProviderRegistry(), threading.Lock(), threading.Lock(),
                                     wake_input=woken)
    durable = DurableSession(MemoryDurableStore())
    run = runtime.reserve_run(session_id="chat", project=None, task="work", provider_id="local", run_id="task")
    durable.admit_input(conversation_id="chat", request_id="task", task_id="task", text="work", provider_snapshot={})
    state = SimpleNamespace(durable=durable, task_runtime=runtime, release_run=runtime.release_run)
    yield state, registry, run, woken
    durable.close()


@pytest.mark.parametrize("phase", ["dispatching", "running", "waiting", "completed"])
def test_losing_http_claim_preserves_worker_and_its_terminal_event(admission, phase):
    state, registry, run, woken = admission
    state.durable.claim_task("task")
    if phase != "dispatching":
        assert registry.start("task")
        state.durable.start_task("task", state={})
    if phase == "waiting":
        state.durable.wait_task("task", state={})
    elif phase == "completed":
        state.durable.complete_task("task", "done", state={})

    assert not _claim_admitted_task(state, run, existing_reservation=False)
    assert registry.is_busy()
    woken.assert_not_called()
    if phase == "dispatching":
        assert registry.start("task")
    terminal = registry.finish("task", {"type": "task_done", "stop_reason": "done"})
    assert terminal is not None and terminal["run_id"] == "task"
    assert not registry.is_busy()


@pytest.mark.parametrize("phase", ["waiting", "completed"])
def test_replay_of_a_parked_or_finished_task_releases_only_the_new_unstarted_slot(admission, phase):
    state, registry, run, woken = admission
    state.durable.claim_task("task")
    state.durable.start_task("task", state={})
    if phase == "waiting":
        state.durable.wait_task("task", state={})
    else:
        state.durable.complete_task("task", "done", state={})
    assert not _claim_admitted_task(state, run, existing_reservation=False)
    assert not registry.is_busy()
    woken.assert_called_once()


def test_scheduler_claim_between_http_read_and_claim_does_not_release_queued_worker(admission, monkeypatch):
    state, registry, run, woken = admission
    claim = state.durable.claim_task

    def competitor_claimed(task_id):
        claim(task_id)
        raise CommitConflict("scheduler won the claim")

    monkeypatch.setattr(state.durable, "claim_task", competitor_claimed)
    assert not _claim_admitted_task(state, run, existing_reservation=False)
    assert registry.is_busy()
    assert registry.start("task")
    assert registry.finish("task", {"type": "task_done", "stop_reason": "done"}) is not None
    woken.assert_not_called()


def test_failed_claim_that_is_still_pending_is_reported(admission, monkeypatch):
    state, registry, run, _ = admission
    monkeypatch.setattr(state.durable, "claim_task", Mock(side_effect=CommitConflict("revision changed")))
    with pytest.raises(CommitConflict, match="revision changed"):
        _claim_admitted_task(state, run, existing_reservation=False)
    assert registry.is_busy()
