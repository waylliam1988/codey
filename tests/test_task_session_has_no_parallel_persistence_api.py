"""TaskSession must not expose a parallel persistence API.

Production recovery is: run log + tool receipts + workspace identity ->
rebuild TaskSession. A second TaskSession -> dict -> TaskSession path would
leave two recovery semantics and let tests bypass the formal entry.
"""
from __future__ import annotations


def test_task_session_has_no_parallel_persistence_api():
    from codey.operations.task_session import TaskSession

    assert not hasattr(TaskSession, "to_payload")
    assert not hasattr(TaskSession, "from_payload")
