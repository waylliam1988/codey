from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from codey.durable import admission, conversation_ops
from codey.durable.approval_operations import ApprovalOperations
from codey.durable.context import ContextSnapshot
from codey.durable.contracts import ApprovalPort, DurableExecutionPort, EffectPort, TaskAdmissionPort
from codey.durable.conversation_queries import ConversationQueryFacade
from codey.durable.effect_operations import EffectOperations
from codey.durable.errors import (
    CommitConflict,
)
from codey.durable.inbox import (
    InboxDoc,
    InboxHistoryEvent,
)
from codey.durable.model import (
    ApprovalRecord,
    ApprovalStatus,
    ArtifactRecord,
    CommitResult,
    CreateConversation,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionPlacement,
    SubmissionRecord,
    SubmissionStatus,
    SubmissionUpdate,
    TaskCheckpoint,
    TaskStatus,
    thaw_json,
)
from codey.durable.session_kernel import SessionKernel, _mutation_line
from codey.durable.store import DurableStore
from codey.durable.task_operations import TaskLifecycleOperations
from codey.durable.task_queries import TaskQueryFacade
from codey.durable.timeline import TaskSnapshot, TaskTransition
from codey.durable.workflow import WorkflowDefinition


@dataclass(frozen=True)
class _BoundaryPlacement:
    entry: EntryDraft | None
    task: TaskCheckpoint | None
    update: SubmissionUpdate


