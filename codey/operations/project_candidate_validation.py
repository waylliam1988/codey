"""One runtime-owned check of a stagnant candidate using existing kernel facts."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.agents.writer_failover import CheckpointView, WriterAttempt
from codey.completion.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    select_verification_plan,
)
from codey.operations.project_completion_context import ProjectRun, commit_runtime_operation
from codey.operations.project_writer_phase import _run_one_writer_attempt
from codey.operations.task_session import TaskSession
from codey.operations.task_state import stop_flag_for
from codey.reviews.coordinator import change_state
from codey.runtime.core.operation_payload import LEAF_REPAIR_SETTLED, RuntimeOperationState
from codey.runtime.core.run_result import RunResult
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.task.model import TaskSubmission
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.operations.task_run import CollectChanges
    from codey.workspace.changes import ChangeTracker

VALIDATE_TASK = 'Validate the stopped candidate using the original authorized checks. Do not edit files.'
READONLY_COMPLETION_TASK = (
    'The original verification already passed on the current unchanged workspace. '
    'Submit the read-only verification result now with done; do not run another test, '
    'read files, or edit files. Submit the read-only verification result only if the '
    'recorded verification is still current.'
)


class _AdmittedCandidateCheck:
    """A fixed kernel action source; never a model decision or model usage.

    The check and submission still pass through the real permission, execution,
    receipt and completion paths. Submission alone cannot prove completion.
    """
    name = 'runtime candidate validation'
    location = 'runtime:candidate-validation'

    def __init__(self, checks: tuple[VerificationCandidate, ...]) -> None:
        self._checks = iter(checks)

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        if (check := next(self._checks, None)) is not None:
            proposal: JsonObject = {'tool': 'run', 'args': {'command': check.command, 'path': check.cwd}}
            return json.dumps(proposal)
        # A yielded command can require a formal-result delivery before the
        # gate accepts done. Resubmit only completion, never the command.
        # The kernel owns waiting, cancellation, no-progress and total budget.
        proposal = {'tool': 'done', 'args': {'summary': 'Submit the checked candidate for validation.'}}
        return json.dumps(proposal)


def _settle_validation(ctx: ProjectRun, previous: RunResult, validated: RunResult, *, repair: bool,
                       readonly: bool = False) -> None:
    if validated.stop_reason in {'approval', 'stopped'}:
        return

    def commit(mutations: RuntimeMutationLine, sid: str, rid: str) -> RuntimeOperationState | None:
        settle = mutations.mark_repair_settled if repair else mutations.mark_writer_settled
        return settle(sid, rid, provider_id=ctx.frame.provider_id,
            turns_used=previous.turns + validated.turns, stop_reason=validated.stop_reason)

    step = 'mark_readonly_completion_settled' if readonly else 'mark_repair_settled' if repair else 'mark_writer_settled'
    commit_runtime_operation(ctx, step, commit)


def _collect_candidate_changes(ctx: ProjectRun) -> dict[str, object]:
    collect: CollectChanges = ctx.deps.verification.collect_changes
    tracker: ChangeTracker | None = ctx.tracker
    return collect(ctx.project, tracker)


def validate_stopped_candidate(ctx: ProjectRun) -> None:
    previous, policy = ctx.result, ctx.frame.entry_policy
    operation = ctx.work.operation
    session: TaskSession | None = ctx.task_session
    request: TaskSubmission = ctx.request
    if (previous is None or policy is None or previous.stop_reason != 'no_progress'
            or not ctx.task_changed or session is None or not session.edited_files
            or stop_flag_for(ctx.state, ctx.frame.run_id).is_set() or operation is None
            or operation.candidate_validation_attempted
            or request.max_turns - previous.turns < 2
            or not all(policy.allows(grant) for grant in ('project.read', 'project.verify', 'project.write'))):
        return
    files = tuple(session.edited_files)
    remaining_turns = request.max_turns - previous.turns
    plan = select_verification_plan(ctx.verification_candidates, files, root=ctx.project)
    checks = tuple(item.candidate for item in plan.obligations)
    if not checks or plan.uncovered_paths or remaining_turns <= len(checks):
        return

    def admit(mutations: RuntimeMutationLine, sid: str, rid: str) -> RuntimeOperationState | None:
        return mutations.mark_candidate_validation_running(sid, rid, provider_id=ctx.frame.provider_id,
            writer_attempt=ctx.writer_attempt_index + 1)

    commit_runtime_operation(ctx, 'mark_candidate_validation_running', admit)
    ctx.hooks.append_ledger(lambda ledger: ledger.append('candidate_validation_admitted',
        command=checks[0].command, cwd=checks[0].cwd,
        checks=[{'command': check.command, 'cwd': check.cwd} for check in checks],
        turn_budget=remaining_turns, source_stop_reason='no_progress',
        driver='runtime', writer_attempt=ctx.writer_attempt_index + 1))
    validated = _run_one_writer_attempt(ctx, WriterAttempt(
        task=VALIDATE_TASK, provider=_AdmittedCandidateCheck(checks), provider_id=ctx.frame.provider_id,
        remaining_turns=remaining_turns, fresh_chat=False, handoff='', checkpoint=CheckpointView(changed_files=files)),
        lambda turn: None)
    _settle_validation(ctx, previous, validated, repair=operation.leaf == LEAF_REPAIR_SETTLED)
    ctx.result = replace(validated, turns=previous.turns + validated.turns,
                         changed=previous.changed or validated.changed,
                         checks_ran=previous.checks_ran or validated.checks_ran)
    facts: TaskSession | None = validated.facts
    ctx.task_session = facts
    ctx.task_changes = _collect_candidate_changes(ctx)
    if validated.stop_reason == 'done':
        ctx.hooks.update_checkpoint(lambda store, item: store.set_status(item, 'ready_for_review'))


def submit_readonly_completion_candidate(ctx: ProjectRun) -> None:
    """Give an unchanged, freshly verified task one bounded done submission."""
    previous, policy = ctx.result, ctx.frame.entry_policy
    operation = ctx.work.operation
    request: TaskSubmission = ctx.request
    changes: dict[str, object] | None = ctx.task_changes
    session: TaskSession | None = ctx.task_session
    if (
        previous is None
        or policy is None
        or previous.stop_reason != 'no_progress'
        or ctx.task_changed
        or change_state(changes) is not False
        or request.project_changes_required is True
        or not ctx.work.evidence.has_successful_checks
        or stop_flag_for(ctx.state, ctx.frame.run_id).is_set()
        or operation is None
        or operation.candidate_validation_attempted
        or request.max_turns - previous.turns < 2
        or not all(policy.allows(grant) for grant in ('project.read', 'project.verify'))
        or ctx.failover is None
        or ctx.failover.provider is None
    ):
        return
    if not any(
        check_covers_selected_candidate(
            candidate,
            check.command,
            check.cwd,
            (),
            root=ctx.project,
        )
        for candidate in ctx.verification_candidates
        for check in ctx.work.evidence.successful_checks
    ):
        return

    def admit(mutations: RuntimeMutationLine, sid: str, rid: str) -> RuntimeOperationState | None:
        return mutations.mark_candidate_validation_running(
            sid, rid, provider_id=ctx.frame.provider_id,
            writer_attempt=ctx.writer_attempt_index + 1,
        )

    commit_runtime_operation(ctx, 'mark_readonly_completion_running', admit)
    ctx.hooks.append_ledger(lambda ledger: ledger.append(
        'readonly_completion_admitted',
        turn_budget=2,
        source_stop_reason='no_progress',
        driver='runtime',
        writer_attempt=ctx.writer_attempt_index + 1,
    ))
    validated = _run_one_writer_attempt(
        ctx,
        WriterAttempt(
            task=READONLY_COMPLETION_TASK,
            provider=ctx.failover.provider,
            provider_id=ctx.frame.provider_id,
            remaining_turns=2,
            fresh_chat=False,
            handoff='',
            checkpoint=CheckpointView(
                changed_files=(),
                successful_checks=tuple(
                    VerificationCandidate(check.command, check.cwd, 'execution evidence')
                    for check in ctx.work.evidence.successful_checks
                ),
            ),
        ),
        lambda turn: None,
        permission_profile='planning_readonly',
    )
    _settle_validation(ctx, previous, validated, repair=operation.leaf == LEAF_REPAIR_SETTLED, readonly=True)
    facts: TaskSession | None = validated.facts
    ctx.result = replace(
        validated,
        turns=previous.turns + validated.turns,
        changed=previous.changed or validated.changed,
        checks_ran=previous.checks_ran or validated.checks_ran,
        facts=facts or session,
    )
    ctx.task_session = facts or session
    changes = _collect_candidate_changes(ctx)
    ctx.task_changes = changes
    collected_changed = change_state(changes)
    if collected_changed is not None:
        ctx.task_changed = collected_changed
    if validated.stop_reason == 'done':
        ctx.hooks.update_checkpoint(lambda store, item: store.set_status(item, 'ready_for_review'))
