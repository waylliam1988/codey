"""Fresh-window recovery delivers results without discarding the original task contract."""

import pytest

from codey.operations.kernel_recovery import apply_recovery_first


@pytest.mark.parametrize("native,changed", [(False, False), (False, True), (True, True)])
def test_recovered_results_keep_original_goal_and_allowed_contract(native, changed):
    prompt = "ORIGINAL_GOAL: verify checkpoint.py\nALLOWED_CONTRACT: read_file, run, done"
    restored, messages = apply_recovery_first(
        object(), native, [object()], prompt, None, provider_session_changed=changed,
        format_results=lambda *args: "ORIGINAL_RESULT: created checkpoint.py",
        native_tool_messages=lambda *args: [],
    )
    assert prompt in restored
    assert "ORIGINAL_RESULT" in restored
    assert messages is None
