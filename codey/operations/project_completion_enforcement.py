"""Completion enforcement for project mode."""

from __future__ import annotations

from dataclasses import replace

from codey.agents.protocol import task_forbids_verification
from codey.completion.engine import CompletionEngine
from codey.completion.repair_context import (
    project_repair_context,
    repair_candidate,
)
from codey.completion.verification import (
    STANCE_FRESH_PASS,
    STANCE_INHERITED_PASS,
    decisive_failure_fact,
)
from codey.completion.verification_policy import select_verification_candidate
from codey.operations.project_completion_context import (
    COMPLETION_REPAIR_FOLLOWUP,
    MAX_COMPLETION_REPAIR_ROUNDS,
    ProjectRun,
    ProjectRuntimeMutationError,
    blocked_result,
    record_completion_proof_trace,
    record_edit_integrity_trace,
)
from codey.operations.project_completion_context import (
    commit_runtime_operation as _commit_runtime_operation,
)
from codey.operations.project_completion_context import (
    refresh_checkpoint_view as _refresh_checkpoint_view,
)
from codey.operations.task_context import safe_verification_candidates
from codey.providers.diagnostics import ProviderActionError
from codey.reviews.coordinator import change_state
from codey.runtime.core import cancellation
from codey.runtime.core.operation_state import (
    LEAF_COMPLETION_PROOF_RECORDED,
)
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.events import RunEvent


def _enforcement_scope(
    ctx: ProjectRun,
    changes: dict | None,
    changed: bool,
) -> tuple[bool, tuple[str, ...]]:
    files = tuple(
        str(item.get("path") or "")
        for item in ((changes or {}).get("files") or [])
        if item.get("path")
    )
    if not files and change_state(changes) is None and ctx.work.evidence.changed_files:
        return True, tuple(ctx.work.evidence.changed_files)
    return changed, files


def _completion_evidence(
    ctx: ProjectRun,
    *,
    changes: object,
    changed: bool,
    scope_files: tuple[str, ...],
    check: object,
    stop: str,
) -> object:
    assert ctx.completion_engine is not None
    evidence = ctx.completion_engine.evaluate(
        run_id=ctx.frame.run_id,
        task=ctx.request.task,
        changes=changes,
        stop_reason=stop,
        task_changed=changed,
        scope_files=scope_files,
        selected_check=check,
        evidence=ctx.work.evidence,
        analysis_run_payloads=ctx.work.analysis_run_payloads,
        project=ctx.project,
        checkpoint_green=ctx.checkpoint_green,
        verification_forbidden=ctx.verification_forbidden,
    )
    return evidence


def _validate_completion_proof(proof: object) -> None:
    if proof is not None and type(getattr(proof, "satisfied", None)) is not bool:
        raise ProjectRuntimeMutationError(
            "runtime mutation failed while committing record_completion_proof: "
            "proof_satisfied must be a bool"
        )


def _commit_operation_proof(ctx: ProjectRun, proof: object) -> None:
    if proof is None:
        return
    _validate_completion_proof(proof)
    _commit_runtime_operation(
        ctx,
        "record_completion_proof",
        lambda mutations, session_id, run_id: mutations.record_completion_proof(
            session_id,
            run_id,
            proof_ref=getattr(proof, "proof_id", ""),
            proof_status=getattr(proof, "status", ""),
        ),
    )


def _record_completion_evidence(ctx: ProjectRun) -> None:
    assert ctx.result is not None
    if ctx.result.stop_reason == "done" and ctx.task_changed and ctx.files:
        ctx.work.refresh_workspace_state(
            ctx.deps.verification.workspace_revisions,
            ctx.project,
            ignored_paths=ctx.configured_ignored_paths,
        )
    evaluation = _completion_evidence(
        ctx,
        changes=ctx.task_changes,
        changed=ctx.task_changed,
        scope_files=ctx.files,
        check=ctx.selected_check,
        stop=ctx.result.stop_reason,
    )
    _validate_completion_proof(evaluation.decision.proof)
    ctx.decision, ctx.integrity = evaluation.decision, evaluation.integrity
    if ctx.task_session is None:
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import build_task_policy

        # Project facts can also be projected from the operation's local
        # evidence (e.g. resumed checkpoints); no model claims enter here.
        ctx.task_session = TaskSession(
            policy=build_task_policy(ctx.request, task_kind="project"),
            task_kind="project", task_text=ctx.request.task, project=str(ctx.project),
        )
    import contextlib

    with contextlib.suppress(Exception):
        ctx.task_session.verification_forbidden = bool(ctx.verification_forbidden is True)
    ctx.proof = None
    if ctx.result.stop_reason == "done":
        from codey.operations.completion_gate import evaluate

        verdict = evaluate(ctx.task_session, ctx.result.summary, context={
            "run_id": ctx.frame.run_id,
            "task": ctx.request.task,
            "question": ctx.request.task,
            "project": str(ctx.project),
            "project_evaluation": evaluation,
            "scope_files": tuple(ctx.files or ()),
            "task_changed": bool(ctx.task_changed),
            "changes": ctx.task_changes,
            "selected_check": ctx.selected_check,
            "execution_evidence": ctx.work.evidence,
            "analysis_run_payloads": ctx.work.analysis_run_payloads,
            "checkpoint_green": bool(ctx.checkpoint_green),
        })
        ctx.proof = verdict.proof
        if verdict.proof is None:
            ctx.blocked_reason = "unobserved"
        ctx.decision = replace(ctx.decision, proof=ctx.proof)
    ctx.result = replace(ctx.result, proof=ctx.proof, facts=ctx.task_session)
    record_completion_proof_trace(ctx.frame.trace, ctx.proof)
    record_edit_integrity_trace(ctx.frame.trace, ctx.integrity)
    _commit_operation_proof(ctx, ctx.proof)


