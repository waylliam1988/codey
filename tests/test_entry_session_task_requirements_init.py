"""Entry sessions must initialize task requirements from the user task.

Same request via unified entry and project adapter must agree on
verification_forbidden and project_changes_required. Model hints must not
rewrite user requirements.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from codey.operations.context import RunFrame, RunWork
from codey.operations.task_entry import build_task_policy_for_entry, start_task_session
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.support.task_entry_helpers import entry_frame, submission


def _frame(task: str) -> RunFrame:
    return entry_frame(replace(submission(task, project="/tmp/proj"),
                               session_id="s1", run_id="r1", provider_id="web"))


def _work() -> RunWork:
    return RunWork(recent_events=[], evidence=ExecutionEvidence())


def test_entry_inits_verification_forbidden_from_user_task():
    from codey.operations.task_entry import build_task_policy_for_entry, start_task_session

    frame = _frame("Please edit a.py; do not run tests.")

    work = _work()
    policy = build_task_policy_for_entry(frame.request, "project")
    session = start_task_session(frame, work, policy, "project")
    assert session.verification_forbidden is True
    assert session.project_changes_required is False


def test_entry_reuses_session_without_resetting_facts() -> None:
    frame = _frame("edit a.py")
    work = _work()
    work.evidence.set_workspace_state(3, "sha256:" + "a" * 64)
    policy = build_task_policy_for_entry(frame.request, "project")
    session = start_task_session(frame, work, policy, "project")
    session.read_files.add("a.py")
    work.evidence.set_workspace_state(4, "sha256:" + "b" * 64)

    reused = start_task_session(frame, work, replace(policy), "hybrid")

    assert reused is session
    assert reused.read_files == {"a.py"}
    assert reused.workspace_revision == 3
    assert reused.workspace_fingerprint == "sha256:" + "a" * 64
    assert reused.task_kind == "project"


@pytest.mark.parametrize("change", ["grant", "requirement", "denial"])
def test_entry_cannot_replace_session_authorization(change: str) -> None:
    frame = _frame("edit a.py")
    work = _work()
    policy = build_task_policy_for_entry(frame.request, "project")
    session = start_task_session(frame, work, policy, "project")
    if change == "grant":
        changed = replace(policy, grants=policy.grants | {"web.read"})
    elif change == "requirement":
        changed = replace(policy, required_checks=("must_check",))
    else:
        changed = replace(policy, denied_capabilities=frozenset({"project.write"}))

    with pytest.raises(ValueError, match="cannot replace authorization"):
        start_task_session(frame, work, changed, "project")

    assert frame.entry_session is session
    assert session.policy == policy


@pytest.mark.parametrize("max_turns,expected", [(0, 8), (-1, 1), (3, 3)])
def test_entry_preserves_turn_budget_normalization(max_turns: int, expected: int) -> None:
    frame = _frame("inspect code")
    frame.request = replace(frame.request, max_turns=max_turns)
    policy = build_task_policy_for_entry(frame.request, "project")

    assert start_task_session(frame, _work(), policy, "project").max_turns == expected


def test_entry_initializes_workspace_and_explicit_requirements() -> None:
    frame = _frame("edit a.py")
    frame.request = replace(frame.request, project_changes_required=True)
    frame.handoff = "continue from prior facts"
    work = _work()
    work.evidence.set_workspace_state(7, "sha256:" + "c" * 64)
    policy = build_task_policy_for_entry(frame.request, "project")

    session = start_task_session(frame, work, policy, "project")

    assert session.project_changes_required is True
    assert session.coding_context_enabled is True
    assert session.handoff == frame.handoff
    assert session.workspace_revision == 7
    assert session.workspace_fingerprint == "sha256:" + "c" * 64
