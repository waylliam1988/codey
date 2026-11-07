"""Application ownership of persistent project writer leases."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from pathlib import Path

from codey.storage.file_lock import FileLease
from codey.workspace.changes import ProjectWriteBusy, SnapshotStore


class WorkspaceLeaseCoordinator:
    def __init__(self, lock: threading.Lock, snapshots: Callable[[], SnapshotStore]) -> None:
        self._lock = lock
        self._snapshots = snapshots
        self.leases: dict[str, FileLease] = {}

    def acquire(self, project: str | Path) -> bool:
        text = str(project or "").strip()
        if not text:
            return True
        key = str(Path(text).expanduser().resolve())
        with self._lock:
            if key in self.leases:
                return False
        try:
            lease = self._snapshots().acquire_writer(key, timeout_seconds=0.0)
        except ProjectWriteBusy:
            return False
        with self._lock:
            if key in self.leases:
                with contextlib.suppress(Exception):
                    lease.release()
                return False
            self.leases[key] = lease
            return True

    def release(self, project: str | Path) -> None:
        text = str(project or "").strip()
        if not text:
            return
        key = str(Path(text).expanduser().resolve())
        with self._lock:
            lease = self.leases.pop(key, None)
        if lease is not None:
            with contextlib.suppress(Exception):
                lease.release()

    def close(self) -> bool:
        """Keep failed releases owned so shutdown can retry them."""
        with self._lock:
            pending = tuple(self.leases.items())
        complete = True
        for project, lease in pending:
            try:
                lease.release()
            except Exception:
                complete = False
            else:
                with self._lock:
                    if self.leases.get(project) is lease:
                        self.leases.pop(project, None)
        return complete
