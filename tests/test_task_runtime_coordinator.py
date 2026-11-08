from __future__ import annotations

import threading

from codey.app.active_provider_registry import ActiveProviderRegistry
from codey.app.run_registry import RunRegistry
from codey.app.task_runtime import TaskRuntimeCoordinator


def test_admission_line_preserves_gate_then_application_lock_order() -> None:
    registry = RunRegistry()
    coordination_lock = threading.Lock()
    shell_spawn_gate = threading.Lock()
    coordinator = TaskRuntimeCoordinator(
        registry,
        ActiveProviderRegistry(),
        coordination_lock,
        shell_spawn_gate,
        wake_input=lambda: None,
    )

    with coordinator.admission_line():
        assert shell_spawn_gate.locked()
        assert coordination_lock.locked()

    assert not shell_spawn_gate.locked()
    assert not coordination_lock.locked()


def test_release_run_wakes_waiting_input_after_registry_release() -> None:
    registry = RunRegistry()
    woken: list[str] = []
    coordinator = TaskRuntimeCoordinator(
        registry,
        ActiveProviderRegistry(),
        threading.Lock(),
        threading.Lock(),
        wake_input=lambda: woken.append("wake"),
    )
    reserved = coordinator.reserve_run(
        session_id="session",
        project=None,
        task="task",
        provider_id="provider",
    )
    assert reserved is not None

    coordinator.release_run(reserved.run_id)

    assert coordinator.active_runs() == ()
    assert woken == ["wake"]


def test_conditional_release_keeps_started_worker_and_only_wakes_after_a_release() -> None:
    registry = RunRegistry()
    woken = []
    coordinator = TaskRuntimeCoordinator(registry, ActiveProviderRegistry(), threading.Lock(), threading.Lock(),
                                         wake_input=lambda: woken.append("wake"))
    run = coordinator.reserve_run(session_id="chat", project=None, task="work", provider_id="local")
    assert run is not None
    assert coordinator.start_run(run.run_id)
    coordinator.release_unstarted_run(run.run_id)
    assert registry.is_busy()
    assert woken == []
    assert registry.finish(run.run_id, {"type": "task_done", "stop_reason": "done"}) is not None
    run = coordinator.reserve_run(session_id="chat", project=None, task="work", provider_id="local")
    coordinator.release_unstarted_run(run.run_id)
    coordinator.release_unstarted_run(run.run_id)
    assert not registry.is_busy()
    assert woken == ["wake"]
