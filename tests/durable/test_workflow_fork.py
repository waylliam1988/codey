from pathlib import Path

import pytest

from codey.durable import CommitConflict, DurableSession, EffectRecord, MemoryDurableStore, SQLiteDurableStore


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_workflow_fork_admits_a_new_task_at_the_historical_cap(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    try:
        first = session.admit_input(conversation_id="source", request_id="one", task_id="old", text="original",
            provider_snapshot={"model_id": "frozen"}, task_state={"task_policy": {"grants": ["read"]}})
        session.append_message("source", "assistant_message", {"text": "later path"})
        head = session.head("source")
        forked = session.fork_task("old", first.sequence, conversation_id="child", task_id="new",
                                  request_id="fork-one", instruction="Try another approach")
        retry = session.fork_task("old", first.sequence, conversation_id="child", task_id="retry",
                                 request_id="fork-one", instruction="Try another approach")
        assert retry == forked
        assert session.head("source") == head
        assert [entry.payload["text"] for entry in session.context("child")] == ["original", "Try another approach"]
        assert session.task("new").phase == "accepted"
        assert session.task("new").state["task_policy"] == session.task("old").state["task_policy"]
        assert session.task("new").provider_snapshot == session.task("old").provider_snapshot
        assert session.effects("new") == ()
        assert session.task("new").state["workflow_origin"] == {"task_id": "old", "commit_seq": first.sequence}
        assert session.entries("child")[0].commit_seq == forked.sequence
        assert session.task_at("new", forked.sequence).task == session.task("new")
        assert len(session.tasks()) == 2
    finally:
        session.close()


def test_workflow_fork_refuses_a_checkpoint_with_unsettled_external_work(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        session.admit_input(conversation_id="c", request_id="r", task_id="t", text="go", provider_snapshot={})
        session.start_task("t", state={})
        session.advance_task(task_id="t", phase="assistant_response_pending", state={})
        pending = session.record_effect_intent("t", EffectRecord("shell", "t", "shell", "unsafe", "digest", "pending", {}))
        with pytest.raises(CommitConflict, match="unsettled"):
            session.fork_task("t", pending.sequence, conversation_id="child", task_id="new", request_id="fork", instruction="retry")
        assert not session.has_conversation("child")
    finally:
        session.close()
