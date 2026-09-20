"""Second-model review runs (reviewer providers).

``task_submit`` builds ``TaskRunDeps.run_review`` from ``run_review``; the
review attempt prompt/render lives in ``reviews/``. Import-light by design
(see ``test_server_lazy_state``): nothing here may load the browser stack,
research, or concepts at import time.
"""

from __future__ import annotations

from codey.app import provider_services as providers
from codey.operations.task_state import TaskState
from codey.policies.limits import REVIEW_TIMEOUT
from codey.providers.catalog import DEFAULT_PROVIDER_ID
from codey.providers import controls as provider_controls
from codey.reviews.core import ReviewResult, parse_review_with_repair, render_review_prompt
from codey.reviews.impact_map import safe_review_impact_map
from codey.runtime.core import cancellation
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt


def emit_review(ctx: TaskState, session_id: str, text: str) -> None:
    ctx.emit({"type": "review", "session_id": session_id, "text": text})


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
) -> tuple[str, ReviewResult]:
    try:
        reviewer.new_chat()
        prompt = render_review_prompt(
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
        with provider_controls.suppress_assistance():
            reply = reviewer.send(prompt, timeout=REVIEW_TIMEOUT)

            def send_repair_prompt(repair: str) -> str:
                record_provider_send_prompt(
                    trace_recorder,
                    name="review_repair_prompt",
                    text=repair,
                    purpose="review repair prompt sent to provider",
                    source_ref="provider_send:review_repair",
                    capability_id="review_runner",
                )
                return reviewer.send(repair, timeout=REVIEW_TIMEOUT)

            review = parse_review_with_repair(
                reply,
                send_repair_prompt,
                changes=changes,
            )
        label = providers.review_label(reviewer_id)
        prefix = f"{label} self-review" if self_review else label
        if review.approved:
            emit_review(ctx, session_id, f"{prefix} approved")
        else:
            emit_review(ctx, session_id, f"{prefix} suggested changes")
        return reviewer_id, review
    finally:
        try:
            reviewer.close()
        except Exception:
            pass


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
) -> tuple[str, ReviewResult] | None:
    cancellation.check()
    last_error: Exception | None = None
    if review_impact_map is None:
        review_impact_map = safe_review_impact_map(project, changes)
    for reviewer_id in providers.reviewer_candidates(ctx, writer_id):
        cancellation.check()
        try:
            reviewer = providers.connect_existing_provider(reviewer_id)
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
            )
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            last_error = exc
    cancellation.check()
    try:
        reviewer_id = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
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
        )
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no review model available")


__all__ = [
    "emit_review",
    "run_review",
    "run_review_attempt",
]
