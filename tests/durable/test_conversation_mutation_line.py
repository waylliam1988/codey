from __future__ import annotations

import threading
from pathlib import Path
from types import TracebackType

import pytest

from codey.agents.handoff import ConversationSnapshot
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
def test_repeated_handoff_snapshot_reuses_the_latest_entry(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    try:
        session.create_conversation("conversation")
        snapshot = ConversationSnapshot(mode="project", goal="ship it", changed_files=("app.py",))
        original = session.save_context_snapshot("conversation", snapshot.to_payload())
        sequence = session.commit_sequence()
        head = session.head("conversation")

        assert session.save_context_snapshot("conversation", snapshot.to_payload()) == original
        assert session.commit_sequence() == sequence
        assert session.head("conversation") == head

        changed = ConversationSnapshot(mode="project", goal="ship it", changed_files=("app.py", "test_app.py"))
        updated = session.save_context_snapshot("conversation", changed.to_payload())
        assert updated.entry_id != original.entry_id
        assert updated.parent_id == original.entry_id
        assert session.commit_sequence() == sequence + 1
    finally:
        session.close()


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


def test_memory_history_snapshot_is_pinned_on_the_session_line(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DurableSession(MemoryDurableStore())
    session.create_conversation("conversation")
    first = session.append_message("conversation", "user_message", {"text": "before lock"})
    awaiting_admission = threading.Event()
    pages: list[dict[str, object]] = []
    errors: list[Exception] = []

    def read() -> None:
        try:
            pages.append(session.history_page("conversation"))
        except Exception as error:
            errors.append(error)

    reader = threading.Thread(target=read)
    lock = session._write_lock

    class ObservedLock:
        def __enter__(self) -> None:
            if threading.current_thread() is reader:
                awaiting_admission.set()
            lock.acquire()

        def __exit__(self, _kind: type[BaseException] | None, _error: BaseException | None,
                     _traceback: TracebackType | None) -> None:
            lock.release()

    monkeypatch.setattr(session, "_write_lock", ObservedLock())
    try:
        with session.control_line():
            reader.start()
            assert awaiting_admission.wait(5)
            second = session.append_message("conversation", "assistant_message", {"text": "on the line"})
    finally:
        reader.join(5)
        session.close()
    assert not reader.is_alive()
    assert not errors
    [page] = pages
    assert page["display_version"] == second.entry_id
    assert page["snapshot_revision"] == 2
    assert second.parent_id == first.entry_id


def test_history_window_holds_the_session_line_only_for_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    session = DurableSession(MemoryDurableStore())
    session.create_conversation("conversation")
    entry = session.append_message("conversation", "user_message", {"text": "hello"})

    store_entries_entered = threading.Event()
    release_store_entries = threading.Event()
    completed = threading.Event()
    errors: list[BaseException] = []
    from codey.durable.history import HistoryEntry, MemoryHistoryReader

    original_entries = MemoryHistoryReader.select_entries

    def blocked_entries(reader: MemoryHistoryReader, selected: tuple[HistoryEntry, ...]) -> tuple[EntryDraft, ...]:
        store_entries_entered.set()
        if not release_store_entries.wait(5):
            raise AssertionError("test did not release the store read")
        return original_entries(reader, selected)

    monkeypatch.setattr(MemoryHistoryReader, "select_entries", blocked_entries)

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
    session.append_message("conversation", "assistant_message", {"text": "concurrent"})
    release_store_entries.set()
    worker.join(5)
    assert completed.is_set()
    assert not errors
