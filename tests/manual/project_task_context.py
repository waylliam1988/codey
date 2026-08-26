"""Production task context helpers for manual probes."""

from __future__ import annotations

from pathlib import Path

from codey.workspace.task_context import ProjectTaskContext, ProjectTaskContextBuilder
from codey.completion.verification_policy import verification_candidate_lines


def build_production_task_context(
    project: str | Path,
    *,
    task: str = "",
) -> ProjectTaskContext:
    return ProjectTaskContextBuilder().build(
        project=project,
        task=task,
        session_id="manual-probe",
        run_id="manual-probe",
        continue_task=False,
        provider_session_changed=False,
    )


def render_production_project_map(project: str | Path, *, task: str = "") -> str:
    return build_production_task_context(project, task=task).project_map


def production_candidate_command_lines(
    project: str | Path,
    *,
    task: str = "",
) -> tuple[str, ...]:
    context = build_production_task_context(project, task=task)
    return verification_candidate_lines(context.verification_candidates)
