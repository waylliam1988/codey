from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import cast

from codey.durable.errors import (
    CommitConflict,
)
from codey.durable.model import (
    EXECUTION_STATE_KEYS,
    CommitResult,
    DurableMutation,
    EntryDraft,
    SubmissionStatus,
    SubmissionUpdate,
    TaskCheckpoint,
    TaskStatus,
    task_status_for_phase,
    thaw_json,
)
from codey.durable.operations import SessionOperations, operation_line
from codey.durable.process import process_is_alive as _process_is_alive
from codey.durable.task_state import release_attempt_state
from codey.durable.workflow import WorkflowDefinition, workflow_digest


class TaskLifecycleOperations(SessionOperations):
    """Keep task transitions, owned-work drain, cancellation, and recovery together."""

    @operation_line
    def transform_task_state(
        self, task_id: str,
        transform: Callable[[TaskCheckpoint], Mapping[str, object] | None],
    ) -> CommitResult | None:
        """Atomically derive metadata; None verifies a read without a commit."""
        current = self._kernel.store.task(task_id)
        patch = transform(current)
        if patch is None:
            return None
        return self.update_task_state(task_id, patch, expected_revision=current.revision)

    @operation_line
    def update_task_state(
        self,
        task_id: str,
        state: Mapping[str, object],
        *,
        expected_revision: int | None = None,
    ) -> CommitResult:
        """Atomically update task-owned metadata without changing its phase.

        This is used for durable control facts such as a pending user question.
        The logical task phase and scheduler ownership remain unchanged, while
        the checkpoint revision provides the CAS that the control surface needs.
        """
        current = self._kernel.store.task(task_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise CommitConflict("task revision changed")
        _head, revision = self._kernel.store.head(current.conversation_id)
        next_task = replace(
            current,
            revision=current.revision + 1,
            state={**dict(current.state), **dict(state)},
        )
        return self._kernel._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                expected_attempt_token=current.attempt_token,
            )
        )

    @operation_line
    def reconcile_workflows(self, definitions: Mapping[str, WorkflowDefinition]) -> tuple[str, ...]:
        """Apply explicit pure migrations; incompatible definitions stay blocked."""
        changed: list[str] = []
        for task in self._kernel.store.tasks():
            name = task.workflow_name
            if name is None:
                continue
            definition = definitions.get(name)
            if definition is None:
                continue
            if task.workflow_version > definition.version:
                continue
            if task.workflow_version == definition.version:
                continue
            try:
                checkpoint = definition.migrate_checkpoint(task.workflow_checkpoint, task.workflow_version)
            except ValueError:
                continue
            state = {
                **dict(task.state),
                "workflow_version": definition.version,
                "workflow_checkpoint": checkpoint,
                "workflow_replay_contract": definition.replay_contract,
                "workflow_replay_digest": workflow_digest(
                    definition.name, definition.version, task.workflow_input, checkpoint,
                ),
                "workflow_blocked_reason": None,
            }
            _head, revision = self._kernel.store.head(task.conversation_id)
            self._kernel._commit(DurableMutation(
                task.conversation_id,
                revision,
                task=replace(task, revision=task.revision + 1, state=state),
                expected_attempt_token=task.attempt_token,
            ))
            changed.append(task.task_id)
        return tuple(changed)

    @operation_line
    def record_task_policy(self, task_id: str, policy: Mapping[str, object]) -> CommitResult:
        """Persist the immutable authorization snapshot in the task checkpoint.

        Runtime JSONL may mirror this value for diagnostics, but a resumed
        task must never reconstruct permissions from that projection.  The
        first policy wins; a later attempt to replace it is a conflict rather
        than an authorization change.
        """
        return self._record_immutable_task_state(
            task_id,
            key="task_policy",
            value=policy,
            conflict_message="task authorization snapshot is immutable",
        )

    @operation_line
    def record_reviewer_selection(
        self, task_id: str, selection: Mapping[str, object]
    ) -> CommitResult:
        """Persist the reviewer provider choice for restart-stable review."""
        return self._record_immutable_task_state(
            task_id,
            key="reviewer_selection",
            value=selection,
            conflict_message="reviewer selection is immutable",
        )

    @operation_line
    def _record_immutable_task_state(
        self,
        task_id: str,
        *,
        key: str,
        value: Mapping[str, object],
        conflict_message: str,
    ) -> CommitResult:
        current = self._kernel.store.task(task_id)
        state = thaw_json(current.state)
        if not isinstance(state, dict):
            raise TypeError("task checkpoint state must be a JSON object")
        existing = state.get(key)
        if existing is not None:
            if not isinstance(existing, Mapping) or dict(existing) != dict(value):
                from codey.durable.errors import CommitConflict

                raise CommitConflict(conflict_message)
            return CommitResult(current.revision, 0)
        state[key] = dict(value)
        _head, revision = self._kernel.store.head(current.conversation_id)
        next_task = TaskCheckpoint(
            task_id=current.task_id,
            conversation_id=current.conversation_id,
            submission_id=current.submission_id,
            phase=current.phase,
            revision=current.revision + 1,
            provider_snapshot=current.provider_snapshot,
            state=state,
            current_turn_id=current.current_turn_id,
            pending_effect_id=current.pending_effect_id,
            status=current.status,
        )
        return self._kernel._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
            )
        )

    @operation_line
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
        """Persist one task step and its submission outcome atomically.

        A task step never guesses the conversation revision: it reads the
        current task and conversation head, then the store validates both CAS
        values and the phase transition in the same write transaction.
        ``answer`` is deliberately an entry, so a successful submission can
        never point at an uncommitted assistant response.
        """
        current = self._kernel.store.task(task_id)
        # Cancellation is a durable admission barrier.  Once requested, the
        # old worker may only settle an already admitted external effect or
        # commit the terminal cancellation; it cannot advance the workflow or
        # create another effect.
        if current.state.get("cancel_requested") and phase not in {"cancelled", "failed"}:
            raise CommitConflict("task cancellation barrier is active")
        _head, conversation_revision = self._kernel.store.head(current.conversation_id)
        entries: tuple[EntryDraft, ...] = ()
        answer_entry_id: str | None = None
        if answer is not None:
            answer_entry_id = f"entry-{uuid.uuid4().hex}"
            entries = (
                EntryDraft(
                    entry_id=answer_entry_id,
                    conversation_id=current.conversation_id,
                    parent_id=_head,
                    kind="assistant_message",
                    payload={"text": answer},
                    by_task_id=task_id,
                ),
            )
        merged_state = {
            **dict(current.state), **state,
            **{key: current.state[key] for key in (*EXECUTION_STATE_KEYS, "operation", "delivery_batches", "approvals")
               if key in current.state},
        }
        if current.workflow_name is not None and isinstance(merged_state.get("workflow_checkpoint"), Mapping):
            raw_version = merged_state.get("workflow_version")
            version = raw_version if type(raw_version) is int else current.workflow_version
            merged_state["workflow_replay_digest"] = workflow_digest(
                current.workflow_name,
                version,
                current.workflow_input,
                cast(Mapping[str, object], merged_state["workflow_checkpoint"]),
            )
        next_task = TaskCheckpoint(
            task_id=current.task_id,
            conversation_id=current.conversation_id,
            submission_id=current.submission_id,
            phase=phase,
            revision=current.revision + 1,
            provider_snapshot=current.provider_snapshot,
            state=merged_state,
            current_turn_id=current.current_turn_id,
            pending_effect_id=current.pending_effect_id,
            status=task_status if task_status is not None else task_status_for_phase(phase),
        )
        update = None
        if submission_status is not None:
            submission = self._kernel.store.submission(current.submission_id)
            update = SubmissionUpdate(
                submission_id=submission.submission_id,
                conversation_id=submission.conversation_id,
                expected_status=submission.status,
                status=submission_status,
                answer_entry_id=answer_entry_id,
                reason=reason,
            )
        return self._kernel._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=conversation_revision,
                entries=entries,
                task=next_task,
                submission_update=update,
            )
        )

    @operation_line
    def start_task(self, task_id: str, *, state: Mapping[str, object]) -> CommitResult:
        return self.advance_task(
            task_id=task_id,
            phase="running",
            state=state,
            submission_status=SubmissionStatus.RUNNING,
        )

    def _owned_descendants(self, task_id: str) -> tuple[TaskCheckpoint, ...]:
        """Return the durable ownership subtree, deepest children first."""
        result: list[TaskCheckpoint] = []
        seen: set[str] = set()

        def visit(parent: str) -> None:
            for child in self._kernel.store.owned_dependents(parent):
                if child.task_id in seen:
                    continue
                seen.add(child.task_id)
                visit(child.task_id)
                result.append(child)

        visit(task_id)
        return tuple(result)

    def _live_owned_descendants(self, task_id: str) -> tuple[TaskCheckpoint, ...]:
        return tuple(task for task in self._owned_descendants(task_id)
                     if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})

    @operation_line
    def request_cancel(
        self, task_id: str, *, reason: str = "cancelled",
        state_update: Mapping[str, object] | None = None, expected_revision: int | None = None,
    ) -> CommitResult:
        """Durably publish cancellation intent before stopping a worker.

        Pending and waiting work has no external code that must be joined, so
        it is cancelled in this commit.  A running task keeps its checkpoint
        and attempt fence while the worker observes the durable barrier; its
        caller must subsequently commit :meth:`cancel_task` after effects are
        settled.
        """
        current = self._kernel.store.task(task_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise CommitConflict("task revision changed")
        if current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return CommitResult(current.revision, self._kernel.store.commit_sequence())
        # Cancellation is a durable barrier, so repeated Stop requests are
        # deliberately idempotent.  In particular, shutdown cleanup can call
        # the same path after the worker has already published the intent;
        # retrying the unchanged running checkpoint would violate the barrier
        # validator because it is not a terminal cancellation transition.
        if current.state.get("cancel_requested") and not state_update:
            return CommitResult(current.revision, self._kernel.store.commit_sequence())

        # Cancellation intent flows down the durable ownership tree. Running
        # descendants receive a barrier first; queued descendants can be
        # terminalized immediately. The owner itself is held until the tree
        # drains, so a terminal owner always implies that its ordinary work
        # has stopped.
        for child in self._owned_descendants(task_id):
            if child.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                continue
            if child.status == TaskStatus.RUNNING:
                self.request_cancel(child.task_id, reason=reason)
            else:
                self.cancel_task(child.task_id, reason=reason)
        current = self._kernel.store.task(task_id)
        state = {**dict(current.state), **dict(state_update or {}), "cancel_requested": True, "cancel_reason": reason}
        if self._live_owned_descendants(task_id):
            status = current.status if current.status in {TaskStatus.RUNNING, TaskStatus.COMPLETING} else TaskStatus.WAITING
            _head, revision = self._kernel.store.head(current.conversation_id)
            return self._kernel._commit(DurableMutation(
                current.conversation_id,
                revision,
                task=replace(current, revision=current.revision + 1, state=state, status=status),
                expected_attempt_token=current.attempt_token,
            ))
        from codey.durable.model import kernel_cleanup_required

        if current.status == TaskStatus.RUNNING or kernel_cleanup_required(current):
            _head, revision = self._kernel.store.head(current.conversation_id)
            next_task = replace(current, revision=current.revision + 1, state=state,
                                status=current.status if current.status == TaskStatus.RUNNING else TaskStatus.PENDING)
            return self._kernel._commit(DurableMutation(
                current.conversation_id, revision, task=next_task,
                expected_attempt_token=current.attempt_token,
            ))
        return self._cancel_terminal(current, state=state, reason=reason)

    @operation_line
    def cancel_task(self, task_id: str, *, reason: str = "cancelled") -> CommitResult:
        """Atomically close cancellation after the worker/effects have stopped."""
        current = self._kernel.store.task(task_id)
        if current.status == TaskStatus.CANCELLED:
            return CommitResult(current.revision, self._kernel.store.commit_sequence())
        if current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            raise CommitConflict(f"terminal task cannot be cancelled: {current.status}")
        for child in self._owned_descendants(task_id):
            if child.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                continue
            if child.status == TaskStatus.RUNNING:
                self.request_cancel(child.task_id, reason=reason)
            else:
                self.cancel_task(child.task_id, reason=reason)
        current = self._kernel.store.task(task_id)
        state = {**dict(current.state), "cancel_requested": True, "cancel_reason": reason}
        if self._live_owned_descendants(task_id):
            _head, revision = self._kernel.store.head(current.conversation_id)
            status = current.status if current.status in {TaskStatus.RUNNING, TaskStatus.COMPLETING} else TaskStatus.WAITING
            return self._kernel._commit(DurableMutation(
                current.conversation_id,
                revision,
                task=replace(current, revision=current.revision + 1, state=state, status=status),
                expected_attempt_token=current.attempt_token,
            ))
        return self._cancel_terminal(current, state=state, reason=reason)

    def _cancel_terminal(
        self, current: TaskCheckpoint, *, state: Mapping[str, object], reason: str,
    ) -> CommitResult:
        _head, revision = self._kernel.store.head(current.conversation_id)
        pending = tuple(effect for effect in self._kernel.store.effects_for_task(current.task_id) if effect.settlement is None)
        pending_ids = current.state.get("pending_effect_ids", ())
        if not isinstance(pending_ids, tuple) or {effect.effect_id for effect in pending} != set(pending_ids):
            raise CommitConflict("task checkpoint and pending effects disagree")
        next_task = replace(
            current,
            phase="cancelled",
            revision=current.revision + 1,
            state=release_attempt_state(state),
            status=TaskStatus.CANCELLED,
        )
        submission = self._kernel.store.submission(current.submission_id)
        update = None
        if submission.status in {SubmissionStatus.QUEUED, SubmissionStatus.RUNNING}:
            update = SubmissionUpdate(
                submission.submission_id, current.conversation_id,
                submission.status, SubmissionStatus.CANCELLED, reason=reason,
            )
        return self._kernel._commit(DurableMutation(
            current.conversation_id,
            revision,
            task=next_task,
            submission_update=update,
            # Cancellation is the recovery winner: it may close a running
            # attempt left by a dead process, so bind the terminal commit to
            # the token that was durable before the process disappeared.
            expected_attempt_token=current.attempt_token,
        ))

    @operation_line
    def claim_task(self, task_id: str) -> CommitResult:
        current = self._kernel.store.task(task_id)
        if current.status != TaskStatus.PENDING:
            raise ValueError(f"task is not pending: {current.status}")
        phase = current.phase
        if phase == "accepted":
            phase = "dispatching"
        elif phase in {"completed", "failed", "cancelled"}:
            raise ValueError(f"task is not dispatchable: {phase}")
        attempt = current.state.get("attempt_number", 0)
        if type(attempt) is not int:
            raise ValueError("invalid task attempt number")
        state = {**current.state, "attempt_number": attempt + 1,
                 "attempt_owner": self._kernel._owner_id, "attempt_token": f"attempt-{uuid.uuid4().hex}",
                 "attempt_process_id": os.getpid()}
        next_task = replace(current, phase=phase, revision=current.revision + 1, state=state, status=TaskStatus.RUNNING)
        _head, revision = self._kernel.store.head(current.conversation_id)
        return self._kernel._commit(DurableMutation(current.conversation_id, revision, task=next_task,
                                           expected_attempt_token=current.attempt_token))

    @operation_line
    def recover_scheduler_state(self, *, force: bool = False) -> None:
        """Return abandoned scheduler ownership to the durable pending queue.

        A process can die after claiming a task but before its worker begins.
        The logical phase remains intact; only the volatile scheduler status is
        reset, so recovery resumes the exact checkpoint rather than replaying
        the submission as a new task. A live owner is never reclaimed unless
        an administrative caller explicitly forces recovery.
        """
        for task in self._kernel.store.pending_tasks():
            if task.status != TaskStatus.RUNNING:
                continue
            if not force and _process_is_alive(task.state.get("attempt_process_id")):
                continue
            try:
                if task.state.get("abandon_on_restart") is True:
                    self.fail_task(
                        task.task_id,
                        "abandoned on restart",
                        state={"abort_reason": "restart"},
                        expected_attempt_token=task.attempt_token,
                    )
                    continue
                from codey.durable.model import kernel_cleanup_required

                if task.state.get("cancel_requested") and not kernel_cleanup_required(task):
                    self._cancel_terminal(
                        task,
                        state={**dict(task.state), "cancel_requested": True},
                        reason=str(task.state.get("cancel_reason") or "cancelled"),
                    )
                    continue
                state = release_attempt_state(task.state)
                next_task = replace(task, revision=task.revision + 1, state=state, status=TaskStatus.PENDING)
                _head, revision = self._kernel.store.head(task.conversation_id)
                self._kernel._commit(DurableMutation(task.conversation_id, revision, task=next_task,
                                             expected_attempt_token=task.attempt_token))
            except CommitConflict:
                continue

    @operation_line
    def unclaim_task(self, task_id: str) -> CommitResult:
        current = self._kernel.store.task(task_id)
        if current.status != TaskStatus.RUNNING:
            raise ValueError(f"task is not running: {current.status}")
        phase = "accepted" if current.phase == "dispatching" else current.phase
        state = release_attempt_state(current.state)
        next_task = replace(current, phase=phase, revision=current.revision + 1, state=state, status=TaskStatus.PENDING)
        _head, revision = self._kernel.store.head(current.conversation_id)
        return self._kernel._commit(DurableMutation(current.conversation_id, revision, task=next_task))

    @operation_line
    def wait_task(
        self,
        task_id: str,
        *,
        state: Mapping[str, object] | None = None,
        on: tuple[str, ...] | None = None,
        join_policy: str = "allSettled",
    ) -> CommitResult:
        """Park a live task at its exact logical checkpoint.

        Waiting is scheduler ownership state, not a synthetic task phase.  It
        is used when a provider turn has intentionally yielded to an external
        decision (approval or question): the checkpoint remains the source of
        truth, while the scheduler must not dispatch it again until an
        explicit continuation is admitted.
        """
        current = self._kernel.store.task(task_id)
        if current.status not in {TaskStatus.RUNNING, TaskStatus.PENDING}:
            raise ValueError(f"task is not live: {current.status}")
        if current.state.get("cancel_requested"):
            from codey.durable.model import kernel_cleanup_required

            if kernel_cleanup_required(current):
                return self.unclaim_task(task_id)
            return self._cancel_terminal(
                current, state={**dict(current.state), **dict(state or {})},
                reason=str(current.state.get("cancel_reason") or "cancelled"),
            )
        if join_policy not in {"failFast", "allSettled"}:
            raise ValueError("unknown task join policy")
        dependencies = current.waiting_on if on is None else tuple(dict.fromkeys(on))
        if task_id in dependencies:
            raise CommitConflict("a task cannot wait on itself")
        known = {task.task_id: task for task in self._kernel.store.tasks()}
        for dependency in dependencies:
            if dependency not in known:
                raise CommitConflict(f"unknown waiting task: {dependency}")
            cursor = known[dependency]
            walked: set[str] = set()
            while cursor.waiting_on:
                if cursor.task_id in walked:
                    break
                walked.add(cursor.task_id)
                if task_id in cursor.waiting_on:
                    raise CommitConflict("task wait cycle")
                next_id = cursor.waiting_on[0]
                next_cursor = known.get(next_id)
                if next_cursor is None:
                    break
                cursor = next_cursor
        question = current.state.get("question")
        answered = (state is not None and state.get("stop_reason") == "question"
                    and isinstance(question, Mapping) and question.get("state") == "answered")
        _head, revision = self._kernel.store.head(current.conversation_id)
        next_task = replace(
            current,
            revision=current.revision + 1,
            state={**release_attempt_state({**dict(current.state), **dict(state or {})}),
                   "waiting_on": dependencies,
                   "join_policy": join_policy,
                   "join_wait": on is not None},
            status=TaskStatus.PENDING if answered else TaskStatus.WAITING,
        )
        return self._kernel._commit(
            DurableMutation(
                current.conversation_id,
                revision,
                task=next_task,
                expected_attempt_token=current.attempt_token,
            )
        )

    @operation_line
    def finish_task(self, task_id: str, answer: str, *, state: Mapping[str, object]) -> CommitResult:
        if self._live_owned_descendants(task_id):
            return self._hold_completion(task_id, answer, state)
        return self.advance_task(
            task_id=task_id,
            phase="completed",
            state=state,
            answer=answer,
            submission_status=SubmissionStatus.DONE,
        )

    @operation_line
    def complete_task(self, task_id: str, answer: str, *, state: Mapping[str, object]) -> CommitResult:
        """Commit the terminal answer after making completion explicit."""
        current = self._kernel.store.task(task_id)
        if self._live_owned_descendants(task_id):
            return self._hold_completion(task_id, answer, state)
        if current.phase != "completion_pending":
            if current.phase in {"accepted", "dispatching"}:
                self.start_task(task_id, state=state)
            current = self._kernel.store.task(task_id)
            if current.phase != "completion_pending":
                self.advance_task(task_id=task_id, phase="completion_pending", state=state)
        return self.finish_task(task_id, answer, state=state)

    def _hold_completion(self, task_id: str, answer: str, state: Mapping[str, object]) -> CommitResult:
        current = self._kernel.store.task(task_id)
        if current.phase in {"accepted", "dispatching"}:
            self.start_task(task_id, state=state)
            current = self._kernel.store.task(task_id)
        if current.phase != "completion_pending":
            self.advance_task(task_id=task_id, phase="completion_pending", state=state,
                              task_status=TaskStatus.COMPLETING)
            current = self._kernel.store.task(task_id)
        return self.advance_task(
            task_id=task_id,
            phase="completion_pending",
            state={**dict(state), "completion_answer": answer, "completion_state": dict(state)},
            task_status=TaskStatus.COMPLETING,
        )

    @operation_line
    def reconcile_waiting_tasks(self, task_id: str | None = None) -> None:
        """Reconcile durable joins without relying on scheduler memory."""
        tasks = (self._kernel.store.tasks() if task_id is None else (self._kernel.store.task(task_id),))
        for task in tasks:
            if task.status != TaskStatus.WAITING or task.state.get("cancel_requested"):
                continue
            if not task.waiting_on and task.state.get("join_wait") is not True:
                continue
            if not task.waiting_on:
                self.advance_task(task_id=task.task_id, phase=task.phase, state={"waiting_on": []},
                                  task_status=TaskStatus.PENDING)
                continue
            children: dict[str, TaskCheckpoint] = {}
            missing: list[str] = []
            for dependency in task.waiting_on:
                try:
                    children[dependency] = self._kernel.store.task(dependency)
                except KeyError:
                    missing.append(dependency)
            if missing:
                self.fail_task(task.task_id, "waiting task disappeared", state={"join_error": missing})
                continue
            joined = [children[task_id] for task_id in task.waiting_on]
            if task.join_policy == "failFast" and any(child.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} for child in joined):
                failed = next(child for child in joined if child.status in {TaskStatus.FAILED, TaskStatus.CANCELLED})
                child_reason = self._kernel.store.submission(failed.submission_id).reason
                self.fail_task(
                    task.task_id,
                    child_reason or f"owned task {failed.task_id} did not complete",
                    state={"join_failed_task": failed.task_id},
                )
                continue
            if all(child.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED} for child in joined):
                self.advance_task(task_id=task.task_id, phase=task.phase,
                                  state={"waiting_on": [], "join_results": {
                                      child.task_id: str(child.status) for child in joined}},
                                  task_status=TaskStatus.PENDING)

    @operation_line
    def reconcile_finalization(self, task_ids: tuple[str, ...] | None = None) -> None:
        """Finish held outcomes and cancellation intents after owned work drains."""
        if task_ids is None:
            task_ids = self._kernel.store.finalization_queue()
        unique_ids = tuple(dict.fromkeys(task_ids))
        for task_id in unique_ids:
            self.reconcile_waiting_tasks(task_id)
        selected: list[TaskCheckpoint] = []
        for task_id in unique_ids:
            try:
                candidate = self._kernel.store.task(task_id)
            except KeyError:
                continue
            if candidate.status in {TaskStatus.COMPLETING, TaskStatus.WAITING}:
                selected.append(candidate)
        tasks = tuple(selected)
        for task in tasks:
            if task.status not in {TaskStatus.COMPLETING, TaskStatus.WAITING}:
                continue
            if self._live_owned_descendants(task.task_id):
                continue
            if task.state.get("cancel_requested"):
                self._cancel_terminal(task, state=dict(task.state), reason=str(task.state.get("cancel_reason") or "cancelled"))
                continue
            if task.status == TaskStatus.COMPLETING:
                answer = task.state.get("completion_answer")
                completion_state = task.state.get("completion_state", {})
                if isinstance(answer, str) and isinstance(completion_state, Mapping):
                    self.finish_task(task.task_id, answer, state=completion_state)

    @operation_line
    def fail_task(
        self,
        task_id: str,
        reason: str,
        *,
        state: Mapping[str, object],
        expected_attempt_token: str | None = None,
    ) -> CommitResult:
        """Stop scheduling and fail the submission without asserting effect outcomes.

        Unknown outcomes are resolved explicitly by operation recovery rules.
        A lifecycle failure preserves pending receipts and execution position.
        """
        current = self._kernel.store.task(task_id)
        _head, revision = self._kernel.store.head(current.conversation_id)
        pending = tuple(effect for effect in self._kernel.store.effects_for_task(task_id) if effect.settlement is None)
        for key in ("operation", "delivery_batches", "pending_effect_ids", "approvals"):
            if key in state and thaw_json(state[key]) != thaw_json(current.state.get(key)):
                raise CommitConflict(f"task failure cannot replace authoritative {key}")
        pending_ids = current.state.get("pending_effect_ids", ())
        if not isinstance(pending_ids, tuple) or {effect.effect_id for effect in pending} != set(pending_ids):
            raise CommitConflict("task checkpoint and pending effects disagree")
        next_task = replace(
            current,
            phase="failed",
            revision=current.revision + 1,
            state=release_attempt_state({**dict(current.state), **dict(state),
                                          "pending_effect_ids": list(pending_ids)}),
            status=TaskStatus.FAILED,
        )
        submission = self._kernel.store.submission(current.submission_id)
        return self._kernel._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                submission_update=SubmissionUpdate(
                    submission.submission_id, current.conversation_id,
                    submission.status, SubmissionStatus.UNANSWERED, reason=reason,
                ),
                expected_attempt_token=expected_attempt_token,
            )
        )
