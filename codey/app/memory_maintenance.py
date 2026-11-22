"""Core-owned periodic projection recovery and bounded memory maintenance."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime

from codey.app.knowledge_observation import observe_knowledge
from codey.app.memory_setup import observe_terminal
from codey.durable.session import DurableSession
from codey.knowledge.store import KnowledgeStore
from codey.memory.contracts import MemoryEvent, MemoryMaintenanceRequest
from codey.memory.service import MemoryService
from codey.storage.managed_outputs import ManagedOutputStore


class MemoryMaintenance:
    def __init__(self, memory: MemoryService, durable: DurableSession, outputs: ManagedOutputStore | None,
                 *, interval: float = 300.0, knowledge_store: Callable[[], KnowledgeStore | None] | None = None) -> None:
        self.memory, self.durable, self.outputs = memory, durable, outputs
        self.interval = interval
        self.knowledge_store = knowledge_store
        self._knowledge_cursor = 0

    def _recover_knowledge(self) -> bool:
        store = self.knowledge_store() if self.knowledge_store is not None else None
        if store is None or self.outputs is None:
            return True
        records = self.durable.commits_after(self._knowledge_cursor, limit=100)
        for record in records:
            projections = record.payload.get("projections")
            if isinstance(projections, tuple):
                for event in projections:
                    if (isinstance(event, Mapping) and event.get("type") == "task_progressed"
                            and event.get("phase") == "completed" and isinstance(run_id := event.get("task_id"), str)
                            and not observe_knowledge(store, self.durable, self.outputs, run_id)):
                        return False
            self._knowledge_cursor = record.sequence
        return len(records) < 100

    def __call__(self) -> float | None:
        knowledge_caught_up = self._recover_knowledge()
        if not self.memory.has_governance:
            return time.monotonic() + (self.interval if knowledge_caught_up else 0.1) if self.knowledge_store else None
        records = self.durable.commits_after(self.memory.observation_cursor(), limit=100)
        last = None
        for record in records:
            task_id = record.payload.get("task_id")
            if isinstance(task_id, str) and task_id:
                task = self.durable.task(task_id)
                if task.execution_kind != "tool" and not observe_terminal(self.memory, self.durable, task_id, self.outputs):
                    break
            last = record
        if last is not None:
            self.memory.observe(MemoryEvent("observation-cursor:" + str(last.sequence), "memory.observation_checkpoint",
                last.committed_at, "", "", "", "user", {"sequence": last.sequence}, ("core-commit:" + str(last.sequence),)))
        at = self.memory.clock()
        instant = datetime.fromisoformat(at)
        bucket = int(instant.timestamp() // self.interval) * self.interval
        logical_time = datetime.fromtimestamp(bucket, instant.tzinfo).isoformat()
        cursor = self.memory.maintenance_cursor()
        result = self.memory.maintain_batch(MemoryMaintenanceRequest(
            "maintenance:v3:" + logical_time + ":" + cursor, logical_time, 100, cursor))
        return time.monotonic() + (self.interval if result.complete and len(records) < 100 and knowledge_caught_up else 0.1)
