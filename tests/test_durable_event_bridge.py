from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from codey.app.durable_event_bridge import DurableEventBridge
from codey.durable.model import CommitRecord, CommitResult, DurableMutation


def _commit_result(sequence: int = 1) -> CommitResult:
    return CommitResult(
        revision=1,
        sequence=sequence,
        record=CommitRecord(
            sequence=sequence,
            kind="task",
            committed_at="2026-11-07T00:00:00Z",
            payload={"task_id": "task-1"},
        ),
    )


def test_publish_commit_defers_while_coordination_lock_is_held() -> None:
    lock = threading.Lock()
    published: list[dict[str, object]] = []
    bridge = DurableEventBridge(lock, published.append)
    mutation = DurableMutation("conversation-1", 0)

    with lock:
        bridge.publish_commit(_commit_result(), mutation)
        assert published == []

    bridge.flush()

    assert published == [
        {
            "task_id": "task-1",
            "type": "durable_commit",
            "commit_seq": 1,
            "kind": "task",
            "committed_at": "2026-11-07T00:00:00Z",
            "schema_version": 1,
        }
    ]


def test_publish_commit_ignores_replayed_noop_without_record() -> None:
    lock = threading.Lock()
    published: list[dict[str, object]] = []
    bridge = DurableEventBridge(lock, published.append)

    bridge.publish_commit(CommitResult(revision=1, sequence=1), DurableMutation("conversation-1", 0))

    assert published == []


def test_flush_cannot_miss_a_publisher_deferring_behind_another_operation() -> None:
    lock = threading.Lock()
    published: list[dict[str, object]] = []
    bridge = DurableEventBridge(lock, published.append)
    appending, resume_append, flushing, flushed = (threading.Event() for _ in range(4))

    class PausedAppend(list[dict[str, object]]):
        def append(self, item: dict[str, object]) -> None:
            appending.set()
            assert resume_append.wait(5), "publisher was not released"
            super().append(item)

    bridge._deferred = PausedAppend()

    def publish() -> None:
        bridge.publish_commit(_commit_result(), DurableMutation("conversation-1", 0))

    def flush() -> None:
        flushing.set()
        bridge.flush()
        flushed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with lock:
            publisher = pool.submit(publish)
            try:
                assert appending.wait(5), "publisher did not observe the held coordination lock"
            except BaseException:
                resume_append.set()
                raise
        flusher = pool.submit(flush)
        try:
            assert flushing.wait(5)
            # A correct flush may wait for the pending append. The old flush
            # returns here, leaving the later append with no operation to drain it.
            flushed.wait(0.2)
        finally:
            resume_append.set()
        publisher.result(timeout=5)
        flusher.result(timeout=5)

    assert len(published) == 1
    assert published[0]["commit_seq"] == 1
    assert not bridge._deferred


def test_flush_publishes_outside_both_locks_and_drains_reentrant_commits() -> None:
    lock = threading.Lock()
    published: list[dict[str, object]] = []

    def publish(payload: dict[str, object]) -> None:
        assert lock.acquire(blocking=False), "projection must run outside the coordination lock"
        try:
            assert bridge._deferred_lock.acquire(blocking=False), "projection must run outside the queue lock"
            bridge._deferred_lock.release()
            published.append(payload)
            if payload["commit_seq"] == 1:
                bridge.publish_commit(_commit_result(2), DurableMutation("conversation-1", 0))
        finally:
            lock.release()

    bridge = DurableEventBridge(lock, publish)
    with lock:
        bridge.publish_commit(_commit_result(), DurableMutation("conversation-1", 0))
    bridge.flush()

    assert [payload["commit_seq"] for payload in published] == [1, 2]
    assert not bridge._deferred


def test_uncontended_publication_is_immediate() -> None:
    published: list[dict[str, object]] = []
    bridge = DurableEventBridge(threading.Lock(), published.append)
    bridge.publish_commit(_commit_result(), DurableMutation("conversation-1", 0))
    assert len(published) == 1
    assert published[0]["commit_seq"] == 1
    assert not bridge._deferred
