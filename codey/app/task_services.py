"""One application service composition for desktop and headless task entries."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from codey.operations.task_state import TaskSubmissionState

if TYPE_CHECKING:
    from codey.operations.task_run import TaskRunDeps


def build_task_deps(
    state: TaskSubmissionState,
    *,
    agent_run: Callable[..., Any] | None = None,
    collect_changes: Callable[..., Any] | None = None,
    capture_provider_failure: Callable[..., Any] | None = None,
    review_policy: str | None = None,
    connect_reviewer: Callable[..., Any] | None = None,
) -> TaskRunDeps:
    # Resolve services only when a task starts; importing the app must not
    # initialize browser connections or load the research runtime.
    from codey.app import consensus_service, review_service
    from codey.app.context import REVIEW_FIX_TURNS, REVIEW_LOG_LINES
    from codey.operations import project_adapter
    from codey.operations.task_run import TaskRunDeps
    from codey.providers import diagnostics
    from codey.reviews.review_policy import allow_self_review, load_review_policy
    from codey.workspace import changes

    policy = load_review_policy() if review_policy is None else review_policy
    allow_self_review(policy)  # Validate even when no review is eventually needed.
    return TaskRunDeps.from_submission_stores(
        state=state,
        stores=state.task_submission_stores,
        agent_run=project_adapter.run if agent_run is None else agent_run,
        collect_changes=changes.collect_changes if collect_changes is None else collect_changes,
        capture_provider_failure=diagnostics.capture_provider_failure if capture_provider_failure is None else capture_provider_failure,
        run_review=partial(review_service.run_review, state, review_policy=policy, connect_reviewer=connect_reviewer),
        run_consensus=partial(consensus_service.run_consensus, state),
        run_project_audit=partial(consensus_service.run_project_audit, state),
        run_research_advisors=partial(consensus_service.run_research_advisors, state),
        is_git_repository=changes.is_git_repository,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
    )
