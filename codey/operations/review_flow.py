"""Review-only task operation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codey.completion.edit_scope import changed_paths_from_changes
from codey.operations.context import RunFrame
from codey.operations.project_completion_flow import record_review_input_prepared_trace
from codey.operations.result import ModeOutcome
from codey.reviews.core import has_reviewable_changes
from codey.reviews.impact_map import safe_review_impact_map
from codey.runtime import cancellation
from codey.runtime.prompt_envelope import FailOpenPromptTrace, PromptEnvelopeSection
from codey.runtime.terminalizer import task_done_event


@dataclass(frozen=True)
class ReviewFlowDeps:
    state: Any
    collect_changes: Callable
    run_review: Callable
    is_git_repository: Callable[[str | Path], bool]
    review_log_lines: int = 80


def has_reviewable_diff(deps: ReviewFlowDeps, project: str | None) -> bool:
    if not project:
        return False
    try:
        return has_reviewable_changes(collect_review_changes(deps, project))
    except Exception:
        return False


def collect_review_changes(deps: ReviewFlowDeps, project: str | None) -> dict:
    if not project:
        return {"ok": False, "error": "project required", "files": [], "diff": ""}
    return deps.collect_changes(project, review_change_tracker(deps, project))


def review_change_tracker(deps: ReviewFlowDeps, project: str | None):
    if not project:
        return None
    try:
        key = str(Path(project).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    tracker_for = getattr(deps.state, "change_tracker_for", None)
    if not callable(tracker_for):
        return None
    try:
        persistent = not deps.is_git_repository(key)
    except Exception:
        persistent = True
    try:
        return tracker_for(key, persistent=persistent)
    except Exception:
        return None


def run_review_mode(deps: ReviewFlowDeps, frame: RunFrame) -> ModeOutcome:
    state = deps.state
    request = frame.request
    project = request.project
    trace = FailOpenPromptTrace(frame.trace)
    trace.call("record_permission_profile", "reviewer", phase="review")
    trace.record_section(
        PromptEnvelopeSection(
            name="review_request",
            text=request.task,
            purpose="review request from the user",
            freshness="run_start",
            source_refs=("request:review",),
        )
    )
    if project is None:
        summary = "No attached project is available to review."
        state.emit({
            "type": "review",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "text": summary,
        })
        return ModeOutcome(
            task_done_event(
                run_id=frame.run_id,
                session_id=request.session_id,
                summary=summary,
                stop_reason="done",
                turns=0,
                max_turns=request.max_turns,
                provider=frame.provider_id,
                mode="review",
                changed=False,
            )
        )
    changes = collect_review_changes(deps, project)
    trace.record_section(
        PromptEnvelopeSection(
            name="review_changes",
            text=changes.get("diff", "") if isinstance(changes, dict) else "",
            purpose="bounded local diff prepared for review",
            freshness="run_start",
            source_refs=("local_diff:review",),
        )
    )
    if not isinstance(changes, dict) or changes.get("ok") is not True:
        summary = "Could not collect a local diff to review."
        state.emit({
            "type": "review",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "text": summary,
        })
        return ModeOutcome(
            task_done_event(
                run_id=frame.run_id,
                session_id=request.session_id,
                summary=summary,
                stop_reason="done",
                turns=0,
                max_turns=request.max_turns,
                provider=frame.provider_id,
                mode="review",
                changed=False,
            )
        )
    if not has_reviewable_changes(changes):
        summary = "No reviewable local diff was found."
        state.emit({
            "type": "review",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "text": summary,
        })
        return ModeOutcome(
            task_done_event(
                run_id=frame.run_id,
                session_id=request.session_id,
                summary=summary,
                stop_reason="done",
                turns=0,
                max_turns=request.max_turns,
                provider=frame.provider_id,
                mode="review",
                changed=False,
                changes={
                    "changed_count": changes.get("changed_count", 0),
                    "files": changes.get("files", [])[:3],
                    "mode": changes.get("mode"),
                    "project": project,
                },
            )
        )
    try:
        try:
            review_impact_map = safe_review_impact_map(project, changes)
        except cancellation.TaskCancelled:
            raise
        except Exception:
            review_impact_map = ""
        record_review_input_prepared_trace(
            frame.trace,
            task=request.task,
            writer_summary="Review-only mode did not run a writer.",
            changes=changes,
            recent_log="",
            change_brief="",
            project_map="",
            verification_map="",
            review_impact_map=review_impact_map,
            execution_evidence="",
        )
        reviewed = deps.run_review(
            session_id=request.session_id,
            project=project,
            task=request.task,
            writer_summary="Review-only mode did not run a writer.",
            changes=changes,
            recent_log="",
            writer_id=frame.provider_id,
            change_brief="",
            project_map="",
            verification_map="",
            review_impact_map=review_impact_map,
            execution_evidence="",
            trace_recorder=frame.trace,
        )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        reviewed = None
    if reviewed is None:
        summary = "Review unavailable. No files were changed."
    else:
        _reviewer_id, review = reviewed
        summary = render_review_only_summary(review)
    state.set_provider_session(frame.provider_id, None)
    frame.conversation.update_snapshot(
        replace(
            frame.conversation.snapshot,
            mode="review",
            goal=request.task,
            project=frame.project_text,
            provider_id=frame.provider_id,
            changed_files=changed_paths_from_changes(changes),
            checks_passed=False,
            summary=summary,
            blocker="",
            latest_user=request.task,
            latest_reply=summary,
        )
    )
    state.emit({
        "type": "review",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "text": summary,
    })
    return ModeOutcome(
        {
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": summary,
            "stop_reason": "done",
            "turns": 1 if reviewed is not None else 0,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "review",
            "changed": False,
            "changes": {
                "changed_count": changes.get("changed_count", 0),
                "files": changes.get("files", [])[:3],
                "mode": changes.get("mode"),
                "project": project,
            },
        }
    )


def render_review_only_summary(review: object) -> str:
    approved = bool(getattr(review, "approved", False))
    summary = str(getattr(review, "summary", "") or "").strip()
    if approved:
        return f"Review approved: {summary or 'No issues found.'}"
    lines = [f"Review requested changes: {summary or 'Issues found.'}"]
    findings = getattr(review, "findings", ()) or ()
    for index, finding in enumerate(list(findings)[:8], start=1):
        path = str(getattr(finding, "path", "") or "").strip()
        issue = str(getattr(finding, "issue", "") or "").strip()
        fix = str(getattr(finding, "suggested_fix", "") or "").strip()
        prefix = f"{index}. "
        if path:
            prefix += f"{path}: "
        text = issue or "Issue found"
        if fix:
            text += f" Suggested fix: {fix}"
        lines.append(prefix + text)
    return "\n".join(lines)


__all__ = [
    "ReviewFlowDeps",
    "collect_review_changes",
    "has_reviewable_diff",
    "render_review_only_summary",
    "review_change_tracker",
    "run_review_mode",
]
