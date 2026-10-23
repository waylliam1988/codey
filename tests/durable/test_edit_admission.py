from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.errors import IdempotencyConflict
from codey.durable.model import DurableMutation, EntryDraft


@pytest.fixture(params=["memory", "sqlite"])
def durable(request, tmp_path: Path):
    store = MemoryDurableStore() if request.param == "memory" else SQLiteDurableStore(tmp_path / "edit.db")
    session = DurableSession(store)
    yield session
    session.close()


def turn(session, name, text, conversation="chat"):
    session.admit_input(conversation_id=conversation, request_id=name, task_id=name, text=text, provider_snapshot={})
    entry = session.task(name).state["input_entry_id"]
    session.complete_task(name, f"answer:{text}", state={})
    return entry


def edit(session, source, text="replacement", *, revision=None, request="edit", conversation="chat"):
    revision = session.store.head(conversation)[1] if revision is None else revision
    return session.admit_input(conversation_id=conversation, request_id=request, task_id=request, text=text,
                               provider_snapshot={}, task_state={"input": {"kind": "edit_user_message", "text": text,
                                   "entry_id": source, "expected_revision": revision}})


def texts(session, conversation="chat"):
    return [entry.payload.get("text") for entry in session.context(conversation) if entry.kind == "user_message"]


@pytest.mark.parametrize("position", [0, 1])
def test_edit_atomically_replaces_suffix_retains_audit_and_replays_once(durable, position):
    first = turn(durable, "first", "one")
    second = turn(durable, "second", "two")
    old_entries = durable.store.entries("chat")
    revision = durable.store.head("chat")[1]
    result = edit(durable, [first, second][position], revision=revision)
    assert texts(durable) == (["one"] if position else []) + ["replacement"]
    assert all(entry in durable.store.entries("chat") for entry in old_entries)
    assert durable.task("edit").state["input_entry_id"] == durable.store.head("chat")[0]
    expected_runs = {"second"} if position else {"first", "second"}
    assert set(result.record.payload["replaced_run_ids"]) == expected_runs
    assert set(durable.history_page("chat")["inactive_run_ids"]) == expected_runs
    sequence = durable.commit_sequence()
    assert edit(durable, [first, second][position], revision=revision) == result
    assert durable.commit_sequence() == sequence
    assert len([t for t in durable.tasks() if t.task_id == "edit"]) == 1


def test_edit_conflicts_preserve_branch_and_never_admit_a_task(durable):
    source = turn(durable, "first", "one")
    revision = durable.store.head("chat")[1]
    turn(durable, "second", "two")
    before = durable.store.head("chat")
    with pytest.raises(CommitConflict):
        edit(durable, source, revision=revision)
    assert durable.store.head("chat") == before
    with pytest.raises(KeyError):
        durable.task("edit")
    durable.admit_input(conversation_id="chat", request_id="busy", task_id="busy", text="running", provider_snapshot={})
    with pytest.raises(CommitConflict, match="Stop"):
        edit(durable, source)


def test_inactive_source_and_non_user_cannot_be_edited(durable):
    source = turn(durable, "first", "one")
    answer = durable.store.head("chat")[0]
    with pytest.raises(CommitConflict):
        edit(durable, answer)
    edit(durable, source)
    durable.cancel_task("edit")
    with pytest.raises(CommitConflict):
        edit(durable, source, request="another")


def test_edit_rewinds_inherited_fork_without_changing_parent(durable):
    first = turn(durable, "first", "one")
    second = turn(durable, "second", "two")
    durable.fork("chat", durable.store.head("chat")[0], child_conversation_id="child")
    turn(durable, "child-turn", "three", "child")
    edit(durable, second, conversation="child")
    assert texts(durable, "child") == ["one", "replacement"]
    assert texts(durable) == ["one", "two"]
    durable.cancel_task("edit")
    edit(durable, first, conversation="child", request="root-edit")
    assert texts(durable, "child") == ["replacement"]


def test_late_old_task_cannot_write_into_new_branch(durable):
    source = turn(durable, "first", "one")
    edit(durable, source)
    head, revision = durable.store.head("chat")
    late = EntryDraft(entry_id="late", conversation_id="chat", parent_id=head, kind="display_message",
                      by_task_id="first", payload={"text": "old work"})
    with pytest.raises(CommitConflict, match="outside"):
        durable.store.commit(DurableMutation("chat", revision, entries=(late,)))
    assert durable.store.head("chat") == (head, revision)
    durable.record_display_message("chat", run_id="first", identity="late", payload={"text": "late work"})
    assert durable.store.head("chat") == (head, revision)


def test_failed_edit_never_repoints_head_or_writes_replacement_input(durable):
    source = turn(durable, "first", "one")
    turn(durable, "collision", "another conversation", "other")
    before = durable.store.head("chat"), durable.store.entries("chat"), durable.commit_sequence()
    with pytest.raises(CommitConflict):
        edit(durable, source, request="collision")
    assert (durable.store.head("chat"), durable.store.entries("chat"), durable.commit_sequence()) == before


def test_reusing_edit_request_for_other_content_is_rejected(durable):
    source = turn(durable, "first", "one")
    revision = durable.store.head("chat")[1]
    edit(durable, source, revision=revision)
    with pytest.raises(IdempotencyConflict):
        edit(durable, source, text="different", revision=revision)


def test_sqlite_reopen_restores_only_replacement_branch(tmp_path):
    path = tmp_path / "restart.db"
    session = DurableSession(SQLiteDurableStore(path))
    source = turn(session, "first", "one")
    revision = session.store.head("chat")[1]
    result = edit(session, source, revision=revision)
    session.close()
    session = DurableSession(SQLiteDurableStore(path))
    assert texts(session) == ["replacement"]
    assert "answer:one" not in str(session.context_snapshot("chat", token_budget=10_000).messages)
    assert edit(session, source, revision=revision) == result
    assert session.task("edit").state["input"]["entry_id"] == source
    session.close()


def test_store_checks_idle_within_transaction_not_only_admission(durable):
    source = turn(durable, "first", "one")
    captured = []
    commit = durable._commit
    durable._commit = lambda mutation: captured.append(mutation)
    edit(durable, source)
    durable._commit = commit
    durable.admit_input(conversation_id="chat", request_id="queued", task_id="queued", text="queued", provider_snapshot={})
    pending = replace(captured[0], expected_revision=durable.store.head("chat")[1])
    with pytest.raises(CommitConflict, match="Stop"):
        durable.store.commit(pending)
