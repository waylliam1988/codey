"""Run-scoped physical provider transports used by the abort barrier."""

from __future__ import annotations

import threading


class ActiveProviderRegistry:
    """Keep the raw provider transport for each live durable task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, object] = {}

    def get(self, run_id: str) -> object | None:
        with self._lock:
            return self._providers.get(run_id)

    def register(self, run_id: str, provider: object) -> None:
        if run_id and provider is not None:
            with self._lock:
                self._providers[run_id] = provider

    def unregister(self, run_id: str, provider: object | None = None) -> None:
        if not run_id:
            return
        with self._lock:
            current = self._providers.get(run_id)
            if provider is None or current is provider:
                self._providers.pop(run_id, None)
