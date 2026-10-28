"""Durable retains lifecycle/effect integrity; operation owns recovery choices."""
from dataclasses import replace

import pytest

from codey.durable import EffectRecord, TaskCheckpoint
from codey.durable.model import TASK_PHASE_TRANSITIONS, migrate_checkpoint_phase
from codey.durable.reducer import with_effect_intents, with_effect_settlement


@pytest.mark.parametrize("phase", [
    "provider_prepared", "assistant_intent_committed", "assistant_response_pending",
    "assistant_response_unknown", "tool_intent_committed", "tool_effect_running",
    "tool_outcome_ready", "effect_unknown", "verification_pending",
])
def test_version_one_execution_phase_migrates_without_inventing_an_outcome(phase):
    migrated = migrate_checkpoint_phase(phase, 1)
    assert migrated == ("running", 2)
    assert migrate_checkpoint_phase(*migrated) == migrated
    with pytest.raises(ValueError, match="unknown task phase"):
        migrate_checkpoint_phase(phase, 2)


def test_task_lifecycle_contains_no_provider_or_tool_execution_position():
    assert set(TASK_PHASE_TRANSITIONS) == {
        "accepted", "dispatching", "running", "completion_pending", "completed", "failed", "cancelled",
    }


def test_effect_receipt_updates_indexes_without_another_execution_phase():
    task = TaskCheckpoint("t", "c", "s", "running", 1, {}, {})
    effect = EffectRecord("provider-1", "t", "provider_send", "unsafe", "digest", "pending", {})
    admitted = with_effect_intents(task, (effect,))
    assert admitted.phase == task.phase and admitted.pending_effect_id == effect.effect_id
    settled = with_effect_settlement(admitted, replace(effect, status="interrupted", settlement={"sent_state": "maybe_sent"}))
    assert settled.phase == task.phase and settled.pending_effect_id is None
    assert not any(key in settled.state for key in {"provider_settlement_state", "provider_response_unknown"})
