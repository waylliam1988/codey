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


class ConnectionLease:
    """Single ownership token for one checked out physical connection."""

    ACTIVE = "active"
    RETURNING = "returning"
    CANCELLING = "cancelling"
    RELEASED = "released"
    CLOSED = "closed"

    def __init__(self, session: ApiHttpSession, key: ConnectionKey, connection: Any = None) -> None:
        self.session = session
        self.key = key
        self.connection: object | None = connection
        self._lock = threading.Lock()
        self.state = self.ACTIVE

    def attach(self, connection: Any) -> bool:
        with self._lock:
            if self.state != self.ACTIVE or self.connection is not None:
                return False
            self.connection = connection
            return True

    def begin_return(self) -> object | None:
        with self._lock:
            if self.state != self.ACTIVE:
                return None
            self.state = self.RETURNING
            connection, self.connection = self.connection, None
            return connection

    def cancel(self) -> object | None:
        with self._lock:
            if self.state != self.ACTIVE:
                return None
            self.state = self.CANCELLING
            connection, self.connection = self.connection, None
            return connection

    def finish_return(self, connection: Any, reusable: bool) -> None:
        if reusable:
            closed = self.session.return_lease(self.key, connection)
        else:
            self.session.discard_connection(connection)
            closed = True
        with self._lock:
            self.state = self.CLOSED if closed else self.RELEASED

    def finish_cancel(self, connection: Any) -> None:
        self.session.discard_connection(connection)
        with self._lock:
            self.state = self.CLOSED


class ApiHttpSession:
    def __init__(self, *, idle_ttl: float = 60.0, max_idle_per_key: int = 3,
                 max_idle_total: int = 16) -> None:
        self.idle_ttl = idle_ttl
        self.max_idle_per_key = max(1, max_idle_per_key)
        self.max_idle_total = max(1, max_idle_total)
        self._lock = threading.Lock()
        self._refs = 1
        self._closed = False
        self._idle: dict[ConnectionKey, list[tuple[float, Any]]] = {}

    def retain(self) -> ApiHttpSession:
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session is closed")
            self._refs += 1
        return self

    def release(self) -> None:
        to_close: list[Any] = []
        with self._lock:
            if self._refs <= 0:
                raise RuntimeError("HTTP session released more than once")
            self._refs -= 1
            if self._refs == 0:
                self._closed = True
                to_close = [connection for group in self._idle.values() for _, connection in group]
                self._idle.clear()
        for connection in to_close:
            self._discard(connection)

    def acquire_connection(self, key: ConnectionKey) -> object | None:
        lease = self.acquire_lease(key)
        return None if lease is None else lease.connection

    def acquire_lease(self, key: ConnectionKey) -> ConnectionLease | None:
        now = time.monotonic()
        expired: list[Any] = []
        lease: ConnectionLease | None = None
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session is closed")
            for other_key, group in list(self._idle.items()):
                keep = [(created, connection) for created, connection in group if now - created <= self.idle_ttl]
                expired.extend(connection for created, connection in group if now - created > self.idle_ttl)
                if keep:
                    self._idle[other_key] = keep
                else:
                    self._idle.pop(other_key, None)
            values = self._idle.get(key, [])
            while values:
                created, connection = values.pop()
                if now - created <= self.idle_ttl:
                    lease = ConnectionLease(self, key, cast(object, connection))
                    break
                expired.append(connection)
            else:
                self._idle.pop(key, None)
        for item in expired:
            self._discard(item)
        return lease

    def release_connection(self, key: ConnectionKey, connection: Any) -> None:
        self.return_lease(key, connection)

    def return_lease(self, key: ConnectionKey, connection: Any) -> bool:
        to_close: list[Any] = []
        with self._lock:
            close = self._closed
            if not close:
                group = self._idle.setdefault(key, [])
                group.append((time.monotonic(), connection))
                while len(group) > self.max_idle_per_key:
                    _, old = group.pop(0)
                    to_close.append(old)
                all_idle = [(created, other_key, item) for other_key, values in self._idle.items()
                            for created, item in values]
                all_idle.sort(key=lambda entry: entry[0])
                while len(all_idle) > self.max_idle_total:
                    created, other_key, old = all_idle.pop(0)
                    values = self._idle.get(other_key, [])
                    try:
                        values.remove((created, old))
                    except ValueError:
                        continue
                    if values:
                        self._idle[other_key] = values
                    else:
                        self._idle.pop(other_key, None)
                    to_close.append(old)
        for item in to_close:
            self._discard(item)
        if close:
            self._discard(connection)
        return close

    def discard_connection(self, connection: Any) -> None:
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
