"""A returned terminal outcome must settle the canonical task, too."""

import pytest

from codey.durable import DurableSession, MemoryDurableStore
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.write.task_runtime import TaskRuntime
from codey.task.model import TaskSubmission


@pytest.mark.parametrize("outcome,expected", [
    (OperationOutcome.failed(reason="provider_connection_failed", summary="connection unavailable"), "failed"),
    (OperationOutcome.completed(summary="done"), "completed"),
])
def test_executor_terminal_settles_task(outcome, expected):
    session = DurableSession(MemoryDurableStore())
    session.admit_input(conversation_id="c", request_id="input", task_id="t", text="work", provider_snapshot={})
    session.claim_task("t")
    session.start_task("t", state=dict(session.task("t").state))
    runtime = TaskRuntime(lambda _request: outcome, durable=session)
    runtime.run(TaskSubmission(session_id="c", project=None, task="work", max_turns=1,
                               continue_task=False, provider_id="deepseek", run_id="t"))
    assert str(session.task("t").status) == expected


def test_selection_failure_before_execution_also_settles_the_admitted_task():
    session = DurableSession(MemoryDurableStore())
    session.admit_input(conversation_id="c", request_id="r", task_id="t", text="work", provider_snapshot={})
    session.claim_task("t")
    session.start_task("t", state=dict(session.task("t").state))
    def prepare(_request):
        raise ValueError("connection endpoint changed")
    runtime = TaskRuntime(lambda _: None, prepare=prepare, durable=session)
    with pytest.raises(ValueError, match="changed"):
        runtime.run(TaskSubmission(session_id="c", project=None, task="work", max_turns=1,
                                   continue_task=False, provider_id="zen", run_id="t"))
    assert str(session.task("t").status) == "failed"
