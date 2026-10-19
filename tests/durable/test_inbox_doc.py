from __future__ import annotations

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore, SubmissionPlacement


def test_inbox_doc_snapshot_and_history_are_reconstructible_from_durable_commits() -> None:
    session = DurableSession(MemoryDurableStore())
    try:
        session.admit_input(
            conversation_id="chat", request_id="active", task_id="active",
            text="current", provider_snapshot={},
        )
        session.start_task("active", state={})
        queued = session.admit_input(
            conversation_id="chat", request_id="steer", task_id="steer",
            text="change", provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
            mode="steer",
        )

        doc = session.inbox_doc("chat")
        assert doc.conversation_id == "chat"
        assert [item.request_id for item in doc.items] == ["steer"]
        assert doc.snapshot["schema_version"] == 1
        assert doc.snapshot["items"][0]["submission_id"] == queued.submission_id

        session.commit_generation_boundary("chat", task_id="active", at="post_tools")
        history = session.inbox_history("chat")
        assert [event.request_id for event in history] == ["active", "active", "steer", "steer"]
        assert history[-1].placement is SubmissionPlacement.PLACED
    finally:
        session.close()


def test_inbox_doc_survives_sqlite_restart(tmp_path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    session.admit_input(
        conversation_id="chat", request_id="queued", task_id="queued", text="later",
        provider_snapshot={}, placement=SubmissionPlacement.QUEUED,
    )
    session.close()

    reopened = DurableSession(SQLiteDurableStore(path))
    try:
        doc = reopened.inbox_doc("chat")
        assert [item.request_id for item in doc.items] == ["queued"]
        assert reopened.inbox_history("chat")[0].request_id == "queued"
    finally:
        reopened.close()
