"""Reusable HTTP resource primitives shared by API providers.

This first implementation deliberately does not retry POSTs.  It provides
ownership and cancellation hooks while the transport keeps framing decisions.
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from typing import Any, cast


@dataclass(frozen=True)
class ConnectionKey:
    base_url: str
    proxy: str
    identity: str
    tls: str


class ApiHttpSession:
    def __init__(self, *, idle_ttl: float = 60.0) -> None:
        self.idle_ttl = idle_ttl
        self._lock = threading.Lock()
        self._refs = 1
        self._idle: dict[ConnectionKey, list[tuple[float, Any]]] = {}

    def retain(self) -> ApiHttpSession:
        with self._lock:
            self._refs += 1
        return self

    def release(self) -> None:
        to_close: list[Any] = []
        with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs == 0:
                to_close = [connection for group in self._idle.values() for _, connection in group]
                self._idle.clear()
        for connection in to_close:
            self._discard(connection)

    def acquire_connection(self, key: ConnectionKey) -> object | None:
        now = time.monotonic()
        with self._lock:
            values = self._idle.get(key, [])
            while values:
                created, connection = values.pop()
                if now - created <= self.idle_ttl:
                    return cast(object, connection)
                self._discard(connection)
        return None

    def release_connection(self, key: ConnectionKey, connection: Any) -> None:
        with self._lock:
            self._idle.setdefault(key, []).append((time.monotonic(), connection))

    def discard_connection(self, connection: Any) -> None:
        with self._lock:
            self._discard(connection)

    def close_idle_connections(self) -> None:
        with self._lock:
            values = [connection for group in self._idle.values() for _, connection in group]
            self._idle.clear()
        for connection in values:
            self._discard(connection)

    @staticmethod
    def _discard(connection: Any) -> None:
        with contextlib.suppress(Exception):
            connection.close()
