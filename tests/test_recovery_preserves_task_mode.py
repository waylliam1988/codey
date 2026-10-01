"""Recovery must not convert unprojected Research into a coding workflow."""
from types import SimpleNamespace

from codey.operations.context import RunWork
from codey.operations.recovery import ResumeRecoveryResult
from codey.operations.task_run import _build_workload
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.task.model import TaskSubmission


def test_recovered_research_keeps_original_kind(monkeypatch):
    work = RunWork([], evidence=ExecutionEvidence())
    monkeypatch.setattr("codey.operations.task_run.build_run_work", lambda *a, **k: (work, None))
    monkeypatch.setattr("codey.operations.task_run.recover_effects_for_resume", lambda *a, **k: ResumeRecoveryResult(
        ok=True, settled_tool_outcomes=("original fact",),
    ))
    setup = SimpleNamespace(session_id="s", run_id="r", project=None, provider_id="web", max_turns=8,
                            baseline_task_kind="research", task_kind="research", trace=None,
                            project_config_result=SimpleNamespace(config=SimpleNamespace(ignored_paths=())),
                            request=TaskSubmission("s", None, "research", 8, False, "web"), continue_task=False)
    workload, early = _build_workload(SimpleNamespace(), setup)
    assert early is None
    assert setup.task_kind == "research"
    assert setup.continue_task is True
    assert workload.settled_tool_outcomes == ("original fact",)


def test_nonproject_recovery_dispatches_to_common_entry(monkeypatch):
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    from codey.policies.task_policy import TaskPolicy

    seen = []
    policy = TaskPolicy(grants=frozenset({"control", "web.read"}), strict_research=True)
    frame = SimpleNamespace(request=None, entry_policy=policy, recovered_tool_outcomes=(),
                            settled_tool_outcomes=("original fact",))
    def entry(active_frame, active_work, active_hooks, deps, **kwargs):
        assert active_frame is frame
        assert active_frame.entry_policy is policy
        assert kwargs["task_kind"] == "research"
        seen.append(active_frame)
        return ModeOutcome({"stop_reason": "blocked"})
    monkeypatch.setattr("codey.operations.task_phases.dispatch.run_entry_kernel", entry)
    result = dispatch_run_mode(SimpleNamespace(), None, None, RunWork([], ExecutionEvidence()),
                               frame, SimpleNamespace(), "research", None)
    assert len(seen) == 1
    assert result.event["stop_reason"] == "blocked"
