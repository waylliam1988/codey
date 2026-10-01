"""Approval continuations cannot obtain fresh or expanded task grants."""

from types import SimpleNamespace

import pytest

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.task_entry import _entry_policy_with_recovery
from codey.operations.task_phases.dispatch import dispatch_run_mode
from codey.policies.task_policy import TaskPolicy


def test_shell_continuation_retains_the_original_policy():
    original = TaskPolicy(grants=frozenset({"control", "project.read", "shell.approval"}))
    request = SimpleNamespace(session_id="s", previous_run_id="previous", project="p",
                              requested_capabilities=("web.read", "project.write"))
    frame = SimpleNamespace(request=request, run_id="next", entry_policy=original,
                            recovered_tool_outcomes=(object(),))
    assert _entry_policy_with_recovery(frame, SimpleNamespace(), "project") == original


def test_missing_original_approval_policy_stops_the_continuation():
    request = SimpleNamespace(previous_run_id="previous", session_id="s")
    frame = SimpleNamespace(request=request, run_id="next", recovered_tool_outcomes=(),
                            settled_tool_outcomes=())
    with pytest.raises(RecoveryFailed, match="policy"):
        dispatch_run_mode(SimpleNamespace(), None, None, None, frame, None, "project", None)
