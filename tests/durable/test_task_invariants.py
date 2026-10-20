from __future__ import annotations

import pytest

from codey.durable.model import (
    EffectRecord,
    TaskCheckpoint,
    TaskStatus,
    task_status_for_phase,
    valid_task_status_transition,
)
from codey.durable.reducer import with_effect_intents


def _checkpoint(
    *,
    phase: str = "assistant_response_pending",
    status: TaskStatus | str | None = None,
    state: dict[str, object] | None = None,
    pending_effect_id: str | None = None,
) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id="task-1",
        conversation_id="conversation-1",
        submission_id="submission-1",
        phase=phase,
        revision=0,
        provider_snapshot={"provider_id": "local", "model_id": "test"},
        state={} if state is None else state,
        status=status,
        pending_effect_id=pending_effect_id,
    )


def test_scheduler_status_transition_matrix_is_explicit() -> None:
    assert valid_task_status_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
    assert valid_task_status_transition(TaskStatus.RUNNING, TaskStatus.PENDING)
    assert valid_task_status_transition(TaskStatus.RUNNING, TaskStatus.WAITING)
    assert valid_task_status_transition(TaskStatus.WAITING, TaskStatus.RUNNING)
    assert valid_task_status_transition(TaskStatus.RUNNING, TaskStatus.COMPLETING)
    assert valid_task_status_transition(TaskStatus.COMPLETING, TaskStatus.COMPLETED)

    assert valid_task_status_transition(TaskStatus.PENDING, TaskStatus.WAITING)
    assert not valid_task_status_transition(TaskStatus.WAITING, TaskStatus.COMPLETING)
    assert not valid_task_status_transition(TaskStatus.COMPLETING, TaskStatus.RUNNING)
    assert not valid_task_status_transition(TaskStatus.COMPLETED, TaskStatus.RUNNING)


def test_completion_phase_owns_completing_scheduler_status() -> None:
    assert task_status_for_phase("completion_pending") is TaskStatus.COMPLETING


def test_tool_effect_intents_can_be_committed_as_each_call_is_admitted() -> None:
    current = _checkpoint(
        phase="tool_effect_running",
        state={"pending_effect_ids": ["effect-1"]},
        pending_effect_id="effect-1",
    )
    next_checkpoint = with_effect_intents(
        current,
        (
            EffectRecord(
                effect_id="effect-2",
                task_id="task-1",
                category="tool_call",
                replay_class="safe",
                args_digest="b" * 64,
                status="pending",
                intent={"effect_id": "effect-2"},
            ),
        ),
    )
    assert next_checkpoint.phase == "tool_effect_running"
    assert next_checkpoint.state["pending_effect_ids"] == ("effect-1", "effect-2")


def test_tool_effect_intent_can_overlap_deferred_provider_settlement() -> None:
    current = _checkpoint(
        phase="assistant_intent_committed",
        state={"pending_effect_ids": ["provider-1"]},
        pending_effect_id="provider-1",
    )
    next_checkpoint = with_effect_intents(
        current,
        (
            EffectRecord(
                effect_id="tool-1",
                task_id="task-1",
                category="tool_call",
                replay_class="unsafe",
                args_digest="c" * 64,
                status="pending",
                intent={"effect_id": "tool-1"},
            ),
        ),
    )
    assert next_checkpoint.phase == "tool_effect_running"
    assert next_checkpoint.state["pending_effect_ids"] == ("provider-1", "tool-1")


@pytest.mark.parametrize(
    ("state", "pending_effect_id"),
    [
        ({"pending_effect_ids": ["effect-1"]}, None),
        ({"pending_effect_ids": []}, "effect-1"),
        ({"pending_effect_ids": ["effect-1", "effect-2"]}, "effect-1"),
        ({"pending_effect_ids": ["effect-1", "effect-1"]}, "effect-1"),
    ],
)
def test_task_checkpoint_rejects_inconsistent_pending_effect_state(
    state: dict[str, object], pending_effect_id: str | None
) -> None:
    with pytest.raises(ValueError, match="pending effect"):
        _checkpoint(state=state, pending_effect_id=pending_effect_id)
