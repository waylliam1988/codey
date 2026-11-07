"""Publish durable commit projections without re-entering coordination locks."""

from __future__ import annotations

import threading
from collections.abc import Callable

from codey.durable.model import CommitResult, DurableMutation


class DurableEventBridge:
    """Bridge committed durable records to an external event projection."""

    def __init__(
        self,
        coordination_lock: threading.Lock,
        publish: Callable[[dict[str, object]], None],
    ) -> None:
        self._coordination_lock = coordination_lock
        self._publish = publish
        self._deferred: list[dict[str, object]] = []

    def publish_commit(self, result: CommitResult, mutation: DurableMutation) -> None:
        del mutation
        if result.record is None:
            return
        payload = result.record.to_payload()
        if self._coordination_lock.locked():
            self._deferred.append(payload)
            return
        self._publish(payload)

    def flush(self) -> None:
        while True:
            with self._coordination_lock:
                if not self._deferred:
                    return
                events = tuple(self._deferred)
                self._deferred.clear()
            for payload in events:
                self._publish(payload)


__all__ = ["DurableEventBridge"]
