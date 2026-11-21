"""Core-owned periodic projection recovery and bounded memory maintenance."""

from __future__ import annotations

import time
from datetime import datetime

from codey.app.memory_setup import observe_terminal
from codey.durable.session import DurableSession
from codey.memory.contracts import MemoryEvent, MemoryMaintenanceRequest
from codey.memory.service import MemoryService
from codey.storage.managed_outputs import ManagedOutputStore


class MemoryMaintenance:
    def __init__(self, memory: MemoryService, durable: DurableSession, outputs: ManagedOutputStore | None,
                 *, interval: float = 300.0) -> None:
        self.memory, self.durable, self.outputs = memory, durable, outputs
        self.interval = interval

    def __call__(self) -> float | None:
        if not self.memory.has_governance:
            return None
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
        return time.monotonic() + (self.interval if result.complete and len(records) < 100 else 0.1)
