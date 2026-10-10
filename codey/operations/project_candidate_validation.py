"""One runtime-owned check of a stagnant candidate using existing kernel facts."""
from __future__ import annotations

import json
from dataclasses import replace

from codey.agents.writer_failover import CheckpointView, WriterAttempt
from codey.completion.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    select_verification_candidate,
)
from codey.operations.project_completion_context import ProjectRun, commit_runtime_operation
from codey.operations.project_writer_phase import _run_one_writer_attempt
from codey.reviews.coordinator import change_state
from codey.runtime.core.operation_state import LEAF_REPAIR_SETTLED

VALIDATE_TASK = 'Validate the stopped candidate using the original authorized check; do not edit files.'
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

    def __init__(self, check: VerificationCandidate) -> None:
        self._steps = iter([{'tool': 'run', 'args': {'command': check.command, 'path': check.cwd}},
                           {'tool': 'done', 'args': {'summary': 'Submit the checked candidate for validation.'}}])

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        return json.dumps(next(self._steps, {}))


def validate_stopped_candidate(ctx: ProjectRun) -> None:
    previous, policy = ctx.result, ctx.frame.entry_policy
    operation = ctx.work.operation
    if (previous is None or policy is None or previous.stop_reason != 'no_progress'
            or not ctx.task_changed or ctx.task_session is None or not ctx.task_session.edited_files
            or ctx.state.run_registry.stop_flag.is_set() or operation is None
            or operation.candidate_validation_attempted
            or ctx.request.max_turns - previous.turns < 2
            or not all(policy.allows(grant) for grant in ('project.read', 'project.verify', 'project.write'))):
        return
    files = tuple(ctx.task_session.edited_files)
    check = select_verification_candidate(ctx.verification_candidates, files)
    if check is None:
        return
    commit_runtime_operation(ctx, 'mark_candidate_validation_running', lambda mutations, sid, rid:
        mutations.mark_candidate_validation_running(sid, rid, provider_id=ctx.frame.provider_id,
                                                    writer_attempt=ctx.writer_attempt_index + 1))
    ctx.hooks.append_ledger(lambda ledger: ledger.append('candidate_validation_admitted',
        command=check.command, cwd=check.cwd, turn_budget=2, source_stop_reason='no_progress',
        driver='runtime', writer_attempt=ctx.writer_attempt_index + 1))
    validated = _run_one_writer_attempt(ctx, WriterAttempt(
        task=VALIDATE_TASK, provider=_AdmittedCandidateCheck(check), provider_id=ctx.frame.provider_id,
        remaining_turns=2, fresh_chat=False, handoff='', checkpoint=CheckpointView(changed_files=files)),
        lambda turn: None)
    if validated.stop_reason not in {'approval', 'stopped'}:
        settle = 'mark_repair_settled' if operation.leaf == LEAF_REPAIR_SETTLED else 'mark_writer_settled'
        commit_runtime_operation(ctx, settle, lambda mutations, sid, rid:
            getattr(mutations, settle)(sid, rid, provider_id=ctx.frame.provider_id,
                turns_used=previous.turns + validated.turns, stop_reason=validated.stop_reason))
    ctx.result = replace(validated, turns=previous.turns + validated.turns,
                         changed=previous.changed or validated.changed,
                         checks_ran=previous.checks_ran or validated.checks_ran)
    ctx.task_session = validated.facts
    ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
    if validated.stop_reason == 'done':
        ctx.hooks.update_checkpoint(lambda store, item: store.set_status(item, 'ready_for_review'))


def submit_readonly_completion_candidate(ctx: ProjectRun) -> None:
    """Give an unchanged, freshly verified task one bounded done submission."""
    previous, policy = ctx.result, ctx.frame.entry_policy
    operation = ctx.work.operation
    if (
        previous is None
        or policy is None
        or previous.stop_reason != 'no_progress'
        or ctx.task_changed
        or change_state(ctx.task_changes) is not False
        or getattr(ctx.request, 'project_changes_required', False) is True
        or not ctx.work.evidence.has_successful_checks
        or ctx.state.run_registry.stop_flag.is_set()
        or operation is None
        or operation.candidate_validation_attempted
        or ctx.request.max_turns - previous.turns < 2
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
    commit_runtime_operation(ctx, 'mark_readonly_completion_running', lambda mutations, sid, rid:
        mutations.mark_candidate_validation_running(
            sid, rid, provider_id=ctx.frame.provider_id,
            writer_attempt=ctx.writer_attempt_index + 1,
        ))
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
    if validated.stop_reason not in {'approval', 'stopped'}:
        settle = 'mark_repair_settled' if operation.leaf == LEAF_REPAIR_SETTLED else 'mark_writer_settled'
        commit_runtime_operation(ctx, 'mark_readonly_completion_settled', lambda mutations, sid, rid:
            getattr(mutations, settle)(
                sid, rid, provider_id=ctx.frame.provider_id,
                turns_used=previous.turns + validated.turns,
                stop_reason=validated.stop_reason,
            ))
    ctx.result = replace(
        validated,
        turns=previous.turns + validated.turns,
        changed=previous.changed or validated.changed,
        checks_ran=previous.checks_ran or validated.checks_ran,
        facts=validated.facts or ctx.task_session,
    )
    ctx.task_session = validated.facts or ctx.task_session
    ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
    collected_changed = change_state(ctx.task_changes)
    if collected_changed is not None:
        ctx.task_changed = collected_changed
    if validated.stop_reason == 'done':
        ctx.hooks.update_checkpoint(lambda store, item: store.set_status(item, 'ready_for_review'))
