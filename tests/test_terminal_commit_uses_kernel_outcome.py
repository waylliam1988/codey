"""Rejected completion stays rejected in the original durable checkpoint."""

from types import SimpleNamespace

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore, SubmissionStatus
from codey.operations.result import ModeOutcome
from codey.operations.task_phases.settlement import _commit_durable_terminal
from codey.runtime.observe.terminalizer import operation_outcome_from_task_done_event


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("reason", ["max_turns", "protocol", "no_progress", "blocked", "done"])
def test_terminal_checkpoint_and_submission_agree_with_kernel_outcome(tmp_path, backend, reason):
    path = tmp_path / "durable.sqlite3"
    durable = DurableSession(MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(path))
    try:
        durable.admit_input(conversation_id="s", request_id="q", task_id="r", text="inspect", provider_snapshot={})
        durable.start_task("r", state={})
        event = {"run_id": "r", "stop_reason": reason, "summary": "candidate", "turns": 2}
        _commit_durable_terminal(SimpleNamespace(state=SimpleNamespace(durable=durable)),
                                 SimpleNamespace(run_id="r"), event, ModeOutcome(event))
        expected = operation_outcome_from_task_done_event(event).status
        task = durable.task("r")
        assert task.phase == expected == event["task_status"]
        assert task.state["stop_reason"] == reason
        submission = durable.store.submission(task.submission_id)
        assert submission.status is (SubmissionStatus.DONE if reason == "done" else SubmissionStatus.UNANSWERED)
        revision = task.revision
    finally:
        durable.close()
    if backend == "sqlite":
        reopened = DurableSession(SQLiteDurableStore(path))
        try:
            task = reopened.task("r")
            assert task.phase == expected and task.revision == revision
        finally:
            reopened.close()
