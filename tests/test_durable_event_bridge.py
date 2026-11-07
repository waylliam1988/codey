from __future__ import annotations

import threading

from codey.app.durable_event_bridge import DurableEventBridge
from codey.durable.model import CommitRecord, CommitResult, DurableMutation


def _commit_result() -> CommitResult:
    return CommitResult(
        revision=1,
        sequence=1,
        record=CommitRecord(
            sequence=1,
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