def _prepare_completion_enforcement(ctx: ProjectRun) -> None:
    assert ctx.result is not None
    ctx.task_changed, ctx.files = _enforcement_scope(
        ctx,
        ctx.task_changes,
        ctx.task_changed,
    )
    ctx.verification_candidates = safe_verification_candidates(
        ctx.project,
        ctx.verification_verified_commands,
        ctx.resumed_verification_commands,
        ctx.configured_verification_commands,
        ctx.configured_ignored_paths,
    )
    ctx.selected_check = (
        select_verification_candidate(ctx.verification_candidates, ctx.files)
        if ctx.result.stop_reason == "done" and ctx.task_changed and ctx.files
        else None
    )
    ctx.checkpoint_green = (
        ctx.inherited_green or ctx.review_cycle.inherited_checks_passed
    )
    ctx.verification_forbidden = task_forbids_verification(ctx.request.task)
    ctx.completion_engine = CompletionEngine()


def _maybe_run_completion_repair(ctx: ProjectRun) -> None:
    assert ctx.result is not None
    assert ctx.completion_engine is not None
    remaining_turns = ctx.request.max_turns - ctx.result.turns
    if not (
        ctx.proof is not None
        and not ctx.proof.satisfied
        and not ctx.state.run_registry.stop_flag.is_set()
        and remaining_turns > 0
        and repair_candidate(
            ctx.proof.status,
            ctx.decision.failure_class,
            max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
        )
    ):
        return
    projection = project_repair_context(
        proof=ctx.proof.to_payload(),
        failure_class=ctx.decision.failure_class,
        decisive_checks=(
            decisive_failure_fact(
                ctx.selected_check,
                ctx.work.evidence,
                ctx.files,
                root=ctx.project,
            ),
        ),
        changed_files=ctx.files,
        analysis_run_refs=ctx.decision.analysis_run_refs,
    )
    if not projection.admitted:
        ctx.blocked_reason = "repair_context_unavailable"
        return

    ctx.repair_projection = projection
    _commit_runtime_operation(
        ctx,
        "admit_repair_context",
        lambda mutations, session_id, run_id: mutations.admit_repair_context(
            session_id,
            run_id,
            context_ref=str(projection.to_payload().get("digest") or ""),
        ),
    )
    ctx.hooks.on_event(
        RunEvent.status(
            "[runner] completion proof did not pass; running one bounded repair round."
        )
    )
    _commit_runtime_operation(
        ctx,
        "mark_repair_running",
        lambda mutations, session_id, run_id: mutations.mark_repair_running(
            session_id,
            run_id,
            provider_id=ctx.frame.provider_id,
        ),
    )

    try:
        repair_result = ctx.failover.run(
            task=COMPLETION_REPAIR_FOLLOWUP,
            turn_budget=remaining_turns,
            fresh=False,
            handoff="",
            checkpoint=_refresh_checkpoint_view(ctx),
        )
    except cancellation.TaskCancelled:
        raise
    except ProviderActionError:
        ctx.blocked_reason = "provider_failure"
        _commit_runtime_operation(
            ctx,
            "mark_repair_settled",
            lambda mutations, session_id, run_id: mutations.mark_repair_settled(
                session_id,
                run_id,
                provider_id=ctx.frame.provider_id,
                stop_reason="",
                blocked_reason="provider_failure",
            ),
        )
    else:
        repair_blocked_reason = ""
        if repair_result.stop_reason not in {"done", "approval", "stopped"}:
            repair_remaining_turns = (
                ctx.request.max_turns - ctx.result.turns - repair_result.turns
            )
            repair_blocked_reason = ctx.completion_engine.blocked_reason(
                proof_status=ctx.proof.status,
                failure_class=ctx.decision.failure_class,
                remaining_turns=repair_remaining_turns,
                repair_rounds=1,
            )
        if repair_result.stop_reason not in {"approval", "stopped"}:
            _commit_runtime_operation(
                ctx,
                "mark_repair_settled",
                lambda mutations, session_id, run_id: mutations.mark_repair_settled(
                    session_id,
                    run_id,
                    provider_id=ctx.frame.provider_id,
                    stop_reason=repair_result.stop_reason,
                    turns_used=ctx.result.turns + repair_result.turns,
                    blocked_reason=repair_blocked_reason,
                ),
            )
        if repair_blocked_reason:
            ctx.blocked_reason = repair_blocked_reason
    finally:
        ctx.repair_projection = None
    ctx.repaired_once = not ctx.blocked_reason
    if ctx.repaired_once:
        _apply_repair_result(ctx, repair_result)


