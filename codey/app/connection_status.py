"""Non-blocking, revision-aware status coordination for provider connections."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

STATUS_TTL_S = 3.0


@dataclass(frozen=True)
class ProbeRequest:
    provider_id: str
    connection_revision: str
    selected_model: str = ""
    deadline: float | None = None
    force: bool = False


@dataclass(frozen=True)
class ConnectionSnapshot:
    provider_id: str
    state: str = "unknown"
    freshness: str = "fresh"
    source: str = ""
    checked_at: float = 0.0
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.state == "ready"

    def payload(self, label: str) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "label": label,
            "available": self.available,
            "state": self.state,
            "freshness": self.freshness,
            "source": self.source,
            "checked_at": self.checked_at,
            "error": self.error,
        }


class ConnectionStatusCoordinator:
    """Coordinate connector probes without owning connector-specific logic."""

    def __init__(
        self,
        *,
        provider_ids: Callable[[], Iterable[str]],
        labels: Callable[[], dict[str, str]],
        enabled: Callable[[str], bool],
        connector: Callable[[str], Any],
        emit: Callable[[dict[str, Any]], None] | None = None,
        ttl_s: float = STATUS_TTL_S,
    ) -> None:
        self._provider_ids = provider_ids
        self._labels = labels
        self._enabled = enabled
        self._connector = connector
        self._emit = emit
        self._ttl_s = ttl_s
        self._lock = threading.RLock()
        self._snapshots: dict[str, ConnectionSnapshot] = {}
        self._last_good: dict[str, tuple[str, ConnectionSnapshot]] = {}
        self._futures: dict[str, Future[ConnectionSnapshot]] = {}
        self._revisions: dict[str, str] = {}
        self._generations: dict[str, int] = {}
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="provider-status")
        self._closed = False

    def _revision(self, provider_id: str) -> str:
        try:
            connector = self._connector(provider_id)
            status_key = getattr(connector, "status_key", None)
            if callable(status_key):
                value = status_key()
                if value:
                    return str(value)
        except Exception:
            pass
        return provider_id

    def _current(self, provider_id: str) -> ConnectionSnapshot:
        return self._snapshots.get(provider_id, ConnectionSnapshot(provider_id))

    def _submit(self, provider_id: str, revision: str, *, force: bool) -> None:
        with self._lock:
            if self._closed:
                return
            existing = self._futures.get(provider_id)
            if existing is not None and not existing.done() and self._revisions.get(provider_id) == revision:
                return
            self._generations[provider_id] = self._generations.get(provider_id, 0) + 1
            generation = self._generations[provider_id]
            if self._revisions.get(provider_id) != revision:
                self._last_good.pop(provider_id, None)
            current = self._current(provider_id)
            if self._revisions.get(provider_id) == revision and current.state == "ready":
                pending = replace(current, freshness="stale")
            else:
                pending = ConnectionSnapshot(provider_id, state="checking")
            self._revisions[provider_id] = revision
            self._snapshots[provider_id] = pending
            request = ProbeRequest(provider_id, revision, force=force)
            future = self._executor.submit(self._run_probe, request)
            self._futures[provider_id] = future
            future.add_done_callback(lambda done: self._publish(provider_id, revision, generation, done))

    def _run_probe(self, request: ProbeRequest) -> ConnectionSnapshot:
        try:
            connector = self._connector(request.provider_id)
            probe = getattr(connector, "probe_status", None)
            if not callable(probe):
                raise RuntimeError("provider does not expose a status probe")
            raw = probe(request)
            if not isinstance(raw, dict):
                raise ValueError("provider status probe returned an invalid payload")
            state = str(raw.get("state") or ("ready" if raw.get("available") is True else "unavailable"))
            if state not in {"ready", "unavailable", "blocked"}:
                raise ValueError("provider status probe returned an invalid state")
            return ConnectionSnapshot(
                request.provider_id,
                state=state,
                freshness="stale" if raw.get("stale") is True else "fresh",
                source=str(raw.get("source") or "api_preflight"),
                checked_at=time.time(),
                error=str(raw.get("error") or "")[:240],
                data={key: value for key, value in raw.items() if key not in {"state", "available", "stale", "source", "error"}},
            )
        except Exception as exc:
            return ConnectionSnapshot(
                request.provider_id,
                state="unavailable",
                source="api_preflight",
                checked_at=time.time(),
                error=f"{type(exc).__name__}: {exc}"[:240],
            )

    def _publish(self, provider_id: str, revision: str, generation: int, future: Future[ConnectionSnapshot]) -> None:
        try:
            snapshot = future.result()
        except Exception as exc:
            snapshot = ConnectionSnapshot(provider_id, state="unavailable", source="api_preflight", error=str(exc)[:240])
        with self._lock:
            if (self._closed or self._futures.get(provider_id) is not future
                    or self._generations.get(provider_id) != generation
                    or self._revisions.get(provider_id) != revision
                    or provider_id not in self._provider_ids() or not self._enabled(provider_id)):
                return
            if snapshot.state == "ready":
                self._last_good[provider_id] = (revision, snapshot)
            elif snapshot.state == "unavailable" and snapshot.error:
                previous = self._last_good.get(provider_id)
                if previous is not None and previous[0] == revision:
                    snapshot = replace(snapshot, state="ready", freshness="stale", data=previous[1].data)
            self._snapshots[provider_id] = snapshot
            self._futures.pop(provider_id, None)
        if self._emit is not None:
            with contextlib.suppress(Exception):
                label = self._labels().get(provider_id, provider_id)
                self._emit({"type": "providers", "providers": [snapshot.payload(label)]})

    def refresh(self, provider_id: str, *, force: bool = False) -> ConnectionSnapshot:
        revision = self._revision(provider_id)
        with self._lock:
            current = self._current(provider_id)
            age = time.time() - current.checked_at if current.checked_at else float("inf")
            same_revision = self._revisions.get(provider_id) == revision
            running = self._futures.get(provider_id)
            fresh = current.checked_at > 0 and age < self._ttl_s and same_revision
            if not force and (fresh or (running is not None and not running.done() and same_revision)):
                return current
        self._submit(provider_id, revision, force=force)
        return self._current(provider_id)

    def snapshot(self) -> dict[str, ConnectionSnapshot]:
        result: dict[str, ConnectionSnapshot] = {}
        for provider_id in tuple(self._provider_ids()):
            if self._enabled(provider_id):
                result[provider_id] = self.refresh(provider_id)
            else:
                result[provider_id] = ConnectionSnapshot(provider_id, state="unavailable", source="disabled")
        return result

    def wait_for(self, provider_id: str, timeout: float | None = None) -> ConnectionSnapshot:
        with self._lock:
            future = self._futures.get(provider_id)
        if future is not None:
            try:
                result = future.result(timeout=timeout)
            except Exception:
                result = None
            if result is not None:
                with self._lock:
                    generation = self._generations.get(provider_id, 0)
                    revision = self._revisions.get(provider_id, "")
                self._publish(provider_id, revision, generation, future)
        with self._lock:
            return self._current(provider_id)

    def data(self, provider_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._current(provider_id).data)

    def current(self, provider_id: str) -> ConnectionSnapshot:
        with self._lock:
            return self._current(provider_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            futures = tuple(self._futures.values())
            self._futures.clear()
        for future in futures:
            future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["ConnectionSnapshot", "ConnectionStatusCoordinator", "ProbeRequest", "STATUS_TTL_S"]
