"""A derived range edit must commit against the revision it actually read."""
from __future__ import annotations

import pytest

from codey.durable import CommitConflict, DurableSession, IdempotencyConflict, MemoryDurableStore, SQLiteDurableStore
from codey.durable.model import SubmissionPlacement


@pytest.fixture(params=["memory", "sqlite"])
def session(request, tmp_path):
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    durable = DurableSession(store)
    durable.create_conversation("chat")
    original = durable.append_message("chat", "user_message", {"text": "early fact " * 1000})
    durable.append_message("chat", "assistant_message", {"text": "accepted result"})
    try:
        yield durable, original
    finally:
        durable.close()


def write_summary(session, original, revision, **kwargs):
    return session.admit_write(
        conversation_id="chat", request_id="derived-summary", kind="context_edit", data={},
        edits=[{"target": original.entry_id, "action": "replace",
                "messages": [{"role": "assistant", "content": "retained early fact"}]}],
        display_only=False, expected_revision=revision, **kwargs)


@pytest.mark.parametrize("change", ["append", "branch", "source_edit"])
def test_edit_refuses_a_changed_source_revision_without_writing(session, change):
    durable, original = session
    _, captured = durable.head("chat")
    if change == "append":
        durable.append_message("chat", "user_message", {"text": "new tail"})
    elif change == "branch":
        durable.branch("chat", original.entry_id, expected_revision=captured)
    else:
        durable.admit_write(conversation_id="chat", request_id="earlier-edit", kind="context_edit", data={},
                            edits=[{"target": original.entry_id, "action": "omit"}], display_only=False)
    before = durable.head("chat"), durable.commit_sequence(), durable.entries("chat")
    with pytest.raises(CommitConflict, match="conversation revision changed"):
        write_summary(durable, original, captured)
    assert (durable.head("chat"), durable.commit_sequence(), durable.entries("chat")) == before


def test_exact_retry_returns_the_committed_edit_after_other_writes(session):
    durable, original = session
    _, captured = durable.head("chat")
    first = write_summary(durable, original, captured)
    durable.append_message("chat", "user_message", {"text": "new tail"})
    before = durable.head("chat"), durable.commit_sequence()
    second = write_summary(durable, original, captured)
    assert first == second
    assert (durable.head("chat"), durable.commit_sequence()) == before
    assert sum(entry.kind == "context_edit" for entry in durable.entries("chat")) == 1
    assert "retained early fact" in str(durable.context_snapshot("chat", token_budget=100).messages)
    assert original in durable.entries("chat")
    with pytest.raises(IdempotencyConflict):
        write_summary(durable, original, durable.head("chat")[1])


def test_store_cas_still_rejects_a_write_between_admission_read_and_commit(session, monkeypatch):
    durable, original = session
    _, captured = durable.head("chat")
    store_commit = durable.store.commit
    competing = DurableSession(durable.store)
    injected = False

    def commit(mutation):
        nonlocal injected
        if mutation.submission is not None and not injected:
            injected = True
            competing.append_message("chat", "user_message", {"text": "concurrent tail"})
        return store_commit(mutation)

    monkeypatch.setattr(durable.store, "commit", commit)
    with pytest.raises(CommitConflict):
        write_summary(durable, original, captured)
    assert injected
    assert not any(entry.kind == "context_edit" for entry in durable.entries("chat"))
    assert "concurrent tail" in str(durable.context_snapshot("chat", token_budget=10000).messages)


@pytest.mark.parametrize("revision", [True, -1, "1"])
def test_invalid_revision_is_not_coerced_or_ignored(session, revision):
    durable, original = session
    before = durable.commit_sequence()
    with pytest.raises(ValueError, match="revision"):
        write_summary(durable, original, revision)
    assert durable.commit_sequence() == before


def test_revision_fence_cannot_claim_to_guard_a_later_queued_placement(session):
    durable, original = session
    before = durable.commit_sequence()
    with pytest.raises(ValueError, match="placed"):
        write_summary(durable, original, durable.head("chat")[1], placement=SubmissionPlacement.QUEUED)
    assert durable.commit_sequence() == before


