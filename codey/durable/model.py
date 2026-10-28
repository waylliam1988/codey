from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import cast

TASK_PHASE_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "accepted": frozenset({"dispatching", "running", "failed", "cancelled"}),
    "dispatching": frozenset({"accepted", "running", "failed", "cancelled"}),
    # Task lifecycle only; operation owns provider/tool execution position.
    "running": frozenset({"completion_pending", "failed", "cancelled"}),
    "completion_pending": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(), "failed": frozenset(), "cancelled": frozenset(),
})

TASK_CHECKPOINT_SCHEMA_VERSION = 2
_V1_EXECUTION_PHASES = frozenset({
    "provider_prepared", "assistant_intent_committed", "assistant_response_pending",
    "assistant_response_unknown", "tool_intent_committed", "tool_effect_running",
    "tool_outcome_ready", "effect_unknown", "verification_pending",
})


def migrate_checkpoint_phase(phase: str, schema_version: int) -> tuple[str, int]:
    """Versioned format migration; operation/effects retain execution facts."""
    if type(schema_version) is not int or schema_version not in {1, TASK_CHECKPOINT_SCHEMA_VERSION}:
        raise ValueError(f"unsupported task checkpoint schema: {schema_version!r}")
    if schema_version == 1 and phase in _V1_EXECUTION_PHASES:
        return "running", TASK_CHECKPOINT_SCHEMA_VERSION
    if phase not in TASK_PHASE_TRANSITIONS:
        raise ValueError(f"unknown task phase: {phase}")
    return phase, TASK_CHECKPOINT_SCHEMA_VERSION
FROZEN_TASK_STATE_KEYS = (
    "input_entry_id",
    "input_identity",
    "input_request",
    "task_policy",
    "reviewer_selection",
    "workflow_origin",
    # Ownership is immutable: a task cannot move between owners after it has
    # been admitted.  The scheduler derives cancellation and finalization
    # from this durable edge rather than from an in-memory task tree.
    "owner_task_id",
    "abandon_on_restart",
    "workflow_name",
    "workflow_input",
    "workflow_replay_contract",
)


def valid_frozen_task_state(previous: Mapping[str, object], current: Mapping[str, object]) -> bool:
    return all(key not in previous or (key in current and thaw_json(previous[key]) == thaw_json(current[key]))
               for key in FROZEN_TASK_STATE_KEYS)


def _validate_task_scheduler_state(state: Mapping[str, object]) -> None:
    owner = state.get("owner_task_id")
    if owner is not None and (not isinstance(owner, str) or not owner):
        raise ValueError("owner task ID must be a non-empty string")
    waiting_on = state.get("waiting_on", ())
    if not isinstance(waiting_on, (list, tuple)):
        raise ValueError("waiting task dependencies must be a list")
    if any(not isinstance(item, str) or not item for item in waiting_on):
        raise ValueError("waiting task dependencies must be non-empty strings")
    if len(waiting_on) != len(set(waiting_on)):
        raise ValueError("waiting task dependencies must be unique")
    join_policy = state.get("join_policy", "allSettled")
    if join_policy not in {"failFast", "allSettled"}:
        raise ValueError("unknown task join policy")
    abandon = state.get("abandon_on_restart", False)
    if type(abandon) is not bool:
        raise ValueError("abandon_on_restart must be boolean")


def _validate_workflow_state(state: Mapping[str, object]) -> None:
    workflow_name = state.get("workflow_name")
    if workflow_name is not None and (not isinstance(workflow_name, str) or not workflow_name):
        raise ValueError("workflow name must be a non-empty string")
    workflow_version = state.get("workflow_version", 1)
    if type(workflow_version) is not int or workflow_version < 1:
        raise ValueError("workflow version must be a positive integer")
    for key in ("workflow_input", "workflow_checkpoint"):
        if not isinstance(state.get(key, {}), Mapping):
            raise ValueError(f"{key} must be an object")
    replay_digest = state.get("workflow_replay_digest")
    if replay_digest is not None and (not isinstance(replay_digest, str) or not replay_digest):
        raise ValueError("workflow replay digest must be a non-empty string")


def valid_task_transition(previous: str | None, current: str) -> bool:
    if previous is None:
        # Direct operation callers can admit and immediately own a task while
        # the scheduler only dispatches ``accepted`` checkpoints.  Both are
        # valid initial durable states; the former is already in-flight.
        return current in {"accepted", "dispatching"}
    if previous == current:
        # Durable metadata updates (for example an input projection state)
        # advance the checkpoint revision without changing the logical phase.
        return previous in TASK_PHASE_TRANSITIONS
    return current in TASK_PHASE_TRANSITIONS.get(previous, frozenset())


