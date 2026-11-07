"""Domain facades share the session's terminal and failed kernel states."""

from __future__ import annotations

import pytest

from codey.durable import (
    DurableSession,
    MemoryDurableStore,
    SessionFailedError,
    SessionSealedError,
)
from codey.durable.model import CommitResult, DurableMutation


def _domain_operation(session: DurableSession, domain: str) -> object:
    if domain == "effect":
        return session.effect_operations.record_effect_intents("task", ())
    if domain == "task":
        return session.task_operations.request_cancel("task")
    return session.approval_operations.settle_approval("missing", settlement={}, resume=False)


def _seeded_session() -> DurableSession:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(
        conversation_id="chat", request_id="request", task_id="task",
        text="work", provider_snapshot={},
    )
    return session


@pytest.mark.parametrize("query", ["conversation", "task"])
def test_closed_session_rejects_direct_domain_queries(query: str) -> None:
    session = _seeded_session()
    def read() -> object:
        if query == "conversation":
            return session.conversation_queries.context("chat")
        return session.task_queries.task("task")

    session.close()

    with pytest.raises(SessionSealedError, match="closed"):
        read()


@pytest.mark.parametrize("query", ["conversation", "task"])
def test_failed_commit_seals_all_domain_query_facades(
    query: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _seeded_session()
    failure = OSError("durable storage unavailable")

    def fail_commit(_mutation: DurableMutation) -> CommitResult:
        raise failure

    monkeypatch.setattr(session.store, "commit", fail_commit)
    with pytest.raises(OSError, match="durable storage unavailable"):
        session.append_message("chat", "user_message", {"text": "next"})

    def read() -> object:
        if query == "conversation":
            return session.conversation_queries.context("chat")
        return session.task_queries.task("task")

    with pytest.raises(SessionFailedError) as raised:
        read()
    assert raised.value.__cause__ is failure
    session.close()


@pytest.mark.parametrize("domain", ["effect", "approval", "task"])
def test_domain_writers_share_closed_kernel(domain: str) -> None:
    session = _seeded_session()
    session.close()
    with pytest.raises(SessionSealedError, match="closed"):
        _domain_operation(session, domain)


@pytest.mark.parametrize("domain", ["effect", "approval", "task"])
def test_domain_writers_share_failure_cause(domain: str, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _seeded_session()
    failure = OSError("disk unavailable")

    def fail_commit(_mutation: DurableMutation) -> CommitResult:
        raise failure

    monkeypatch.setattr(session.store, "commit", fail_commit)
    with pytest.raises(OSError, match="disk unavailable"):
        session.append_message("chat", "user_message", {"text": "next"})
    with pytest.raises(SessionFailedError) as raised:
        _domain_operation(session, domain)
    assert raised.value.__cause__ is failure
    session.close()
