from __future__ import annotations

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_create_conversation_is_a_durable_commit(backend: str, tmp_path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    observed: list[tuple[object, object]] = []
    session = DurableSession(store, on_commit=lambda result, mutation: observed.append((result, mutation)))

    result = session.create_conversation("conversation")

    assert result.sequence == 1
    assert session.commit_sequence() == 1
    assert session.head("conversation") == (None, 0)
    assert len(observed) == 1
    assert observed[0][0] == result
    assert observed[0][1].create_conversation is not None


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_fork_is_published_as_the_child_creation_commit(backend: str, tmp_path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    observed: list[tuple[object, object]] = []
    session = DurableSession(store, on_commit=lambda result, mutation: observed.append((result, mutation)))
    session.create_conversation("root")
    entry = session.append_message("root", "user_message", {"text": "root"})
    observed.clear()

    child_id = session.fork("root", entry.entry_id, child_conversation_id="child")

    assert child_id == "child"
    assert session.context("child") == (entry,)
    assert len(observed) == 1
    result, mutation = observed[0]
    assert result.sequence == session.commit_sequence()
    assert mutation.conversation_id == "child"
    assert mutation.create_conversation is not None
    assert mutation.create_conversation.parent_conversation_id == "root"
    assert mutation.create_conversation.parent_entry_id == entry.entry_id
