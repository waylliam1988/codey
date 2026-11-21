"""The existing Core scheduler owns deadlines and failed projection recovery."""

from __future__ import annotations

import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.memory_maintenance import MemoryMaintenance
from codey.app.memory_setup import observe_terminal
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.scheduler import DurableTaskScheduler
from codey.ghost.backend import GhostMemory
from codey.memory.contracts import MemoryEvent


def test_idle_deadline_runs_without_a_pending_task_and_stops_on_close(tmp_path: Path) -> None:
    with closing(DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))) as durable:
        scheduler = DurableTaskScheduler(durable, lambda _task: False, retry_interval=0.01)
        completed = threading.Event()
        calls: list[int] = []

        def idle() -> float | None:
            calls.append(1)
            if len(calls) >= 2:
                completed.set()
                return None
            return time.monotonic() + 0.02

        scheduler.set_idle_callback(idle)
        scheduler.start()
        try:
            assert completed.wait(2), scheduler.loop_failure
            assert len(calls) == 2
        finally:
            assert scheduler.close()


def test_failed_terminal_projection_is_recovered_without_reopening_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = GhostMemory.observe
    failures: list[str] = []

    def fail_once(self: GhostMemory, event: MemoryEvent) -> None:
        if event.kind == "task.completed" and not failures:
            failures.append(event.event_id)
            raise OSError("fixture projection failure")
        original(self, event)

    monkeypatch.setattr(GhostMemory, "observe", fail_once)
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="request", task_id="run", text="Remember Python",
                                  provider_snapshot={}, task_state={"project": ""})
        state.durable.claim_task("run")
        state.durable.complete_task("run", "Recorded", state={})
        assert not observe_terminal(state.memory, state.durable, "run", state.managed_outputs)
        assert state.memory.export_state()["items"] == []
        MemoryMaintenance(state.memory, state.durable, state.managed_outputs)()
        assert state.memory.export_state()["items"]
        assert failures and state.durable.task("run").phase == "completed"
        cursor = state.memory.observation_cursor()
        assert cursor > 0
        MemoryMaintenance(state.memory, state.durable, state.managed_outputs)()
        assert state.memory.observation_cursor() >= cursor
