"""Typed durable business contracts.

The concrete :class:`DurableSession` remains the transaction coordinator.  These
protocols describe the stable capability slices that application services may
depend on without importing the session's internal helpers or store.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from codey.durable.model import (
    ApprovalRecord,
    ApprovalStatus,
    ArtifactRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    SubmissionPlacement,
    SubmissionRecord,
    TaskCheckpoint,
)


@runtime_checkable
class TaskAdmissionPort(Protocol):
    """Admission and generation-boundary operations for task inputs."""

    def withdraw_queued_submission_at(
        self,
        conversation_id: str,
        position: int,
        *,
        expected_submission_id: str | None = None,
    ) -> CommitResult: ...

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
    ) -> CommitResult: ...

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
    ) -> CommitResult: ...

    def place_submission(self, submission_id: str) -> CommitResult: ...

    def place_boundary_inputs(
        self,
        conversation_id: str,
        *,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[SubmissionRecord, ...]: ...

    def commit_generation_boundary(
        self,
        conversation_id: str,
        *,
        task_id: str,
        at: str,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
    ) -> tuple[SubmissionRecord, ...]: ...


@runtime_checkable
class ApprovalPort(Protocol):
    """Durable authorization transitions for external effects."""

    def approval(self, approval_id: str) -> ApprovalRecord: ...

    def create_approval(self, approval: ApprovalRecord) -> CommitResult: ...

    def transition_approval(
        self,
        approval_id: str,
        approval: ApprovalRecord,
        *,
        expected_status: ApprovalStatus | str,
        task: TaskCheckpoint | None = None,
    ) -> CommitResult: ...

    def claim_approval_and_begin_effect(
        self,
        approval_id: str,
        effect: EffectRecord,
        *,
        expected_status: ApprovalStatus | str = ApprovalStatus.APPROVED,
    ) -> CommitResult | None: ...

    def settle_approval(
        self,
        approval_id: str,
        *,
        settlement: Mapping[str, object],
        resume: bool,
        tool_results: tuple[Mapping[str, object], ...] = (),
    ) -> CommitResult | None: ...


@runtime_checkable
class EffectPort(Protocol):
    """Durable intent, settlement, and uncertain-outcome operations."""

    def record_effect_intent(self, task_id: str, effect: EffectRecord) -> CommitResult: ...

    def record_effect_intents(
        self,
        task_id: str,
        effects: tuple[EffectRecord, ...],
    ) -> CommitResult: ...

    def settle_effect(
        self,
        task_id: str,
        effect: EffectRecord,
        *,
        expected_status: str = "pending",
        artifacts: tuple[ArtifactRecord, ...] = (),
    ) -> CommitResult: ...

    def settle_unknown_effect(self, task_id: str, effect_id: str) -> CommitResult: ...


@runtime_checkable
class DurableCommitObserver(Protocol):
    """Observer callback invoked after a durable commit has succeeded."""

    def __call__(self, result: CommitResult, mutation: DurableMutation) -> None: ...


@runtime_checkable
class DurableExecutionPort(Protocol):
    """Minimal execution fencing surface exposed to a task worker."""

    def execution(self, task_id: str, attempt_token: str) -> AbstractContextManager[None]: ...

    def transact_task(
        self,
        task_id: str,
        derive: Callable[[TaskCheckpoint, tuple[EffectRecord, ...], int], DurableMutation | None],
    ) -> CommitResult | None: ...


__all__ = [
    "ApprovalPort",
    "DurableCommitObserver",
    "DurableExecutionPort",
    "EffectPort",
    "TaskAdmissionPort",
]
