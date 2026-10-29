"""Recovery must not convert unprojected Research into a coding workflow."""
from types import SimpleNamespace

from codey.operations.context import RunWork
from codey.operations.task_run import _build_workload
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.task.model import TaskSubmission


def test_recovered_research_keeps_original_kind(monkeypatch):
    work = RunWork([], evidence=ExecutionEvidence(), operation=SimpleNamespace(task_policy={"grants": ["web.read"]}))
    monkeypatch.setattr("codey.operations.task_run.build_run_work", lambda *a, **k: (work, None))
    setup = SimpleNamespace(session_id="s", run_id="r", project=None, provider_id="web", max_turns=8,
                            baseline_task_kind="research", task_kind="research", trace=None,
                            state=SimpleNamespace(durable=None),
                            project_config_result=SimpleNamespace(config=SimpleNamespace(ignored_paths=())),
                            request=TaskSubmission("s", None, "research", 8, False, "web"), continue_task=False)
    workload, early = _build_workload(SimpleNamespace(), setup)
    assert early is None
    assert setup.task_kind == "research"
    assert setup.continue_task is True
    assert workload.work is work


def test_entry_has_no_parallel_recovery_dto():
    from dataclasses import fields

    from codey.agents.request import AgentRequest
    from codey.operations.context import RunFrame
    for contract in (AgentRequest, RunFrame):
        names = {field.name for field in fields(contract)}
        assert not names & {"recovered_tool_outcomes", "settled_tool_outcomes", "recovered_tool_result_batch_id"}
