from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from codey.durable.approval_operations import ApprovalOperations
from codey.durable.context import ContextSnapshot
from codey.durable.contracts import ApprovalPort, DurableExecutionPort, EffectPort, TaskAdmissionPort
from codey.durable.conversation_operations import ConversationOperations
from codey.durable.conversation_queries import ConversationQueryFacade
from codey.durable.effect_operations import EffectOperations
from codey.durable.inbox import (
    InboxDoc,
    InboxHistoryEvent,
)
from codey.durable.model import (
    ApprovalRecord,
    ApprovalStatus,
    ArtifactRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionPlacement,
    SubmissionRecord,
    SubmissionStatus,
    TaskCheckpoint,
    TaskStatus,
)
from codey.durable.session_kernel import SessionKernel, _mutation_line
from codey.durable.store import DurableStore
from codey.durable.submission_operations import SubmissionOperations
from codey.durable.task_operations import TaskLifecycleOperations
from codey.durable.task_queries import TaskQueryFacade
from codey.durable.timeline import TaskSnapshot, TaskTransition
from codey.durable.workflow import WorkflowDefinition


class DurableSession(SessionKernel, TaskAdmissionPort, ApprovalPort, EffectPort, DurableExecutionPort):
    """Owns durable conversation admission and reads for one application state directory."""

    def conversation_ids(self) -> tuple[str, ...]:
        return self.conversation_queries.conversation_ids()

    @_mutation_line
    def submission(self, submission_id: str) -> SubmissionRecord:
        return self.store.submission(submission_id)

    def __init__(
        self,
        store: DurableStore,
        *,
        on_commit: Callable[[CommitResult, DurableMutation], None] | None = None,
    ) -> None:
        super().__init__(store, on_commit=on_commit)
        self.conversation_queries = ConversationQueryFacade(self, self._query_line)
        self.task_queries = TaskQueryFacade(self.store, self._query_line)
        self.conversation_operations = ConversationOperations(self)
        self.submission_operations = SubmissionOperations(self)
        self.task_operations = TaskLifecycleOperations(self)
        self.approval_operations = ApprovalOperations(self)
        self.effect_operations = EffectOperations(self)

    def create_conversation(self, conversation_id: str) -> CommitResult:
        return self.conversation_operations.create_conversation(conversation_id)

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        return self.conversation_queries.head(conversation_id)

    def has_conversation(self, conversation_id: str) -> bool:
        return self.conversation_queries.has_conversation(conversation_id)

    def record_display_message(self, conversation_id: str, *, run_id: str, identity: str,
                               payload: Mapping[str, object]) -> None:
        return self.conversation_operations.record_display_message(conversation_id, run_id=run_id, identity=identity, payload=payload)

    def navigation_page(self, conversation_id: str, *, before: str | None = None,
                        limit: int = 200) -> dict[str, object]:
        return self.conversation_queries.navigation_page(conversation_id, before=before, limit=limit)

    def history_window(self, conversation_id: str, entry_id: str, *, limit: int = 100,
                       leaf_id: str | None = None) -> dict[str, object]:
        return self.conversation_queries.history_window(
            conversation_id, entry_id, limit=limit, leaf_id=leaf_id,
        )

    def conversation_tree(self, conversation_id: str) -> dict[str, object]:
        return self.conversation_queries.conversation_tree(conversation_id)

    def conversation_lineage(self, conversation_id: str) -> list[dict[str, object]]:
        return self.conversation_queries.conversation_lineage(conversation_id)

    def select_idle_branch(self, conversation_id: str, entry_id: str, *, expected_revision: int) -> CommitResult:
        return self.conversation_operations.select_idle_branch(conversation_id, entry_id, expected_revision=expected_revision)

    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]:
        """Read schedulable checkpoints through the session mutation line."""
        return self.task_queries.pending_tasks()

    def queued_submissions(self, conversation_id: str | None = None) -> tuple[SubmissionRecord, ...]:
        return self.conversation_queries.queued_submissions(conversation_id)

    def inbox_doc(self, conversation_id: str) -> InboxDoc:
        return self.conversation_queries.inbox_doc(conversation_id)

    def inbox_history(self, conversation_id: str) -> tuple[InboxHistoryEvent, ...]:
        return self.conversation_queries.inbox_history(conversation_id)

    def withdraw_queued_submission_at(
        self,
        conversation_id: str,
        position: int,
        *,
        expected_submission_id: str | None = None,
    ) -> CommitResult:
        return self.submission_operations.withdraw_queued_submission_at(conversation_id, position, expected_submission_id=expected_submission_id)

    def tasks(self) -> tuple[TaskCheckpoint, ...]:
        """Read every checkpoint for projections that need terminal history."""
        return self.task_queries.tasks()

    def revise_queued_input(
        self, task_id: str, *, expected_revision: int, text: str | None = None,
        mode: str | None = None,
    ) -> CommitResult:
        return self.submission_operations.revise_queued_input(task_id, expected_revision=expected_revision, text=text, mode=mode)

    def transform_task_state(
        self, task_id: str,
        transform: Callable[[TaskCheckpoint], Mapping[str, object] | None],
    ) -> CommitResult | None:
        return self.task_operations.transform_task_state(task_id, transform)

    def update_task_state(
        self,
        task_id: str,
        state: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> CommitResult:
        return self.task_operations.update_task_state(task_id, state, expected_revision=expected_revision)

    def admit_input(
        self,
        *,
        conversation_id: str,
        request_id: str,
        task_id: str,
        text: str,
        provider_snapshot: Mapping[str, object],
        task_state: Mapping[str, object] | None = None,
        initial_phase: str = "accepted",
        tool_results: tuple[Mapping[str, object], ...] = (),
        owner_task_id: str | None = None,
        abandon_on_restart: bool = False,
        workflow_name: str | None = None,
        workflow_version: int = 1,
        workflow_input: Mapping[str, object] | None = None,
        workflow_checkpoint: Mapping[str, object] | None = None,
        workflow_replay_contract: str = "deterministic-v1",
        placement: SubmissionPlacement | str = SubmissionPlacement.PLACED,
        mode: str = "follow_up",
    ) -> CommitResult:
        return self.submission_operations.admit_input(conversation_id=conversation_id, request_id=request_id, task_id=task_id, text=text, provider_snapshot=provider_snapshot, task_state=task_state, initial_phase=initial_phase, tool_results=tool_results, owner_task_id=owner_task_id, abandon_on_restart=abandon_on_restart, workflow_name=workflow_name, workflow_version=workflow_version, workflow_input=workflow_input, workflow_checkpoint=workflow_checkpoint, workflow_replay_contract=workflow_replay_contract, placement=placement, mode=mode)

    def admit_write(
        self,
        *,
        conversation_id: str,
        request_id: str,
        kind: str,
        data: Mapping[str, object],
        display_only: bool = True,
        model: Mapping[str, object] | None = None,
        edits: Sequence[Mapping[str, object]] | None = None,
        head: str | None = None,
        placement: SubmissionPlacement | str = SubmissionPlacement.PLACED,
        expected_revision: int | None = None,
        expected_head: str | None = None,
    ) -> CommitResult:
        return self.submission_operations.admit_write(conversation_id=conversation_id, request_id=request_id, kind=kind, data=data, display_only=display_only, model=model, edits=edits, head=head, placement=placement, expected_revision=expected_revision, expected_head=expected_head)

    def place_submission(self, submission_id: str) -> CommitResult:
        return self.submission_operations.place_submission(submission_id)

    def place_boundary_inputs(
        self,
        conversation_id: str,
        *,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[SubmissionRecord, ...]:
        return self.submission_operations.place_boundary_inputs(conversation_id, at=at, steering_mode=steering_mode, follow_up_mode=follow_up_mode)

    def commit_generation_boundary(
        self,
        conversation_id: str,
        *,
        task_id: str,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[SubmissionRecord, ...]:
        return self.submission_operations.commit_generation_boundary(conversation_id, task_id=task_id, at=at, steering_mode=steering_mode, follow_up_mode=follow_up_mode)

    def merge_generation_boundary(
        self,
        mutation: DurableMutation,
        *,
        task_id: str,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[tuple[SubmissionRecord, ...], DurableMutation]:
        return self.submission_operations.merge_generation_boundary(mutation, task_id=task_id, at=at, steering_mode=steering_mode, follow_up_mode=follow_up_mode)


    def reconcile_workflows(self, definitions: Mapping[str, WorkflowDefinition]) -> tuple[str, ...]:
        return self.task_operations.reconcile_workflows(definitions)

    def fork_task(
        self, source_task_id: str, commit_seq: int, *, conversation_id: str,
        task_id: str, request_id: str, instruction: str,
    ) -> CommitResult:
        return self.submission_operations.fork_task(source_task_id, commit_seq, conversation_id=conversation_id, task_id=task_id, request_id=request_id, instruction=instruction)

    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]:
        return self.conversation_queries.entries(conversation_id)

    def history_page(
        self,
        conversation_id: str,
        *,
        before: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        return self.conversation_queries.history_page(conversation_id, before=before, limit=limit)

    def search_history(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return self.conversation_queries.search_history(conversation_id, query, limit=limit)

    def task(self, task_id: str) -> TaskCheckpoint:
        return self.task_queries.task(task_id)

    def task_timeline(self, task_id: str) -> tuple[TaskTransition, ...]:
        return self.task_queries.task_timeline(task_id)

    def task_at(self, task_id: str, commit_seq: int) -> TaskSnapshot:
        return self.task_queries.task_at(task_id, commit_seq)

    def effects(self, task_id: str) -> tuple[EffectRecord, ...]:
        return self.task_queries.effects(task_id)

    def artifact(self, handle: str) -> ArtifactRecord:
        return self.task_queries.artifact(handle)

    def artifacts_for_task(self, task_id: str) -> tuple[ArtifactRecord, ...]:
        return self.task_queries.artifacts_for_task(task_id)

    def artifacts_for_effect(self, effect_id: str) -> tuple[ArtifactRecord, ...]:
        return self.task_queries.artifacts_for_effect(effect_id)

    def register_artifacts(self, task_id: str, artifacts: tuple[ArtifactRecord, ...]) -> CommitResult:
        return self.effect_operations.register_artifacts(task_id, artifacts)

    def task_facts(self, task_id: str) -> tuple[TaskCheckpoint, tuple[EffectRecord, ...]]:
        """Read a checkpoint and its effects without an intervening commit."""
        return self.task_queries.task_facts(task_id)

    def effect(self, effect_id: str) -> EffectRecord:
        return self.task_queries.effect(effect_id)

    def approval(self, approval_id: str) -> ApprovalRecord:
        return self.task_queries.approval(approval_id)

    def create_approval(self, approval: ApprovalRecord) -> CommitResult:
        return self.approval_operations.create_approval(approval)

    def transition_approval(
        self,
        approval_id: str,
        approval: ApprovalRecord,
        *,
        expected_status: ApprovalStatus | str,
        task: TaskCheckpoint | None = None,
    ) -> CommitResult:
        return self.approval_operations.transition_approval(approval_id, approval, expected_status=expected_status, task=task)

    def claim_approval_and_begin_effect(
        self,
        approval_id: str,
        effect: EffectRecord,
        *,
        expected_status: ApprovalStatus | str = ApprovalStatus.APPROVED,
    ) -> CommitResult | None:
        return self.approval_operations.claim_approval_and_begin_effect(approval_id, effect, expected_status=expected_status)

    def settle_approval(
        self,
        approval_id: str,
        *,
        settlement: Mapping[str, object],
        resume: bool,
        tool_results: tuple[Mapping[str, object], ...] = (),
    ) -> CommitResult | None:
        return self.approval_operations.settle_approval(approval_id, settlement=settlement, resume=resume, tool_results=tool_results)

    def record_task_policy(self, task_id: str, policy: Mapping[str, object]) -> CommitResult:
        return self.task_operations.record_task_policy(task_id, policy)

    def record_reviewer_selection(
        self, task_id: str, selection: Mapping[str, object]
    ) -> CommitResult:
        return self.task_operations.record_reviewer_selection(task_id, selection)

    def advance_task(
        self,
        *,
        task_id: str,
        phase: str,
        state: Mapping[str, object],
        answer: str | None = None,
        submission_status: SubmissionStatus | None = None,
        reason: str | None = None,
        task_status: TaskStatus | str | None = None,
    ) -> CommitResult:
        return self.task_operations.advance_task(task_id=task_id, phase=phase, state=state, answer=answer, submission_status=submission_status, reason=reason, task_status=task_status)

    def start_task(self, task_id: str, *, state: Mapping[str, object]) -> CommitResult:
        return self.task_operations.start_task(task_id, state=state)


    def request_cancel(
        self, task_id: str, *, reason: str = "cancelled",
        state_update: Mapping[str, object] | None = None, expected_revision: int | None = None,
    ) -> CommitResult:
        return self.task_operations.request_cancel(task_id, reason=reason, state_update=state_update, expected_revision=expected_revision)

    def cancel_task(self, task_id: str, *, reason: str = "cancelled") -> CommitResult:
        return self.task_operations.cancel_task(task_id, reason=reason)

    def claim_task(self, task_id: str) -> CommitResult:
        return self.task_operations.claim_task(task_id)

    def recover_scheduler_state(self, *, force: bool = False) -> None:
        return self.task_operations.recover_scheduler_state(force=force)

    def unclaim_task(self, task_id: str) -> CommitResult:
        return self.task_operations.unclaim_task(task_id)

    def wait_task(
        self,
        task_id: str,
        *,
        state: Mapping[str, object] | None = None,
        on: tuple[str, ...] | None = None,
        join_policy: str = "allSettled",
    ) -> CommitResult:
        return self.task_operations.wait_task(task_id, state=state, on=on, join_policy=join_policy)

    def finish_task(self, task_id: str, answer: str, *, state: Mapping[str, object]) -> CommitResult:
        return self.task_operations.finish_task(task_id, answer, state=state)

    def complete_task(self, task_id: str, answer: str, *, state: Mapping[str, object]) -> CommitResult:
        return self.task_operations.complete_task(task_id, answer, state=state)

    def reconcile_waiting_tasks(self, task_id: str | None = None) -> None:
        return self.task_operations.reconcile_waiting_tasks(task_id)

    def reconcile_finalization(self, task_ids: tuple[str, ...] | None = None) -> None:
        return self.task_operations.reconcile_finalization(task_ids)

    def fail_task(
        self,
        task_id: str,
        reason: str,
        *,
        state: Mapping[str, object],
        expected_attempt_token: str | None = None,
    ) -> CommitResult:
        return self.task_operations.fail_task(task_id, reason, state=state, expected_attempt_token=expected_attempt_token)

    def record_effect_intent(self, task_id: str, effect: EffectRecord) -> CommitResult:
        return self.effect_operations.record_effect_intent(task_id, effect)

    def record_effect_intents(
        self, task_id: str, effects: tuple[EffectRecord, ...]
    ) -> CommitResult:
        return self.effect_operations.record_effect_intents(task_id, effects)

    def settle_effect(
        self,
        task_id: str,
        effect: EffectRecord,
        *,
        expected_status: str = "pending",
        artifacts: tuple[ArtifactRecord, ...] = (),
    ) -> CommitResult:
        return self.effect_operations.settle_effect(task_id, effect, expected_status=expected_status, artifacts=artifacts)

    def settle_unknown_effect(self, task_id: str, effect_id: str) -> CommitResult:
        return self.effect_operations.settle_unknown_effect(task_id, effect_id)

    def append_message(
        self,
        conversation_id: str,
        kind: str,
        payload: Mapping[str, object],
    ) -> EntryDraft:
        return self.conversation_operations.append_message(conversation_id, kind, payload)

    def branch(
        self,
        conversation_id: str,
        entry_id: str,
        *,
        expected_revision: int | None = None,
    ) -> CommitResult:
        return self.conversation_operations.branch(conversation_id, entry_id, expected_revision=expected_revision)

    def fork(self, conversation_id: str, entry_id: str, *, child_conversation_id: str | None = None) -> str:
        return self.conversation_operations.fork(conversation_id, entry_id, child_conversation_id=child_conversation_id)

    def delete_conversation(self, conversation_id: str) -> CommitResult:
        return self.conversation_operations.delete_conversation(conversation_id)

    def context(self, conversation_id: str, *, leaf_id: str | None = None) -> tuple[EntryDraft, ...]:
        """Return one immutable branch path, including fork ancestors.

        ``leaf_id`` is deliberately scoped to the child conversation.  A
        caller can inspect an old branch without changing the active head;
        the parent conversation is always read only through its recorded fork
        cap.
        """
        return self.conversation_queries.context(conversation_id, leaf_id=leaf_id)

    def context_at(
        self, conversation_id: str, commit_seq: int, *, leaf_id: str | None = None,
    ) -> tuple[EntryDraft, ...]:
        return self.conversation_queries.context_at(conversation_id, commit_seq, leaf_id=leaf_id)

    def context_snapshot(
        self,
        conversation_id: str,
        *,
        token_budget: int,
        leaf_id: str | None = None,
        before_entry_id: str | None = None,
        token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
    ) -> ContextSnapshot:
        return self.conversation_queries.context_snapshot(
            conversation_id, token_budget=token_budget, leaf_id=leaf_id,
            before_entry_id=before_entry_id, token_counter=token_counter,
        )

    def context_snapshot_at(
        self,
        conversation_id: str,
        commit_seq: int,
        *,
        token_budget: int,
        leaf_id: str | None = None,
        token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
    ) -> ContextSnapshot:
        return self.conversation_queries.context_snapshot_at(
            conversation_id, commit_seq, token_budget=token_budget,
            leaf_id=leaf_id, token_counter=token_counter,
        )

    def save_context_snapshot(
        self, conversation_id: str, payload: Mapping[str, object]
    ) -> EntryDraft:
        return self.conversation_operations.save_context_snapshot(conversation_id, payload)

    def latest_context_snapshot(
        self, conversation_id: str
    ) -> EntryDraft | None:
        return self.conversation_queries.latest_context_snapshot(conversation_id)