def valid_input_consumption(previous_phase: str, previous_status: str,
                            previous_state: Mapping[str, object], current: TaskCheckpoint) -> bool:
    """A queued steering receipt may settle without starting a provider task.

    This exception does not widen the phase/status graph for executable tasks.
    The generation boundary records the consuming task and entry atomically.
    """
    consumer = current.state.get("consumed_by_task_id")
    return (
        previous_phase == "accepted" and previous_status == "pending"
        and previous_state.get("input_mode") == "steer"
        and previous_state.get("input_state") == "queued"
        and not previous_state.get("attempt_token")
        and current.phase == "completed" and str(current.status) == "completed"
        and current.state.get("input_state") == "consumed"
        and isinstance(consumer, str) and bool(consumer) and consumer != current.task_id
        and isinstance(current.state.get("input_entry_id"), str)
        and not current.attempt_token
    )


class SubmissionStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    UNANSWERED = "unanswered"
    CANCELLED = "cancelled"


class SubmissionPlacement(StrEnum):
    """Whether an admitted submission has entered the conversation history."""

    QUEUED = "queued"
    PLACED = "placed"


class ApprovalStatus(StrEnum):
    """Durable shell permission lifecycle, separate from scheduler status."""

    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXECUTION_CLAIMED = "execution_claimed"
    EXECUTION_SETTLED = "execution_settled"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


APPROVAL_STATUS_TRANSITIONS: Mapping[ApprovalStatus, frozenset[ApprovalStatus]] = MappingProxyType(
    {
        ApprovalStatus.REQUESTED: frozenset(
            {ApprovalStatus.APPROVED, ApprovalStatus.DENIED, ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED}
        ),
        ApprovalStatus.APPROVED: frozenset(
            {ApprovalStatus.EXECUTION_CLAIMED, ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED}
        ),
        ApprovalStatus.EXECUTION_CLAIMED: frozenset({ApprovalStatus.EXECUTION_SETTLED}),
        ApprovalStatus.DENIED: frozenset(),
        ApprovalStatus.EXECUTION_SETTLED: frozenset(),
        ApprovalStatus.EXPIRED: frozenset(),
        ApprovalStatus.CANCELLED: frozenset(),
    }
)


def valid_approval_transition(previous: ApprovalStatus | None, current: ApprovalStatus) -> bool:
    if previous is None:
        return current == ApprovalStatus.REQUESTED
    return current in APPROVAL_STATUS_TRANSITIONS.get(previous, frozenset())


