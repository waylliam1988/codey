"""Review phase for project completion."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from functools import partial
from typing import Any

from codey.completion.edit_scope import changed_paths_from_changes
from codey.completion.verification_policy import (
    selected_verification_candidate_lines,
    verification_candidate_lines,
)
from codey.operations.project_completion_context import (
    ProjectRun,
    commit_runtime_operation,
    record_review_input_prepared_trace,
    safe_verification_map,
)
from codey.operations.task_context import (
    safe_project_map,
    safe_verification_candidates,
)
from codey.reviews.coordinator import ReviewCoordinator, change_state
from codey.reviews.impact_map import safe_review_impact_map
from codey.reviews.persistence import append_review_result_ledger
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
    runner = ctx.failover
    assert runner is not None
    runner.provider = None


def _repair_writer(
    ctx: ProjectRun,
    followup: str,
    checkpoint: Any,
) -> Any:
    runner = ctx.failover
    assert runner is not None
    assert ctx.result is not None
    previous_turns = ctx.result.turns
    remaining = ctx.request.max_turns - previous_turns
    if remaining <= 0:
        return replace(ctx.result, stop_reason='max_turns', checks_passed=False)
    commit_runtime_operation(
        ctx, "mark_writer_running",
        lambda mutations, session_id, run_id: mutations.mark_writer_running(
            session_id, run_id, provider_id=runner.provider_id,
            writer_attempt=ctx.writer_attempt_index + 1,
        ),
    )
    try:
        result = runner.run(
            task=followup + _behavioral_facts(ctx),
            turn_budget=min(remaining, ctx.deps.review.review_fix_turns),
            fresh=False,
            handoff="",
            checkpoint=checkpoint,
        )
        result = replace(result, turns=previous_turns + result.turns)
        if result.stop_reason not in {"approval", "stopped"}:
            commit_runtime_operation(
                ctx, "mark_writer_settled",
                lambda mutations, session_id, run_id: mutations.mark_writer_settled(
                    session_id, run_id, provider_id=runner.provider_id,
                    turns_used=result.turns, stop_reason=result.stop_reason,
                ),
            )
        return result
    finally:
        # Inline sync to avoid importing the writer phase (acyclic split).
        ctx.frame.provider = runner.provider
        ctx.frame.provider_id = runner.provider_id
        ctx.frame.preflight_switches = runner.switches


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


def _run_review_with_trace(ctx: ProjectRun, **kwargs: Any) -> Any:
    changes_value = kwargs.get("changes")
    changes = changes_value if isinstance(changes_value, dict) else dict[str, object]()
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
    try:
        run_id = str(getattr(ctx.frame, "run_id", "") or "")
    except Exception:
        run_id = ""
    if run_id and "run_id" not in kwargs:
        kwargs["run_id"] = run_id
    try:
        source = str(getattr(getattr(ctx, "request", None), "review_source_run_id", "") or "")
    except Exception:
        source = ""
    if source and "review_source_run_id" not in kwargs:
        kwargs["review_source_run_id"] = source
    reviewed = ctx.deps.review.run(**kwargs)
    with contextlib.suppress(Exception):
        _persist_project_review_ledger(ctx, reviewed)
    return reviewed


def _persist_project_review_ledger(ctx: ProjectRun, reviewed: object) -> None:
    if reviewed is None or not isinstance(reviewed, tuple) or len(reviewed) != 2:
        return
    try:
        hooks = ctx.hooks
    except Exception:
        return
    append_review_result_ledger(hooks.append_ledger, reviewed[1])


def _build_review_verification_map(
    ctx: ProjectRun,
    changes: dict[str, Any],
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


def run_review_phase(ctx: ProjectRun, *, allow_repair: bool = True) -> None:
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
        execution_evidence=ctx.work.evidence.render_for_review() + _behavioral_facts(ctx),
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
        allow_repair=allow_repair,
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


def _behavioral_facts(ctx: ProjectRun) -> str:
    from codey.operations.behavioral_verification import behavioral_review_facts
    return behavioral_review_facts(ctx)


def validate_candidate(ctx: ProjectRun, *, allow_review_repair: bool = True) -> None:
    """One bounded candidate validation entry before initial/final proof.

    Writer checks remain required by the existing completion gate. Every
    post-review edit invalidates observations and receives a new review.
    """
    from codey.operations.behavioral_verification import refresh_behavioral_observation
    from codey.operations.project_candidate_validation import (
        submit_readonly_completion_candidate,
        validate_stopped_candidate,
    )

    validate_stopped_candidate(ctx)
    submit_readonly_completion_candidate(ctx)
    refresh_behavioral_observation(ctx)
    run_review_phase(ctx, allow_repair=allow_review_repair)
    repaired_after_review = ctx.review_cycle.review_repair_attempted
    review_deferred = False
    if repaired_after_review and ctx.result is not None and ctx.result.stop_reason == 'done':
        refresh_behavioral_observation(ctx)
        from codey.completion.behavioral_checks import behavioral_completion_check
        from codey.workspace.revision import workspace_fingerprint

        check = behavioral_completion_check(ctx.behavioral_plan, ctx.behavioral_observation,
                                           ctx.request.task, workspace_fingerprint(ctx.project))
        review_deferred = check is not None and check.status == 'fail'
        # A fresh observed failure belongs to the existing completion repair.
        # Reviewing this known failing candidate cannot supply missing proof.
        if not review_deferred:
            run_review_phase(ctx, allow_repair=False)
    if (ctx.result is not None and ctx.result.stop_reason == 'done' and ctx.task_changed
            and ((ctx.work.operation is not None and ctx.work.operation.candidate_validation_attempted)
                 or ctx.repaired_once or repaired_after_review)
            and not review_deferred and not ctx.review_cycle.approved_current_snapshot):
        ctx.blocked_reason = 'current_candidate_review_unavailable_or_not_approved'
        ctx.result = replace(ctx.result, stop_reason='blocked', checks_passed=False,
                             summary='Current candidate requires a fresh approved review.')


__all__ = [
    "run_review_phase",
]
