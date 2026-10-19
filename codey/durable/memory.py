from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from codey.durable.ancestry import context_path
from codey.durable.errors import CommitConflict, IdempotencyConflict
from codey.durable.events import mutation_projection
from codey.durable.model import (
    TASK_TERMINAL_STATUSES,
    ApprovalRecord,
    CommitRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionDraft,
    SubmissionPlacement,
    SubmissionRecord,
    SubmissionStatus,
    SubmissionUpdate,
    TaskCheckpoint,
    TaskStatus,
    same_submission,
    valid_execution_attempt,
    valid_frozen_task_state,
    valid_submission_transition,
    valid_task_status_transition,
    valid_task_transition,
)
from codey.durable.timeline import TaskTransition, execution_payload, transition_from_payload


@dataclass(frozen=True)
class _Submission:
    record: SubmissionRecord
    entry: EntryDraft | None
    result: CommitResult
    fingerprint: str


class MemoryDurableStore:
    """In-memory implementation of the durable append contract for tests."""

    @property
    def database_id(self) -> str:
        return self._database_id

    def __init__(self) -> None:
        self._database_id = f"database-{uuid.uuid4().hex}"
        self._lock = threading.RLock()
        self._entries: dict[str, list[EntryDraft]] = {}
        self._heads: dict[str, tuple[str | None, int]] = {}
        self._parents: dict[str, tuple[str | None, str | None]] = {}
        self._submissions: dict[tuple[str, str], _Submission] = {}
        self._submission_ids: set[str] = set()
        self._tasks: dict[str, TaskCheckpoint] = {}
        self._effects: dict[str, EffectRecord] = {}
        self._sequence = 0
        self._commits: list[CommitRecord] = []
        self._history: dict[str, list[TaskTransition]] = {}

    def create_conversation(self, conversation_id: str, *, parent_conversation_id: str | None = None, parent_entry_id: str | None = None) -> None:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        with self._lock:
            if conversation_id in self._heads:
                if self._parents[conversation_id] != (parent_conversation_id, parent_entry_id):
                    raise CommitConflict("conversation identity already exists with different ancestry")
                return
            self._heads[conversation_id] = (None, 0)
            self._entries[conversation_id] = []
            self._parents[conversation_id] = (parent_conversation_id, parent_entry_id)

    def append(self, draft: EntryDraft, *, expected_revision: int) -> CommitResult:
        return self.commit(DurableMutation(draft.conversation_id, expected_revision, entries=(draft,)))

    def submit(self, draft: SubmissionDraft) -> CommitResult:
        _head, revision = self._conversation(draft.conversation_id)
        entries = () if draft.entry is None else (draft.entry,)
        return self.commit(DurableMutation(draft.conversation_id, revision, entries=entries, submission=draft))

    def commit(self, mutation: DurableMutation) -> CommitResult:
        with self._lock:
            creation = mutation.create_conversation
            if creation is not None:
                prior = self._existing_submission(mutation.submission)
                if prior is not None:
                    return prior
                if mutation.conversation_id in self._heads:
                    raise CommitConflict("conversation already exists")
                if creation.parent_conversation_id is not None:
                    context_path(creation.parent_conversation_id, creation.parent_entry_id,
                                 entries=self.entries, parent=self.conversation_parent)
                head, revision = None, 0
            else:
                head, revision = self._conversation(mutation.conversation_id)
            if revision != mutation.expected_revision:
                prior_submission = self._existing_submission(mutation.submission)
                if prior_submission is not None:
                    return prior_submission
                raise CommitConflict("conversation revision changed")
            if mutation.delete_conversation is not None:
                return self._commit_delete_conversation(mutation, revision)
            prior_submission = self._existing_submission(mutation.submission)
            if prior_submission is not None:
                return replace(prior_submission, record=None)
            if mutation.submission is not None and mutation.submission.submission_id in self._submission_ids:
                raise IdempotencyConflict("submission ID was already used")
            self._validate_entry_chain(mutation.entries, mutation.conversation_id, head)
            self._validate_task(mutation.task, mutation.submission, mutation.expected_attempt_token)
            self._validate_submission_update(mutation.submission_update, mutation.entries)
            self._validate_effects(mutation)
            new_revision = revision + len(mutation.entries)
            new_head = mutation.entries[-1].entry_id if mutation.entries else head
            if (
                mutation.task is None
                and mutation.effect is None
                and not mutation.effects
                and not mutation.settlements
                and mutation.submission is None
                and mutation.submission_update is None
                and not mutation.entries
                and creation is None
            ):
                return CommitResult(revision, self._sequence)
            self._sequence += 1
            record = CommitRecord(self._sequence, "durable", datetime.now(UTC).isoformat(), {
                **mutation_projection(mutation), "database_id": self.database_id,
            })
            self._commits.append(record)
            result = CommitResult(new_revision, self._sequence, record=record)
            stored_entries = tuple(
                replace(entry, commit_seq=self._sequence,
                        by_task_id=mutation.task.task_id if mutation.task is not None else entry.by_task_id)
                for entry in mutation.entries
            )
            if creation is not None:
                self._entries[mutation.conversation_id] = []
                self._parents[mutation.conversation_id] = (creation.parent_conversation_id, creation.parent_entry_id)
            self._entries[mutation.conversation_id].extend(stored_entries)
            self._heads[mutation.conversation_id] = (new_head, new_revision)
            if mutation.task is not None:
                self._tasks[mutation.task.task_id] = mutation.task
                self._history.setdefault(mutation.task.task_id, []).append(
                    transition_from_payload(self._sequence, execution_payload(mutation, new_head))
                )
            if mutation.effect is not None:
                self._effects[mutation.effect.effect_id] = mutation.effect
            for effect in (*mutation.effects, *mutation.settlements):
                self._effects[effect.effect_id] = effect
            self._write_submission(mutation, new_revision, record)
            if mutation.submission is not None:
                result = replace(result, submission_id=mutation.submission.submission_id)
            if mutation.submission_update is not None:
                self._update_submission(mutation.submission_update, mutation.entries)
                result = replace(result, submission_id=mutation.submission_update.submission_id)
            return result

    def _write_submission(self, mutation: DurableMutation, revision: int, record: CommitRecord) -> None:
        submission = mutation.submission
        if submission is None:
            return
        self._submissions[(mutation.conversation_id, submission.request_id)] = _Submission(
            SubmissionRecord(
                submission.submission_id,
                mutation.conversation_id,
                submission.request_id,
                submission.status,
                None if submission.entry is None else submission.entry.entry_id,
                placement=submission.placement,
                mode=submission.mode,
                content=submission.content,
                reason=submission.reason,
            ),
            submission.entry,
            CommitResult(revision, record.sequence, submission.submission_id, record),
            submission.fingerprint,
        )
        self._submission_ids.add(submission.submission_id)

    def _update_submission(self, update: SubmissionUpdate, entries: tuple[EntryDraft, ...]) -> None:
        update_key = next(
            (key for key, value in self._submissions.items() if value.record.submission_id == update.submission_id),
            None,
        )
        if update_key is None:
            raise CommitConflict("submission disappeared during commit")
        current = self._submissions[update_key]
        placed_entry = current.entry
        if update.input_entry_id is not None:
            placed_entry = next(
                (entry for entry in entries if entry.entry_id == update.input_entry_id),
                placed_entry,
            )
        self._submissions[update_key] = _Submission(
            SubmissionRecord(
                current.record.submission_id,
                current.record.conversation_id,
                current.record.request_id,
                update.status,
                current.record.input_entry_id if update.input_entry_id is None else update.input_entry_id,
                update.answer_entry_id,
                update.reason,
                update.detail,
                current.record.placement if update.placement is None else update.placement,
                current.record.mode,
                current.record.content,
            ),
            placed_entry,
            current.result,
            current.fingerprint,
        )

    def _commit_delete_conversation(self, mutation: DurableMutation, revision: int) -> CommitResult:
        if any(parent == mutation.conversation_id for parent, _entry in self._parents.values()):
            raise CommitConflict("conversation has forked children")
        self._sequence += 1
        record = CommitRecord(self._sequence, "durable", datetime.now(UTC).isoformat(), {
            **mutation_projection(mutation), "database_id": self.database_id,
        })
        self._commits.append(record)
        submission_keys = [
            key for key, value in self._submissions.items()
            if value.record.conversation_id == mutation.conversation_id
        ]
        task_ids = [
            task_id for task_id, task in self._tasks.items()
            if task.conversation_id == mutation.conversation_id
        ]
        for key in submission_keys:
            self._submission_ids.discard(self._submissions[key].record.submission_id)
            self._submissions.pop(key, None)
        for task_id in task_ids:
            self._tasks.pop(task_id, None)
            self._history.pop(task_id, None)
        for effect_id, effect in tuple(self._effects.items()):
            if effect.task_id in task_ids:
                self._effects.pop(effect_id, None)
        self._entries.pop(mutation.conversation_id, None)
        self._heads.pop(mutation.conversation_id, None)
        self._parents.pop(mutation.conversation_id, None)
        return CommitResult(revision + 1, self._sequence, record=record)

    def _validate_effects(self, mutation: DurableMutation) -> None:
        effect_rows = ((mutation.effect,) if mutation.effect is not None else ()) + mutation.effects + mutation.settlements
        if effect_rows and mutation.task is None:
            raise CommitConflict("effect mutation requires its task checkpoint")
        for effect in effect_rows:
            if effect.task_id not in self._tasks and (mutation.task is None or mutation.task.task_id != effect.task_id):
                raise CommitConflict("effect task does not exist")
            existing_effect = self._effects.get(effect.effect_id)
            if existing_effect is None:
                if effect in mutation.settlements:
                    raise CommitConflict("effect status changed")
                continue
            expected = "pending" if effect in mutation.settlements else mutation.expected_effect_status
            if existing_effect.settlement is not None:
                raise CommitConflict("effect is already settled")
            if (effect is not mutation.effect and effect not in mutation.settlements) or expected != existing_effect.status:
                raise CommitConflict("effect status changed")
            if _effect_identity(existing_effect) != _effect_identity(effect):
                raise CommitConflict("effect identity is immutable")

    def _validate_submission_update(
        self,
        update: SubmissionUpdate | None,
        entries: tuple[EntryDraft, ...],
    ) -> None:
        if update is None:
            return
        current = next(
            (value.record for value in self._submissions.values() if value.record.submission_id == update.submission_id),
            None,
        )
        if current is None or current.conversation_id != update.conversation_id:
            raise CommitConflict("unknown submission")
        if current.status != update.expected_status or (
            current.status != update.status and not valid_submission_transition(current.status, update.status)
        ):
            raise CommitConflict("submission status changed")
        if update.answer_entry_id is not None and update.answer_entry_id not in {entry.entry_id for entry in entries}:
            raise CommitConflict("answer entry must be part of the same commit")
        if update.input_entry_id is not None and update.input_entry_id not in {entry.entry_id for entry in entries}:
            raise CommitConflict("placed input entry must be part of the same commit")
        if update.placement is not None:
            placement = SubmissionPlacement(update.placement)
            if current.placement is SubmissionPlacement.PLACED and placement is not SubmissionPlacement.PLACED:
                raise CommitConflict("placed submission cannot return to queued")
            if placement is SubmissionPlacement.PLACED and update.input_entry_id is None and current.input_entry_id is None:
                raise CommitConflict("placing a submission requires an input entry")

    def _validate_entry_chain(self, entries: tuple[EntryDraft, ...], conversation_id: str, head: str | None) -> None:
        parent = head
        existing_ids = {entry.entry_id for rows in self._entries.values() for entry in rows}
        for entry in entries:
            if entry.parent_id != parent or entry.entry_id in existing_ids:
                raise CommitConflict("mutation entries do not extend the active branch")
            existing_ids.add(entry.entry_id)
            parent = entry.entry_id

    def _validate_task(self, task: TaskCheckpoint | None, proposed_submission: SubmissionDraft | None = None,
                       expected_attempt_token: str | None = None) -> None:
        if task is None:
            return
        submission_exists = any(
            item.record.submission_id == task.submission_id
            and item.record.conversation_id == task.conversation_id
            for item in self._submissions.values()
        )
        proposed_matches = (
            proposed_submission is not None
            and proposed_submission.submission_id == task.submission_id
            and proposed_submission.conversation_id == task.conversation_id
        )
        if not submission_exists and not proposed_matches:
            raise CommitConflict("task must belong to a committed submission")
        existing = self._tasks.get(task.task_id)
        if existing is None and task.revision != 0:
            raise CommitConflict("new task must start at revision zero")
        if existing is None and not valid_task_transition(None, task.phase):
            raise CommitConflict("new task must start in accepted phase")
        owner_task_id = task.owner_task_id
        if owner_task_id is not None:
            if owner_task_id == task.task_id:
                raise CommitConflict("a task cannot own itself")
            if owner_task_id not in self._tasks:
                raise CommitConflict("owner task does not exist")
            seen: set[str] = {task.task_id}
            cursor = owner_task_id
            while cursor not in seen:
                seen.add(cursor)
                parent = self._tasks.get(cursor)
                next_owner = parent.owner_task_id if parent is not None else None
                if next_owner is None:
                    break
                cursor = next_owner
            if cursor == task.task_id:
                raise CommitConflict("task ownership cycle")
        if existing is not None:
            if existing.state.get("cancel_requested") and task.phase not in {"cancelled", "failed"}:
                raise CommitConflict("task cancellation barrier is active")
            if not valid_execution_attempt(existing.state, TaskStatus(str(existing.status)), task, expected_attempt_token):
                raise CommitConflict("task execution attempt changed")
            if task.revision != existing.revision + 1:
                raise CommitConflict("task revision changed")
            if (
                task.conversation_id != existing.conversation_id
                or task.submission_id != existing.submission_id
                or dict(task.provider_snapshot) != dict(existing.provider_snapshot)
            ):
                raise CommitConflict("task identity is immutable")
            if not valid_frozen_task_state(existing.state, task.state):
                raise CommitConflict("admitted task state is immutable")
            if not valid_task_transition(existing.phase, task.phase):
                raise CommitConflict(f"invalid task phase transition: {existing.phase} -> {task.phase}")
            if not valid_task_status_transition(TaskStatus(str(existing.status)), TaskStatus(str(task.status))):
                raise CommitConflict(f"invalid task status transition: {existing.status} -> {task.status}")
        elif not valid_task_status_transition(None, TaskStatus(str(task.status))):
            raise CommitConflict("new task must start with a non-terminal scheduler status")

    def task(self, task_id: str) -> TaskCheckpoint:
        with self._lock:
            return self._tasks[task_id]

    def effect(self, effect_id: str) -> EffectRecord:
        with self._lock:
            return self._effects[effect_id]

    def approval(self, approval_id: str) -> ApprovalRecord:
        with self._lock:
            for task in self._tasks.values():
                raw = task.state.get("approvals", {})
                if isinstance(raw, Mapping) and isinstance(raw.get(approval_id), Mapping):
                    return ApprovalRecord.from_payload(raw[approval_id])
            raise KeyError(f"unknown approval: {approval_id}")

    def effects_for_task(self, task_id: str) -> tuple[EffectRecord, ...]:
        with self._lock:
            return tuple(effect for effect in self._effects.values() if effect.task_id == task_id)

    def submission(self, submission_id: str) -> SubmissionRecord:
        with self._lock:
            for item in self._submissions.values():
                if item.record.submission_id == submission_id:
                    return item.record
            raise KeyError(f"unknown submission: {submission_id}")

    def queued_submissions(self) -> tuple[SubmissionRecord, ...]:
        with self._lock:
            return tuple(
                item.record
                for item in self._submissions.values()
                if item.record.status is SubmissionStatus.QUEUED
                and item.record.placement is SubmissionPlacement.QUEUED
            )

    def submission_result_for_request(
        self, conversation_id: str, request_id: str, fingerprint: str,
    ) -> CommitResult | None:
        with self._lock:
            item = self._submissions.get((conversation_id, request_id))
            if item is None:
                return None
            if item.fingerprint != fingerprint:
                raise IdempotencyConflict("request_id was already admitted with different durable result content")
            return item.result

    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]:
        with self._lock:
            return tuple(task for task in self._tasks.values() if task.status not in TASK_TERMINAL_STATUSES)

    def tasks(self) -> tuple[TaskCheckpoint, ...]:
        with self._lock:
            return tuple(self._tasks.values())

    def task_history(self, task_id: str, *, through: int | None = None) -> tuple[TaskTransition, ...]:
        with self._lock:
            return tuple(point for point in self._history.get(task_id, ())
                         if through is None or point.commit_seq <= through)

    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]:
        with self._lock:
            self._conversation(conversation_id)
            return tuple(self._entries[conversation_id])

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        with self._lock:
            return self._conversation(conversation_id)

    def branch(
        self, conversation_id: str, entry_id: str, *, expected_revision: int | None = None,
    ) -> CommitResult:
        with self._lock:
            entries = self._entries.get(conversation_id)
            if entries is None or entry_id not in {entry.entry_id for entry in entries}:
                raise KeyError(f"unknown entry: {entry_id}")
            _head, revision = self._conversation(conversation_id)
            if expected_revision is not None and revision != expected_revision:
                raise CommitConflict("conversation revision changed")
            self._heads[conversation_id] = (entry_id, revision + 1)
            self._sequence += 1
            record = CommitRecord(self._sequence, "branch", datetime.now(UTC).isoformat(), {
                "database_id": self.database_id,
                "session_id": conversation_id, "head_entry_id": entry_id,
            })
            self._commits.append(record)
            return CommitResult(revision + 1, self._sequence, record=record)

    def fork(self, conversation_id: str, entry_id: str, child_conversation_id: str) -> CommitResult:
        with self._lock:
            context_path(conversation_id, entry_id, entries=self.entries, parent=self.conversation_parent)
            if child_conversation_id in self._heads:
                raise ValueError(f"conversation already exists: {child_conversation_id}")
            self.create_conversation(
                child_conversation_id,
                parent_conversation_id=conversation_id,
                parent_entry_id=entry_id,
            )
            self._sequence += 1
            record = CommitRecord(self._sequence, "fork", datetime.now(UTC).isoformat(), {
                "database_id": self.database_id,
                "session_id": child_conversation_id, "parent_session_id": conversation_id, "parent_entry_id": entry_id,
            })
            self._commits.append(record)
            return CommitResult(0, self._sequence, record=record)

    def delete_conversation(self, conversation_id: str) -> None:
        with self._lock:
            self._conversation(conversation_id)
            if any(parent == conversation_id for parent, _entry in self._parents.values()):
                raise CommitConflict("conversation has forked children")
            self._entries.pop(conversation_id, None)
            self._heads.pop(conversation_id, None)
            self._parents.pop(conversation_id, None)
            for key, value in tuple(self._submissions.items()):
                if value.record.conversation_id == conversation_id:
                    self._submissions.pop(key)
                    self._submission_ids.discard(value.record.submission_id)
                    self._tasks.pop(value.record.submission_id, None)
            for task_id, task in tuple(self._tasks.items()):
                if task.conversation_id == conversation_id:
                    self._tasks.pop(task_id, None)
                    for effect_id, effect in tuple(self._effects.items()):
                        if effect.task_id == task_id:
                            self._effects.pop(effect_id, None)

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]:
        with self._lock:
            self._conversation(conversation_id)
            return self._parents[conversation_id]

    def has_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            return conversation_id in self._heads

    def close(self) -> None:
        return None

    def commit_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def commits_after(self, sequence: int, *, limit: int = 512) -> tuple[CommitRecord, ...]:
        with self._lock:
            bounded = max(1, min(2048, int(limit)))
            return tuple(item for item in self._commits if item.sequence > int(sequence))[:bounded]

    def _conversation(self, conversation_id: str) -> tuple[str | None, int]:
        try:
            return self._heads[conversation_id]
        except KeyError as exc:
            raise KeyError(f"unknown conversation: {conversation_id}") from exc

    def _existing_submission(self, draft: SubmissionDraft | None) -> CommitResult | None:
        if draft is None:
            return None
        existing = self._submissions.get((draft.conversation_id, draft.request_id))
        if existing is None:
            return None
        if not same_submission(
            SubmissionDraft(
                existing.record.submission_id,
                draft.request_id,
                draft.conversation_id,
                existing.entry,
                existing.fingerprint,
                placement=existing.record.placement,
                mode=existing.record.mode,
                content=existing.record.content,
            ),
            draft,
        ):
            raise IdempotencyConflict("request_id was already admitted with different input")
        return replace(existing.result, record=None)


def _effect_identity(effect: EffectRecord) -> tuple[str, str, str, str, Mapping[str, object]]:
    return (effect.task_id, effect.category, effect.replay_class, effect.args_digest, effect.intent)