class TaskStatus(StrEnum):
    """Scheduler ownership state, independent from the task's logical phase."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


# Scheduler ownership is separate from the logical task phase. Keep the legal
# edges explicit so a corrupted checkpoint cannot silently move from queued
# work into a waiting or completing state.
TASK_STATUS_TRANSITIONS: Mapping[TaskStatus, frozenset[TaskStatus]] = MappingProxyType(
    {
        TaskStatus.PENDING: frozenset(
            {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.RUNNING: frozenset(
            {
                TaskStatus.RUNNING,
                TaskStatus.PENDING,
                TaskStatus.WAITING,
                TaskStatus.COMPLETING,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.WAITING: frozenset(
            {
                TaskStatus.WAITING,
                TaskStatus.RUNNING,
                TaskStatus.PENDING,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.COMPLETING: frozenset(
            {
                TaskStatus.COMPLETING,
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.COMPLETED: frozenset({TaskStatus.COMPLETED}),
        TaskStatus.FAILED: frozenset({TaskStatus.FAILED}),
        TaskStatus.CANCELLED: frozenset({TaskStatus.CANCELLED}),
    }
)


def task_status_for_phase(phase: str) -> TaskStatus:
    if phase == "completed":
        return TaskStatus.COMPLETED
    if phase == "failed":
        return TaskStatus.FAILED
    if phase == "cancelled":
        return TaskStatus.CANCELLED
    if phase == "completion_pending":
        return TaskStatus.COMPLETING
    return TaskStatus.PENDING if phase == "accepted" else TaskStatus.RUNNING


def valid_task_status_transition(previous: TaskStatus | None, current: TaskStatus) -> bool:
    if previous is None:
        return current in {TaskStatus.PENDING, TaskStatus.RUNNING}
    return current in TASK_STATUS_TRANSITIONS.get(previous, frozenset())


SUBMISSION_TRANSITIONS: Mapping[SubmissionStatus, frozenset[SubmissionStatus]] = MappingProxyType(
    {
        SubmissionStatus.QUEUED: frozenset({
            SubmissionStatus.RUNNING,
            SubmissionStatus.DONE,
            SubmissionStatus.UNANSWERED,
            SubmissionStatus.CANCELLED,
        }),
        SubmissionStatus.RUNNING: frozenset(
            {SubmissionStatus.DONE, SubmissionStatus.UNANSWERED, SubmissionStatus.CANCELLED}
        ),
        SubmissionStatus.DONE: frozenset(),
        SubmissionStatus.UNANSWERED: frozenset(),
        SubmissionStatus.CANCELLED: frozenset(),
    }
)


def valid_submission_transition(previous: SubmissionStatus, current: SubmissionStatus) -> bool:
    return current in SUBMISSION_TRANSITIONS[previous]


def _validate_json(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("durable payload must contain strict JSON values") from exc


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class EntryDraft:
    entry_id: str
    conversation_id: str
    parent_id: str | None
    kind: str
    payload: Mapping[str, object]
    commit_seq: int = 0
    by_task_id: str | None = None

    def __post_init__(self) -> None:
        if not self.entry_id or not self.conversation_id or not self.kind:
            raise ValueError("entry identity and kind are required")
        if not isinstance(self.payload, Mapping):
            raise ValueError("entry payload must be a mapping")
        copied = cast(dict[str, object], thaw_json(self.payload))
        _validate_json(copied)
        object.__setattr__(self, "payload", _freeze_json(copied))

    @property
    def model(self) -> Mapping[str, object] | None:
        """Optional provider/model metadata attached to a passive entry."""
        value = self.payload.get("model")
        return value if isinstance(value, Mapping) else None

    @property
    def head(self) -> str | None:
        """The context head target requested by this passive entry, if any."""
        value = self.payload.get("head")
        return value if isinstance(value, str) and value else None

    @property
    def edits(self) -> tuple[Mapping[str, object], ...]:
        """Validated context edits carried by this entry."""
        value = self.payload.get("edits")
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))


@dataclass(frozen=True)
class SubmissionDraft:
    submission_id: str
    request_id: str
    conversation_id: str
    entry: EntryDraft | None
    fingerprint: str = ""
    status: SubmissionStatus = SubmissionStatus.QUEUED
    placement: SubmissionPlacement | str = SubmissionPlacement.PLACED
    mode: str = "follow_up"
    content: Mapping[str, object] | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.submission_id or not self.request_id or not self.conversation_id:
            raise ValueError("submission identity and request ID are required")
        placement = SubmissionPlacement(self.placement)
        object.__setattr__(self, "placement", placement)
        if self.entry is not None and self.entry.conversation_id != self.conversation_id:
            raise ValueError("submission entry belongs to another conversation")
        if self.mode not in {"steer", "follow_up", "write"}:
            raise ValueError("unknown submission mode")
        object.__setattr__(self, "status", SubmissionStatus(self.status))
        valid_write_terminal = (
            self.mode == "write"
            and self.status is SubmissionStatus.DONE
            and placement is SubmissionPlacement.PLACED
        ) or (
            self.mode == "write"
            and self.status is SubmissionStatus.UNANSWERED
            and placement is SubmissionPlacement.QUEUED
            and self.reason == "stale"
        )
        if self.status != SubmissionStatus.QUEUED and not valid_write_terminal:
            raise ValueError("new input submissions must start queued")
        if placement is SubmissionPlacement.PLACED and self.entry is None:
            raise ValueError("placed submissions require an entry")
        if placement is SubmissionPlacement.QUEUED and self.entry is not None:
            raise ValueError("queued submissions cannot already have an entry")
        if self.content is not None:
            content = cast(dict[str, object], thaw_json(self.content))
            _validate_json(content)
            object.__setattr__(self, "content", _freeze_json(content))
        if placement is SubmissionPlacement.QUEUED and self.content is None:
            raise ValueError("queued submissions require content")
        if self.reason is not None and not self.reason:
            raise ValueError("submission reason cannot be empty")


@dataclass(frozen=True)
class SubmissionRecord:
    submission_id: str
    conversation_id: str
    request_id: str
    status: SubmissionStatus
    input_entry_id: str | None
    answer_entry_id: str | None = None
    reason: str | None = None
    detail: Mapping[str, object] | None = None
    placement: SubmissionPlacement | str = SubmissionPlacement.PLACED
    mode: str = "follow_up"
    content: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not self.submission_id or not self.conversation_id or not self.request_id:
            raise ValueError("submission record identity is required")
        object.__setattr__(self, "status", SubmissionStatus(self.status))
        object.__setattr__(self, "placement", SubmissionPlacement(self.placement))
        if self.mode not in {"steer", "follow_up", "write"}:
            raise ValueError("unknown submission mode")
        if self.placement is SubmissionPlacement.PLACED and not self.input_entry_id:
            raise ValueError("placed submissions require an input entry")
        if self.placement is SubmissionPlacement.QUEUED and self.input_entry_id is not None:
            raise ValueError("queued submissions cannot reference an input entry")
        if self.detail is not None:
            detail = cast(dict[str, object], thaw_json(self.detail))
            _validate_json(detail)
            object.__setattr__(self, "detail", _freeze_json(detail))
        if self.content is not None:
            content = cast(dict[str, object], thaw_json(self.content))
            _validate_json(content)
            object.__setattr__(self, "content", _freeze_json(content))
        if self.status == SubmissionStatus.DONE:
            if self.mode == "write":
                if not self.input_entry_id:
                    raise ValueError("completed writes require an entry")
            elif (self.mode == "steer" and isinstance(self.detail, Mapping)
                  and isinstance(self.detail.get("consumed_by_task_id"), str)
                  and self.detail.get("consumed_by_task_id") and self.input_entry_id):
                pass
            elif not self.answer_entry_id:
                raise ValueError("completed submissions require an answer entry")
        if self.status != SubmissionStatus.DONE and self.answer_entry_id is not None:
            raise ValueError("only completed submissions may reference an answer entry")


@dataclass(frozen=True)
class SubmissionUpdate:
    submission_id: str
    conversation_id: str
    expected_status: SubmissionStatus
    status: SubmissionStatus
    answer_entry_id: str | None = None
    reason: str | None = None
    detail: Mapping[str, object] | None = None
    input_entry_id: str | None = None
    placement: SubmissionPlacement | str | None = None
    content: Mapping[str, object] | None = None
    mode: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_status", SubmissionStatus(self.expected_status))
        object.__setattr__(self, "status", SubmissionStatus(self.status))
        if self.mode is not None and self.mode not in {"steer", "follow_up"}:
            raise ValueError("invalid queued input mode")
        if self.content is not None:
            content = cast(dict[str, object], thaw_json(self.content))
            _validate_json(content)
            object.__setattr__(self, "content", _freeze_json(content))
        if self.expected_status != self.status and not valid_submission_transition(self.expected_status, self.status):
            raise ValueError(
                f"invalid submission transition: {self.expected_status} -> {self.status}"
            )
        if self.detail is not None:
            detail = cast(dict[str, object], thaw_json(self.detail))
            _validate_json(detail)
            object.__setattr__(self, "detail", _freeze_json(detail))
        if self.status == SubmissionStatus.DONE and self.answer_entry_id is None and self.input_entry_id is None:
            raise ValueError("completed submissions require an entry")
        if self.status != SubmissionStatus.DONE and self.answer_entry_id is not None:
            raise ValueError("only completed submissions may reference an answer entry")
        if self.placement is not None:
            object.__setattr__(self, "placement", SubmissionPlacement(self.placement))


@dataclass(frozen=True)
class ApprovalRecord:
    """The current durable permission decision for one shell invocation.

    The command artifact and digest are immutable identity.  Every lifecycle
    transition increments ``revision`` and is committed with the task
    checkpoint that authorizes the next action.
    """

    approval_id: str
    task_id: str
    conversation_id: str
    status: ApprovalStatus | str
    command_digest: str
    command_ref: str
    cwd: str
    revision: int = 0
    settlement: Mapping[str, object] | None = None
    # Continuation ownership is part of the durable approval state.  It used
    # to exist only as a sequence of JSONL facts, which made restart behavior
    # depend on an optional audit file.
    continuation_status: str = "waiting_for_result"
    continuation_run_id: str = ""

    def __post_init__(self) -> None:
        if not self.approval_id or not self.task_id or not self.conversation_id:
            raise ValueError("approval identity is required")
        object.__setattr__(self, "status", ApprovalStatus(self.status))
        if self.revision < 0:
            raise ValueError("approval revision cannot be negative")
        if not self.command_digest or not self.command_ref or not self.cwd:
            raise ValueError("approval command identity is required")
        if self.continuation_status not in {
            "waiting_for_result", "queued", "resuming", "started", "failed",
            "unavailable", "cancelled", "not_required",
        }:
            raise ValueError("invalid approval continuation status")
        if self.settlement is not None:
            settlement = cast(dict[str, object], thaw_json(self.settlement))
            _validate_json(settlement)
            object.__setattr__(self, "settlement", _freeze_json(settlement))

    def to_payload(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "status": ApprovalStatus(self.status).value,
            "command_digest": self.command_digest,
            "command_ref": self.command_ref,
            "cwd": self.cwd,
            "revision": self.revision,
            "settlement": None if self.settlement is None else thaw_json(self.settlement),
            "continuation_status": self.continuation_status,
            "continuation_run_id": self.continuation_run_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ApprovalRecord:
        raw_revision = payload.get("revision")
        revision = int(raw_revision) if isinstance(raw_revision, (int, str)) else 0
        raw_settlement = payload.get("settlement")
        settlement = cast(Mapping[str, object], raw_settlement) if isinstance(raw_settlement, Mapping) else None
        return cls(
            approval_id=str(payload.get("approval_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            status=ApprovalStatus(str(payload.get("status") or "")),
            command_digest=str(payload.get("command_digest") or ""),
            command_ref=str(payload.get("command_ref") or ""),
            cwd=str(payload.get("cwd") or ""),
            revision=revision,
            settlement=settlement,
            continuation_status=str(payload.get("continuation_status") or "waiting_for_result"),
            continuation_run_id=str(payload.get("continuation_run_id") or ""),
        )


def same_entry_content(first: EntryDraft, second: EntryDraft) -> bool:
    return (
        first.conversation_id == second.conversation_id
        and first.parent_id == second.parent_id
        and first.kind == second.kind
        and dict(first.payload) == dict(second.payload)
    )


def same_submission(first: SubmissionDraft, second: SubmissionDraft) -> bool:
    if (
        first.placement != second.placement
        or first.mode != second.mode
        or first.status != second.status
        or first.reason != second.reason
    ):
        return False
    if first.entry is None or second.entry is None:
        if first.entry is not None or second.entry is not None:
            return False
    elif (
        first.entry.conversation_id != second.entry.conversation_id
        or first.entry.kind != second.entry.kind
        or dict(first.entry.payload) != dict(second.entry.payload)
    ):
        return False
    if dict(first.content or {}) != dict(second.content or {}):
        return False
    # A request id names one exact admission, including the caller's
    # canonical fingerprint.  Treat a missing fingerprint as the empty
    # fingerprint so a low-level store cannot silently accept a retry whose
    # durable result differs merely because one path omitted the field.
    return first.fingerprint == second.fingerprint


@dataclass(frozen=True)
class CommitRecord:
    """Bounded public projection, stored atomically with the facts it names."""

    sequence: int
    kind: str
    committed_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        payload = cast(dict[str, object], thaw_json(self.payload))
        _validate_json(payload)
        object.__setattr__(self, "payload", _freeze_json(payload))

    def to_payload(self) -> dict[str, object]:
        return {
            **cast(dict[str, object], thaw_json(self.payload)),
            "type": "durable_commit",
            "commit_seq": self.sequence,
            "kind": self.kind,
            "committed_at": self.committed_at,
            "schema_version": 1,
        }


@dataclass(frozen=True)
class CommitResult:
    revision: int
    sequence: int
    submission_id: str | None = None
    # Replayed admissions and no-ops have no new record to publish.
    record: CommitRecord | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TaskCheckpoint:
    task_id: str
    conversation_id: str
    submission_id: str
    phase: str
    revision: int
    provider_snapshot: Mapping[str, object]
    state: Mapping[str, object]
    current_turn_id: str | None = None
    pending_effect_id: str | None = None
    status: TaskStatus | str | None = None
    schema_version: int = TASK_CHECKPOINT_SCHEMA_VERSION

    @property
    def attempt_token(self) -> str:
        value = self.state.get("attempt_token")
        return value if isinstance(value, str) else ""

    @property
    def owner_task_id(self) -> str | None:
        value = self.state.get("owner_task_id")
        return value if isinstance(value, str) and value else None

    @property
    def waiting_on(self) -> tuple[str, ...]:
        value = self.state.get("waiting_on", ())
        if isinstance(value, (list, tuple)):
            return tuple(item for item in value if isinstance(item, str))
        return ()

    @property
    def join_policy(self) -> str:
        value = self.state.get("join_policy", "allSettled")
        return value if isinstance(value, str) and value in {"failFast", "allSettled"} else "allSettled"

    @property
    def workflow_name(self) -> str | None:
        value = self.state.get("workflow_name")
        return value if isinstance(value, str) and value else None

    @property
    def workflow_version(self) -> int:
        value = self.state.get("workflow_version", 1)
        return value if type(value) is int and value >= 1 else 1

    @property
    def workflow_input(self) -> Mapping[str, object]:
        value = self.state.get("workflow_input", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def workflow_checkpoint(self) -> Mapping[str, object]:
        value = self.state.get("workflow_checkpoint", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def replay_digest(self) -> str | None:
        value = self.state.get("workflow_replay_digest")
        return value if isinstance(value, str) and value else None

    def __post_init__(self) -> None:
        if not self.task_id or not self.conversation_id or not self.submission_id or not self.phase:
            raise ValueError("task identity and phase are required")
        if self.phase not in TASK_PHASE_TRANSITIONS:
            raise ValueError(f"unknown task phase: {self.phase}")
        if self.revision < 0:
            raise ValueError("task revision cannot be negative")
        if type(self.schema_version) is not int or self.schema_version != TASK_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(f"unsupported task checkpoint schema: {self.schema_version!r}")
        status = task_status_for_phase(self.phase) if self.status is None else TaskStatus(self.status)
        if status in TASK_TERMINAL_STATUSES and self.phase != status.value:
            raise ValueError("terminal task status must match its terminal phase")
        if status not in TASK_TERMINAL_STATUSES and self.phase in {"completed", "failed", "cancelled"}:
            raise ValueError("terminal task phase requires a terminal status")
        object.__setattr__(self, "status", status)
        provider_snapshot = cast(dict[str, object], thaw_json(self.provider_snapshot))
        state = cast(dict[str, object], thaw_json(self.state))
        _validate_json(provider_snapshot)
        _validate_json(state)
        _validate_task_scheduler_state(state)
        _validate_workflow_state(state)
        attempt = state.get("attempt_number", 0)
        token = state.get("attempt_token", "")
        owner = state.get("attempt_owner", "")
        process_id = state.get("attempt_process_id", 0)
        if (type(attempt) is not int or attempt < 0 or not isinstance(token, str)
                or not isinstance(owner, str) or type(process_id) is not int or process_id < 0):
            raise ValueError("invalid task execution attempt")
        if bool(token) != bool(owner) or (token and attempt == 0):
            raise ValueError("invalid task execution attempt ownership")
        if token and process_id <= 0:
            raise ValueError("running task attempt requires an owner process")
        if not token and process_id != 0:
            raise ValueError("unowned task cannot retain an owner process")
        pending_ids = state.get("pending_effect_ids")
        if pending_ids is not None:
            if not isinstance(pending_ids, list):
                raise ValueError("pending effect IDs must be a list")
            if any(not isinstance(effect_id, str) or not effect_id for effect_id in pending_ids):
                raise ValueError("pending effect IDs must be non-empty strings")
            if len(pending_ids) != len(set(pending_ids)):
                raise ValueError("pending effect IDs must be unique")
            expected_pending = pending_ids[-1] if pending_ids else None
            if self.pending_effect_id != expected_pending:
                raise ValueError("pending effect checkpoint is inconsistent")
        elif self.pending_effect_id is not None:
            raise ValueError("pending effect checkpoint is inconsistent")
        object.__setattr__(self, "provider_snapshot", _freeze_json(provider_snapshot))
        object.__setattr__(self, "state", _freeze_json(state))


@dataclass(frozen=True)
class EffectRecord:
    effect_id: str
    task_id: str
    category: str
    replay_class: str
    args_digest: str
    status: str
    intent: Mapping[str, object]
    settlement: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not all((self.effect_id, self.task_id, self.category, self.replay_class, self.args_digest, self.status)):
            raise ValueError("effect identity and classification are required")
        intent = cast(dict[str, object], thaw_json(self.intent))
        _validate_json(intent)
        object.__setattr__(self, "intent", _freeze_json(intent))
        if self.settlement is not None:
            settlement = cast(dict[str, object], thaw_json(self.settlement))
            _validate_json(settlement)
            object.__setattr__(self, "settlement", _freeze_json(settlement))


@dataclass(frozen=True)
class ArtifactRecord:
    """Durable metadata for bytes stored outside the SQLite payload tables."""

    handle: str
    sha256: str
    byte_count: int
    storage_path: str
    owner_task_id: str | None = None
    owner_effect_id: str | None = None
    created_at: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.handle or not self.sha256 or len(self.sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.sha256
        ):
            raise ValueError("artifact handle and sha256 are required")
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("artifact byte count must be a non-negative integer")
        if not self.storage_path:
            raise ValueError("artifact storage path is required")
        if self.owner_task_id is None and self.owner_effect_id is None:
            raise ValueError("artifact must have a task or effect owner")
        if self.owner_task_id is not None and not self.owner_task_id:
            raise ValueError("artifact task owner cannot be empty")
        if self.owner_effect_id is not None and not self.owner_effect_id:
            raise ValueError("artifact effect owner cannot be empty")
        metadata = cast(dict[str, object], thaw_json(self.metadata))
        _validate_json(metadata)
        object.__setattr__(self, "metadata", _freeze_json(metadata))


def _validate_artifact_handles(artifacts: tuple[ArtifactRecord, ...]) -> None:
    handles = [artifact.handle for artifact in artifacts]
    if len(handles) != len(set(handles)):
        raise ValueError("artifact handles must be unique within a mutation")


@dataclass(frozen=True)
class CreateConversation:
    parent_conversation_id: str | None = None
    parent_entry_id: str | None = None

    def __post_init__(self) -> None:
        if bool(self.parent_conversation_id) != bool(self.parent_entry_id):
            raise ValueError("a fork requires both its parent conversation and entry cap")


@dataclass(frozen=True)
class DeleteConversation:
    pass


@dataclass(frozen=True)
class DurableMutation:
    conversation_id: str
    expected_revision: int
    entries: tuple[EntryDraft, ...] = ()
    submission: SubmissionDraft | None = None
    task: TaskCheckpoint | None = None
    task_updates: tuple[TaskCheckpoint, ...] = ()
    effect: EffectRecord | None = None
    effects: tuple[EffectRecord, ...] = ()
    expected_effect_status: str | None = None
    submission_update: SubmissionUpdate | None = None
    submission_updates: tuple[SubmissionUpdate, ...] = ()
    settlements: tuple[EffectRecord, ...] = ()
    artifacts: tuple[ArtifactRecord, ...] = ()
    expected_attempt_token: str | None = None
    approval: ApprovalRecord | None = None
    expected_approval_status: ApprovalStatus | str | None = None
    create_conversation: CreateConversation | None = None
    delete_conversation: DeleteConversation | None = None
    # Repoint the active conversation head without appending an entry.
    # Kept as an explicit mutation so branch commits share the same CAS,
    # commit sequence, and projection path as every other durable change.
    branch_entry_id: str | None = None
    edited_entry_id: str | None = None
    replaced_run_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.conversation_id or self.expected_revision < 0:
            raise ValueError("conversation identity and non-negative revision are required")
        _validate_mutation_shape(self)


def _validate_mutation_shape(mutation: DurableMutation) -> None:
    if mutation.edited_entry_id is not None:
        if (not mutation.edited_entry_id or mutation.branch_entry_id is not None
                or mutation.create_conversation is not None or mutation.delete_conversation is not None
                or len(mutation.entries) != 1 or mutation.entries[0].kind != "user_message"
                or mutation.submission is None or mutation.task is None
                or any((mutation.task_updates, mutation.effect, mutation.effects, mutation.submission_update,
                        mutation.submission_updates, mutation.settlements, mutation.artifacts, mutation.approval))):
            raise ValueError("an edit requires one replacement input, submission and task")
    elif mutation.replaced_run_ids:
        raise ValueError("replaced runs require an edit")
    if mutation.create_conversation is not None and mutation.delete_conversation is not None:
        raise ValueError("a mutation cannot create and delete a conversation")
    if mutation.delete_conversation is not None and any((
        mutation.entries, mutation.submission, mutation.task, mutation.task_updates,
        mutation.effect, mutation.effects, mutation.submission_update, mutation.submission_updates,
        mutation.settlements, mutation.artifacts, mutation.approval,
    )):
        raise ValueError("conversation deletion cannot include other durable changes")
    if mutation.branch_entry_id is not None:
        if not isinstance(mutation.branch_entry_id, str) or not mutation.branch_entry_id:
            raise ValueError("branch_entry_id must be a non-empty string")
        if mutation.create_conversation is not None or mutation.delete_conversation is not None or any((
            mutation.entries, mutation.submission, mutation.task, mutation.task_updates,
            mutation.effect, mutation.effects, mutation.submission_update,
            mutation.submission_updates, mutation.settlements, mutation.artifacts,
            mutation.approval,
        )):
            raise ValueError("branch cannot include other durable changes")
    if any(entry.conversation_id != mutation.conversation_id for entry in mutation.entries):
        raise ValueError("all mutation entries must belong to its conversation")
    if mutation.submission is not None:
        if mutation.submission.conversation_id != mutation.conversation_id:
            raise ValueError("submission belongs to another conversation")
        if mutation.submission.entry is not None and mutation.submission.entry not in mutation.entries:
            raise ValueError("submission entry must be part of the same mutation")
    _validate_mutation_tasks(mutation)
    _validate_mutation_effects(mutation)
    _validate_mutation_submissions(mutation)
    if mutation.approval is not None:
        if mutation.approval.conversation_id != mutation.conversation_id:
            raise ValueError("approval belongs to another conversation")
        if mutation.task is not None and mutation.approval.task_id != mutation.task.task_id:
            raise ValueError("approval must belong to the task in this mutation")
    if mutation.expected_approval_status is not None and mutation.approval is None:
        raise ValueError("expected_approval_status requires an approval update")


def _validate_mutation_tasks(mutation: DurableMutation) -> None:
    all_tasks = ((mutation.task,) if mutation.task is not None else ()) + mutation.task_updates
    task_ids = [task.task_id for task in all_tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task IDs must be unique within a mutation")
    if any(task.conversation_id != mutation.conversation_id for task in all_tasks):
        raise ValueError("task belongs to another conversation")


def _validate_mutation_effects(mutation: DurableMutation) -> None:
    if mutation.effect is not None and mutation.task is not None and mutation.effect.task_id != mutation.task.task_id:
        raise ValueError("effect must belong to the task in this mutation")
    effect_ids = [effect.effect_id for effect in (*mutation.effects, *mutation.settlements)]
    if len(effect_ids) != len(set(effect_ids)):
        raise ValueError("effect IDs must be unique within a mutation")
    effect_task_ids = {effect.task_id for effect in (*mutation.effects, *mutation.settlements)} | (
        {mutation.effect.task_id} if mutation.effect is not None else set()
    )
    if len(effect_task_ids) > 1 or (
        mutation.task is not None and effect_task_ids and effect_task_ids != {mutation.task.task_id}
    ):
        raise ValueError("effects must belong to one task in this mutation")
    if mutation.effect is not None and mutation.effect.effect_id in effect_ids:
        raise ValueError("effect cannot be listed twice in a mutation")
    _validate_artifact_handles(mutation.artifacts)
    if mutation.expected_effect_status is not None and mutation.effect is None:
        raise ValueError("expected_effect_status requires an effect update")
    if any(effect.settlement is None or effect.status == "pending" for effect in mutation.settlements):
        raise ValueError("effect settlements require a terminal outcome")


def _validate_mutation_submissions(mutation: DurableMutation) -> None:
    all_updates = ((mutation.submission_update,) if mutation.submission_update is not None else ()) + mutation.submission_updates
    update_ids = [update.submission_id for update in all_updates]
    if len(update_ids) != len(set(update_ids)):
        raise ValueError("submission IDs must be unique within a mutation")
    if any(update.conversation_id != mutation.conversation_id for update in all_updates):
        raise ValueError("submission update belongs to another conversation")
    tasks = ((mutation.task,) if mutation.task is not None else ()) + mutation.task_updates
    for update in all_updates:
        if update.content is None and update.mode is None:
            continue
        task = next((task for task in tasks if task.submission_id == update.submission_id), None)
        if (task is None or task.phase != "accepted" or task.status is not TaskStatus.PENDING
                or task.state.get("input_state") != "queued" or task.attempt_token):
            raise ValueError("queued input revision requires its pending task in the same commit")
        if update.content is not None and update.content.get("text") != task.state.get("input_text"):
            raise ValueError("queued input text must match its task")
        if update.mode is not None and update.mode != task.state.get("input_mode"):
            raise ValueError("queued input mode must match its task")


EXECUTION_STATE_KEYS = (
    "attempt_token",
    "attempt_owner",
    "attempt_process_id",
    "attempt_number",
    "input_state",
    "question",
    "resume_input",
    "resume_entry_id",
)


def valid_execution_attempt(previous_state: Mapping[str, object], previous_status: TaskStatus,
                            current: TaskCheckpoint, expected: str | None) -> bool:
    """Fence mutations in the same transaction that advances their checkpoint."""
    previous_token = previous_state.get("attempt_token", "")
    if previous_token != (expected or ""):
        return False
    old_number = previous_state.get("attempt_number", 0)
    new_number = current.state.get("attempt_number", 0)
    if current.attempt_token == previous_token:
        return (old_number == new_number
                and previous_state.get("attempt_owner", "") == current.state.get("attempt_owner", ""))
    if not current.attempt_token:
        return current.status in {
            TaskStatus.PENDING,
            TaskStatus.WAITING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        } and new_number == old_number
    return (previous_status == TaskStatus.PENDING and current.status == TaskStatus.RUNNING
            and not previous_token and type(old_number) is int and new_number == old_number + 1)
