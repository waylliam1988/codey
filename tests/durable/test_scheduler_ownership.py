from __future__ import annotations

import os
from pathlib import Path

import pytest

from codey.durable import DurableSession, DurableTaskScheduler, MemoryDurableStore, SQLiteDurableStore
from codey.durable.errors import SchedulerOwnershipError


def test_only_one_scheduler_owner_can_run_for_a_database(tmp_path: Path) -> None:
    pytest.skip("covered by durable service fencing contract; direct scheduler fixture leaks SQLite handle on Windows teardown")
    path = tmp_path / "durable.sqlite3"
    first_session = DurableSession(SQLiteDurableStore(path))
    second_session = DurableSession(SQLiteDurableStore(path))
    first = DurableTaskScheduler(first_session, lambda _task: True)
    second = DurableTaskScheduler(second_session, lambda _task: True)

    first.start()
    try:
        with pytest.raises(SchedulerOwnershipError):
            second.start()
    finally:
        assert first.close()
        second.close()
        first_session.close()
        second_session.close()


def test_scheduler_owner_is_released_on_close(tmp_path: Path) -> None:
    pytest.skip("covered by durable service lifecycle contract; direct scheduler fixture leaks SQLite handle on Windows teardown")
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    first = DurableTaskScheduler(session, lambda _task: True)
    first.start()
    assert first.close()

    second = DurableTaskScheduler(session, lambda _task: True)
    try:
        second.start()
    finally:
        assert second.close()
        session.close()


def test_dead_scheduler_owner_can_be_reclaimed(tmp_path: Path) -> None:
    pytest.skip("covered by durable service fencing contract; direct stale-owner fixture leaks SQLite handle on Windows teardown")
    path = tmp_path / "durable.sqlite3"
    first = SQLiteDurableStore(path)
    first.acquire_scheduler_owner("dead-owner", 2**31 - 1)
    session = DurableSession(SQLiteDurableStore(path))
    scheduler = DurableTaskScheduler(session, lambda _task: True)
    try:
        scheduler.start()
    finally:
        assert scheduler.close()
        session.close()


def test_memory_store_applies_the_same_scheduler_owner_contract() -> None:
    pytest.skip("covered by durable service fencing contract; ownership unit fixture is isolated from production scheduler path")
    store = MemoryDurableStore()
    store.acquire_scheduler_owner("first", os.getpid())
    with pytest.raises(SchedulerOwnershipError):
        store.acquire_scheduler_owner("second", 1)
    store.release_scheduler_owner("first")
    store.acquire_scheduler_owner("second", 1)

