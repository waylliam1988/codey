"""Old coding execution/verification facades have no place beside the kernel."""

import pytest

from codey.agents import context, protocol, state, tool_execution
from codey.operations import kernel_result


@pytest.mark.parametrize("module,name", [
    (context, "render_completion_repair_sources"),
    (protocol, "verification_reminder"),
    (protocol, "default_verification_reminder"),
    (protocol, "edit_blocks_from_call"),
    (tool_execution, "execute_edit_call"),
    (tool_execution, "execute_run_call"),
    (tool_execution, "maybe_externalize_large_tool_output"),
    (state, "AgentLoopSession"),
    (state, "LoopProgress"),
    (state, "LoopVerification"),
    (state, "LoopStagnation"),
    (state, "ResolvedLoopConfig"),
    (state, "PendingPromptState"),
    (state, "emit"),
    (state, "snapshot"),
    (kernel_result, "_result_ok"),
])
def test_unused_pre_kernel_facades_are_removed(module, name):
    assert not hasattr(module, name)


def test_no_unused_verification_driver_or_test_kernel_facade():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert not (root / "codey/agents/verification_driver.py").exists()
    assert not (root / "tests/support/kernel_harness.py").exists()
