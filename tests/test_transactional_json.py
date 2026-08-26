"""Transactional JSON primitives: locked read-modify-write for local state."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from codey.storage.transactional_json import (
    LockTimeout,
    LOCK_STALE_SECONDS,
    append_json_array_locked,
    mutate_json_atomic,
    with_file_lock,
)


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class WithFileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_lock_is_reentrant_within_process(self) -> None:
        target = self.root / "state.json"
        with with_file_lock(target, timeout_seconds=2.0, stale_seconds=60.0):
            # Nested acquisition must not deadlock or time out.
            with with_file_lock(target, timeout_seconds=2.0, stale_seconds=60.0):
                pass

    def test_timeout_raises_locktimeout_when_holder_persists(self) -> None:
        target = self.root / "state.json"

        def holder() -> None:
            with with_file_lock(target, timeout_seconds=5.0, stale_seconds=60.0):
                time.sleep(1.5)

        thread = threading.Thread(target=holder)
        thread.start()
        time.sleep(0.2)
        try:
            with self.assertRaises(LockTimeout):
                with with_file_lock(target, timeout_seconds=0.3, stale_seconds=60.0):
                    pass
        finally:
            thread.join()

    def test_stale_lock_is_taken_over(self) -> None:
        target = self.root / "state.json"
        lock = target.with_name(f".{target.name}.lock")
        lock.write_text("deadbeef", encoding="utf-8")
        old = time.time() - (LOCK_STALE_SECONDS + 30.0)
        import os

        os.utime(lock, (old, old))

        with with_file_lock(target, timeout_seconds=2.0, stale_seconds=LOCK_STALE_SECONDS):
            self.assertTrue(lock.exists())

        self.assertFalse(lock.exists())

    def test_release_removes_only_own_lock_file(self) -> None:
        target = self.root / "state.json"
        lock_path = target.with_name(f".{target.name}.lock")
        with with_file_lock(target):
            pass
        self.assertFalse(lock_path.exists())


class MutateJsonAtomicTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_creates_missing_file_and_returns_stored_value(self) -> None:
        path = self.root / "nested" / "state.json"
        result = mutate_json_atomic(path, lambda current: {"count": 1})
        self.assertEqual(result, {"count": 1})
        self.assertEqual(_read_payload(path), {"count": 1})

    def test_mutator_sees_previous_value(self) -> None:
        path = self.root / "state.json"
        _write_payload(path, {"count": 1})

        seen: list = []

        def bump(current: dict | None) -> dict | None:
            seen.append(current)
            return {**(current or {}), "count": current["count"] + 1}

        result = mutate_json_atomic(path, bump)

        self.assertEqual(seen, [{"count": 1}])
        self.assertEqual(result, {"count": 2})
        self.assertEqual(_read_payload(path), {"count": 2})

    def test_none_from_mutator_leaves_file_unchanged(self) -> None:
        path = self.root / "state.json"
        _write_payload(path, {"keep": True})
        result = mutate_json_atomic(path, lambda current: None)
        self.assertEqual(result, {"keep": True})
        self.assertEqual(_read_payload(path), {"keep": True})

    def test_concurrent_mutations_both_survive(self) -> None:
        path = self.root / "state.json"
        _write_payload(path, {"a": False, "b": False})
        start = threading.Barrier(2)

        def flip(key: str) -> None:
            start.wait()
            mutate_json_atomic(
                path,
                lambda current: (
                    time.sleep(0.15),
                    {**current, key: True},
                )[-1],
                timeout_seconds=10.0,
            )

        threads = [
            threading.Thread(target=flip, args=(key,))
            for key in ("a", "b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = _read_payload(path)
        self.assertEqual(final, {"a": True, "b": True})


class AppendJsonArrayLockedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "rows.json"

    def test_appends_to_missing_and_existing_files(self) -> None:
        append_json_array_locked(self.path, [{"id": 1}])
        rows = append_json_array_locked(self.path, [{"id": 2}, {"id": 3}])
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])

    def test_concurrent_appends_keep_every_row(self) -> None:
        start = threading.Barrier(4)

        def add(index: int) -> None:
            start.wait()
            append_json_array_locked(self.path, [{"id": index}], timeout_seconds=10.0)

        threads = [threading.Thread(target=add, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = append_json_array_locked(self.path, [])
        self.assertEqual(sorted(row["id"] for row in rows), [0, 1, 2, 3])

    def test_rejects_non_object_rows(self) -> None:
        with self.assertRaises(TypeError):
            append_json_array_locked(self.path, ["nope"])


if __name__ == "__main__":
    unittest.main()
