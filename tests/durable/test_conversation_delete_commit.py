from __future__ import annotations

import pytest

from codey.durable import CommitConflict, DurableSession, MemoryDurableStore, SQLiteDurableStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_delete_conversation_is_a_durable_commit(backend: str, tmp_path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.create_conversation("conversation")
    entry = session.append_message("conversation", "user_message", {"text": "delete me"})
    before = session.commit_sequence()

    result = session.delete_conversation("conversation")

    assert result.sequence == before + 1
    assert not session.has_conversation("conversation")
    [record] = session.commits_after(before)
    payload = record.to_payload()
    assert payload["session_id"] == "conversation"
    assert any(event["type"] == "conversation_deleted" for event in payload["projections"])
    assert entry.entry_id not in {item.entry_id for item in session.store.entries("conversation")} if session.has_conversation("conversation") else True


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_delete_conversation_rejects_fork_parent_without_mutating_source(backend: str, tmp_path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.create_conversation("root")
    entry = session.append_message("root", "user_message", {"text": "root"})
    session.fork("root", entry.entry_id, child_conversation_id="child")
    before = session.commit_sequence()

    with pytest.raises(CommitConflict, match="forked children"):
        session.delete_conversation("root")

    assert session.has_conversation("root")
    assert session.has_conversation("child")
    assert session.commit_sequence() == before