def test_committed_range_edit_is_the_same_authority_after_sqlite_restart(tmp_path):
    path = tmp_path / "durable.sqlite3"
    durable = DurableSession(SQLiteDurableStore(path))
    durable.create_conversation("chat")
    original = durable.append_message("chat", "user_message", {"text": "early fact " * 1000})
    durable.append_message("chat", "user_message", {"text": "latest constraint"})
    result = write_summary(durable, original, durable.head("chat")[1])
    snapshot = durable.context_snapshot("chat", token_budget=100)
    durable.close()
    restarted = DurableSession(SQLiteDurableStore(path))
    try:
        assert restarted.context_snapshot("chat", token_budget=100) == snapshot
        assert snapshot.messages == ({"role": "assistant", "content": "retained early fact"},
                                     {"role": "user", "content": "latest constraint"})
        assert restarted.commit_sequence() == result.sequence
    finally:
        restarted.close()


@pytest.mark.parametrize("during_commit", [False, True])
def test_head_identity_refuses_delete_and_recreate_at_the_same_revision(session, monkeypatch, during_commit):
    durable, original = session
    captured_head, captured_revision = durable.head("chat")
    store_commit = durable.store.commit
    competing = DurableSession(durable.store)

    def recreate():
        competing.delete_conversation("chat")
        competing.create_conversation("chat")
        competing.append_message("chat", "user_message", {"text": "new conversation"})
        competing.append_message("chat", "assistant_message", {"text": "new answer"})
        assert durable.head("chat")[1] == captured_revision

    if during_commit:
        def commit(mutation):
            if mutation.submission is not None:
                recreate()
            return store_commit(mutation)
        monkeypatch.setattr(durable.store, "commit", commit)
    else:
        recreate()
    with pytest.raises(CommitConflict):
        write_summary(durable, original, captured_revision, expected_head=captured_head)
    assert not any(entry.kind == "context_edit" for entry in durable.entries("chat"))
    assert "new conversation" in str(durable.context_snapshot("chat", token_budget=100).messages)


def test_head_identity_is_part_of_idempotency_and_checked_after_an_exact_retry(session):
    durable, original = session
    captured_head, captured_revision = durable.head("chat")
    first = write_summary(durable, original, captured_revision, expected_head=captured_head)
    durable.append_message("chat", "user_message", {"text": "later tail"})
    assert write_summary(durable, original, captured_revision, expected_head=captured_head) == first
    with pytest.raises(IdempotencyConflict):
        write_summary(durable, original, captured_revision, expected_head=original.entry_id)


@pytest.mark.parametrize("head", [False, "", 1])
def test_invalid_expected_head_is_not_coerced(session, head):
    durable, original = session
    with pytest.raises(ValueError, match="expected head"):
        write_summary(durable, original, durable.head("chat")[1], expected_head=head)


def test_expected_head_cannot_silently_omit_the_revision_check(session):
    durable, original = session
    before = durable.commit_sequence()
    with pytest.raises(ValueError, match="expected revision"):
        write_summary(durable, original, None, expected_head=durable.head("chat")[0])
    assert durable.commit_sequence() == before


def test_fenced_write_cannot_turn_a_stale_head_marker_into_a_queued_write(session):
    durable, original = session
    head, revision = durable.head("chat")
    before = durable.commit_sequence()
    with pytest.raises(CommitConflict, match="cannot be queued"):
        write_summary(durable, original, revision, expected_head=head, head="missing-entry")
    assert durable.commit_sequence() == before


def test_a_deleted_source_is_not_recreated_by_a_derived_write(session):
    durable, original = session
    head, revision = durable.head("chat")
    durable.delete_conversation("chat")
    before = durable.commit_sequence()
    with pytest.raises(CommitConflict):
        write_summary(durable, original, revision, expected_head=head)
    assert not durable.has_conversation("chat")
    assert durable.commit_sequence() == before
