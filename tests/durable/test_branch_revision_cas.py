from __future__ import annotations

from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, MemoryDurableStore, SQLiteDurableStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_branch_requires_the_revision_captured_by_the_writer(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    first = DurableSession(store)
    second = DurableSession(store)
    first.create_conversation("conversation")
    entry = first.append_message("conversation", "user_message", {"text": "one"})
    _head, revision = first.head("conversation")

    first.branch("conversation", entry.entry_id, expected_revision=revision)

    with pytest.raises(CommitConflict, match="conversation revision changed"):
        second.branch("conversation", entry.entry_id, expected_revision=revision)


def test_branch_commit_is_published_with_the_new_head_and_revision() -> None:
    published: list[object] = []
    session = DurableSession(MemoryDurableStore(), on_commit=lambda result, _mutation: published.append(result))
    session.create_conversation("conversation")
    entry = session.append_message("conversation", "user_message", {"text": "one"})
    _head, revision = session.head("conversation")

    result = session.branch("conversation", entry.entry_id, expected_revision=revision)

    assert result.revision == revision + 1
    assert session.head("conversation") == (entry.entry_id, revision + 1)
    assert published[-1] == result
