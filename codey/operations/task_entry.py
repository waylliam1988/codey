"""Single task entry: runtime submission + mode dispatch (one import point).

Runtime entry ``run_task_submission`` and mode entry ``run_task_mode`` converge
here so project/research/hybrid/readonly share one authorization + completion
path. ``unified_mode`` remains as the implementation module during migration;
new code should import the mode entry from here.
"""

from __future__ import annotations

from codey.operations.task_run import (
    TaskRunDeps,
    execute_task_run,
    prepare_submission,
    release_unstarted_submission,
)
from codey.operations.unified_mode import build_unified_policy, run_task_mode, run_unified_mode
from codey.runtime.write.task_runtime import TaskRuntime
from codey.task.model import TaskSubmission


def run_task_submission(deps: TaskRunDeps, request: TaskSubmission) -> None:
    runtime = TaskRuntime(
        deps.state.runtime_log,
        lambda submission: execute_task_run(deps, submission),
        prepare=lambda submission: prepare_submission(deps.state, submission),
        on_unstarted_failure=lambda submission: release_unstarted_submission(deps.state, submission),
    )
    runtime.run(request)


__all__ = [
    "TaskRunDeps",
    "build_unified_policy",
    "run_task_mode",
    "run_task_submission",
    "run_unified_mode",
]
