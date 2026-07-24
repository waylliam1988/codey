"""Small deduplicated queue for Provider adapter self-repair."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from codey.adapter_repair import AdapterRepairResult
from codey.provider_diagnostics import (
    FAILURE_CONTROL_MISSING,
    FAILURE_READINESS_STALE,
    FAILURE_RESPONSE_MISSING,
    ProviderFailure,
    sanitize_failure_facts,
)
from codey.provider_supervisor import ProviderHealth, STATE_OPEN
from codey.repair_journal import RepairJournal


STRUCTURAL_FAILURES = {
    FAILURE_CONTROL_MISSING,
    FAILURE_RESPONSE_MISSING,
    FAILURE_READINESS_STALE,
}
REPAIR_COOLDOWN_SECONDS = 15 * 60


@dataclass(frozen=True)
class SelfRepairJob:
    provider_id: str
    failure_kind: str
    failure_stage: str = ""
    enqueued_at: float = 0.0
    next_retry_at: float = 0.0
    failure_facts: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failure_facts", sanitize_failure_facts(self.failure_facts))


class SelfRepairSupervisor:
    """Queue adapter repairs without blocking active user tasks."""

    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        runner: Callable[[SelfRepairJob], AdapterRepairResult] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.journal = RepairJournal(state_home)
        self.runner = runner
        self.clock = clock
        self._lock = threading.Lock()
        self._queued: dict[str, SelfRepairJob] = {}
        self._last_enqueued: dict[str, float] = {}

    def maybe_enqueue(
        self,
        provider_id: str,
        failure: ProviderFailure,
        health: ProviderHealth,
    ) -> bool:
        if not _is_repairable_failure(failure, health):
            return False
        key = _job_key(provider_id, failure)
        now = self.clock()
        with self._lock:
            last = self._last_enqueued.get(key)
            if last is not None and now - last < REPAIR_COOLDOWN_SECONDS:
                return False
            job = SelfRepairJob(
                provider_id=_provider_id(provider_id),
                failure_kind=failure.kind,
                failure_stage=failure.stage,
                enqueued_at=now,
                next_retry_at=now,
                failure_facts=failure.facts,
            )
            self._queued[key] = job
            self._last_enqueued[key] = now
        self.journal.append(
            "self_repair_queued",
            provider=provider_id,
            failure_kind=failure.kind,
            failure_stage=failure.stage,
        )
        return True

    def pending(self) -> tuple[SelfRepairJob, ...]:
        with self._lock:
            return tuple(self._queued.values())

    def has_due_work(self) -> bool:
        now = self.clock()
        with self._lock:
            return any(job.next_retry_at <= now for job in self._queued.values())

    def run_pending_once(self) -> tuple[AdapterRepairResult, ...]:
        if self.runner is None:
            return ()
        now = self.clock()
        with self._lock:
            jobs = [
                (key, job)
                for key, job in self._queued.items()
                if job.next_retry_at <= now
            ]
            for key, _job in jobs:
                self._queued.pop(key, None)
        results: list[AdapterRepairResult] = []
        for key, job in jobs:
            try:
                result = self.runner(job)
            except Exception as exc:
                self.journal.append(
                    "self_repair_runner_error",
                    provider=job.provider_id,
                    error=str(exc),
                )
                self._retry_later(key, job)
                continue
            results.append(result)
            self.journal.append(
                "self_repair_finished",
                provider=job.provider_id,
                ok=result.ok,
                generation=result.generation,
                error=result.error,
            )
            if not result.ok:
                self._retry_later(key, job)
        return tuple(results)

    def _retry_later(self, key: str, job: SelfRepairJob) -> None:
        retry_at = self.clock() + REPAIR_COOLDOWN_SECONDS
        with self._lock:
            self._queued[key] = replace(job, next_retry_at=retry_at)


def _is_repairable_failure(failure: ProviderFailure, health: ProviderHealth) -> bool:
    return (
        failure.kind in STRUCTURAL_FAILURES
        and health.state == STATE_OPEN
        and bool(_provider_id(failure.model) or failure.model)
    )


def _job_key(provider_id: str, failure: ProviderFailure) -> str:
    return f"{_provider_id(provider_id)}:{failure.kind}:{failure.stage}"


def _provider_id(value: object) -> str:
    return str(value or "").strip().lower()
