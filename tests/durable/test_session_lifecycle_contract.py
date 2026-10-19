from __future__ import annotations

import pytest

from codey.durable import DurableSession, MemoryDurableStore
from codey.durable.errors import SessionFailedError, SessionSealedError


def _session() -> DurableSession:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="conversation",
        request_id="request",
        task_id="task",
        text="inspect",
        provider_snapshot={"provider_id": "local"},
    )
    return session


def test_internal_observer_failure_fails_session_after_durable_commit() -> None:
    session = _session()

    def broken(_result: object, _mutation: object) -> None:
        raise RuntimeError("scheduler adoption failed")

    session.observe_commits(broken)
    with pytest.raises(RuntimeError, match="scheduler adoption failed"):
        session.start_task("task", state={})

    assert session.store.task("task").phase == "provider_prepared"
    with pytest.raises(SessionFailedError) as failure:
        session.task("task")
    assert isinstance(failure.value.cause, RuntimeError)
    assert str(failure.value.cause) == "scheduler adoption failed"


def test_external_projection_failure_is_reported_without_failing_session() -> None:
    session = _session()

    def broken(_result: object, _mutation: object) -> None:
        raise RuntimeError("sse projection failed")

    session.subscribe_commits(broken)
    session.start_task("task", state={})

    assert session.task("task").phase == "provider_prepared"


def test_close_seals_admission_before_notifying_and_is_idempotent() -> None:
    session = _session()
    close_calls: list[str] = []

    def on_close() -> None:
        close_calls.append("closed")
        with pytest.raises(SessionSealedError):
            session.task("task")

    session.subscribe_close(on_close)
    session.close()
    session.close()

    assert close_calls == ["closed"]
    with pytest.raises(SessionSealedError):
        session.entries("conversation")
