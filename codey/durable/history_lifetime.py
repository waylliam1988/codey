"""Read lifetimes shared by sessions opening the same database.

Local readers share one OS lease. Other processes may write SQLite normally,
but history deletion/cleanup must acquire the same lease. No session write
lock is held while draining readers. The last reader may release the OS
handle on a different thread from the first reader.
"""

from __future__ import annotations

import os
import threading
import weakref
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from codey.storage.file_lock import LockTimeout, _acquire_os_lock, _release_os_lock


class HistoryLifetime:
    def __init__(self, lock_path: Path | None = None) -> None:
        self._path = lock_path
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._writers = 0
        self._owner: int | None = None
        self._depth = 0
        self._fd: int | None = None

    def _acquire(self, *, timeout: float = 30) -> None:
        if self._path is not None:
            self._fd = _acquire_os_lock(self._path, timeout_seconds=timeout)

    def _release(self) -> None:
        if self._fd is not None:
            _release_os_lock(self._fd)
            self._fd = None

    @contextmanager
    def read(self) -> Generator[None]:
        with self._condition:
            if self._owner != threading.get_ident() and (self._owner is not None or self._writers):
                # Admission may be called from an existing control/mutation
                # line. Waiting here would invert deletion's lock ordering.
                raise ValueError("history snapshot changed")
            owned = self._owner == threading.get_ident()
            if not owned:
                if self._readers == 0:
                    try:
                        self._acquire(timeout=0)
                    except LockTimeout as exc:
                        raise ValueError("history snapshot changed") from exc
                self._readers += 1
        try:
            yield
        finally:
            if not owned:
                with self._condition:
                    self._readers -= 1
                    if self._readers == 0:
                        self._release()
                        self._condition.notify_all()

    @contextmanager
    def exclusive(self) -> Generator[None]:
        identity = threading.get_ident()
        with self._condition:
            if self._owner == identity:
                self._depth += 1
            else:
                self._writers += 1
                try:
                    while self._owner is not None or self._readers:
                        self._condition.wait()
                    self._acquire()
                    self._owner = identity
                    self._depth = 1
                finally:
                    self._writers -= 1
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._depth -= 1
                if self._depth == 0:
                    self._owner = None
                    self._release()
                    self._condition.notify_all()


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: weakref.WeakValueDictionary[str, HistoryLifetime] = weakref.WeakValueDictionary()


def database_lifetime(path: Path) -> HistoryLifetime:
    key = os.path.normcase(str(path.resolve()))
    with _REGISTRY_LOCK:
        lifetime = _REGISTRY.get(key)
        if lifetime is None:
            lifetime = HistoryLifetime(path.with_name(f".{path.name}.history.lock"))
            _REGISTRY[key] = lifetime
        return lifetime
