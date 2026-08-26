"""Locked, atomic JSON mutations for local Codey state.

An atomic write is not a transaction: ``temp + os.replace`` guarantees that
readers never see a torn file, but two concurrent processes can both read
version N and both write their own N+1 -- silently dropping one side's
mutation. These primitives wrap the same durable write in an advisory
sidecar lock so every read-modify-write cycle against one state file is
serialized between cooperating Codey processes on one machine.

This is local single-machine coordination, not distributed transactionality:
holders that crash leave a stale lock (taken over after a bounded age), and
non-cooperating readers are never blocked.
"""

from __future__ import annotations

import errno
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from codey.storage.local_store import MAX_JSON_BYTES, read_json, write_json_atomic


LOCK_TIMEOUT_SECONDS = 10.0
LOCK_STALE_SECONDS = 30.0
LOCK_POLL_INTERVAL = 0.02


class LockTimeout(RuntimeError):
    """A state lock could not be acquired within the bounded timeout."""


# Reentrancy is tracked PER THREAD: two threads of one process are
# independent writers and must exclude each other through the lock file,
# while nested calls inside one thread reuse its held lock.
_LOCAL = threading.local()


def _held_locks() -> dict[str, list]:
    held = getattr(_LOCAL, "locks", None)
    if held is None:
        held = {}
        _LOCAL.locks = held
    return held


def _lock_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _lock_key(lock_path: Path) -> str:
    return os.path.normcase(str(lock_path.resolve()))


@contextmanager
def with_file_lock(
    path: str | Path,
    *,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = LOCK_STALE_SECONDS,
) -> Iterator[None]:
    """Serialize the enclosed block across processes for one state file.

    The lock is reentrant within a process so composed store operations
    (an append that internally rewrites) cannot deadlock against themselves.
    """

    lock_path = _lock_path_for(Path(path))
    key = _lock_key(lock_path)
    held = _held_locks().get(key)
    if held is not None:
        held[2] += 1
        try:
            yield
        finally:
            held[2] -= 1
        return
    handle, token = _acquire_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
        stale_seconds=stale_seconds,
    )
    _held_locks()[key] = [handle, token, 1]
    try:
        yield
    finally:
        _held_locks().pop(key, None)
        _release_lock(handle, lock_path, token)


def mutate_json_atomic(
    path: str | Path,
    mutator: Callable[[dict | None], dict | None],
    *,
    max_bytes: int = MAX_JSON_BYTES,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = LOCK_STALE_SECONDS,
) -> dict | None:
    """Locked read-modify-write of one JSON object file.

    ``mutator`` receives the parsed current object (``None`` when absent or
    unreadable) and returns the next object to persist; returning ``None``
    aborts without writing. Returns the payload now stored at ``path``.
    """

    target = Path(path)
    with with_file_lock(
        target,
        timeout_seconds=timeout_seconds,
        stale_seconds=stale_seconds,
    ):
        current = read_json(target, max_bytes=max_bytes)
        updated = mutator(current)
        if updated is None:
            return current
        write_json_atomic(target, updated, max_bytes=max_bytes)
        return updated


def append_json_array_locked(
    path: str | Path,
    rows: list[dict],
    *,
    max_bytes: int = MAX_JSON_BYTES,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    stale_seconds: float = LOCK_STALE_SECONDS,
) -> list[dict]:
    """Locked append of rows to a JSON array file, creating it when absent.

    Raises ValueError when the resulting array would exceed ``max_bytes``;
    the previous contents stay intact because the write itself is atomic.
    """

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TypeError("append rows must be a list of objects")
    target = Path(path)

    def _mutate(current: dict | None) -> dict | None:
        raw_items = current.get("items") if isinstance(current, dict) else None
        merged = [
            *(item for item in (raw_items or []) if isinstance(item, dict)),
            *rows,
        ]
        return {"schema_version": 1, "items": merged}

    payload = mutate_json_atomic(
        target,
        _mutate,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        stale_seconds=stale_seconds,
    )
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _acquire_lock(
    lock_path: Path,
    *,
    timeout_seconds: float,
    stale_seconds: float,
) -> tuple[int, str]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            # Atomic acquisition: only one process can create the lock file.
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(handle, token.encode("utf-8"))
            except OSError:
                pass
            return handle, token
        except FileExistsError:
            pass
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EPERM):
                raise
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > stale_seconds:
                # A crashed holder never releases; take over the lock.
                lock_path.unlink()
                continue
        except FileNotFoundError:
            continue
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise LockTimeout(f"timed out acquiring lock: {lock_path.name}")
        time.sleep(LOCK_POLL_INTERVAL)


def _release_lock(handle: int, lock_path: Path, token: str) -> None:
    try:
        os.close(handle)
    except OSError:
        pass
    try:
        # Release only when still ours: a stale-lock takeover may have
        # handed ownership to another process between acquire and release.
        if lock_path.read_text(encoding="utf-8").strip() == token:
            lock_path.unlink()
    except (OSError, UnicodeDecodeError):
        pass


__all__ = [
    "LockTimeout",
    "LOCK_POLL_INTERVAL",
    "LOCK_STALE_SECONDS",
    "LOCK_TIMEOUT_SECONDS",
    "append_json_array_locked",
    "mutate_json_atomic",
    "with_file_lock",
]
