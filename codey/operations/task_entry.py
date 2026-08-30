"""Production task submission entrypoint."""

from __future__ import annotations

from codey.operations.task_run import (
    TaskRunDeps,
    execute_task_run,
    prepare_submission,
    release_unstarted_submission,
)
from codey.runtime.task_runtime import TaskRuntime
from codey.task.model import TaskSubmission


def run_task_submission(deps: TaskRunDeps, request: TaskSubmission) -> None:
    runtime_log = getattr(deps.state, "runtime_log", None)
    if runtime_log is None:
        raise RuntimeError("task entry requires RuntimeSessionLog")

    runtime = TaskRuntime(
        runtime_log,
        lambda submission: execute_task_run(deps, submission),
        prepare=lambda submission: prepare_submission(deps.state, submission),
        on_unstarted_failure=lambda submission: release_unstarted_submission(deps.state, submission),
    )
    runtime.run(request)


__all__ = ["TaskRunDeps", "run_task_submission"]
