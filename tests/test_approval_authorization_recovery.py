"""Approval continuations cannot obtain fresh or expanded task grants."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.task_entry import _entry_policy_with_recovery
from codey.operations.task_phases.dispatch import dispatch_run_mode
from codey.policies.task_policy import TaskPolicy
from tests.support.task_entry_helpers import entry_frame, submission


def test_shell_continuation_retains_the_original_policy():
    original = TaskPolicy(grants=frozenset({"control", "project.read", "shell.approval"}))
    request = replace(submission(), previous_run_id="previous", run_id="next",
                      requested_capabilities=("web.read", "project.write"))
    frame = entry_frame(request)
    frame.entry_policy = original
    assert _entry_policy_with_recovery(frame, None, "project") == original


def test_missing_original_approval_policy_stops_the_continuation():
    request = SimpleNamespace(previous_run_id="previous", session_id="s")
    frame = SimpleNamespace(request=request, run_id="next", project_text="bound-workspace", recovered_tool_outcomes=(),
                            settled_tool_outcomes=())
    with pytest.raises(RecoveryFailed, match="policy"):
        dispatch_run_mode(SimpleNamespace(), None, None, None, frame, None, "project", None)
