"""A failed worker launch must not reserve single-flight ownership forever."""

from __future__ import annotations

import threading
from collections.abc import Callable
from threading import Thread
from typing import Protocol
from unittest.mock import patch

import pytest

from codey.app.knowledge_indexer import KnowledgeIndexer


class _ThreadStarter(Protocol):
    def start(self, worker: Thread, /) -> None: ...


THREAD_STARTER: _ThreadStarter = Thread


class _Store:
    def __init__(self) -> None:
        self.rebuilds = 0

    def rebuild(self) -> None:
        self.rebuilds += 1


def test_disabled_store_does_not_attempt_to_start_a_worker() -> None:
    indexer = KnowledgeIndexer(store=lambda: None)
    with patch.object(THREAD_STARTER, "start", side_effect=AssertionError("no store to rebuild")):
        indexer.schedule()
    assert not indexer.running
    assert not indexer.pending


def test_failed_start_releases_ownership_and_allows_a_later_schedule() -> None:
    store = _Store()
    indexer = KnowledgeIndexer(store=lambda: store)
    workers: list[Thread] = []

    with (
        patch.object(THREAD_STARTER, "start", side_effect=RuntimeError("can't start new thread")),
        pytest.raises(RuntimeError, match="can't start new thread"),
    ):
        indexer.schedule()

    assert not indexer.running, "failed launch retained ownership without a worker"

    def worker(*, target: Callable[[], None], name: str, daemon: bool) -> Thread:
        thread = Thread(target=target, name=name, daemon=daemon)
        workers.append(thread)
        return thread

    with patch.object(threading, "Thread", worker):
        indexer.schedule()
    for thread in workers:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert store.rebuilds == 1
    assert not indexer.running


def test_schedule_racing_with_failed_start_can_admit_a_live_worker() -> None:
    store = _Store()
    indexer = KnowledgeIndexer(store=lambda: store)
    starting, release, entered, returned = (threading.Event() for _ in range(4))
    errors: list[RuntimeError] = []
    workers: list[Thread] = []
    real_start = THREAD_STARTER.start

    def controlled_start(worker: Thread) -> None:
        if not starting.is_set():
            starting.set()
            assert release.wait(5), "failed launch was not released"
            raise RuntimeError("can't start new thread")
        real_start(worker)
        workers.append(worker)

    def failed_schedule() -> None:
        try:
            indexer.schedule()
        except RuntimeError as exc:
            errors.append(exc)

    def other_schedule() -> None:
        entered.set()
        indexer.schedule()
        returned.set()

    first = Thread(target=failed_schedule, daemon=True)
    second = Thread(target=other_schedule, daemon=True)
    with patch.object(THREAD_STARTER, "start", controlled_start):
        real_start(first)
        try:
            assert starting.wait(5)
            real_start(second)
            assert entered.wait(5)
            # The old scheduler returns a pending request against the worker
            # whose launch is about to fail. The corrected admission can wait.
            returned.wait(0.2)
        finally:
            release.set()
            first.join(timeout=5)
            if second.ident is not None:
                second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()

    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert len(errors) == 1
    assert returned.is_set()
    assert store.rebuilds == 1, "the racing request returned without any admitted live worker"
    assert not indexer.running
