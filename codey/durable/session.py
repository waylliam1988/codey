from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from typing import TypeVar, cast

from codey.durable import admission, conversation_ops
from codey.durable.context import ContextSnapshot
from codey.durable.effects import interrupted_effect
from codey.durable.errors import (
    CommitConflict,
    IdempotencyConflict,
    SessionFailedError,
    SessionSealedError,
)
from codey.durable.model import (
    EXECUTION_STATE_KEYS,
    ApprovalRecord,
    ApprovalStatus,
    CommitRecord,
    CommitResult,
    CreateConversation,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionStatus,
    SubmissionUpdate,
    TaskCheckpoint,
    TaskStatus,
    task_status_for_phase,
    thaw_json,
    valid_approval_transition,
)
from codey.durable.reducer import with_effect_intents, with_effect_settlement
from codey.durable.store import DurableStore
from codey.durable.timeline import TaskSnapshot, TaskTransition, snapshot_at
from codey.durable.workflow import WorkflowDefinition, workflow_digest

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _process_is_alive(value: object) -> bool:
    """Return whether a durable attempt owner process is still running."""
    if type(value) is not int or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _release_attempt_state(state: Mapping[str, object]) -> dict[str, object]:
    """Clear volatile scheduler ownership while preserving logical state."""
    released = dict(state)
    released["attempt_token"] = ""
    released["attempt_owner"] = ""
    released["attempt_process_id"] = 0
    return released


def _mutation_line(method: Callable[..., _T]) -> Callable[..., _T]:
    """Serialize one complete session operation, including its reads.

    SQLite already serializes individual transactions.  The line is still
    needed above it: a read/derive/commit operation must not observe another
    writer between its first read and its durable commit.  ``RLock`` keeps
    nested session helpers (for example ``complete_task``) composable.
    """

    @wraps(method)
    def run(self: DurableSession, *args: object, **kwargs: object) -> _T:
        with self._write_lock:
            self._ensure_usable()
            return method(self, *args, **kwargs)

    return cast(Callable[..., _T], run)


