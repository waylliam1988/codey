from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.support.kernel_protocol_test_helpers import normalize_turn


def test_shell_mixed_with_read_is_rejected_before_execution() -> None:
    plan = normalize_turn(
        AssistantTurn(
            text="",
            tool_calls=(
                ProviderToolCall(id="read-1", name="read_file", arguments={"path": "a.py"}),
                ProviderToolCall(id="shell-1", name="shell", arguments={"path": ".", "command": "echo hi"}),
            ),
        ),
        policy=TaskPolicy(grants=frozenset({"project.read", "shell.approval", "control"})),
    )

    assert plan.protocol_error
    assert "shell" in plan.protocol_error.lower()
