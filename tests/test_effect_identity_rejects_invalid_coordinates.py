"""Malformed coordinates must never alias a settled effect at turn zero."""

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.task_loop import KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall


@pytest.mark.parametrize("value", ["invalid", "1", 1.0, True, False, None, -1])
@pytest.mark.parametrize("coordinate", ["turn", "tool_index"])
def test_effect_identity_rejects_invalid_coordinates(value, coordinate):
    args = {"run_id": "r:task", "turn": 1, "tool_index": 0}
    args[coordinate] = value
    with pytest.raises(ValueError, match="nonnegative integer"):
        turn_effect_id(**args)


@pytest.mark.parametrize("value", ["invalid", "1", 1.0, True, False, -1])
def test_invalid_turn_cannot_deliver_turn_zero_receipt_or_execute(value):
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    session.turn = 3
    with pytest.raises(ValueError, match="nonnegative integer"):
        execute_turn(session, [ToolCall("done", {})], turn=value)
    assert session.executed == {}


def test_valid_effect_identity_is_stable_and_distinct():
    assert turn_effect_id("r:task", 1, 0) == turn_effect_id("r:task", 1, 0)
    assert len({turn_effect_id("r:task", turn, index) for turn in range(3) for index in range(3)}) == 9


@pytest.mark.parametrize("value", ["invalid", "1", 1.0, True, False, -1])
def test_invalid_resume_turn_is_rejected_before_any_provider_send(value):
    class Provider:
        def send(self, *args, **kwargs):
            pytest.fail("invalid resume identity must not reach the provider")

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(provider=Provider(), start_turn=value),
        ),
    )
    assert result.completed is False
    assert result.stop_reason == "controller_failure"
    assert "nonnegative integer" in result.summary
    assert session.executed == {}
