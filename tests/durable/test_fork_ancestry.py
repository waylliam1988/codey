from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore, SubmissionPlacement


@pytest.fixture(params=["memory", "sqlite"])
def session(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[DurableSession]:
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    durable = DurableSession(store)
    durable.create_conversation("root")
    try:
        yield durable
    finally:
        durable.close()


def test_nested_forks_keep_every_ancestor_and_each_parent_cap(session: DurableSession) -> None:
    root = session.append_message("root", "user_message", {"text": "root"})
    child = session.fork("root", root.entry_id)
    session.append_message("root", "assistant_message", {"text": "excluded root tail"})
    child_entry = session.append_message(child, "user_message", {"text": "child"})
    grandchild = session.fork(child, child_entry.entry_id)
    session.append_message(child, "assistant_message", {"text": "excluded child tail"})
    session.append_message(grandchild, "user_message", {"text": "grandchild"})

    assert [entry.payload["text"] for entry in session.context(grandchild)] == ["root", "child", "grandchild"]
    assert [item["text"] for item in session.history_page(grandchild)["items"]] == ["root", "child", "grandchild"]


def test_fork_at_inherited_entry_excludes_later_local_history(session: DurableSession) -> None:
    first = session.append_message("root", "user_message", {"text": "one"})
    second = session.append_message("root", "assistant_message", {"text": "two"})
    child = session.fork("root", second.entry_id)
    session.append_message(child, "user_message", {"text": "child"})
    parent_head = session.store.head(child)
    grandchild = session.fork(child, first.entry_id)
    session.append_message(grandchild, "user_message", {"text": "new branch"})

    assert [entry.payload["text"] for entry in session.context(grandchild)] == ["one", "new branch"]
    assert session.context(child, leaf_id=first.entry_id) == (first,)
    assert session.store.head(child) == parent_head


def test_fork_rejects_ancestor_entries_beyond_cap_without_committing(session: DurableSession) -> None:
    first = session.append_message("root", "user_message", {"text": "one"})
    child = session.fork("root", first.entry_id)
    hidden = session.append_message("root", "assistant_message", {"text": "hidden"})
    before = session.store.commit_sequence()

    with pytest.raises(KeyError):
        session.fork(child, hidden.entry_id, child_conversation_id="invalid")
    with pytest.raises(KeyError):
        session.context(child, leaf_id=hidden.entry_id)
    assert not session.has_conversation("invalid")
    assert session.store.commit_sequence() == before


def test_inherited_fork_and_compaction_rebuild_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    durable = DurableSession(SQLiteDurableStore(path))
    durable.create_conversation("root")
    first = durable.append_message("root", "user_message", {"text": "old"})
    compact = durable.append_message("root", "compaction", {"summary": "retained summary"})
    child = durable.fork("root", compact.entry_id)
    durable.append_message(child, "user_message", {"text": "child"})
    grandchild = durable.fork(child, compact.entry_id)
    durable.append_message(grandchild, "user_message", {"text": "new"})
    expected = durable.context_snapshot(grandchild, token_budget=1000)
    durable.close()

    reopened = DurableSession(SQLiteDurableStore(path))
    try:
        assert reopened.context_snapshot(grandchild, token_budget=1000) == expected
        assert first.entry_id in expected.omitted_entry_ids
        assert [message["content"] for message in expected.messages] == ["retained summary", "new"]
    finally:
        reopened.close()


def test_fork_has_a_local_inbox_without_copying_parent_queue(session: DurableSession) -> None:
    root_entry = session.append_message("root", "user_message", {"text": "root"})
    queued = session.admit_input(
        conversation_id="root",
        request_id="queued-root",
        task_id="queued-root-task",
        text="parent only",
        provider_snapshot={"provider_id": "local"},
        placement=SubmissionPlacement.QUEUED,
    )
    child = session.fork("root", root_entry.entry_id)

    assert session.queued_submissions("root") == (session.store.submission(str(queued.submission_id)),)
    assert session.queued_submissions(child) == ()

    child_queued = session.admit_input(
        conversation_id=child,
        request_id="queued-child",
        task_id="queued-child-task",
        text="child only",
        provider_snapshot={"provider_id": "local"},
        placement=SubmissionPlacement.QUEUED,
    )
    assert [item.request_id for item in session.queued_submissions("root")] == ["queued-root"]
    assert [item.request_id for item in session.queued_submissions(child)] == ["queued-child"]
    assert session.store.submission(str(child_queued.submission_id)).conversation_id == child
