from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from codey.durable.errors import CommitConflict
from codey.durable.history import HistoryReader
from codey.durable.history_lifetime import HistoryLifetime
from codey.durable.model import (
    ApprovalRecord,
    ArtifactRecord,
    CommitRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionDraft,
    SubmissionPlacement,
    SubmissionRecord,
    SubmissionUpdate,
    TaskCheckpoint,
    validate_submission_outcome,
)
from codey.durable.timeline import TaskSettlementHistory, TaskTransition


def validate_submission_update_state(
    update: SubmissionUpdate, *, current_placement: SubmissionPlacement,
    current_entry_id: str | None, current_mode: str,
) -> None:
    placement = current_placement if update.placement is None else SubmissionPlacement(update.placement)
    if current_placement is SubmissionPlacement.PLACED and placement is SubmissionPlacement.QUEUED:
        raise CommitConflict("placed submission cannot return to queued")
    entry_id = current_entry_id if update.input_entry_id is None else update.input_entry_id
    if placement is SubmissionPlacement.PLACED and entry_id is None:
        raise CommitConflict("placing a submission requires an input entry")
    if placement is SubmissionPlacement.QUEUED and entry_id is not None:
        raise CommitConflict("queued submission cannot reference an input entry")
    try:
        validate_submission_outcome(
            update.status, current_mode if update.mode is None else update.mode,
            input_entry_id=entry_id, answer_entry_id=update.answer_entry_id, detail=update.detail,
        )
    except ValueError as exc:
        raise CommitConflict(str(exc)) from exc


class DurableStore(Protocol):
    history_lifetime: HistoryLifetime

    @property
    def database_id(self) -> str: ...

    def create_conversation(
        self,
        conversation_id: str,
        *,
        parent_conversation_id: str | None = None,
        parent_entry_id: str | None = None,
    ) -> None: ...

    def has_conversation(self, conversation_id: str) -> bool: ...

    def conversation_ids(self) -> tuple[str, ...]: ...

    def append(self, draft: EntryDraft, *, expected_revision: int) -> CommitResult: ...

    def submit(self, draft: SubmissionDraft) -> CommitResult: ...

    def commit(self, mutation: DurableMutation) -> CommitResult: ...

    def submission(self, submission_id: str) -> SubmissionRecord: ...

    def queued_submissions(self, conversation_id: str | None = None) -> tuple[SubmissionRecord, ...]: ...

    def submission_result_for_request(
        self, conversation_id: str, request_id: str, fingerprint: str,
    ) -> CommitResult | None: ...

    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]: ...

    def finalization_queue(self) -> tuple[str, ...]: ...

    def tasks(self) -> tuple[TaskCheckpoint, ...]: ...

    def waiting_dependents(self, task_id: str) -> tuple[TaskCheckpoint, ...]: ...

    def owned_dependents(self, task_id: str) -> tuple[TaskCheckpoint, ...]: ...

    def acquire_scheduler_owner(self, owner_id: str, process_id: int) -> None: ...

    def release_scheduler_owner(self, owner_id: str) -> None: ...

    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]: ...

    def history_snapshot(
        self, *, conversation_id: str | None = None, admission: AbstractContextManager[None] | None = None,
    ) -> AbstractContextManager[HistoryReader]:
        """Acquire the history lease before pinning the snapshot under admission."""
        ...

    def head(self, conversation_id: str) -> tuple[str | None, int]: ...

    def branch(
        self, conversation_id: str, entry_id: str, *, expected_revision: int | None = None,
    ) -> CommitResult: ...

    def fork(self, conversation_id: str, entry_id: str, child_conversation_id: str) -> CommitResult: ...

    def delete_conversation(self, conversation_id: str) -> None: ...

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]: ...

    def task(self, task_id: str) -> TaskCheckpoint: ...

    def task_history(self, task_id: str, *, through: int | None = None) -> tuple[TaskTransition, ...]: ...

    def task_settlement_history(
        self, task_ids: tuple[str, ...], *, through: int,
    ) -> tuple[TaskSettlementHistory, ...]: ...

    def effect(self, effect_id: str) -> EffectRecord: ...

    def approval(self, approval_id: str) -> ApprovalRecord: ...

    def effects_for_task(self, task_id: str) -> tuple[EffectRecord, ...]: ...

    def artifact(self, handle: str) -> ArtifactRecord: ...

    def artifacts_for_task(self, task_id: str) -> tuple[ArtifactRecord, ...]: ...

    def artifacts_for_effect(self, effect_id: str) -> tuple[ArtifactRecord, ...]: ...

    def close(self) -> None: ...

    def commit_sequence(self) -> int: ...

    def commits_after(self, sequence: int, *, limit: int = 512) -> tuple[CommitRecord, ...]: ...
