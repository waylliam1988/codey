"""One application service composition for desktop and headless task entries."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from codey.app.task_state import TaskSubmissionState

if TYPE_CHECKING:
    from codey.agents.request import AgentRequest
    from codey.operations.task_run import CaptureProviderFailure, CollectChanges, TaskRunDeps
    from codey.providers.base import ChatProvider
    from codey.runtime.core.run_result import RunResult


def build_task_deps(
    state: TaskSubmissionState,
    *,
    agent_run: Callable[[AgentRequest], RunResult] | None = None,
    collect_changes: CollectChanges | None = None,
    collect_review_changes: CollectChanges | None = None,
    capture_provider_failure: CaptureProviderFailure | None = None,
    review_policy: str | None = None,
    connect_reviewer: Callable[[str], ChatProvider] | None = None,
    connect_provider: Callable[[str], ChatProvider] | None = None,
) -> TaskRunDeps:
    # Resolve services only when a task starts; importing the app must not
    # initialize browser connections or load the research runtime.
    from codey.app import consensus_service, review_service, sibling_probe
    from codey.app.context import REVIEW_FIX_TURNS, REVIEW_LOG_LINES
    from codey.operations import project_adapter
    from codey.operations.task_run import TaskRunDeps
    from codey.providers import diagnostics
    from codey.reviews.review_policy import allow_self_review, load_review_policy
    from codey.workspace import changes, task_changes

    policy = load_review_policy() if review_policy is None else review_policy
    project_review = not allow_self_review(policy) or connect_reviewer is not None
    default_failure_capture: CaptureProviderFailure = diagnostics.capture_provider_failure
    return TaskRunDeps.from_submission_stores(
        state=state,
        stores=state.task_submission_stores,
        agent_run=project_adapter.run if agent_run is None else agent_run,
        collect_changes=task_changes.collect_task_changes if collect_changes is None else collect_changes,
        collect_review_changes=(changes.collect_changes if collect_review_changes is None
                                else collect_review_changes),
        capture_provider_failure=default_failure_capture if capture_provider_failure is None else capture_provider_failure,
        run_review=partial(review_service.run_review, state, review_policy=policy, connect_reviewer=connect_reviewer),
        run_consensus=partial(consensus_service.run_consensus, state),
        project_review=project_review,
        is_git_repository=changes.is_git_repository,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
        connect_provider=connect_provider,
        bind_provider_handlers=partial(sibling_probe.bind_provider_handlers, state),
    )