class DurableSession:
    """Owns durable conversation admission and reads for one application state directory."""

    @property
    def database_id(self) -> str:
        return self.store.database_id

    def __init__(
        self,
        store: DurableStore,
        *,
        on_commit: Callable[[CommitResult, DurableMutation], None] | None = None,
    ) -> None:
        self.store = store
        self._on_commit = on_commit
        self._write_lock = threading.RLock()
        self._sealed = False
        self._closed = False
        self._failure: BaseException | None = None
        self._internal_listeners: list[Callable[[CommitResult, DurableMutation], None]] = []
        self._commit_listeners: list[Callable[[CommitResult, DurableMutation], None]] = []
        self._close_listeners: list[Callable[[], None]] = []
        self._close_notified = False
        self._owner_id = f"owner-{uuid.uuid4().hex}"
        self._execution: ContextVar[tuple[str, str] | None] = ContextVar("durable_execution", default=None)

    @_mutation_line
    def _commit(self, mutation: DurableMutation) -> CommitResult:
        self._ensure_writable()
        try:
            mutation = self._materialize_approval(mutation)
            if mutation.task is not None and mutation.expected_attempt_token is None:
                task_id = mutation.task.task_id
                try:
                    current = self.store.task(task_id)
                except KeyError:
                    current = None
                if current is not None:
                    bound = self._execution.get()
                    expected = (bound[1] if bound is not None and bound[0] == task_id
                                else current.attempt_token if current.state.get("attempt_owner") == self._owner_id else "")
                    mutation = replace(mutation, expected_attempt_token=expected)
            result = self.store.commit(mutation)
            self._publish_commit(result, mutation)
            return result
        except (CommitConflict, IdempotencyConflict, KeyError, TypeError, ValueError):
            raise
        except Exception as exc:
            self._fail(exc)
            raise

    def _materialize_approval(self, mutation: DurableMutation) -> DurableMutation:
        """Fold an approval transition into the task checkpoint atomically.

        Approval state is task-owned durable state.  Keeping it inside the
        checkpoint avoids a second database and lets a decision, effect
        settlement, and next task phase share one commit sequence.
        """
        approval = mutation.approval
        if approval is None:
            return mutation
        current = self.store.task(approval.task_id)
        if current.conversation_id != mutation.conversation_id:
            raise CommitConflict("approval conversation boundary mismatch")
        raw = thaw_json(current.state.get("approvals", {}))
        if not isinstance(raw, dict):
            raise CommitConflict("task approval state is corrupt")
        existing_payload = raw.get(approval.approval_id)
        existing = (
            ApprovalRecord.from_payload(existing_payload)
            if isinstance(existing_payload, Mapping)
            else None
        )
        if existing is None:
            if approval.revision != 0 or approval.status != ApprovalStatus.REQUESTED:
                raise CommitConflict("new approval must start as requested revision zero")
            if mutation.expected_approval_status is not None:
                raise CommitConflict("new approval cannot have an expected status")
        else:
            expected = (
                ApprovalStatus(mutation.expected_approval_status)
                if mutation.expected_approval_status is not None
                else None
            )
            if expected != existing.status:
                raise CommitConflict("approval status changed")
            if approval.revision != existing.revision + 1:
                raise CommitConflict("approval revision changed")
            # A lifecycle annotation (for example continuation ownership) may
            # advance while the permission decision remains unchanged.  This
            # is still a durable CAS transition, but it must not require a
            # second JSONL history to explain the current state.
            if (
                ApprovalStatus(existing.status) != ApprovalStatus(approval.status)
                and not valid_approval_transition(ApprovalStatus(existing.status), ApprovalStatus(approval.status))
            ):
                raise CommitConflict(f"invalid approval transition: {existing.status} -> {approval.status}")
            if (
                approval.task_id != existing.task_id
                or approval.conversation_id != existing.conversation_id
                or approval.command_digest != existing.command_digest
                or approval.command_ref != existing.command_ref
                or approval.cwd != existing.cwd
            ):
                raise CommitConflict("approval identity is immutable")
        raw[approval.approval_id] = approval.to_payload()
        proposed = mutation.task
        if proposed is None:
            current_state = thaw_json(current.state)
            if not isinstance(current_state, Mapping):
                raise CommitConflict("task state is corrupt")
            proposed = replace(
                current,
                revision=current.revision + 1,
                state={**dict(current_state), "approvals": raw},
            )
        else:
            proposed_state = thaw_json(proposed.state)
            if not isinstance(proposed_state, dict):
                raise CommitConflict("task state is corrupt")
            proposed = replace(proposed, state={**dict(proposed_state), "approvals": raw})
        return replace(mutation, task=proposed, approval=None, expected_approval_status=None)

    def _ensure_writable(self) -> None:
        self._ensure_usable()

    def _ensure_usable(self) -> None:
        if self._failure is not None:
            raise SessionFailedError(self._failure) from self._failure
        if self._sealed:
            raise SessionSealedError(
                "durable session is sealed after a storage failure; reopen it"
            )
        if self._closed:
            raise SessionSealedError("durable session is closed; reopen it")

    def _fail(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = error
            self._sealed = True
            self._notify_close()

    @_mutation_line
    def transact_task(
        self,
        task_id: str,
        derive: Callable[[TaskCheckpoint, tuple[EffectRecord, ...], int], DurableMutation | None],
    ) -> CommitResult | None:
        """Derive and commit one typed mutation on the session mutation line.

        The reducer is pure: external work starts only after this returns.
        All checkpoint/effect reads, validation and publication share the
        same line; the store still checks revisions against other writers.
        """
        self._ensure_writable()
        current = self.store.task(task_id)
        _head, revision = self.store.head(current.conversation_id)
        mutation = derive(current, tuple(self.store.effects_for_task(task_id)), revision)
        if mutation is None:
            return None
        if (mutation.task is None or mutation.task.task_id != task_id
                or mutation.task.revision != current.revision + 1
                or mutation.expected_revision != revision):
            raise ValueError("task transaction must advance its captured checkpoint")
        return self._commit(mutation)

    @contextmanager
    def execution(self, task_id: str, attempt_token: str) -> Generator[None]:
        """Bind a queued worker to the exact attempt captured at dispatch."""
        with self._write_lock:
            self._ensure_writable()
            current = self.store.task(task_id)
            if current.status != TaskStatus.RUNNING or current.attempt_token != attempt_token:
                raise CommitConflict("task execution attempt changed")
        binding = self._execution.set((task_id, attempt_token))
        try:
            yield
        finally:
            self._execution.reset(binding)

    def _publish_commit(self, result: CommitResult, mutation: DurableMutation) -> None:
        if result.record is None:
            return
        listeners = list(self._internal_listeners)
        for listener in listeners:
            try:
                listener(result, mutation)
            except Exception as exc:
                self._fail(exc)
                raise
        external = list(self._commit_listeners)
        callback = self._on_commit
        if callback is not None:
            external.insert(0, callback)
        for listener in external:
            try:
                listener(result, mutation)
            except Exception:
                # External publication is a projection. A listener failure
                # cannot make an already committed durable mutation fail.
                logger.exception("durable commit publication failed")

    def observe_commits(
        self, listener: Callable[[CommitResult, DurableMutation], None]
    ) -> Callable[[], None]:
        with self._write_lock:
            self._ensure_usable()
            self._internal_listeners.append(listener)

        def unsubscribe() -> None:
            with self._write_lock:
                if listener in self._internal_listeners:
                    self._internal_listeners.remove(listener)

        return unsubscribe

    def subscribe_commits(
        self, listener: Callable[[CommitResult, DurableMutation], None]
    ) -> Callable[[], None]:
        with self._write_lock:
            self._ensure_usable()
            self._commit_listeners.append(listener)

        def unsubscribe() -> None:
            with self._write_lock:
                if listener in self._commit_listeners:
                    self._commit_listeners.remove(listener)

        return unsubscribe

    def subscribe_close(self, listener: Callable[[], None]) -> Callable[[], None]:
        with self._write_lock:
            self._ensure_usable()
            self._close_listeners.append(listener)

        def unsubscribe() -> None:
            with self._write_lock:
                if listener in self._close_listeners:
                    self._close_listeners.remove(listener)

        return unsubscribe

    def _notify_close(self) -> None:
        if self._close_notified:
            return
        self._close_notified = True
        listeners = list(self._close_listeners)
        self._close_listeners.clear()
        for listener in listeners:
            try:
                listener()
            except Exception:
                logger.exception("durable session close listener failed")

    @_mutation_line
    def create_conversation(self, conversation_id: str) -> CommitResult:
        if self.store.has_conversation(conversation_id):
            return CommitResult(self.store.head(conversation_id)[1], self.store.commit_sequence())
        return self._commit(DurableMutation(
            conversation_id=conversation_id,
            expected_revision=0,
            create_conversation=CreateConversation(),
        ))

    @_mutation_line
    def head(self, conversation_id: str) -> tuple[str | None, int]:
        head, revision = self.store.head(conversation_id)
        return str(head) if head is not None else None, int(revision)

    @_mutation_line
    def has_conversation(self, conversation_id: str) -> bool:
        return self.store.has_conversation(conversation_id)

    @_mutation_line
    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]:
        """Read schedulable checkpoints through the session mutation line."""
        return tuple(self.store.pending_tasks())

    @_mutation_line
    def tasks(self) -> tuple[TaskCheckpoint, ...]:
        """Read every checkpoint for projections that need terminal history."""
        return tuple(self.store.tasks())

    @_mutation_line
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
        current = self.store.task(task_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise CommitConflict("task revision changed")
        _head, revision = self.store.head(current.conversation_id)
        next_task = replace(
            current,
            revision=current.revision + 1,
            state={**dict(current.state), **dict(state)},
        )
        return self._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                expected_attempt_token=current.attempt_token,
            )
        )

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
    ) -> CommitResult:
        return admission.admit_input(
            self, conversation_id=conversation_id, request_id=request_id, task_id=task_id,
            text=text, provider_snapshot=provider_snapshot, task_state=task_state,
            initial_phase=initial_phase, tool_results=tool_results,
            owner_task_id=owner_task_id, abandon_on_restart=abandon_on_restart,
            workflow_name=workflow_name, workflow_version=workflow_version,
            workflow_input=workflow_input, workflow_checkpoint=workflow_checkpoint,
            workflow_replay_contract=workflow_replay_contract,
        )

    @_mutation_line
    def reconcile_workflows(self, definitions: Mapping[str, WorkflowDefinition]) -> tuple[str, ...]:
        """Apply explicit pure migrations; incompatible definitions stay blocked."""
        changed: list[str] = []
        for task in self.store.tasks():
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
            _head, revision = self.store.head(task.conversation_id)
            self._commit(DurableMutation(
                task.conversation_id,
                revision,
                task=replace(task, revision=task.revision + 1, state=state),
                expected_attempt_token=task.attempt_token,
            ))
            changed.append(task.task_id)
        return tuple(changed)

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

    @_mutation_line
    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]:
        return conversation_ops.entries(self, conversation_id)

    @_mutation_line
    def history_page(
        self,
        conversation_id: str,
        *,
        before: str | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        return conversation_ops.history_page(self, conversation_id, before=before, limit=limit)

    @_mutation_line
    def search_history(
        self,
        conversation_id: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return conversation_ops.search_history(self, conversation_id, query, limit=limit)

    @_mutation_line
    def task(self, task_id: str) -> TaskCheckpoint:
        return self.store.task(task_id)

    @_mutation_line
    def task_timeline(self, task_id: str) -> tuple[TaskTransition, ...]:
        return self.store.task_history(task_id)

    @_mutation_line
    def task_at(self, task_id: str, commit_seq: int) -> TaskSnapshot:
        if type(commit_seq) is not int or commit_seq < 0:
            raise ValueError("historical commit sequence must be a non-negative integer")
        return snapshot_at(self.store.task_history(task_id, through=commit_seq))

    @_mutation_line
    def effects(self, task_id: str) -> tuple[EffectRecord, ...]:
        return tuple(self.store.effects_for_task(task_id))

    @_mutation_line
    def task_facts(self, task_id: str) -> tuple[TaskCheckpoint, tuple[EffectRecord, ...]]:
        """Read a checkpoint and its effects without an intervening commit."""
        return self.store.task(task_id), tuple(self.store.effects_for_task(task_id))

    @_mutation_line
    def effect(self, effect_id: str) -> EffectRecord:
        return self.store.effect(effect_id)

    @_mutation_line
    def approval(self, approval_id: str) -> ApprovalRecord:
        return self.store.approval(approval_id)

    @_mutation_line
    def create_approval(self, approval: ApprovalRecord) -> CommitResult:
        _head, revision = self.store.head(approval.conversation_id)
        return self._commit(
            DurableMutation(
                conversation_id=approval.conversation_id,
                expected_revision=revision,
                approval=approval,
            )
        )

    @_mutation_line
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
        _head, revision = self.store.head(approval.conversation_id)
        return self._commit(
            DurableMutation(
                conversation_id=approval.conversation_id,
                expected_revision=revision,
                task=task,
                approval=approval,
                expected_approval_status=ApprovalStatus(expected_status),
            )
        )

    @_mutation_line
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
        current_approval = self.store.approval(approval_id)
        expected = ApprovalStatus(expected_status)
        current_task = self.store.task(current_approval.task_id)
        if effect.task_id != current_task.task_id:
            raise CommitConflict("approval effect belongs to another task")
        if effect.category != "shell" or effect.status != "pending" or effect.settlement is not None:
            raise ValueError("approval claim requires a pending shell effect intent")
        if current_approval.status == ApprovalStatus.EXECUTION_CLAIMED:
            existing = self.store.effect(effect.effect_id)
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
        _head, revision = self.store.head(current_task.conversation_id)
        return self._commit(
            DurableMutation(
                conversation_id=current_task.conversation_id,
                expected_revision=revision,
                task=next_task,
                effects=(effect,),
                approval=claimed,
                expected_approval_status=expected,
            )
        )

    @_mutation_line
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
        current_approval = self.store.approval(approval_id)
        current_task = self.store.task(current_approval.task_id)
        continuation_id = current_task.task_id if resume else current_approval.continuation_run_id
        if current_approval.continuation_status in {"started", "cancelled"}:
            return None

        effect_settlement: EffectRecord | None = None
        next_task = current_task
        if current_approval.status == ApprovalStatus.EXECUTION_CLAIMED:
            effect = self.store.effect(f"shell-{approval_id}")
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
            self,
            current_task.conversation_id,
            self.store.head(current_task.conversation_id)[0],
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
            state = _release_attempt_state(state)
            next_task = replace(next_task, state=state, status=TaskStatus.PENDING)
        updated_approval = replace(
            current_approval,
            status=next_status,
            revision=current_approval.revision + 1,
            settlement=dict(settlement) if next_status == ApprovalStatus.EXECUTION_SETTLED else current_approval.settlement,
            continuation_status="started" if resume else "not_required",
            continuation_run_id=continuation_id,
        )
        _head, revision = self.store.head(current_task.conversation_id)
        return self._commit(DurableMutation(
            conversation_id=current_task.conversation_id,
            expected_revision=revision,
            entries=tuple(entries),
            task=next_task,
            settlements=(effect_settlement,) if effect_settlement is not None else (),
            approval=updated_approval,
            expected_approval_status=current_approval.status,
            expected_attempt_token=current_task.attempt_token,
        ))

    @_mutation_line
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

    @_mutation_line
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

    @_mutation_line
    def _record_immutable_task_state(
        self,
        task_id: str,
        *,
        key: str,
        value: Mapping[str, object],
        conflict_message: str,
    ) -> CommitResult:
        current = self.store.task(task_id)
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
        _head, revision = self.store.head(current.conversation_id)
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
        return self._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
            )
        )

    @_mutation_line
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
        current = self.store.task(task_id)
        # Cancellation is a durable admission barrier.  Once requested, the
        # old worker may only settle an already admitted external effect or
        # commit the terminal cancellation; it cannot advance the workflow or
        # create another effect.
        if current.state.get("cancel_requested") and phase not in {"cancelled", "failed"}:
            raise CommitConflict("task cancellation barrier is active")
        _head, conversation_revision = self.store.head(current.conversation_id)
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
            submission = self.store.submission(current.submission_id)
            update = SubmissionUpdate(
                submission_id=submission.submission_id,
                conversation_id=submission.conversation_id,
                expected_status=submission.status,
                status=submission_status,
                answer_entry_id=answer_entry_id,
                reason=reason,
            )
        return self._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=conversation_revision,
                entries=entries,
                task=next_task,
                submission_update=update,
            )
        )

    @_mutation_line
    def start_task(self, task_id: str, *, state: Mapping[str, object]) -> CommitResult:
        return self.advance_task(
            task_id=task_id,
            phase="provider_prepared",
            state=state,
            submission_status=SubmissionStatus.RUNNING,
        )

    def _owned_descendants(self, task_id: str) -> tuple[TaskCheckpoint, ...]:
        """Return the durable ownership subtree, deepest children first."""
        tasks = tuple(self.store.tasks())
        children: dict[str, list[TaskCheckpoint]] = {}
        for task in tasks:
            if task.owner_task_id is not None:
                children.setdefault(task.owner_task_id, []).append(task)
        result: list[TaskCheckpoint] = []
        seen: set[str] = set()

        def visit(parent: str) -> None:
            for child in children.get(parent, ()):
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

    @_mutation_line
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
        current = self.store.task(task_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise CommitConflict("task revision changed")
        if current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return CommitResult(current.revision, self.store.commit_sequence())
        # Cancellation is a durable barrier, so repeated Stop requests are
        # deliberately idempotent.  In particular, shutdown cleanup can call
        # the same path after the worker has already published the intent;
        # retrying the unchanged running checkpoint would violate the barrier
        # validator because it is not a terminal cancellation transition.
        if current.state.get("cancel_requested") and not state_update:
            return CommitResult(current.revision, self.store.commit_sequence())

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
        current = self.store.task(task_id)
        state = {**dict(current.state), **dict(state_update or {}), "cancel_requested": True, "cancel_reason": reason}
        if self._live_owned_descendants(task_id):
            status = current.status if current.status in {TaskStatus.RUNNING, TaskStatus.COMPLETING} else TaskStatus.WAITING
            _head, revision = self.store.head(current.conversation_id)
            return self._commit(DurableMutation(
                current.conversation_id,
                revision,
                task=replace(current, revision=current.revision + 1, state=state, status=status),
                expected_attempt_token=current.attempt_token,
            ))
        if current.status == TaskStatus.RUNNING:
            _head, revision = self.store.head(current.conversation_id)
            next_task = replace(current, revision=current.revision + 1, state=state)
            return self._commit(DurableMutation(
                current.conversation_id, revision, task=next_task,
                expected_attempt_token=current.attempt_token,
            ))
        return self._cancel_terminal(current, state=state, reason=reason)

    @_mutation_line
    def cancel_task(self, task_id: str, *, reason: str = "cancelled") -> CommitResult:
        """Atomically close cancellation after the worker/effects have stopped."""
        current = self.store.task(task_id)
        if current.status == TaskStatus.CANCELLED:
            return CommitResult(current.revision, self.store.commit_sequence())
        if current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            raise CommitConflict(f"terminal task cannot be cancelled: {current.status}")
        for child in self._owned_descendants(task_id):
            if child.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                continue
            if child.status == TaskStatus.RUNNING:
                self.request_cancel(child.task_id, reason=reason)
            else:
                self.cancel_task(child.task_id, reason=reason)
        current = self.store.task(task_id)
        state = {**dict(current.state), "cancel_requested": True, "cancel_reason": reason}
        if self._live_owned_descendants(task_id):
            _head, revision = self.store.head(current.conversation_id)
            status = current.status if current.status in {TaskStatus.RUNNING, TaskStatus.COMPLETING} else TaskStatus.WAITING
            return self._commit(DurableMutation(
                current.conversation_id,
                revision,
                task=replace(current, revision=current.revision + 1, state=state, status=status),
                expected_attempt_token=current.attempt_token,
            ))
        return self._cancel_terminal(current, state=state, reason=reason)

    def _cancel_terminal(
        self, current: TaskCheckpoint, *, state: Mapping[str, object], reason: str,
    ) -> CommitResult:
        _head, revision = self.store.head(current.conversation_id)
        pending = tuple(effect for effect in self.store.effects_for_task(current.task_id) if effect.settlement is None)
        pending_ids = current.state.get("pending_effect_ids", ())
        if not isinstance(pending_ids, tuple) or {effect.effect_id for effect in pending} != set(pending_ids):
            raise CommitConflict("task checkpoint and pending effects disagree")
        next_task = replace(
            current,
            phase="cancelled",
            revision=current.revision + 1,
            state={**_release_attempt_state(state), "pending_effect_ids": []},
            pending_effect_id=None,
            status=TaskStatus.CANCELLED,
        )
        submission = self.store.submission(current.submission_id)
        update = None
        if submission.status in {SubmissionStatus.QUEUED, SubmissionStatus.RUNNING}:
            update = SubmissionUpdate(
                submission.submission_id, current.conversation_id,
                submission.status, SubmissionStatus.CANCELLED, reason=reason,
            )
        return self._commit(DurableMutation(
            current.conversation_id,
            revision,
            task=next_task,
            settlements=tuple(interrupted_effect(current, effect) for effect in pending),
            submission_update=update,
            # Cancellation is the recovery winner: it may close a running
            # attempt left by a dead process, so bind the terminal commit to
            # the token that was durable before the process disappeared.
            expected_attempt_token=current.attempt_token,
        ))

    @_mutation_line
    def claim_task(self, task_id: str) -> CommitResult:
        current = self.store.task(task_id)
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
                 "attempt_owner": self._owner_id, "attempt_token": f"attempt-{uuid.uuid4().hex}",
                 "attempt_process_id": os.getpid()}
        next_task = replace(current, phase=phase, revision=current.revision + 1, state=state, status=TaskStatus.RUNNING)
        _head, revision = self.store.head(current.conversation_id)
        return self._commit(DurableMutation(current.conversation_id, revision, task=next_task,
                                           expected_attempt_token=current.attempt_token))

    @_mutation_line
    def recover_scheduler_state(self, *, force: bool = False) -> None:
        """Return abandoned scheduler ownership to the durable pending queue.

        A process can die after claiming a task but before its worker begins.
        The logical phase remains intact; only the volatile scheduler status is
        reset, so recovery resumes the exact checkpoint rather than replaying
        the submission as a new task. A live owner is never reclaimed unless
        an administrative caller explicitly forces recovery.
        """
        for task in self.store.pending_tasks():
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
                if task.state.get("cancel_requested"):
                    self._cancel_terminal(
                        task,
                        state={**dict(task.state), "cancel_requested": True},
                        reason=str(task.state.get("cancel_reason") or "cancelled"),
                    )
                    continue
                state = _release_attempt_state(task.state)
                next_task = replace(task, revision=task.revision + 1, state=state, status=TaskStatus.PENDING)
                _head, revision = self.store.head(task.conversation_id)
                self._commit(DurableMutation(task.conversation_id, revision, task=next_task,
                                             expected_attempt_token=task.attempt_token))
            except CommitConflict:
                continue

    @_mutation_line
    def unclaim_task(self, task_id: str) -> CommitResult:
        current = self.store.task(task_id)
        if current.status != TaskStatus.RUNNING:
            raise ValueError(f"task is not running: {current.status}")
        phase = "accepted" if current.phase == "dispatching" else current.phase
        state = _release_attempt_state(current.state)
        next_task = replace(current, phase=phase, revision=current.revision + 1, state=state, status=TaskStatus.PENDING)
        _head, revision = self.store.head(current.conversation_id)
        return self._commit(DurableMutation(current.conversation_id, revision, task=next_task))

    @_mutation_line
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
        current = self.store.task(task_id)
        if current.status not in {TaskStatus.RUNNING, TaskStatus.PENDING}:
            raise ValueError(f"task is not live: {current.status}")
        if current.state.get("cancel_requested"):
            return self._cancel_terminal(
                current, state={**dict(current.state), **dict(state or {})},
                reason=str(current.state.get("cancel_reason") or "cancelled"),
            )
        if join_policy not in {"failFast", "allSettled"}:
            raise ValueError("unknown task join policy")
        dependencies = current.waiting_on if on is None else tuple(dict.fromkeys(on))
        if task_id in dependencies:
            raise CommitConflict("a task cannot wait on itself")
        known = {task.task_id: task for task in self.store.tasks()}
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
        _head, revision = self.store.head(current.conversation_id)
        next_task = replace(
            current,
            revision=current.revision + 1,
            state={**_release_attempt_state({**dict(current.state), **dict(state or {})}),
                   "waiting_on": dependencies,
                   "join_policy": join_policy,
                   "join_wait": on is not None},
            status=TaskStatus.PENDING if answered else TaskStatus.WAITING,
        )
        return self._commit(
            DurableMutation(
                current.conversation_id,
                revision,
                task=next_task,
                expected_attempt_token=current.attempt_token,
            )
        )

    @_mutation_line
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

    @_mutation_line
    def complete_task(self, task_id: str, answer: str, *, state: Mapping[str, object]) -> CommitResult:
        """Commit the terminal answer after making completion explicit."""
        current = self.store.task(task_id)
        if self._live_owned_descendants(task_id):
            return self._hold_completion(task_id, answer, state)
        if current.phase != "completion_pending":
            if current.phase in {"accepted", "dispatching"}:
                self.start_task(task_id, state=state)
            current = self.store.task(task_id)
            if current.phase != "completion_pending":
                self.advance_task(task_id=task_id, phase="completion_pending", state=state)
        return self.finish_task(task_id, answer, state=state)

    def _hold_completion(self, task_id: str, answer: str, state: Mapping[str, object]) -> CommitResult:
        current = self.store.task(task_id)
        if current.phase in {"accepted", "dispatching"}:
            self.start_task(task_id, state=state)
            current = self.store.task(task_id)
        if current.phase != "completion_pending":
            self.advance_task(task_id=task_id, phase="completion_pending", state=state,
                              task_status=TaskStatus.COMPLETING)
            current = self.store.task(task_id)
        return self.advance_task(
            task_id=task_id,
            phase="completion_pending",
            state={**dict(state), "completion_answer": answer, "completion_state": dict(state)},
            task_status=TaskStatus.COMPLETING,
        )

    @_mutation_line
    def reconcile_waiting_tasks(self) -> None:
        """Reconcile durable joins without relying on scheduler memory."""
        for task in self.store.tasks():
            if task.status != TaskStatus.WAITING or task.state.get("cancel_requested"):
                continue
            if not task.waiting_on and task.state.get("join_wait") is not True:
                continue
            if not task.waiting_on:
                self.advance_task(task_id=task.task_id, phase=task.phase, state={"waiting_on": []},
                                  task_status=TaskStatus.PENDING)
                continue
            children = {candidate.task_id: candidate for candidate in self.store.tasks()}
            missing = [task_id for task_id in task.waiting_on if task_id not in children]
            if missing:
                self.fail_task(task.task_id, "waiting task disappeared", state={"join_error": missing})
                continue
            joined = [children[task_id] for task_id in task.waiting_on]
            if task.join_policy == "failFast" and any(child.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} for child in joined):
                failed = next(child for child in joined if child.status in {TaskStatus.FAILED, TaskStatus.CANCELLED})
                child_reason = self.store.submission(failed.submission_id).reason
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

    @_mutation_line
    def reconcile_finalization(self) -> None:
        """Finish held outcomes and cancellation intents after owned work drains."""
        self.reconcile_waiting_tasks()
        for task in self.store.tasks():
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

    @_mutation_line
    def fail_task(
        self,
        task_id: str,
        reason: str,
        *,
        state: Mapping[str, object],
        expected_attempt_token: str | None = None,
    ) -> CommitResult:
        """Settle unknown effects and fail the task/submission in one commit."""
        current = self.store.task(task_id)
        _head, revision = self.store.head(current.conversation_id)
        pending = tuple(effect for effect in self.store.effects_for_task(task_id) if effect.settlement is None)
        pending_ids = current.state.get("pending_effect_ids", ())
        if not isinstance(pending_ids, tuple) or {effect.effect_id for effect in pending} != set(pending_ids):
            raise CommitConflict("task checkpoint and pending effects disagree")
        next_task = replace(
            current,
            phase="failed",
            revision=current.revision + 1,
            state={**_release_attempt_state({**dict(current.state), **dict(state)}), "pending_effect_ids": []},
            pending_effect_id=None,
            status=TaskStatus.FAILED,
        )
        submission = self.store.submission(current.submission_id)
        return self._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                settlements=tuple(interrupted_effect(current, effect) for effect in pending),
                submission_update=SubmissionUpdate(
                    submission.submission_id, current.conversation_id,
                    submission.status, SubmissionStatus.UNANSWERED, reason=reason,
                ),
                expected_attempt_token=expected_attempt_token,
            )
        )

    @_mutation_line
    def record_effect_intent(self, task_id: str, effect: EffectRecord) -> CommitResult:
        return self.record_effect_intents(task_id, (effect,))

    @_mutation_line
    def record_effect_intents(
        self, task_id: str, effects: tuple[EffectRecord, ...]
    ) -> CommitResult:
        current = self.store.task(task_id)
        if current.state.get("cancel_requested"):
            raise CommitConflict("task cancellation barrier is active")
        _head, revision = self.store.head(current.conversation_id)
        next_task = with_effect_intents(current, effects)
        return self._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                effects=effects,
            )
        )

    @_mutation_line
    def settle_effect(self, task_id: str, effect: EffectRecord, *, expected_status: str = "pending") -> CommitResult:
        self._ensure_writable()
        if effect.task_id != task_id:
            raise ValueError("effect must belong to the task")
        if effect.settlement is None or effect.status == "pending":
            raise ValueError("effect settlement requires a terminal outcome")
        current = self.store.task(task_id)
        _head, revision = self.store.head(current.conversation_id)
        existing = self.store.effect(effect.effect_id)
        if existing.settlement is not None:
            if existing != effect:
                raise CommitConflict("effect is already settled with a different outcome")
            return CommitResult(revision, self.store.commit_sequence())
        next_task = with_effect_settlement(current, effect)
        return self._commit(
            DurableMutation(
                conversation_id=current.conversation_id,
                expected_revision=revision,
                task=next_task,
                effect=effect,
                expected_effect_status=expected_status,
            )
        )

    @_mutation_line
    def settle_unknown_effect(self, task_id: str, effect_id: str) -> CommitResult:
        """Record an interrupted external effect without replaying it."""
        current = self.store.task(task_id)
        existing = self.store.effect(effect_id)
        return self.settle_effect(
            task_id,
            interrupted_effect(current, existing),
            expected_status=existing.status,
        )

    def close(self) -> None:
        with self._write_lock:
            if self._closed:
                return
            self._closed = True
            self._notify_close()
            try:
                self.store.close()
            except Exception as exc:
                self._fail(exc)
                raise

    @_mutation_line
    def commit_sequence(self) -> int:
        return self.store.commit_sequence()

    @_mutation_line
    def commits_after(self, sequence: int, *, limit: int = 512) -> tuple[CommitRecord, ...]:
        return tuple(self.store.commits_after(sequence, limit=limit))

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
    def delete_conversation(self, conversation_id: str) -> None:
        """Delete an unreferenced conversation without orphaning a fork."""
        conversation_ops.delete_conversation(self, conversation_id)

    @_mutation_line
    def context(self, conversation_id: str, *, leaf_id: str | None = None) -> tuple[EntryDraft, ...]:
        """Return one immutable branch path, including fork ancestors.

        ``leaf_id`` is deliberately scoped to the child conversation.  A
        caller can inspect an old branch without changing the active head;
        the parent conversation is always read only through its recorded fork
        cap.
        """
        return conversation_ops.context(self, conversation_id, leaf_id=leaf_id)

    @_mutation_line
    def context_at(
        self, conversation_id: str, commit_seq: int, *, leaf_id: str | None = None,
    ) -> tuple[EntryDraft, ...]:
        return conversation_ops.context_at(self, conversation_id, commit_seq, leaf_id=leaf_id)

    @_mutation_line
    def context_snapshot(
        self,
        conversation_id: str,
        *,
        token_budget: int,
        leaf_id: str | None = None,
        before_entry_id: str | None = None,
        token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
    ) -> ContextSnapshot:
        return conversation_ops.context_snapshot(
            self, conversation_id, token_budget=token_budget, leaf_id=leaf_id,
            before_entry_id=before_entry_id, token_counter=token_counter,
        )

    @_mutation_line
    def context_snapshot_at(
        self,
        conversation_id: str,
        commit_seq: int,
        *,
        token_budget: int,
        leaf_id: str | None = None,
        token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None,
    ) -> ContextSnapshot:
        return conversation_ops.context_snapshot_at(
            self, conversation_id, commit_seq, token_budget=token_budget,
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

    @_mutation_line
    def latest_context_snapshot(
        self, conversation_id: str
    ) -> EntryDraft | None:
        return conversation_ops.latest_context_snapshot(self, conversation_id)
