"""Checking browser results must not cancel a user's ban on local commands."""
from __future__ import annotations

import pytest

from codey.agents.protocol import task_forbids_verification
from codey.operations.task_entry import build_task_policy_for_entry
from codey.task.model import TaskSubmission


@pytest.mark.parametrize("task", [
    "每一步检查工具结果。不要运行测试或命令。然后写回 result.json。",
    "Check the browser output, then update result.json. Do not run tests or commands.",
    "Do not run tests. Check the loaded record and write its value to result.json.",
    "Skip tests and check the page title.",
    "Do not skip tests. Do not run commands.",
])
def test_explicit_denial_survives_other_domain_observations(task: str) -> None:
    assert task_forbids_verification(task)
    policy = build_task_policy_for_entry(TaskSubmission("s", "project", task, 8, False, "local"), "hybrid")
    assert "project.verify" in policy.denied_capabilities
    assert not policy.allows("project.verify")
    assert policy.allows("project.write")


@pytest.mark.parametrize("task", [
    "Run tests after editing.",
    "Do not skip tests.",
    "Don't avoid running tests.",
    "Never skip verification.",
    "不要跳过测试。",
    "Check the browser output, then update result.json.",
])
def test_positive_or_unrelated_requests_do_not_invent_a_denial(task: str) -> None:
    assert not task_forbids_verification(task)
