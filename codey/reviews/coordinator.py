"""Coordinate the bounded diff-review lifecycle for one project task.

The reviewer connection and Writer failover machinery stay in ``TaskRunner``.
This module owns only the diff-review lifecycle:

* retry an unavailable diff before deciding whether to review,
* run the configured review callback for reviewable changes,
* turn reviewer findings into a Writer follow-up,
* mark the diff cache dirty after any repair attempt,
* preserve the narrow pre-review green-check inheritance rule.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from codey.runtime import cancellation
from codey.agents.runner import RunResult
from codey.runtime.execution_evidence import CheckEvidence
from codey.reviews.core import has_reviewable_changes, render_writer_followup
from codey.completion.verification_policy import VerificationCandidate
from codey.agents.writer_failover import CheckpointView


@dataclass(frozen=True)
class ReviewCycleResult:
    result: RunResult
    task_changed: bool
    changes: dict | None
    changes_dirty: bool
    review_attempted: bool = False
    review_repair_attempted: bool = False
    # The narrow pre-review green-check rule fired: the repair changed
    # nothing, so the earlier local green still covers the workspace. The
    # caller records this as inherited provenance -- never as this round's
    # clean verification fact.
    inherited_checks_passed: bool = False


def change_state(changes: object) -> bool | None:
    """Return final diff state, or None when change collection was unavailable."""

    if not isinstance(changes, dict) or changes.get("ok") is not True:
        return None
    files = changes.get("files")
    changed_count = changes.get("changed_count")
    return bool(
        (isinstance(changed_count, int) and changed_count > 0)
        or (isinstance(files, list) and files)
    )


class ReviewCoordinator:
    """Coordinate Review without owning reviewers, providers, or receipts."""

    def __init__(self, collect_changes: Callable) -> None:
        self.collect_changes = collect_changes

    def run_cycle(
        self,
        *,
        project: str | Path,
        tracker,
        session_id: str,
        task: str,
        result: RunResult,
        task_changed: bool,
        changes: dict | None,
        changes_dirty: bool,
        writer_id: str,
        recent_log: str,
        render_change_brief: Callable[[], str],
        execution_evidence: str,
        successful_checks: tuple[CheckEvidence, ...],
        checkpoint_prompt: str,
        checks_before_review_followup: bool,
        stop_requested: Callable[[], bool],
        refresh_project_map: Callable[[], str],
        build_verification_map: Callable[[dict, str], str],
        run_review: Callable,
        close_writer_for_review: Callable[[], None],
        repair_writer: Callable[[str, CheckpointView], RunResult],
        set_checkpoint_status: Callable[[str], None],
        emit_review_unavailable: Callable[[], None],
    ) -> ReviewCycleResult:
        if result.stop_reason != "done" or not task_changed or stop_requested():
            return ReviewCycleResult(result, task_changed, changes, changes_dirty)

        if changes_dirty:
            changes = self.collect_changes(project, tracker)
            collected_changed = change_state(changes)
            changes_dirty = collected_changed is None
            if collected_changed is not None:
                task_changed = collected_changed

        if not task_changed or stop_requested():
            return ReviewCycleResult(result, task_changed, changes, changes_dirty)

        refreshed_project_map: str | None = None

        def current_project_map() -> str:
            nonlocal refreshed_project_map
            if refreshed_project_map is None:
                refreshed_project_map = refresh_project_map()
            return refreshed_project_map

        project_map = current_project_map()
        if not has_reviewable_changes(changes or {}):
            return ReviewCycleResult(result, task_changed, changes, changes_dirty)

        change_brief = render_change_brief()
        verification_map = build_verification_map(changes or {}, project_map)
        close_writer_for_review()
        try:
            reviewed = run_review(
                session_id=session_id,
                project=project,
                task=task,
                writer_summary=result.summary,
                changes=changes,
                recent_log=recent_log,
                execution_evidence=execution_evidence,
                writer_id=writer_id,
                change_brief=change_brief,
                project_map=project_map,
                verification_map=verification_map,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            emit_review_unavailable()
            return ReviewCycleResult(
                result,
                task_changed,
                changes,
                changes_dirty,
                review_attempted=True,
            )

        if reviewed is None:
            return ReviewCycleResult(
                result,
                task_changed,
                changes,
                changes_dirty,
                review_attempted=True,
            )

        _reviewer_id, review = reviewed
        if review.approved:
            return ReviewCycleResult(
                result,
                task_changed,
                changes,
                changes_dirty,
                review_attempted=True,
            )

        set_checkpoint_status("fixing_review")
        followup = render_writer_followup(
            task,
            review,
            change_brief=change_brief,
        )
        current_project_map()
        repaired = repair_writer(
            followup,
            CheckpointView(
                prompt=checkpoint_prompt,
                changed_files=_changed_files(changes),
                successful_checks=tuple(
                    VerificationCandidate(
                        item.command,
                        item.cwd,
                        "execution evidence",
                    )
                    for item in successful_checks
                ),
            ),
        )
        changes_dirty = True
        inherited = False
        if repaired.stop_reason == "done":
            set_checkpoint_status("ready_for_review")
        if (
            repaired.stop_reason == "done"
            and not repaired.changed
            and checks_before_review_followup
            and not repaired.checks_ran
        ):
            # Same narrow inheritance rule as before, now surfaced instead of
            # mutating silently: the caller's completion proof decides what
            # this green is worth.
            repaired = replace(repaired, checks_passed=True)
            inherited = True
        return ReviewCycleResult(
            repaired,
            task_changed or repaired.changed,
            changes,
            changes_dirty,
            review_attempted=True,
            review_repair_attempted=True,
            inherited_checks_passed=inherited,
        )


def _changed_files(changes: dict | None) -> tuple[str, ...]:
    return tuple(
        str(item.get("path") or "")
        for item in ((changes or {}).get("files") or [])
        if isinstance(item, dict) and item.get("path")
    )