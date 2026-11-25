from __future__ import annotations

from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, SQLiteDurableStore
from tests.support.sqlite_store import TemporarySQLiteStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_branch_requires_the_revision_captured_by_the_writer(backend: str, tmp_path: Path) -> None:
    store = TemporarySQLiteStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    first = DurableSession(store)
    second = DurableSession(store)
    first.create_conversation("conversation")
    entry = first.append_message("conversation", "user_message", {"text": "one"})
    _head, revision = first.head("conversation")

    first.branch("conversation", entry.entry_id, expected_revision=revision)

    with pytest.raises(CommitConflict, match="conversation revision changed"):
        second.branch("conversation", entry.entry_id, expected_revision=revision)


@pytest.mark.parametrize("invalid_revision", [True, 1.0])
def test_message_edit_requires_an_integer_revision(invalid_revision: object) -> None:
    session = DurableSession(TemporarySQLiteStore())
    try:
        session.create_conversation("conversation")
        entry = session.append_message("conversation", "user_message", {"text": "one"})
        head = session.head("conversation")
        sequence = session.commit_sequence()
        assert head[1] == 1

        with pytest.raises(ValueError, match="expected_revision.*non-negative integer"):
            session.admit_input(
                conversation_id="conversation", request_id="edit", task_id="edit", text="updated",
                provider_snapshot={}, task_state={"input": {
                    "kind": "edit_user_message", "entry_id": entry.entry_id, "expected_revision": invalid_revision,
                }},
            )

        assert session.head("conversation") == head
        assert session.commit_sequence() == sequence
    finally:
        session.close()


def test_branch_commit_is_published_with_the_new_head_and_revision() -> None:
    published: list[object] = []
    session = DurableSession(TemporarySQLiteStore(), on_commit=lambda result, _mutation: published.append(result))
    session.create_conversation("conversation")
    entry = session.append_message("conversation", "user_message", {"text": "one"})
    _head, revision = session.head("conversation")

    result = session.branch("conversation", entry.entry_id, expected_revision=revision)

    assert result.revision == revision + 1
    assert session.head("conversation") == (entry.entry_id, revision + 1)
    assert published[-1] == result
    record = session.commits_after(result.sequence - 1)[0].to_payload()
    assert record["projections"] == [{
        "type": "conversation_branched",
        "conversation_id": "conversation",
        "head_entry_id": entry.entry_id,
    }]
