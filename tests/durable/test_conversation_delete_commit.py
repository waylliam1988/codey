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


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("direct", [False, True])
def test_delete_purges_private_timeline_preserves_other_chat_and_commit_cursors(backend, direct, tmp_path):
    import sqlite3

    from codey.durable import ArtifactRecord, DurableMutation, EffectRecord

    path = tmp_path / "durable.sqlite3"
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(path)
    session = DurableSession(store)
    try:
        for scope in ("remove", "keep"):
            session.admit_input(conversation_id=scope, request_id=scope, task_id=scope,
                                text=f"private-input-{scope}", provider_snapshot={})
            session.start_task(scope, state={})
            effect = EffectRecord(f"effect-{scope}", scope, "tool_call", "safe", "digest", "pending",
                                  {"private": f"private-effect-{scope}"})
            session.record_effect_intent(scope, effect)
            _, revision = store.head(scope)
            store.commit(DurableMutation(scope, revision, artifacts=(ArtifactRecord(
                f"artifact-{scope}", "a" * 64, 1, "unused", scope, effect.effect_id, "now", {},
            ),)))
            session.complete_task(scope, "finished", state={})
        before = session.commit_sequence()
        public_before = session.commits_after(0)
        kept_timeline = session.task_timeline("keep")
        assert session.task_timeline("remove")

        (store if direct else session).delete_conversation("remove")

        assert not session.has_conversation("remove")
        assert session.task_timeline("remove") == ()
        assert session.task_timeline("keep") == kept_timeline
        with pytest.raises(KeyError):
            session.task_at("remove", before)
        with pytest.raises(KeyError):
            store.artifact("artifact-remove")
        assert store.artifact("artifact-keep").owner_task_id == "keep"
        assert session.commits_after(0)[:-1] == public_before
        assert session.commit_sequence() == before + 1
        assert session.commits_after(before)[0].payload["projections"][0]["type"] == "conversation_deleted"
        if backend == "sqlite":
            with sqlite3.connect(path) as database:
                payloads = "\n".join(row[0] for row in database.execute("SELECT payload FROM commits"))
                assert "private-input-remove" not in payloads
                assert "private-effect-remove" not in payloads
                assert "private-input-keep" in payloads
            session.close()
            session = DurableSession(SQLiteDurableStore(path))
            assert session.task_timeline("remove") == ()
            assert session.task_timeline("keep") == kept_timeline
            assert not session.has_conversation("remove")
    finally:
        session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_parent_delete_conflict_keeps_timeline_and_fork_can_continue(backend, tmp_path):
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    try:
        session.admit_input(conversation_id="parent", request_id="r", task_id="r",
                            text="inherited private input", provider_snapshot={})
        session.complete_task("r", "inherited answer", state={})
        session.fork("parent", store.head("parent")[0], child_conversation_id="child")
        timeline = session.task_timeline("r")
        before = session.commits_after(0)
        with pytest.raises(CommitConflict, match="forked children"):
            session.delete_conversation("parent")
        assert session.task_timeline("r") == timeline
        assert session.commits_after(0) == before
        session.admit_input(conversation_id="child", request_id="child-task", task_id="child-task",
                            text="continue here", provider_snapshot={})
        session.complete_task("child-task", "child answer", state={})
        assert [item["text"] for item in session.history_page("child")["items"]] == [
            "inherited private input", "inherited answer", "continue here", "child answer",
        ]
        session.delete_conversation("child")
        assert session.task_timeline("r") == timeline
        session.delete_conversation("parent")
        assert session.task_timeline("r") == ()
    finally:
        session.close()


def test_sqlite_failed_delete_rolls_back_private_history_purge_and_tombstone(tmp_path):
    import sqlite3

    path = tmp_path / "durable.sqlite3"
    store = SQLiteDurableStore(path)
    session = DurableSession(store)
    try:
        session.admit_input(conversation_id="chat", request_id="r", task_id="task",
                            text="must survive rollback", provider_snapshot={})
        timeline = session.task_timeline("task")
        commits = session.commits_after(0)
        with sqlite3.connect(path) as database:
            database.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON entries "
                             "BEGIN SELECT RAISE(ABORT, 'simulated delete failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="simulated delete failure"):
            store.delete_conversation("chat")
        assert session.has_conversation("chat")
        assert session.task_timeline("task") == timeline
        assert session.commits_after(0) == commits
    finally:
        session.close()
