"""Approval continuations cannot obtain fresh or expanded task grants."""

from types import SimpleNamespace

import pytest

from codey.operations.task_entry import _entry_policy_with_recovery
from codey.operations.task_phases.dispatch import _previous_run_policy
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
    with pytest.raises(RuntimeError, match="authorization"):
        _previous_run_policy(SimpleNamespace(), request, SimpleNamespace())
