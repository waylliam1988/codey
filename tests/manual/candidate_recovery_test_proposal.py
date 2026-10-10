"""Process-local proposal for the manual experiment, never a production fallback.

Uses existing writer admission, kernel execution, observations and completion.
The fixed validation provider is supplied by the experiment's scripted adapter;
it calls only the original check and done. This is not an LLM recovery strategy.
"""
import json
from dataclasses import replace
from unittest import mock

from codey.agents.writer_failover import CheckpointView, WriterAttempt
from codey.app import review_service
from codey.completion.verification_policy import select_verification_candidate
from codey.operations import project_completion_enforcement as enforcement
from codey.operations import project_completion_flow as flow
from codey.operations import project_review_phase as review_phase
from codey.operations.project_completion_context import commit_runtime_operation
from codey.operations.project_writer_phase import _run_one_writer_attempt
from codey.reviews.coordinator import _snapshot_still_current
from codey.reviews.core import REVIEW_REPAIR_PROMPT, parse_review_response
from codey.runtime.core import operation_state
from codey.runtime.write import mutation_line
from tests.manual.candidate_validation_and_behavioral_repair_experiment import REUSE_FACTS, VALIDATE_TASK


class AdmittedCandidateCheck:
    name = 'runtime candidate validation'
    location = 'runtime:candidate-validation'

    def __init__(self, check):
        self.steps = iter([{'tool': 'run', 'args': {'command': check.command, 'path': check.cwd}},
                           {'tool': 'done', 'args': {'summary': 'Submit the checked candidate for validation.'}}])

    def new_chat(self, timeout=None):
        pass

    def close(self):
        pass

    def send(self, text, timeout=None):
        return json.dumps(next(self.steps, {}))


def _validate_stopped_candidate(ctx, admitted_restarts):
    result = ctx.result
    policy = ctx.frame.entry_policy
    remaining = ctx.request.max_turns - result.turns
    if (result.stop_reason != 'no_progress' or not ctx.task_changed or ctx.task_session is None
            or not ctx.task_session.edited_files or ctx.state.run_registry.stop_flag.is_set()
            or ctx.writer_attempt_index != 1 or remaining < 2
            or not policy.allows('project.read') or not policy.allows('project.verify')
            or not policy.allows('project.write')):
        return
    check = select_verification_candidate(ctx.verification_candidates, tuple(ctx.task_session.edited_files))
    if check is None:
        return
    previous = result
    admitted_restarts.add((ctx.frame.run_id, ctx.writer_attempt_index + 1))
    commit_runtime_operation(ctx, 'mark_writer_running', lambda mutations, sid, rid:
        mutations.mark_writer_running(sid, rid, provider_id=ctx.frame.provider_id,
                                      writer_attempt=ctx.writer_attempt_index + 1))
    validated = _run_one_writer_attempt(ctx, WriterAttempt(task=VALIDATE_TASK,
        provider=AdmittedCandidateCheck(check), provider_id=ctx.frame.provider_id, remaining_turns=2,
        fresh_chat=False, handoff='', checkpoint=CheckpointView(changed_files=tuple(ctx.task_session.edited_files))),
        lambda turn: None)
    if validated.stop_reason not in {'approval', 'stopped'}:
        commit_runtime_operation(ctx, 'mark_writer_settled', lambda mutations, sid, rid:
            mutations.mark_writer_settled(sid, rid, provider_id=ctx.frame.provider_id,
                                         turns_used=previous.turns + validated.turns,
                                         stop_reason=validated.stop_reason))
    # Preserve the actual result. No synthetic done or borrowed green receipt.
    ctx.result = replace(validated, turns=previous.turns + validated.turns,
                         changed=previous.changed or validated.changed,
                         checks_ran=previous.checks_ran or validated.checks_ran)
    ctx.task_session = validated.facts
    ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
    if validated.stop_reason == 'done':
        ctx.hooks.update_checkpoint(lambda store, item: store.set_status(item, 'ready_for_review'))