def _apply_repair_result(ctx: ProjectRun, repair_result: RunResult) -> None:
    assert ctx.result is not None
    turns = ctx.result.turns + repair_result.turns
    if repair_result.stop_reason == "stopped":
        ctx.result = replace(repair_result, turns=turns)
    elif repair_result.stop_reason == "done":
        ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
        collected = change_state(ctx.task_changes)
        if collected is not None:
            ctx.task_changed = collected
        ctx.task_changed, ctx.files = _enforcement_scope(
            ctx,
            ctx.task_changes,
            ctx.task_changed,
        )
        ctx.verification_candidates = safe_verification_candidates(
            ctx.project,
            ctx.verification_verified_commands,
            ctx.resumed_verification_commands,
            ctx.configured_verification_commands,
            ctx.configured_ignored_paths,
        )
        ctx.selected_check = (
            select_verification_candidate(ctx.verification_candidates, ctx.files)
            if ctx.files
            else None
        )
        ctx.result = RunResult(
            summary=repair_result.summary,
            stop_reason="done",
            turns=turns,
            checks_passed=False,
            changed=ctx.result.changed or repair_result.changed,
            checks_ran=ctx.result.checks_ran or repair_result.checks_ran,
            facts=repair_result.facts or ctx.task_session,
        )
        _record_completion_evidence(ctx)
    else:
        ctx.result = replace(repair_result, turns=turns)


def _settle_blocked_completion(ctx: ProjectRun) -> None:
    assert ctx.result is not None
    assert ctx.completion_engine is not None
    if (
        not ctx.blocked_reason
        and ctx.result.stop_reason == "done"
        and ctx.proof is not None
        and ctx.proof.status in ("failed", "blocked")
    ):
        ctx.blocked_reason = ctx.completion_engine.blocked_reason(
            proof_status=ctx.proof.status,
            failure_class=ctx.decision.failure_class,
            remaining_turns=ctx.request.max_turns - ctx.result.turns,
            repair_rounds=1 if ctx.repaired_once else 0,
        )
    if (
        ctx.blocked_reason
        and ctx.work.operation is not None
        and ctx.work.operation.leaf == LEAF_COMPLETION_PROOF_RECORDED
    ):
        _commit_runtime_operation(
            ctx,
            "mark_completion_blocked",
            lambda mutations, session_id, run_id: mutations.mark_completion_blocked(
                session_id,
                run_id,
                reason=ctx.blocked_reason,
            ),
        )


def _apply_completion_verdict(ctx: ProjectRun) -> None:
    assert ctx.result is not None
    verified = False
    if ctx.blocked_reason and ctx.result.stop_reason in (
        "done",
        "max_turns",
        "no_progress",
        "protocol",
    ):
        ctx.result = blocked_result(ctx.result, ctx.blocked_reason)
    elif ctx.result.stop_reason == "done":
        if ctx.proof is not None:
            verified = ctx.decision.provenance.stance in (
                STANCE_FRESH_PASS,
                STANCE_INHERITED_PASS,
            )
        else:
            verified = bool(ctx.result.checks_passed)
    ctx.result = replace(ctx.result, checks_passed=verified)


def _enforce_completion(ctx: ProjectRun) -> None:
    _prepare_completion_enforcement(ctx)
    _record_completion_evidence(ctx)
    _maybe_run_completion_repair(ctx)
    _settle_blocked_completion(ctx)
    _apply_completion_verdict(ctx)


def enforce_completion(ctx: ProjectRun) -> None:
    """Public enforcement entry."""
    _enforce_completion(ctx)


__all__ = [
    "enforce_completion",
]
