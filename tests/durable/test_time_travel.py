from __future__ import annotations

from pathlib import Path

from codey.durable import DurableSession, SQLiteDurableStore


def test_context_at_rebuilds_an_older_committed_conversation_without_mutation(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        first = session.admit_input(
            conversation_id="chat", request_id="one", task_id="task-one", text="one", provider_snapshot={}
        )
        second = session.append_message("chat", "assistant_message", {"text": "two"})
        assert [entry.payload["text"] for entry in session.context_at("chat", first.sequence)] == ["one"]
        assert [entry.payload["text"] for entry in session.context_at("chat", second.commit_seq)] == ["one", "two"]
        assert [entry.payload["text"] for entry in session.context("chat")] == ["one", "two"]
    finally:
        session.close()


def test_context_snapshot_at_has_stable_digest_for_the_same_historical_cut(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        first = session.admit_input(
            conversation_id="chat", request_id="one", task_id="task-one", text="历史", provider_snapshot={}
        )
        session.append_message("chat", "assistant_message", {"text": "later"})
        a = session.context_snapshot_at("chat", first.sequence, token_budget=100)
        b = session.context_snapshot_at("chat", first.sequence, token_budget=100)
        assert a == b
        assert a.digest
        assert a.messages == ({"role": "user", "content": "历史"},)
    finally:
        session.close()