def install(monkeypatch):
    original_validate = review_phase.validate_candidate
    original_review = review_phase._run_review_with_trace
    original_repair = enforcement._maybe_run_completion_repair
    original_review_repair = review_phase._repair_writer
    original_behavioral_fact = enforcement._behavioral_failure_fact
    original_restart = mutation_line.state_mark_writer_running
    original_review_send = review_service._send_review_prompt
    original_review_parse = review_service._parse_with_single_repair
    last_review = {}
    admitted_restarts = set()
    format_retried = set()

    def review_send(reviewer, trace, prompt):
        from codey.providers.error_classification import OutputLengthError

        try:
            return original_review_send(reviewer, trace, prompt)
        except review_service.ReviewSendUnknown as exc:
            if not isinstance(exc.__cause__, OutputLengthError):
                raise
            format_retried.add(id(reviewer))
            return original_review_send(reviewer, trace, REVIEW_REPAIR_PROMPT
                + '\nKeep summary under 160 characters; each finding must identify a concrete defect. '
                  'Do not put step-by-step analysis in JSON fields.')

    def review_parse(reviewer, trace, reply, prepared):
        if id(reviewer) in format_retried:
            format_retried.remove(id(reviewer))
            return parse_review_response(reply, changes=prepared.reviewer_view)
        return original_review_parse(reviewer, trace, reply, prepared)

    def restart(state, *, provider_id, writer_attempt=1):
        # The first experiment exposed this durable boundary too: production
        # permits restart only after done. Admit exactly one qualified stopped
        # candidate here, without forging a done fact or bypassing persistence.
        admission = (state.run_id, writer_attempt)
        if admission not in admitted_restarts:
            return original_restart(state, provider_id=provider_id, writer_attempt=writer_attempt)
        admitted_restarts.remove(admission)
        if (state.leaf != operation_state.LEAF_WRITER_SETTLED or state.stop_reason != 'no_progress'
                or state.completion_proof_ref or state.turn_budget - state.turns_used < 2
                or writer_attempt != state.writer_attempt + 1):
            raise operation_state.RuntimeOperationTransitionError('stopped candidate admission is stale')
        return operation_state._transition(state, operation_state.LEAF_WRITER_RUNNING,
                                           provider_id=provider_id, writer_attempt=writer_attempt,
                                           turns_used=0, stop_reason='')

    def observe_review(ctx, **kwargs):
        last_review[id(ctx)] = None
        reviewed = original_review(ctx, **kwargs)
        last_review[id(ctx)] = reviewed
        return reviewed

    def validate(ctx, *, allow_review_repair=True):
        if allow_review_repair:
            _validate_stopped_candidate(ctx, admitted_restarts)
        last_review.pop(id(ctx), None)
        original_validate(ctx, allow_review_repair=allow_review_repair)
        if (ctx.result.stop_reason != 'done' or not ctx.task_changed
                or ctx.writer_attempt_index < 2):
            return
        reviewed = last_review.get(id(ctx))
        review = reviewed[1] if reviewed is not None else None
        if (review is None or review.status != 'complete' or not review.approved
                or not _snapshot_still_current(ctx.project, review)):
            ctx.blocked_reason = 'current_candidate_review_unavailable_or_not_approved'
            ctx.result = replace(ctx.result, stop_reason='blocked', checks_passed=False,
                                 summary='Current candidate requires a fresh approved review.')

    def repair(ctx):
        observation = ctx.behavioral_observation
        if observation is None or observation.status != 'fail':
            return original_repair(ctx)
        original_followup = enforcement.COMPLETION_REPAIR_FOLLOWUP
        followup = (original_followup + '\n\n' + REUSE_FACTS
                    + '\nThe observed values are actual execution results, not guessed expectations.'
                    + '\nRepair only authorized files. Run the original requested verification: '
                    + ctx.request.task
                    + '\nThe runtime will execute the admitted behavioral check again and obtain a new review.'
                    + '\nThe recorded behavioral failure belongs to the previous candidate. After repairing the code '
                    + 'and passing the original verification, call done to submit the repaired candidate. '
                    + 'Submission triggers a new behavioral observation and review; it does not declare success. '
                    + 'Do not repeat a passing suite to refresh the old behavioral observation.'
                    + '\nExisting passing ordinary tests do not satisfy the failed behavioral requirement.')
        followup += ('\nThe ordinary test results do not contain these behavioral inputs. '
                     'Use the behavioral_verification result_ref ' + observation.output_ref
                     + ' for details; the counterexample in the repair context is already actual evidence.')
        with mock.patch.object(enforcement, 'COMPLETION_REPAIR_FOLLOWUP', followup):
            return original_repair(ctx)

    def review_repair(ctx, followup, checkpoint):
        previous = ctx.result.turns
        remaining = ctx.request.max_turns - previous
        if remaining <= 0:
            return replace(ctx.result, stop_reason='max_turns', checks_passed=False)
        native_run = ctx.failover.run

        def bounded_run(**kwargs):
            result = native_run(**{**kwargs, 'turn_budget': min(kwargs['turn_budget'], remaining)})
            return replace(result, turns=previous + result.turns)

        with mock.patch.object(ctx.failover, 'run', bounded_run):
            return original_review_repair(ctx, followup, checkpoint)

    def behavioral_fact(ctx):
        fact = original_behavioral_fact(ctx)
        if fact is None:
            return None
        summary = ctx.behavioral_observation.summary
        try:
            rows = json.loads(summary)
        except json.JSONDecodeError:
            rows = None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get('equal') is False:
            row = {**rows[0], 'required_right_value_given_left': rows[0]['left_value']}
            summary = json.dumps(row, ensure_ascii=False)
        return replace(fact, result_summary=(
            'Actual outputs (not expected outputs): ' + summary
            + '. Required relation: right_value == left_value; deletion inserts no separator.'))

    monkeypatch.setattr(review_phase, '_run_review_with_trace', observe_review)
    monkeypatch.setattr(review_phase, 'validate_candidate', validate)
    monkeypatch.setattr(flow, 'validate_candidate', validate)
    monkeypatch.setattr(enforcement, '_maybe_run_completion_repair', repair)
    monkeypatch.setattr(review_phase, '_repair_writer', review_repair)
    monkeypatch.setattr(enforcement, '_behavioral_failure_fact', behavioral_fact)
    monkeypatch.setattr(mutation_line, 'state_mark_writer_running', restart)
    monkeypatch.setattr(review_service, '_send_review_prompt', review_send)
    monkeypatch.setattr(review_service, '_parse_with_single_repair', review_parse)
