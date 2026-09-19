"""Ghost work-claim, auto-route, and task-policy helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from codey.ghost.work_queue import GhostWorkItem
from codey.operations.ghost_post_turn import (
    GhostTaskPolicyDeps,
    maybe_claim_work_item,
    maybe_route_auto,
)
from codey.operations.project_completion_flow import record_completion_proof_trace
from codey.operations.review_flow import ReviewFlowDeps, has_reviewable_diff
from codey.task.model import TaskSubmission


@dataclass(frozen=True)
class _ClaimRoute:
    request: TaskSubmission
    task_kind: str
    claimed_work_item: GhostWorkItem | None
    route_result: Any | None


def claim_or_route_ghost_work(
    ghost_deps: GhostTaskPolicyDeps,
    request: TaskSubmission,
    *,
    baseline_task_kind: str,
    run_id: str,
) -> _ClaimRoute:
    """Claim a ghost work item or auto-route; raises TaskCancelled to the caller."""
    claimed_work_item: GhostWorkItem | None = None
    task_kind = baseline_task_kind
    route_result = None
    claim_result = maybe_claim_work_item(ghost_deps, request, run_id=run_id)
    if claim_result is not None:
        claimed_work_item = claim_result.item
        task_kind = claim_result.mode or task_kind
        request = replace(
            request,
            task=claim_result.task or request.task,
            continue_task=True,
        )
    else:
        route_result = maybe_route_auto(
            ghost_deps,
            request,
            baseline_mode=baseline_task_kind,
            run_id=run_id,
        )
        if route_result is not None:
            task_kind = route_result.final_mode
    return _ClaimRoute(
        request=request,
        task_kind=task_kind,
        claimed_work_item=claimed_work_item,
        route_result=route_result,
    )


def ghost_task_deps(deps: Any, review_deps: ReviewFlowDeps) -> GhostTaskPolicyDeps:
    return GhostTaskPolicyDeps(
        state=deps.state,
        run_ledgers=deps.run_ledgers,
        evidence_ledgers=deps.evidence_ledgers,
        work_checkpoints=deps.work_checkpoints,
        knowledge_store=deps.knowledge_store,
        router_provider_factory=deps.ghost_router_provider_factory,
        learning_provider_factory=deps.ghost_learning_provider_factory,
        learning_modes=tuple(str(item or "").strip() for item in deps.ghost_learning_modes),
        has_reviewable_diff=lambda project: has_reviewable_diff(review_deps, project),
        record_completion_proof_trace=record_completion_proof_trace,
    )