def _release_attempt_state(state: Mapping[str, object]) -> dict[str, object]:
    """Clear volatile scheduler ownership while preserving logical state."""
    released = dict(state)
    released["attempt_token"] = ""
    released["attempt_owner"] = ""
    released["attempt_process_id"] = 0
    return released


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
        self.task_operations = TaskLifecycleOperations(self)
        self.approval_operations = ApprovalOperations(self)
        self.effect_operations = EffectOperations(self)

    @_mutation_line
    def create_conversation(self, conversation_id: str) -> CommitResult:
        if self.store.has_conversation(conversation_id):
            return CommitResult(self.store.head(conversation_id)[1], self.store.commit_sequence())
        return self._commit(DurableMutation(
            conversation_id=conversation_id,
            expected_revision=0,
            create_conversation=CreateConversation(),
        ))

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        return self.conversation_queries.head(conversation_id)

    def has_conversation(self, conversation_id: str) -> bool:
        return self.conversation_queries.has_conversation(conversation_id)

    @_mutation_line
    def record_display_message(self, conversation_id: str, *, run_id: str, identity: str,
                               payload: Mapping[str, object]) -> None:
        """Idempotent presentation history; it never enters provider context."""
        entry_id = 'display-' + hashlib.sha256(f'{len(run_id)}:{run_id}{identity}'.encode()).hexdigest()
        if any(entry.entry_id == entry_id for entry in self.store.entries(conversation_id)):
            return
        # Display observations from a removed turn remain in the execution
        # audit, but must never be appended onto a replacement conversation.
        try:
            owner = self.store.task(run_id)
        except KeyError:
            owner = None
        if owner is not None and owner.state.get("input_entry_id") and owner.state["input_entry_id"] not in {
            entry.entry_id for entry in self.context(conversation_id)
        }:
            return
        head, revision = self.store.head(conversation_id)
        entry = EntryDraft(entry_id=entry_id, conversation_id=conversation_id, parent_id=head,
                           kind='display_message', by_task_id=run_id,
                           payload={**dict(payload), 'display_only': True})
        self._commit(DurableMutation(conversation_id, revision, entries=(entry,)))

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

    @_mutation_line
    def select_idle_branch(self, conversation_id: str, entry_id: str, *, expected_revision: int) -> CommitResult:
        if any(task.conversation_id == conversation_id for task in self.store.pending_tasks()):
            raise CommitConflict('Wait for this conversation to finish before switching branches.')
        return conversation_ops.branch(self, conversation_id, entry_id, expected_revision=expected_revision)

    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]:
        """Read schedulable checkpoints through the session mutation line."""
        return self.task_queries.pending_tasks()

    def queued_submissions(self, conversation_id: str | None = None) -> tuple[SubmissionRecord, ...]:
        return self.conversation_queries.queued_submissions(conversation_id)

    def inbox_doc(self, conversation_id: str) -> InboxDoc:
        return self.conversation_queries.inbox_doc(conversation_id)

    def inbox_history(self, conversation_id: str) -> tuple[InboxHistoryEvent, ...]:
        return self.conversation_queries.inbox_history(conversation_id)

    @_mutation_line
    def withdraw_queued_submission_at(
        self,
        conversation_id: str,
        position: int,
        *,
        expected_submission_id: str | None = None,
    ) -> CommitResult:
        """Withdraw one queued inbox item by its durable conversation position."""
        if type(position) is not int or position < 0:
            raise ValueError("queued submission position must be a non-negative integer")
        queued = self.store.queued_submissions(conversation_id)
        if position >= len(queued):
            raise IndexError("queued submission position is out of range")
        submission = queued[position]
        if expected_submission_id is not None and submission.submission_id != expected_submission_id:
            raise CommitConflict("queued submission position changed")
        if submission.mode != "write":
            task = next(
                (task for task in self.store.tasks() if task.submission_id == submission.submission_id),
                None,
            )
            if task is None:
                raise CommitConflict("queued input has no task checkpoint")
            if task.status is not TaskStatus.PENDING:
                raise CommitConflict("queued input is no longer pending")
            _head, revision = self.store.head(conversation_id)
            pending = tuple(
                effect for effect in self.store.effects_for_task(task.task_id)
                if effect.settlement is None
            )
            if pending:
                raise CommitConflict("queued input has unsettled effects")
            next_task = replace(
                task,
                phase="cancelled",
                revision=task.revision + 1,
                state={**_release_attempt_state(task.state), "cancel_requested": True, "cancel_reason": "aborted",
                       "pending_effect_ids": []},
                pending_effect_id=None,
                status=TaskStatus.CANCELLED,
            )
            return self._commit(DurableMutation(
                conversation_id=conversation_id,
                expected_revision=revision,
                task=next_task,
                submission_update=SubmissionUpdate(
                    submission_id=submission.submission_id,
                    conversation_id=conversation_id,
                    expected_status=SubmissionStatus.QUEUED,
                    status=SubmissionStatus.UNANSWERED,
                    reason="aborted",
                ),
            ))
        _head, revision = self.store.head(conversation_id)
        return self._commit(DurableMutation(
            conversation_id=conversation_id,
            expected_revision=revision,
            submission_update=SubmissionUpdate(
                submission_id=submission.submission_id,
                conversation_id=conversation_id,
                expected_status=SubmissionStatus.QUEUED,
                status=SubmissionStatus.UNANSWERED,
                reason="aborted",
            ),
        ))

    def tasks(self) -> tuple[TaskCheckpoint, ...]:
        """Read every checkpoint for projections that need terminal history."""
        return self.task_queries.tasks()

    @_mutation_line
    def revise_queued_input(
        self, task_id: str, *, expected_revision: int, text: str | None = None,
        mode: str | None = None,
    ) -> CommitResult:
        """Revise unplaced input and its task projection in one fenced commit."""
        current = self.store.task(task_id)
        if current.revision != expected_revision:
            raise CommitConflict("input revision conflict")
        submission = self.store.submission(current.submission_id)
        if (current.status is not TaskStatus.PENDING or current.phase != "accepted"
                or current.state.get("input_state") != "queued" or current.attempt_token
                or submission.placement is not SubmissionPlacement.QUEUED
                or submission.status is not SubmissionStatus.QUEUED):
            raise CommitConflict("input is already placed or settled")
        if text is not None and (not isinstance(text, str) or not 0 < len(text.strip()) <= 64_000):
            raise ValueError("invalid input text")
        if mode is not None and mode not in {"steer", "follow_up"}:
            raise ValueError("invalid input mode")
        state = dict(current.state)
        request = state.get("input_request")
        if not isinstance(request, Mapping):
            raise CommitConflict("queued input request unavailable")
        content = dict(submission.content or {})
        if text is not None:
            content["text"] = state["input_text"] = state["task"] = text
        if mode is not None:
            state["input_mode"] = mode
        _head, revision = self.store.head(current.conversation_id)
        return self._commit(DurableMutation(
            conversation_id=current.conversation_id, expected_revision=revision,
            task=replace(current, revision=current.revision + 1, state=state),
            submission_update=SubmissionUpdate(
                submission_id=submission.submission_id, conversation_id=current.conversation_id,
                expected_status=submission.status, status=submission.status,
                content=content if text is not None else None, mode=mode,
            ),
        ))

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

    @_mutation_line
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
        return admission.admit_input(
            self, conversation_id=conversation_id, request_id=request_id, task_id=task_id,
            text=text, provider_snapshot=provider_snapshot, task_state=task_state,
            initial_phase=initial_phase, tool_results=tool_results,
            owner_task_id=owner_task_id, abandon_on_restart=abandon_on_restart,
            workflow_name=workflow_name, workflow_version=workflow_version,
            workflow_input=workflow_input, workflow_checkpoint=workflow_checkpoint,
            workflow_replay_contract=workflow_replay_contract,
            placement=placement, mode=mode,
        )

    @_mutation_line
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
        return admission.admit_write(
            self,
            conversation_id=conversation_id,
            request_id=request_id,
            kind=kind,
            data=data,
            display_only=display_only,
            model=model,
            edits=edits,
            head=head,
            placement=placement,
            expected_revision=expected_revision,
            expected_head=expected_head,
        )

    @_mutation_line
    def place_submission(self, submission_id: str) -> CommitResult:
        """Place one queued input into history and make its task runnable atomically."""
        submission = self.store.submission(submission_id)
        if submission.placement is SubmissionPlacement.PLACED:
            return CommitResult(0, self.store.commit_sequence(), submission_id)
        if submission.status is not SubmissionStatus.QUEUED:
            raise CommitConflict("only queued submissions may be placed")
        if submission.mode == "write":
            return self._place_write_submission(submission)
        task = next(
            (candidate for candidate in self.store.tasks() if candidate.submission_id == submission_id),
            None,
        )
        if task is None:
            raise CommitConflict("queued submission has no task")
        if task.status in {TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED}:
            raise CommitConflict("cannot place a terminal task")
        content = submission.content
        text = content.get("text") if isinstance(content, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise CommitConflict("queued submission has no input text")
        head, conversation_revision = self.store.head(task.conversation_id)
        entry = EntryDraft(
            entry_id=f"entry-{uuid.uuid4().hex}",
            conversation_id=task.conversation_id,
            parent_id=head,
            kind="user_message",
            payload={"text": text, **admission.message_metadata(content)},
            by_task_id=task.task_id,
        )
        thawed_state = thaw_json(task.state)
        if not isinstance(thawed_state, Mapping):
            raise CommitConflict("task state is corrupt")
        state = dict(thawed_state)
        state["input_state"] = "placed"
        state["input_entry_id"] = entry.entry_id
        next_task = replace(task, revision=task.revision + 1, state=state)
        update = SubmissionUpdate(
            submission_id=submission.submission_id,
            conversation_id=submission.conversation_id,
            expected_status=submission.status,
            status=submission.status,
            input_entry_id=entry.entry_id,
            placement=SubmissionPlacement.PLACED,
        )
        return self._commit(
            DurableMutation(
                conversation_id=task.conversation_id,
                expected_revision=conversation_revision,
                entries=(entry,),
                task=next_task,
                submission_update=update,
                expected_attempt_token=task.attempt_token,
            )
        )

    @_mutation_line
    def place_boundary_inputs(
        self,
        conversation_id: str,
        *,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[SubmissionRecord, ...]:
        """Place the inbox items selected by one generation boundary.

        ``post_tools`` admits passive writes and steering inputs.  Follow-ups
        wait for ``final`` unless a queued reset write forces that boundary.
        Selection is derived from durable submission order; each selected item
        is then placed through the existing atomic submission mutation.  The
        method is deliberately narrow so the provider boundary has one named
        entry point without introducing a generic repository layer.
        """
        selected, mutation = self._prepare_boundary_mutation(
            conversation_id, at=at, steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
        )
        if not selected:
            return ()
        self._commit(mutation)
        return tuple(self.store.submission(item.submission_id) for item in selected)

    @_mutation_line
    def commit_generation_boundary(
        self,
        conversation_id: str,
        *,
        task_id: str,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[SubmissionRecord, ...]:
        """Commit boundary placement and the active task checkpoint together."""
        active = self.store.task(task_id)
        if active.conversation_id != conversation_id:
            raise CommitConflict("generation boundary task conversation mismatch")
        if active.status in {TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED}:
            raise CommitConflict("cannot advance a terminal generation task")
        selected, mutation = self._prepare_boundary_mutation(
            conversation_id, at=at, steering_mode=steering_mode,
            follow_up_mode=follow_up_mode, task_id=task_id,
        )
        # The active task owns this boundary and must travel as the mutation's
        # task checkpoint.  A plain task update cannot carry the bound
        # execution attempt token, so committing it while the attempt is
        # running fails the execution-attempt fence.
        boundary_task = next(
            (update for update in mutation.task_updates if update.task_id == task_id),
            None,
        )
        if boundary_task is None:
            raise CommitConflict("generation boundary mutation lost the active task")
        mutation = replace(
            mutation,
            task=boundary_task,
            task_updates=tuple(
                update for update in mutation.task_updates if update.task_id != task_id
            ),
        )
        self._commit(mutation)
        return tuple(self.store.submission(item.submission_id) for item in selected)

    @_mutation_line
    def merge_generation_boundary(
        self,
        mutation: DurableMutation,
        *,
        task_id: str,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[tuple[SubmissionRecord, ...], DurableMutation]:
        """Fold boundary placement into an already derived task mutation.

        Runtime provider settlement and generation placement must share one
        durable commit.  The runtime reducer supplies the task/effect rows;
        this method appends the boundary rows and advances that same task
        checkpoint before the caller submits the mutation.
        """
        if mutation.task is None or mutation.task.task_id != task_id:
            raise CommitConflict("generation boundary mutation must own the active task")
        selected, boundary = self._prepare_boundary_mutation(
            mutation.conversation_id,
            at=at,
            steering_mode=steering_mode,
            follow_up_mode=follow_up_mode,
            task_id=task_id,
        )
        if boundary.expected_revision != mutation.expected_revision:
            raise CommitConflict("generation boundary conversation revision changed")
        base_head = mutation.entries[-1].entry_id if mutation.entries else self.store.head(
            mutation.conversation_id
        )[0]
        rebased_entries: list[EntryDraft] = []
        for entry in boundary.entries:
            rebased = replace(entry, parent_id=base_head)
            rebased_entries.append(rebased)
            base_head = rebased.entry_id

        active_state = thaw_json(mutation.task.state)
        if not isinstance(active_state, Mapping):
            raise CommitConflict("generation boundary task state is corrupt")
        next_active = replace(
            mutation.task,
            state={**dict(active_state), "generation_boundary": at,
                   "generation_boundary_receipt": {"at": at,
                       "provider_effect_ids": [effect.effect_id for effect in mutation.settlements
                                               if effect.category == "provider_send"],
                       "submission_ids": [item.submission_id for item in selected],
                       "conversation_revision": mutation.expected_revision + len(mutation.entries) + len(rebased_entries)}},
        )
        boundary_updates = tuple(
            update for update in boundary.task_updates if update.task_id != task_id
        )
        merged = replace(
            mutation,
            entries=mutation.entries + tuple(rebased_entries),
            task=next_active,
            task_updates=mutation.task_updates + boundary_updates,
            submission_updates=mutation.submission_updates + boundary.submission_updates,
        )
        return tuple(selected), merged

    def _prepare_boundary_mutation(
        self,
        conversation_id: str,
        *,
        at: str,
        steering_mode: str,
        follow_up_mode: str,
        task_id: str | None = None,
    ) -> tuple[tuple[SubmissionRecord, ...], DurableMutation]:
        """Build a boundary mutation without publishing it."""
        if at not in {"post_tools", "approval", "final", "settle"}:
            raise ValueError("unknown generation boundary")
        if steering_mode not in {"all", "one-at-a-time"}:
            raise ValueError("invalid steering queue mode")
        if follow_up_mode not in {"all", "one-at-a-time"}:
            raise ValueError("invalid follow-up queue mode")
        queued = list(self.store.queued_submissions(conversation_id))
        reset = any(
            item.mode == "write"
            and isinstance(item.content, Mapping)
            and item.content.get("head") == "self"
            for item in queued
        )
        final = at == "final" or reset
        writes = [item for item in queued if item.mode == "write"]
        steers = [item for item in queued if item.mode == "steer"]
        follow_ups = [item for item in queued if item.mode == "follow_up"]
        selected_users: list[SubmissionRecord] = (
            steers if steering_mode == "all" else steers[:1]
        ) if at in {'post_tools', 'final'} else []
        # A steering input owns the current boundary.  Follow-ups stay queued
        # until the next boundary, even when this is a final handoff; this
        # keeps a late steer from being overtaken by an older continuation.
        if final and not selected_users and task_id is None:
            selected_users += (
                follow_ups if follow_up_mode == "all" else follow_ups[:1]
            )
        selected_ids = {item.submission_id for item in writes} if at != 'settle' else set()
        selected_ids.update(item.submission_id for item in selected_users)
        selected = [
            item for item in queued
            if item.submission_id in selected_ids
        ]
        # Pi places passive writes first, then selected user inputs in inbox
        # order.  Keep that ordering even when a write was admitted later.
        selected.sort(key=lambda item: (0 if item.mode == "write" else 1, queued.index(item)))
        head, conversation_revision = self.store.head(conversation_id)
        current_head = head
        entries: list[EntryDraft] = []
        task_updates: list[TaskCheckpoint] = []
        submission_updates: list[SubmissionUpdate] = []
        visible_entry_ids = {entry.entry_id for entry in self.context(conversation_id)}
        task_by_submission = {
            task.submission_id: task
            for task in self.store.tasks()
            if task.conversation_id == conversation_id
        }

        for submission in selected:
            placement = self._prepare_boundary_placement(
                submission,
                current_head=current_head,
                visible_entry_ids=visible_entry_ids,
                task_by_submission=task_by_submission,
                consumer_task_id=task_id,
            )
            if placement.entry is not None:
                entries.append(placement.entry)
                visible_entry_ids.add(placement.entry.entry_id)
                current_head = placement.entry.entry_id
            if placement.task is not None:
                task_updates.append(placement.task)
            submission_updates.append(placement.update)

        if task_id is not None:
            active = self.store.task(task_id)
            if active.conversation_id != conversation_id:
                raise CommitConflict("generation boundary task conversation mismatch")
            raw_state = thaw_json(active.state)
            if not isinstance(raw_state, Mapping):
                raise CommitConflict("generation boundary task state is corrupt")
            state = dict(raw_state)
            state["generation_boundary"] = at
            task_updates.append(replace(active, revision=active.revision + 1, state=state))
        mutation = DurableMutation(
            conversation_id=conversation_id,
            expected_revision=conversation_revision,
            entries=tuple(entries),
            task_updates=tuple(task_updates),
            submission_updates=tuple(submission_updates),
        )
        return tuple(selected), mutation

    def _prepare_boundary_placement(
        self,
        submission: SubmissionRecord,
        *,
        current_head: str | None,
        visible_entry_ids: set[str],
        task_by_submission: Mapping[str, TaskCheckpoint],
        consumer_task_id: str | None = None,
    ) -> _BoundaryPlacement:
        if submission.mode == "write":
            return self._prepare_boundary_write(
                submission, submission.content,
                current_head=current_head, visible_entry_ids=visible_entry_ids,
            )
        task = task_by_submission.get(submission.submission_id)
        if task is None:
            raise CommitConflict("queued submission has no task")
        if task.status in {TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.COMPLETED}:
            raise CommitConflict("cannot place a terminal task")
        content = submission.content
        text = content.get("text") if isinstance(content, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise CommitConflict("queued submission has no input text")
        entry = EntryDraft(
            entry_id=f"entry-{uuid.uuid4().hex}", conversation_id=submission.conversation_id,
            parent_id=current_head, kind="user_message",
            payload={"text": text, **admission.message_metadata(content)}, by_task_id=task.task_id,
        )
        thawed_state = thaw_json(task.state)
        if not isinstance(thawed_state, Mapping):
            raise CommitConflict("task state is corrupt")
        state = dict(thawed_state)
        state["input_state"] = "placed"
        state["input_entry_id"] = entry.entry_id
        consumed = submission.mode == "steer" and consumer_task_id is not None
        if consumed:
            state["input_state"] = "consumed"
            state["consumed_by_task_id"] = consumer_task_id
        return _BoundaryPlacement(
            entry, replace(task, revision=task.revision + 1, state=state,
                           phase="completed" if consumed else task.phase,
                           status=TaskStatus.COMPLETED if consumed else task.status),
            SubmissionUpdate(
                submission_id=submission.submission_id, conversation_id=submission.conversation_id,
                expected_status=submission.status, status=SubmissionStatus.DONE if consumed else submission.status,
                detail={"consumed_by_task_id": consumer_task_id} if consumed else None,
                input_entry_id=entry.entry_id, placement=SubmissionPlacement.PLACED,
            ),
        )

    def _prepare_boundary_write(
        self,
        submission: SubmissionRecord,
        content: Mapping[str, object] | None,
        *,
        current_head: str | None,
        visible_entry_ids: set[str],
    ) -> _BoundaryPlacement:
        if not isinstance(content, Mapping):
            raise CommitConflict("queued write has no content")
        kind, data = content.get("kind"), content.get("data")
        display_only, model = content.get("display_only"), content.get("model")
        edits, head_target = content.get("edits"), content.get("head")
        if (
            not isinstance(kind, str) or not kind or not isinstance(data, Mapping)
            or type(display_only) is not bool
            or (head_target is not None and (not isinstance(head_target, str) or not head_target))
        ):
            raise CommitConflict("queued write content is invalid")
        try:
            normalized_model, normalized_edits = admission.normalize_passive_fields(
                model if isinstance(model, Mapping) else None,
                edits if isinstance(edits, Sequence) and not isinstance(edits, (str, bytes)) else None,
            )
        except ValueError as exc:
            raise CommitConflict("queued write content is invalid") from exc
        if model is not None and normalized_model is None:
            raise CommitConflict("queued write model is invalid")
        if edits is not None and normalized_edits is None:
            raise CommitConflict("queued write edits are invalid")
        if display_only and (normalized_model is not None or normalized_edits is not None or head_target is not None):
            raise CommitConflict("display-only writes cannot carry context metadata")
        if head_target is not None and head_target != "self" and head_target not in visible_entry_ids:
            return _BoundaryPlacement(
                None, None, SubmissionUpdate(
                    submission_id=submission.submission_id, conversation_id=submission.conversation_id,
                    expected_status=submission.status, status=SubmissionStatus.UNANSWERED, reason="stale",
                ),
            )
        payload: dict[str, object] = {"data": dict(data), "display_only": display_only}
        if normalized_model is not None:
            payload["model"] = normalized_model
        if normalized_edits is not None:
            payload["edits"] = normalized_edits
        if head_target is not None:
            payload.update({"head": head_target, "head_marker": True})
        entry = EntryDraft(
            entry_id=f"entry-{uuid.uuid4().hex}", conversation_id=submission.conversation_id,
            parent_id=current_head, kind=kind, payload=payload,
        )
        return _BoundaryPlacement(
            entry, None, SubmissionUpdate(
                submission_id=submission.submission_id, conversation_id=submission.conversation_id,
                expected_status=submission.status, status=SubmissionStatus.DONE,
                input_entry_id=entry.entry_id, placement=SubmissionPlacement.PLACED,
            ),
        )

    def _place_write_submission(self, submission: SubmissionRecord) -> CommitResult:
        content = submission.content
        if not isinstance(content, Mapping):
            raise CommitConflict("queued write has no content")
        kind = content.get("kind")
        data = content.get("data")
        display_only = content.get("display_only")
        model = content.get("model")
        edits = content.get("edits")
        head_target = content.get("head")
        if (
            not isinstance(kind, str)
            or not kind
            or not isinstance(data, Mapping)
            or type(display_only) is not bool
            or (head_target is not None and (not isinstance(head_target, str) or not head_target))
        ):
            raise CommitConflict("queued write content is invalid")
        try:
            normalized_model, normalized_edits = admission.normalize_passive_fields(
                model if isinstance(model, Mapping) else None,
                edits if isinstance(edits, Sequence) and not isinstance(edits, (str, bytes)) else None,
            )
        except ValueError as exc:
            raise CommitConflict("queued write content is invalid") from exc
        if model is not None and normalized_model is None:
            raise CommitConflict("queued write model is invalid")
        if edits is not None and normalized_edits is None:
            raise CommitConflict("queued write edits are invalid")
        if display_only and (normalized_model is not None or normalized_edits is not None or head_target is not None):
            raise CommitConflict("display-only writes cannot carry context metadata")
        if head_target is not None and head_target != "self":
            visible = {entry.entry_id for entry in self.context(submission.conversation_id)}
            if head_target not in visible:
                return self._settle_stale_write(submission)
        head, revision = self.store.head(submission.conversation_id)
        payload: dict[str, object] = {"data": dict(data), "display_only": display_only}
        if normalized_model is not None:
            payload["model"] = normalized_model
        if normalized_edits is not None:
            payload["edits"] = normalized_edits
        if head_target is not None:
            payload.update({"head": head_target, "head_marker": True})
        entry = EntryDraft(
            entry_id=f"entry-{uuid.uuid4().hex}",
            conversation_id=submission.conversation_id,
            parent_id=head,
            kind=kind,
            payload=payload,
        )
        update = SubmissionUpdate(
            submission_id=submission.submission_id,
            conversation_id=submission.conversation_id,
            expected_status=submission.status,
            status=SubmissionStatus.DONE,
            input_entry_id=entry.entry_id,
            placement=SubmissionPlacement.PLACED,
        )
        return self._commit(DurableMutation(
            conversation_id=submission.conversation_id,
            expected_revision=revision,
            entries=(entry,),
            submission_update=update,
        ))

    def _settle_stale_write(self, submission: SubmissionRecord) -> CommitResult:
        _head, revision = self.store.head(submission.conversation_id)
        return self._commit(DurableMutation(
            conversation_id=submission.conversation_id,
            expected_revision=revision,
            submission_update=SubmissionUpdate(
                submission_id=submission.submission_id,
                conversation_id=submission.conversation_id,
                expected_status=submission.status,
                status=SubmissionStatus.UNANSWERED,
                reason="stale",
            ),
        ))

    def reconcile_workflows(self, definitions: Mapping[str, WorkflowDefinition]) -> tuple[str, ...]:
        return self.task_operations.reconcile_workflows(definitions)

    @_mutation_line
    def fork_task(
        self, source_task_id: str, commit_seq: int, *, conversation_id: str,
        task_id: str, request_id: str, instruction: str,
    ) -> CommitResult:
        """Atomically admit a new execution rooted in a historical context.

        This forks durable state; workspace changes require their own isolated
        execution scope. No settled effect is executed by this operation.
        """
        return admission.fork_task(
            self, source_task_id, commit_seq, conversation_id=conversation_id,
            task_id=task_id, request_id=request_id, instruction=instruction,
        )

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

    @_mutation_line
    def append_message(
        self,
        conversation_id: str,
        kind: str,
        payload: Mapping[str, object],
    ) -> EntryDraft:
        return conversation_ops.append_message(self, conversation_id, kind, payload)

    @_mutation_line
    def branch(
        self,
        conversation_id: str,
        entry_id: str,
        *,
        expected_revision: int | None = None,
    ) -> CommitResult:
        return conversation_ops.branch(
            self, conversation_id, entry_id, expected_revision=expected_revision,
        )

    @_mutation_line
    def fork(self, conversation_id: str, entry_id: str, *, child_conversation_id: str | None = None) -> str:
        return conversation_ops.fork(self, conversation_id, entry_id, child_conversation_id=child_conversation_id)

    @_mutation_line
    def delete_conversation(self, conversation_id: str) -> CommitResult:
        """Delete an unreferenced conversation without orphaning a fork."""
        return conversation_ops.delete_conversation(self, conversation_id)

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

    @_mutation_line
    def save_context_snapshot(
        self, conversation_id: str, payload: Mapping[str, object]
    ) -> EntryDraft:
        """Append the latest handoff facts to the canonical session history.

        Snapshot entries are durable metadata, not provider messages. Keeping
        them in the same append stream makes abrupt-process recovery atomic
        without giving the UI or the provider-window cache ownership of facts.
        """
        return conversation_ops.save_context_snapshot(self, conversation_id, payload)

    def latest_context_snapshot(
        self, conversation_id: str
    ) -> EntryDraft | None:
        return self.conversation_queries.latest_context_snapshot(conversation_id)
