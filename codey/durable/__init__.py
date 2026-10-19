"""Durable conversation primitives with atomic append and submission admission."""

from codey.durable.context import ContextSnapshot, build_context
from codey.durable.errors import CommitConflict, IdempotencyConflict, SessionSealedError
from codey.durable.memory import MemoryDurableStore
from codey.durable.model import (
    TASK_CHECKPOINT_SCHEMA_VERSION,
    ApprovalRecord,
    ApprovalStatus,
    CommitResult,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionDraft,
    SubmissionRecord,
    SubmissionStatus,
    SubmissionUpdate,
    TaskCheckpoint,
    TaskStatus,
)
from codey.durable.recovery import RecoveryDecision, decide_recovery
from codey.durable.scheduler import DurableTaskScheduler
from codey.durable.session import DurableSession
from codey.durable.sqlite import SQLiteDurableStore
from codey.durable.store import DurableStore

__all__ = [
    "CommitConflict",
    "ApprovalRecord",
    "ApprovalStatus",
    "ContextSnapshot",
    "CommitResult",
    "DurableMutation",
    "DurableSession",
    "DurableTaskScheduler",
    "RecoveryDecision",
    "DurableStore",
    "EffectRecord",
    "EntryDraft",
    "IdempotencyConflict",
    "SessionSealedError",
    "MemoryDurableStore",
    "SQLiteDurableStore",
    "SubmissionDraft",
    "SubmissionRecord",
    "SubmissionStatus",
    "SubmissionUpdate",
    "TaskCheckpoint",
    "TASK_CHECKPOINT_SCHEMA_VERSION",
    "TaskStatus",
    "build_context",
    "decide_recovery",
]
