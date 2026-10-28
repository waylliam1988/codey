from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import (
    ApprovalRecord,
    ApprovalStatus,
    DurableSession,
    MemoryDurableStore,
    SQLiteDurableStore,
    TaskStatus,
)
from codey.durable.model import EffectRecord


def _waiting_approval(path: Path, backend: str, *, approved: bool, park: bool) -> DurableSession:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(path)
    session = DurableSession(store)
    session.admit_input(conversation_id="c", request_id="r", task_id="t", text="Inspect the command result.",
                        provider_snapshot={"provider_id": "original"}, task_state={"task_policy": {"shell": "ask"}})
    session.claim_task("t")
    session.start_task("t", state={})
    session.advance_task(task_id="t", phase="running", state={})
    session.append_message("c", "tool_call", {"tool_calls": [
        {"id": "call", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]})
    request = ApprovalRecord("a", "t", "c", ApprovalStatus.REQUESTED, "command-digest", "command-ref", ".")
    session.create_approval(request)
    session.transition_approval("a", replace(request, status=ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED,
                                            revision=1), expected_status=ApprovalStatus.REQUESTED)
    if park:
        session.wait_task("t", state={"stop_reason": "approval"})
    if approved:
        session.claim_approval_and_begin_effect("a", EffectRecord(
            "shell-a", "t", "shell", "unsafe", "command-digest", "pending", {"approval_id": "a"}))
    return session


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("park_first", [False, True])
def test_approval_result_wakes_original_task_in_the_settlement_commit(
    tmp_path: Path, backend: str, approved: bool, park_first: bool,
) -> None:
    session = _waiting_approval(tmp_path / "durable.sqlite3", backend, approved=approved, park=park_first)
    try:
        original = session.task("t")
        result = {"result_ref": "result", "result_digest": "digest", "status": "exit" if approved else "denied"}
        before = session.commit_sequence()
        committed = session.settle_approval("a", settlement=result, resume=True,
            tool_results=({"call_id": "call", "content": "checked result", "is_error": not approved},))
        assert committed is not None
        assert committed.sequence == before + 1
        task = session.task("t")
        assert task.provider_snapshot == original.provider_snapshot
        assert task.submission_id == original.submission_id
        assert task.state["task_policy"] == original.state["task_policy"]
        assert task.state["resume_input"] == {"kind": "resolve_shell_approval", "approval_id": "a"}
        assert session.approval("a").continuation_run_id == "t"
        assert session.entries("c")[-1].kind == "tool_result"
        assert session.entries("c")[-1].commit_seq == committed.sequence
        assert len([entry for entry in session.entries("c") if entry.kind == "user_message"]) == 1
        assert len(session.tasks()) == 1
        if approved:
            assert session.effect("shell-a").settlement == result
        assert not task.attempt_token
        assert session.task("t").status == TaskStatus.PENDING
        assert not session.task("t").attempt_token
        count = len(session.entries("c"))
        assert session.settle_approval("a", settlement=result, resume=True) is None
        assert len(session.entries("c")) == count
    finally:
        session.close()


def test_settlement_failure_rolls_back_receipt_tool_result_and_wakeup(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "durable.sqlite3"
    session = _waiting_approval(path, "sqlite", approved=True, park=True)
    before = session.task("t")
    sequence = session.commit_sequence()
    published = []
    session._on_commit = lambda *args: published.append(args)

    def fail(_mutation):
        raise OSError("injected commit failure")

    monkeypatch.setattr(session.store, "commit", fail)
    with pytest.raises(OSError, match="injected"):
        session.settle_approval("a", settlement={"result_ref": "r", "result_digest": "d"}, resume=True,
                               tool_results=({"call_id": "call", "content": "ok"},))
    assert published == []
    session.close()
    reopened = DurableSession(SQLiteDurableStore(path))
    try:
        assert reopened.task("t") == before
        assert reopened.commit_sequence() == sequence
        assert reopened.approval("a").status == ApprovalStatus.EXECUTION_CLAIMED
        assert reopened.effect("shell-a").settlement is None
        assert reopened.entries("c")[-1].kind == "tool_call"
    finally:
        reopened.close()
