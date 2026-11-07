from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from codey.durable import conversation_ops
from codey.durable.errors import (
    CommitConflict,
)
from codey.durable.model import (
    ApprovalRecord,
    ApprovalStatus,
    CommitResult,
    DurableMutation,
    EffectRecord,
    TaskCheckpoint,
    TaskStatus,
    thaw_json,
)
from codey.durable.operations import SessionOperations, operation_line
from codey.durable.reducer import with_effect_intents, with_effect_settlement
from codey.durable.task_state import release_attempt_state


class ApprovalOperations(SessionOperations):
    """Authorization and effect/task transitions remain atomic durable operations."""

    @operation_line
    def create_approval(self, approval: ApprovalRecord) -> CommitResult:
        _head, revision = self._kernel.store.head(approval.conversation_id)
        return self._kernel._commit(
            DurableMutation(
                conversation_id=approval.conversation_id,
                expected_revision=revision,
                approval=approval,
            )
        )

    @operation_line
    def transition_approval(
        self,
        approval_id: str,
        approval: ApprovalRecord,
        *,
        expected_status: ApprovalStatus | str,
        task: TaskCheckpoint | None = None,
    ) -> CommitResult:
        if approval.approval_id != approval_id:
            raise ValueError("approval identity does not match transition target")
        _head, revision = self._kernel.store.head(approval.conversation_id)
        return self._kernel._commit(
            DurableMutation(
                conversation_id=approval.conversation_id,
                expected_revision=revision,
                task=task,
                approval=approval,
                expected_approval_status=ApprovalStatus(expected_status),
            )
        )

    @operation_line
    def claim_approval_and_begin_effect(
        self,
        approval_id: str,
        effect: EffectRecord,
        *,
        expected_status: ApprovalStatus | str = ApprovalStatus.APPROVED,
    ) -> CommitResult | None:
        """Consume an approval and record its external effect intent together.

        Approval consumption is an authorization fact, while the effect intent
        is the recovery fact that makes the subsequent shell spawn explicit.
        They must share one checkpoint commit: after a crash, recovery sees
        either both facts or neither.  A repeated claim is an idempotent
        no-op only when the same pending effect is already owned by the
        claimed approval.
        """
        current_approval = self._kernel.store.approval(approval_id)
        expected = ApprovalStatus(expected_status)
        current_task = self._kernel.store.task(current_approval.task_id)
        if effect.task_id != current_task.task_id:
            raise CommitConflict("approval effect belongs to another task")
        if effect.category != "shell" or effect.status != "pending" or effect.settlement is not None:
            raise ValueError("approval claim requires a pending shell effect intent")
        if current_approval.status == ApprovalStatus.EXECUTION_CLAIMED:
            existing = self._kernel.store.effect(effect.effect_id)
            if existing != effect:
                raise CommitConflict("approval shell effect identity changed")
            return None
        if current_approval.status != expected:
            raise CommitConflict("approval status changed")
        next_task = with_effect_intents(current_task, (effect,))
        claimed = replace(
            current_approval,
            status=ApprovalStatus.EXECUTION_CLAIMED,
            revision=current_approval.revision + 1,
        )
        _head, revision = self._kernel.store.head(current_task.conversation_id)
        return self._kernel._commit(
            DurableMutation(
                conversation_id=current_task.conversation_id,
                expected_revision=revision,
                task=next_task,
                effects=(effect,),
                approval=claimed,
                expected_approval_status=expected,
            )
        )

    @operation_line
    def settle_approval(
        self,
        approval_id: str,
        *,
        settlement: Mapping[str, object],
        resume: bool,
        tool_results: tuple[Mapping[str, object], ...] = (),
    ) -> CommitResult | None:
        """Settle an approval and wake the original task atomically.

        Approval continuation is a state transition of the task that emitted
        the shell call. It never creates a second task or conversation input.
        The optional tool result is appended beside the effect settlement and
        the scheduler wakeup, so a crash cannot leave only one side visible.
        """
        current_approval = self._kernel.store.approval(approval_id)
        current_task = self._kernel.store.task(current_approval.task_id)
        continuation_id = current_task.task_id if resume else current_approval.continuation_run_id
        if current_approval.continuation_status in {"started", "cancelled"}:
            return None

        effect_settlement: EffectRecord | None = None
        next_task = current_task
        if current_approval.status == ApprovalStatus.EXECUTION_CLAIMED:
            effect = self._kernel.store.effect(f"shell-{approval_id}")
            if effect.settlement is not None:
                return None
            if effect.status == "pending" and not settlement:
                raise CommitConflict("shell settlement payload is required")
            effect_settlement = replace(effect, status="ok", settlement=dict(settlement))
            next_task = with_effect_settlement(current_task, effect_settlement)
            next_status = ApprovalStatus.EXECUTION_SETTLED
        elif current_approval.status == ApprovalStatus.DENIED:
            next_status = ApprovalStatus.DENIED
            next_task = replace(next_task, revision=next_task.revision + 1)
        else:
            raise CommitConflict("shell approval is not settled or denied")

        entries = list(conversation_ops.append_tool_results(
            self._kernel,
            current_task.conversation_id,
            self._kernel.store.head(current_task.conversation_id)[0],
            tool_results,
            by_task_id=current_task.task_id,
        )) if tool_results else []
        state = thaw_json(next_task.state)
        if not isinstance(state, dict):
            raise CommitConflict("task state is corrupt")
        if resume:
            state["resume_input"] = {"kind": "resolve_shell_approval", "approval_id": approval_id}
            state.pop("resume_entry_id", None)
            # Keep the single checkpoint revision consumed by the effect
            # settlement. The wakeup is part of that same transition.
            state = release_attempt_state(state)
            next_task = replace(next_task, state=state, status=TaskStatus.PENDING)
        updated_approval = replace(
            current_approval,
            status=next_status,
            revision=current_approval.revision + 1,
            settlement=dict(settlement),
            continuation_status="started" if resume else "not_required",
            continuation_run_id=continuation_id,
        )
        _head, revision = self._kernel.store.head(current_task.conversation_id)
        return self._kernel._commit(DurableMutation(
            conversation_id=current_task.conversation_id,
            expected_revision=revision,
            entries=tuple(entries),
            task=next_task,
            settlements=(effect_settlement,) if effect_settlement is not None else (),
            approval=updated_approval,
            expected_approval_status=current_approval.status,
            expected_attempt_token=current_task.attempt_token,
        ))
