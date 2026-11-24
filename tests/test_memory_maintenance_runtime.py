"""The existing Core scheduler owns deadlines and failed projection recovery."""

from __future__ import annotations

import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from codey.app import memory_maintenance as maintenance_module
from codey.app.context import AppContext
from codey.app.knowledge_observation import observe_knowledge
from codey.app.memory_maintenance import MemoryMaintenance
from codey.app.memory_setup import build_memory, observe_terminal
from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.scheduler import DurableTaskScheduler
from codey.ghost.backend import GhostMemory
from codey.knowledge.store import KnowledgeStore
from codey.memory.contracts import MemoryEvent
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import require_json_strings
from codey.utils.object_fields import require_object_rows


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


@pytest.mark.parametrize("backend", ("memory", "sqlite"))
def test_deleted_task_history_does_not_block_surviving_task_recovery(tmp_path: Path, backend: str) -> None:
    store = SQLiteDurableStore(tmp_path / "durable.sqlite3") if backend == "sqlite" else MemoryDurableStore()
    with closing(DurableSession(store)) as durable:
        memory = build_memory(tmp_path, durable, enabled=True)
        outputs = ManagedOutputStore(tmp_path)
        durable.admit_input(conversation_id="gone", request_id="old-input", task_id="old-run", text="Old task",
                            provider_snapshot={}, task_state={"project": ""})
        durable.claim_task("old-run")
        durable.complete_task("old-run", "Old answer", state={})
        history_before_delete = durable.commits_after(0)
        durable.delete_conversation("gone")
        with pytest.raises(KeyError):
            durable.task("old-run")
        assert durable.commits_after(0)[:len(history_before_delete)] == history_before_delete

        durable.admit_input(conversation_id="kept", request_id="new-input", task_id="kept-run", text="Remember Python",
                            provider_snapshot={}, task_state={"project": ""})
        durable.claim_task("kept-run")
        durable.complete_task("kept-run", "Recorded", state={})
        last_sequence = durable.commits_after(0)[-1].sequence
        maintenance = MemoryMaintenance(memory, durable, outputs)
        assert maintenance() is not None
        assert memory.observation_cursor() == last_sequence
        items = require_object_rows(memory.export_state()["items"])
        assert items
        refs = {ref for item in items for ref in require_json_strings(item["source_refs"], "source_refs")}
        assert "run:kept-run" in refs
        assert "run:old-run" not in refs
        assert not durable.has_conversation("gone")
        with pytest.raises(KeyError):
            durable.task("old-run")
        assert durable.task("kept-run").phase == "completed"
        assert maintenance() is not None
        assert memory.observation_cursor() == last_sequence
        assert memory.export_state()["items"] == items


def test_maintenance_retries_failed_projection_without_advancing_past_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="chat", request_id="input", task_id="run", text="Remember Python",
                                  provider_snapshot={}, task_state={"project": ""})
        state.durable.claim_task("run")
        state.durable.complete_task("run", "Recorded", state={})
        last_sequence = state.durable.commits_after(0)[-1].sequence
        original = GhostMemory.observe
        attempts: list[str] = []

        def fail_once(self: GhostMemory, event: MemoryEvent) -> None:
            if event.kind == "task.completed":
                attempts.append(event.event_id)
                if len(attempts) == 1:
                    raise OSError("projection unavailable")
            original(self, event)

        monkeypatch.setattr(GhostMemory, "observe", fail_once)
        maintenance = MemoryMaintenance(state.memory, state.durable, state.managed_outputs)
        assert maintenance() is not None
        assert state.memory.observation_cursor() < last_sequence
        assert state.memory.export_state()["items"] == []
        assert maintenance() is not None
        assert state.memory.observation_cursor() == last_sequence
        assert state.memory.export_state()["items"]
        assert len(attempts) >= 2 and attempts[0] == attempts[1]
        assert state.durable.task("run").phase == "completed"


def test_task_storage_error_is_not_treated_as_deleted_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="chat", request_id="input", task_id="run", text="Remember Python",
                                  provider_snapshot={}, task_state={"project": ""})

        def unavailable(self: DurableSession, task_id: str) -> None:
            raise OSError("task storage unavailable")

        monkeypatch.setattr(DurableSession, "task", unavailable)
        with pytest.raises(OSError, match="task storage unavailable"):
            MemoryMaintenance(state.memory, state.durable, state.managed_outputs)()
        assert state.memory.observation_cursor() == 0


@pytest.mark.parametrize("memory_enabled", (False, True))
def test_knowledge_recovery_retries_completion_then_consumes_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory_enabled: bool,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False, memory_enabled=memory_enabled)) as state:
        for task_id in ("first", "second"):
            state.durable.admit_input(conversation_id="chat", request_id=task_id, task_id=task_id, text=task_id,
                                      provider_snapshot={}, task_state={"project": ""})
            state.durable.claim_task(task_id)
            state.durable.complete_task(task_id, "Recorded", state={})
        knowledge = KnowledgeStore(tmp_path / "vault")
        state.knowledge_store = knowledge
        attempts: list[str] = []

        def fail_once(store: KnowledgeStore, durable: DurableSession, outputs: ManagedOutputStore, run_id: str) -> bool:
            assert store is knowledge
            attempts.append(run_id)
            return False if len(attempts) == 1 else observe_knowledge(store, durable, outputs, run_id)

        monkeypatch.setattr(maintenance_module, "observe_knowledge", fail_once)
        maintenance = MemoryMaintenance(state.memory, state.durable, state.managed_outputs, knowledge_store=lambda: knowledge)
        assert maintenance() is not None
        assert attempts == ["first"]
        assert maintenance() is not None
        assert attempts == ["first", "first", "second"]
        assert maintenance() is not None
        assert attempts == ["first", "first", "second"]
        assert all(state.durable.task(task_id).phase == "completed" for task_id in ("first", "second"))
