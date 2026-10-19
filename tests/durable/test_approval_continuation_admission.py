from __future__ import annotations

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.errors import CommitConflict, IdempotencyConflict


def _session(tmp_path):
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="chat",
        request_id="source-request",
        task_id="source-task",
        text="run the checked command",
        provider_snapshot={"provider_id": "local"},
    )
    session.append_message(
        "chat",
        "tool_call",
        {
            "tool_calls": [
                {
                    "id": "shell-call-1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": "{}"},
                }
            ]
        },
    )
    return session


def test_approval_result_and_continuation_input_commit_as_one_protocol_pair(tmp_path):
    session = _session(tmp_path)
    try:
        result = {"call_id": "shell-call-1", "content": "checked output", "is_error": False}
        committed = session.admit_input(
            conversation_id="chat",
            request_id="continuation-request",
            task_id="continuation-task",
            text="Continue with the approved result.",
            provider_snapshot={"provider_id": "local"},
            tool_results=(result,),
        )

        entries = session.entries("chat")
        path = session.context("chat")
        snapshot = session.context_snapshot(
            "chat",
            token_budget=4096,
            before_entry_id=session.task("continuation-task").state["input_entry_id"],
        )

        assert [entry.kind for entry in entries[-2:]] == ["tool_result", "user_message"]
        assert entries[-2].parent_id == entries[-3].entry_id
        assert entries[-1].parent_id == entries[-2].entry_id
        assert [message.get("role") for message in snapshot.messages] == ["user", "assistant", "tool"]
        assert snapshot.messages[-1] == {
            "role": "tool",
            "content": "checked output",
            "tool_call_id": "shell-call-1",
        }
        assert path[-1].entry_id == entries[-1].entry_id

        retried = session.admit_input(
            conversation_id="chat",
            request_id="continuation-request",
            task_id="continuation-task",
            text="Continue with the approved result.",
            provider_snapshot={"provider_id": "local"},
            tool_results=(result,),
        )
        assert retried == committed
        assert session.entries("chat") == entries
    finally:
        session.close()


def test_approval_result_must_match_an_unanswered_tool_call_and_leave_no_partial_admission(tmp_path):
    session = _session(tmp_path)
    try:
        before = session.entries("chat")
        with pytest.raises(CommitConflict, match="unanswered tool call"):
            session.admit_input(
                conversation_id="chat",
                request_id="bad-continuation",
                task_id="bad-continuation-task",
                text="Continue.",
                provider_snapshot={"provider_id": "local"},
                tool_results=({"call_id": "foreign-call", "content": "output", "is_error": False},),
            )
        assert session.entries("chat") == before
        with pytest.raises(KeyError):
            session.task("bad-continuation-task")
    finally:
        session.close()


def test_idempotency_fingerprint_covers_the_approval_result(tmp_path):
    session = _session(tmp_path)
    try:
        session.admit_input(
            conversation_id="chat",
            request_id="continuation-request",
            task_id="continuation-task",
            text="Continue.",
            provider_snapshot={"provider_id": "local"},
            tool_results=({"call_id": "shell-call-1", "content": "first", "is_error": False},),
        )
        with pytest.raises(IdempotencyConflict):
            session.admit_input(
                conversation_id="chat",
                request_id="continuation-request",
                task_id="continuation-task",
                text="Continue.",
                provider_snapshot={"provider_id": "local"},
                tool_results=({"call_id": "shell-call-1", "content": "different", "is_error": False},),
            )
    finally:
        session.close()
