"""OS-backed advisory file locks.

This module provides cross-process and cross-thread advisory file locking
using operating-system native locks (msvcrt on Windows, fcntl on POSIX)
paired with process-local thread synchronization.

Lock files are permanent sidecars on disk acting solely as lock carriers.
Ownership is tracked via kernel lock handles and process synchronization,
eliminating file creation/deletion TOCTOU races and stale-lock takeovers.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_INTERVAL = 0.02


class LockTimeout(TimeoutError):
    """A file lock could not be acquired within the bounded timeout."""


@dataclass
class _HeldLock:
    fd: int
    depth: int


@dataclass
class _ProcessLockEntry:
    lock: threading.RLock
    refs: int = 0


_LOCAL = threading.local()
_PROCESS_LOCKS_MUTEX = threading.Lock()
_PROCESS_LOCKS: dict[str, _ProcessLockEntry] = {}


def _held_locks() -> dict[str, _HeldLock]:
    held = getattr(_LOCAL, "held_locks", None)
    if held is None:
        held = {}
        _LOCAL.held_locks = held
    return held


def _lock_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _lock_key(lock_path: Path) -> str:
    try:
        return os.path.normcase(str(lock_path.resolve()))
    except (OSError, RuntimeError):
        return os.path.normcase(str(lock_path))


def _borrow_process_lock(key: str) -> _ProcessLockEntry:
    with _PROCESS_LOCKS_MUTEX:
        entry = _PROCESS_LOCKS.get(key)
        if entry is None:
            entry = _ProcessLockEntry(threading.RLock())
            _PROCESS_LOCKS[key] = entry
        entry.refs += 1
        return entry


def _return_process_lock(key: str, entry: _ProcessLockEntry) -> None:
    with _PROCESS_LOCKS_MUTEX:
        current = _PROCESS_LOCKS.get(key)
        if current is not entry:
            return
        entry.refs -= 1
        if entry.refs <= 0:
            _PROCESS_LOCKS.pop(key, None)


def _acquire_os_lock(lock_path: Path, *, timeout_seconds: float) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    fd = os.open(lock_path, flags, 0o666)
    deadline = time.monotonic() + max(0.0, timeout_seconds)

    while True:
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, PermissionError):
            pass
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EBUSY):
                pass
            else:
                os.close(fd)
                raise

        if time.monotonic() >= deadline:
            os.close(fd)
            raise LockTimeout(f"timed out acquiring lock: {lock_path.name}")
        time.sleep(LOCK_POLL_INTERVAL)


def _release_os_lock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


@contextmanager
def with_file_lock(
    path: str | Path,
    *,
    timeout_seconds: float | None = None,
) -> Iterator[None]:
    """Serialize execution across processes and threads using OS advisory lock."""
    timeout = LOCK_TIMEOUT_SECONDS if timeout_seconds is None else max(0.0, float(timeout_seconds))
    lock_path = _lock_path_for(Path(path))
    key = _lock_key(lock_path)
    held = _held_locks().get(key)
    if held is not None:
        held.depth += 1
        try:
            yield
        finally:
            held.depth -= 1
        return

    entry = _borrow_process_lock(key)
    process_lock = entry.lock
    deadline = time.monotonic() + timeout
    acquired_process = False

    try:
        acquired_process = process_lock.acquire(timeout=timeout)
        if not acquired_process:
            raise LockTimeout(f"timed out acquiring thread lock: {lock_path.name}")

        try:
            remaining_timeout = max(0.0, deadline - time.monotonic())
            fd = _acquire_os_lock(lock_path, timeout_seconds=remaining_timeout)
            _held_locks()[key] = _HeldLock(fd=fd, depth=1)
            try:
                yield
            finally:
                _held_locks().pop(key, None)
                _release_os_lock(fd)
        finally:
            if acquired_process:
                process_lock.release()
    finally:
        _return_process_lock(key, entry)


__all__ = [
    "LockTimeout",
    "LOCK_POLL_INTERVAL",
    "LOCK_TIMEOUT_SECONDS",
    "with_file_lock",
]
