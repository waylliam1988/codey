"""Second-model review runs (reviewer providers).

``task_submit`` builds ``TaskRunDeps.run_review`` from ``run_review``; the
review attempt prompt/render lives in ``reviews/``. Import-light by design
(see ``test_server_lazy_state``): nothing here may load the browser stack,
research, or concepts at import time.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import replace

from codey.app import provider_services as providers
from codey.operations.task_state import TaskState
from codey.policies.limits import REVIEW_TIMEOUT
from codey.providers import controls as provider_controls
from codey.providers.catalog import DEFAULT_PROVIDER_ID
from codey.reviews.core import ReviewResult, parse_review_with_repair
from codey.reviews.impact_map import safe_review_impact_map
from codey.reviews.review_policy import WEB_IF_AVAILABLE, allow_self_review
from codey.runtime.core import cancellation
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt


def emit_review(ctx: TaskState, session_id: str, text: str) -> None:
    ctx.emit({"type": "review", "session_id": session_id, "text": text})


def emit_review_with_payload(
    ctx: TaskState,
    session_id: str,
    text: str,
    review: ReviewResult,
) -> None:
    from codey.reviews.core import review_result_payload

    ctx.emit({"type": "review", "session_id": session_id, "text": text,
              "review": review_result_payload(review)})


class ReviewSendUnknown(RuntimeError):
    """Provider request was sent but the result is unknown: never auto-resend."""


def run_review_attempt(
    ctx: TaskState,
    *,
    session_id: str,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    change_brief: str,
    project_map: str,
    verification_map: str,
    review_impact_map: str,
    execution_evidence: str,
    reviewer_id: str,
    reviewer,
    self_review: bool,
    trace_recorder: object | None = None,
    run_id: str = "",
    review_policy: str = WEB_IF_AVAILABLE,
    review_source_run_id: str = "",
) -> tuple[str, ReviewResult]:
    from codey.reviews.identity import build_identity, capture_snapshot, review_model_identity, verify_snapshot
    from codey.reviews.input import prepare_review_input

    try:
        cancellation.check()
        prepared = prepare_review_input(
            project=project,
            task=task,
            writer_summary=writer_summary,
            changes=changes,
            recent_log=recent_log,
            change_brief=change_brief,
            project_map=project_map,
            verification_map=verification_map,
            review_impact_map=review_impact_map,
            execution_evidence=execution_evidence,
        )
        snapshot = capture_snapshot(project, prepared.scope.provided_files)
        early = _early_snapshot_result(ctx, session_id, reviewer_id, prepared, snapshot)
        if early is not None:
            return reviewer_id, early
        cancellation.check()
        if not verify_snapshot(snapshot):
            return reviewer_id, _stale_before_send(ctx, session_id, reviewer_id, prepared)
        model_id = review_model_identity(reviewer)
        if review_source_run_id and model_id:
            from codey.reviews.reuse import try_reuse_review

            identity = build_identity(prepared, reviewer_id=reviewer_id, policy=review_policy,
                                      model_id=model_id, self_review=self_review, project=project, snapshot=snapshot)
            state_home = getattr(ctx, "state_home", None)
            reused = try_reuse_review(
                state_home=state_home, session_id=session_id, current_run_id=run_id,
                current_project=project, source_run_id=review_source_run_id,
                current_scope=prepared.scope, current_identity=identity, current_snapshot_ok=True,
            ) if state_home is not None else None
            if reused is not None and reused.identity is not None and verify_snapshot(snapshot):
                # Historical artifact reference, current applicability guard.
                identity = replace(identity, attempt_id=reused.identity.attempt_id,
                                   artifact_sha256=reused.identity.artifact_sha256)
                reused = replace(reused, identity=identity)
                emit_review_with_payload(ctx, session_id, "Previous review reused", reused)
                return reviewer_id, reused
        cancellation.check()
        reviewer.new_chat()
        reply = _send_review_prompt(
            reviewer, trace_recorder, prepared.prompt
        )
        review = _parse_with_single_repair(
            reviewer, trace_recorder, reply, prepared
        )
        return reviewer_id, _finalize_review(
            ctx, session_id, project, reviewer_id, self_review,
            prepared, snapshot, review, trace_recorder, run_id, review_policy, model_id,
        )
    finally:
        with contextlib.suppress(Exception):
            reviewer.close()


def _early_snapshot_result(ctx, session_id, reviewer_id, prepared, snapshot):
    if snapshot.ok:
        return None
    review = ReviewResult(
        verdict="unknown",
        summary="Review incomplete",
        findings=[],
        status="incomplete",
        origin="fresh",
        diagnostics=("snapshot_unavailable",),
        scope=prepared.scope,
    )
    emit_review(ctx, session_id, f"{providers.review_label(reviewer_id)} incomplete")
    return review


def _stale_before_send(ctx, session_id, reviewer_id, prepared):
    review = ReviewResult(
        verdict="unknown",
        summary="Review outdated · files changed",
        findings=[],
        status="stale",
        origin="fresh",
        diagnostics=("snapshot_stale_before_send",),
        scope=prepared.scope,
    )
    emit_review(ctx, session_id, f"{providers.review_label(reviewer_id)} incomplete")
    return review


def _send_review_prompt(reviewer, trace_recorder, prompt):
    trace = FailOpenPromptTrace(trace_recorder)
    trace.call("record_permission_profile", "reviewer", phase="review")
    record_provider_send_prompt(
        trace_recorder,
        name="review_prompt",
        text=prompt,
        purpose="review prompt sent to provider",
        source_ref="provider_send:review",
        capability_id="review_runner",
    )
    try:
        with provider_controls.suppress_assistance():
            return reviewer.send(prompt, timeout=REVIEW_TIMEOUT)
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception as exc:
        raise ReviewSendUnknown(str(exc)) from exc


def _parse_with_single_repair(reviewer, trace_recorder, reply, prepared):
    def send_repair_prompt(repair: str) -> str:
        record_provider_send_prompt(
            trace_recorder,
            name="review_repair_prompt",
            text=repair,
            purpose="review repair prompt sent to provider",
            source_ref="provider_send:review_repair",
            capability_id="review_runner",
        )
        try:
            return reviewer.send(repair, timeout=REVIEW_TIMEOUT)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            raise ReviewSendUnknown(str(exc)) from exc

    return parse_review_with_repair(reply, send_repair_prompt, changes=prepared.reviewer_view)


def _finalize_review(
    ctx, session_id, project, reviewer_id, self_review,
    prepared, snapshot, review, trace_recorder, run_id, review_policy=WEB_IF_AVAILABLE, model_id="",
):
    import uuid as _uuid

    from codey.reviews.identity import (
        build_identity,
        prompt_digest_for,
        scope_digest_for,
        snapshot_digest_for,
        verify_snapshot,
    )

    scope_digest = scope_digest_for(prepared.scope)
    prompt_digest = prompt_digest_for(prepared.prompt)
    snapshot_digest = snapshot_digest_for(snapshot)
    attempt_id = _uuid.uuid4().hex[:12]
    snapshot_current = verify_snapshot(snapshot)
    persisted_status = (
        "stale"
        if not snapshot_current
        else ("incomplete" if not prepared.scope.is_complete else review.status)
    )
    review = replace(review, status=persisted_status, scope=prepared.scope)
    artifact_sha = _persist_attempt_artifact(
        ctx, session_id, run_id, attempt_id, review,
        scope_digest, prompt_digest, snapshot_digest,
        reviewer_id, self_review, model_id, review_policy,
    )
    identity = build_identity(
        prepared,
        reviewer_id=reviewer_id,
        policy=review_policy,
        model_id=model_id,
        self_review=self_review,
        project=project,
        snapshot=snapshot,
        attempt_id=attempt_id,
        artifact_sha256=artifact_sha,
    )
    review = replace(review, identity=identity)
    from codey.reviews.persistence import review_trace_payload

    FailOpenPromptTrace(trace_recorder).call(
        "record_coding_review",
        review_trace_payload(review, scope_digest=scope_digest, prompt_digest=prompt_digest),
    )
    label = providers.review_label(reviewer_id)
    prefix = f"{label} self-review" if self_review else label
    if review.approved:
        emit_text = f"{prefix} approved"
    elif not review.is_complete:
        emit_text = f"{prefix} incomplete"
    else:
        emit_text = f"{prefix} suggested changes"
    emit_review_with_payload(ctx, session_id, emit_text, review)
    return review


def _persist_attempt_artifact(
    ctx, session_id, run_id, attempt_id, review,
    scope_digest, prompt_digest, snapshot_digest, reviewer_id, self_review,
    model_id, review_policy,
) -> str:
    if not run_id:
        return ""
    try:
        from codey.reviews.persistence import ReviewArtifactStore, save_review_artifact

        state_home = getattr(ctx, "state_home", None)
        if state_home is None:
            return ""
        ref = save_review_artifact(
            ReviewArtifactStore(state_home),
            session_id=session_id,
            run_id=run_id,
            attempt_id=attempt_id,
            result=review,
            scope_digest=scope_digest,
            prompt_digest=prompt_digest,
            snapshot_digest=snapshot_digest,
            reviewer_id=reviewer_id,
            policy=review_policy,
            model_id=model_id,
            self_review=self_review,
        )
        return ref.sha256 if ref is not None else ""
    except Exception:
        return ""


def run_review(
    ctx: TaskState,
    *,
    session_id: str,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    writer_id: str,
    change_brief: str = "",
    project_map: str = "",
    verification_map: str = "",
    review_impact_map: str | None = None,
    execution_evidence: str = "",
    trace_recorder: object | None = None,
    review_policy: str = WEB_IF_AVAILABLE,
    run_id: str = "",
    review_source_run_id: str = "",
    connect_reviewer: Callable | None = None,
) -> tuple[str, ReviewResult] | None:
    from codey.reviews.reuse import validate_source_run_id

    cancellation.check()
    # Validate up front so a misspelled policy fails even when a web reviewer
    # is available; the result is reused for the self-review gate below.
    self_review_allowed = allow_self_review(review_policy)
    last_error: Exception | None = None
    if review_impact_map is None:
        review_impact_map = safe_review_impact_map(project, changes)
    source = validate_source_run_id(review_source_run_id) if review_source_run_id else ""
    if connect_reviewer is not None:
        if not self_review_allowed:
            raise ValueError("headless selected-provider review requires self-review permission")
        reviewer = connect_reviewer(writer_id)
        return run_review_attempt(ctx, session_id=session_id, project=project, task=task,
            writer_summary=writer_summary, changes=changes, recent_log=recent_log,
            change_brief=change_brief, project_map=project_map, verification_map=verification_map,
            review_impact_map=review_impact_map, execution_evidence=execution_evidence,
            reviewer_id=writer_id, reviewer=reviewer, self_review=True, trace_recorder=trace_recorder,
            run_id=run_id, review_policy=review_policy, review_source_run_id=source)
    for reviewer_id in providers.reviewer_candidates(ctx, writer_id):
        cancellation.check()
        try:
            reviewer = providers.connect_existing_provider(reviewer_id)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            last_error = exc
            continue
        ctx.set_provider_session(reviewer_id, None)
        return run_review_attempt(
            ctx,
            session_id=session_id,
            project=project,
            task=task,
            writer_summary=writer_summary,
            changes=changes,
            recent_log=recent_log,
            change_brief=change_brief,
            project_map=project_map,
            verification_map=verification_map,
            review_impact_map=review_impact_map,
            execution_evidence=execution_evidence,
            reviewer_id=reviewer_id,
            reviewer=reviewer,
            self_review=False,
            trace_recorder=trace_recorder,
            run_id=run_id,
            review_policy=review_policy,
            review_source_run_id=source,
        )
    cancellation.check()
    if not self_review_allowed:
        emit_review(ctx, session_id, "Review unavailable: no web reviewer is open.")
        return None
    try:
        from codey.providers.ids import normalize_provider_id

        reviewer_id = normalize_provider_id(writer_id) or DEFAULT_PROVIDER_ID
        reviewer = providers.connect_fresh_provider_tab(reviewer_id)
        return run_review_attempt(
            ctx,
            session_id=session_id,
            project=project,
            task=task,
            writer_summary=writer_summary,
            changes=changes,
            recent_log=recent_log,
            change_brief=change_brief,
            project_map=project_map,
            verification_map=verification_map,
            review_impact_map=review_impact_map,
            execution_evidence=execution_evidence,
            reviewer_id=reviewer_id,
            reviewer=reviewer,
            self_review=True,
            trace_recorder=trace_recorder,
            run_id=run_id,
            review_policy=review_policy,
            review_source_run_id=source,
        )
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception as exc:
        last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no review model available")


__all__ = [
    "ReviewSendUnknown",
    "emit_review",
    "emit_review_with_payload",
    "run_review",
    "run_review_attempt",
]
