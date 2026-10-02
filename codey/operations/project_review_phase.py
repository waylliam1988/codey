"""Review phase for project completion."""

from __future__ import annotations

import contextlib
from functools import partial
from typing import Any

from codey.completion.edit_scope import changed_paths_from_changes
from codey.completion.verification_policy import (
    selected_verification_candidate_lines,
    verification_candidate_lines,
)
from codey.operations.project_completion_context import (
    ProjectRun,
    record_review_input_prepared_trace,
    safe_verification_map,
)
from codey.operations.task_context import (
    safe_project_map,
    safe_verification_candidates,
)
from codey.reviews.coordinator import ReviewCoordinator, change_state
from codey.reviews.impact_map import safe_review_impact_map
from codey.runtime.core import cancellation


def _render_review_change_brief(ctx: ProjectRun) -> str:
    return (
        ctx.change_brief.render(audience="reviewer")
        if ctx.change_brief is not None
        else ""
    )


def _refresh_review_project_map(ctx: ProjectRun) -> str:
    ctx.verified_facts = (
        ctx.deps.persistence.project_facts.render(ctx.project)
        if ctx.deps.persistence.project_facts is not None
        else ""
    )
    ctx.verification_candidates = safe_verification_candidates(
        ctx.project,
        ctx.verification_verified_commands,
        ctx.resumed_verification_commands,
        ctx.configured_verification_commands,
        ctx.configured_ignored_paths,
    )
    ctx.project_map = safe_project_map(
        ctx.project,
        ctx.verified_facts,
        ctx.request.task,
        verification_candidate_lines(ctx.verification_candidates),
        ignored_paths=ctx.configured_ignored_paths,
        max_chars=ctx.project_map_chars,
    )
    return ctx.project_map


def _close_writer_for_review(ctx: ProjectRun) -> None:
    if ctx.frame.provider is not None:
        with contextlib.suppress(Exception):
            ctx.frame.provider.close()
    ctx.frame.provider = None
    assert ctx.failover is not None
    ctx.failover.provider = None


def _repair_writer(
    ctx: ProjectRun,
    followup: str,
    checkpoint: Any,
) -> Any:
    assert ctx.failover is not None
    try:
        result = ctx.failover.run(
            task=followup,
            turn_budget=min(ctx.request.max_turns, ctx.deps.review.review_fix_turns),
            fresh=False,
            handoff="",
            checkpoint=checkpoint,
        )
        return result
    finally:
        # Inline sync to avoid importing the writer phase (acyclic split).
        assert ctx.failover is not None
        ctx.frame.provider = ctx.failover.provider
        ctx.frame.provider_id = ctx.failover.provider_id
        ctx.frame.preflight_switches = ctx.failover.switches


def _set_checkpoint_status(ctx: ProjectRun, status: str) -> None:
    ctx.hooks.update_checkpoint(lambda store, item: store.set_status(item, status))


def _emit_review_unavailable(ctx: ProjectRun) -> None:
    ctx.task_state.emit(
        {
            "type": "review",
            "session_id": ctx.request.session_id,
            "text": "Unavailable. Continued with one model.",
        }
    )


def _run_review_with_trace(ctx: ProjectRun, **kwargs):
    changes_value = kwargs.get("changes")
    changes = changes_value if isinstance(changes_value, dict) else {}
    try:
        review_impact_map = safe_review_impact_map(
            kwargs.get("project") or ctx.project,
            changes,
        )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        review_impact_map = ""
    record_review_input_prepared_trace(
        ctx.frame.trace,
        task=str(kwargs.get("task") or ""),
        writer_summary=str(kwargs.get("writer_summary") or ""),
        changes=changes,
        recent_log=str(kwargs.get("recent_log") or ""),
        change_brief=str(kwargs.get("change_brief") or ""),
        project_map=str(kwargs.get("project_map") or ""),
        verification_map=str(kwargs.get("verification_map") or ""),
        review_impact_map=review_impact_map,
        execution_evidence=str(kwargs.get("execution_evidence") or ""),
    )
    kwargs["review_impact_map"] = review_impact_map
    kwargs["trace_recorder"] = ctx.frame.trace
    return ctx.deps.review.run(**kwargs)


def _build_review_verification_map(
    ctx: ProjectRun,
    changes: dict,
    current_project_map: str,
) -> str:
    return safe_verification_map(
        ctx.project,
        changes,
        ctx.work.evidence.successful_checks,
        current_project_map,
        selected_verification_candidate_lines(
            ctx.verification_candidates,
            changed_paths_from_changes(changes),
        ),
    )


def _review_cycle_phase(ctx: ProjectRun) -> None:
    assert ctx.result is not None
    review_coordinator = ReviewCoordinator(ctx.deps.verification.collect_changes)
    ctx.review_cycle = review_coordinator.run_cycle(
        project=ctx.project,
        tracker=ctx.tracker,
        session_id=ctx.request.session_id,
        task=ctx.request.task,
        result=ctx.result,
        task_changed=ctx.task_changed,
        changes=ctx.task_changes,
        changes_dirty=ctx.task_changes_dirty,
        writer_id=ctx.frame.provider_id,
        recent_log="\n".join(ctx.work.recent_events[-ctx.deps.review.review_log_lines :]),
        render_change_brief=partial(_render_review_change_brief, ctx),
        execution_evidence=ctx.work.evidence.render_for_review(),
        successful_checks=ctx.work.evidence.successful_checks,
        checkpoint_prompt=ctx.checkpoint_prompt,
        checks_before_review_followup=(
            ctx.work.evidence.has_successful_checks
            or (not ctx.work.evidence.observed_tool_events and ctx.result.checks_passed)
        ),
        stop_requested=ctx.task_state.run_registry.stop_flag.is_set,
        refresh_project_map=partial(_refresh_review_project_map, ctx),
        build_verification_map=partial(_build_review_verification_map, ctx),
        run_review=partial(_run_review_with_trace, ctx),
        close_writer_for_review=partial(_close_writer_for_review, ctx),
        repair_writer=partial(_repair_writer, ctx),
        set_checkpoint_status=partial(_set_checkpoint_status, ctx),
        emit_review_unavailable=partial(_emit_review_unavailable, ctx),
    )
    ctx.result = ctx.review_cycle.result
    ctx.task_changed = ctx.review_cycle.task_changed
    ctx.task_changes = ctx.review_cycle.changes
    ctx.task_changes_dirty = ctx.review_cycle.changes_dirty
    if ctx.result.facts is not None:
        ctx.task_session = ctx.result.facts
    if ctx.task_changes is None or ctx.task_changes_dirty:
        ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
    collected_changed = change_state(ctx.task_changes)
    if collected_changed is not None:
        ctx.task_changed = collected_changed


def run_review_phase(ctx: ProjectRun) -> None:
    """Public review phase entry (renamed from _review_cycle_phase)."""
    _review_cycle_phase(ctx)


__all__ = [
    "run_review_phase",
]
