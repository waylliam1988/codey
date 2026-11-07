from __future__ import annotations

import threading
from pathlib import Path

import pytest

from codey.durable import (
    CommitResult,
    DurableMutation,
    DurableSession,
    DurableStore,
    EntryDraft,
    MemoryDurableStore,
    SQLiteDurableStore,
)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_create_conversation_is_a_durable_commit(backend: str, tmp_path: Path) -> None:
    store: DurableStore = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    observed: list[tuple[CommitResult, DurableMutation]] = []
    session = DurableSession(store, on_commit=lambda result, mutation: observed.append((result, mutation)))

    result = session.create_conversation("conversation")

    assert result.sequence == 1
    assert session.commit_sequence() == 1
    assert session.head("conversation") == (None, 0)
    assert len(observed) == 1
    assert observed[0][0] == result
    assert observed[0][1].create_conversation is not None


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_fork_is_published_as_the_child_creation_commit(backend: str, tmp_path: Path) -> None:
    store: DurableStore = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    observed: list[tuple[CommitResult, DurableMutation]] = []
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


def test_history_window_shares_the_session_mutation_line(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DurableSession(MemoryDurableStore())
    session.create_conversation("conversation")
    entry = session.append_message("conversation", "user_message", {"text": "hello"})

    store_entries_entered = threading.Event()
    release_store_entries = threading.Event()
    completed = threading.Event()
    errors: list[BaseException] = []
    original_entries = session.store.entries

    def blocked_entries(conversation_id: str) -> tuple[EntryDraft, ...]:
        store_entries_entered.set()
        if not release_store_entries.wait(5):
            raise AssertionError("test did not release the store read")
        return original_entries(conversation_id)

    monkeypatch.setattr(session.store, "entries", blocked_entries)

    def read_window() -> None:
        try:
            assert session.history_window("conversation", entry.entry_id)["items"]
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    with session.control_line():
        worker = threading.Thread(target=read_window)
        worker.start()
        assert not store_entries_entered.wait(0.2)
        assert not completed.is_set()

    assert store_entries_entered.wait(5)
    release_store_entries.set()
    worker.join(5)
    assert completed.is_set()
    assert not errors
