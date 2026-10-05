"""Rendering prompts cannot refresh candidates, scan the workspace, or mutate facts."""

from copy import deepcopy

from codey.operations import kernel_prompt
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.workspace.coding_context import CodingContext


def test_rendering_prompt_does_not_invoke_verification_loader_or_change_session():
    calls = []
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read"})),
                          edited_files={"app.py": 1},
                          verification_candidate_loader=lambda: calls.append("load") or ())
    before = deepcopy(vars(session))
    kernel_prompt.kernel_prompt_for_session(session, tool_names=("read_file",))
    assert calls == []
    assert vars(session) == before


def test_rendering_uses_prepared_context_without_rechecking_workspace(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("prompt renderer must not evaluate completion")

    monkeypatch.setattr("codey.operations.project_completion_checks.project_completion_checks", unexpected)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read"})))
    text = kernel_prompt.kernel_prompt_for_session(
        session, tool_names=("read_file",), coding_context=CodingContext(
            changed_files=("app.py",), verification_fresh=True,
        ),
    )
    assert "Changed files covered by verification: app.py" in text


def test_prompt_guidance_does_not_grant_permissions():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    text = kernel_prompt.kernel_prompt_for_session(
        session, tool_names=("done",), task_guidance="Use additional tools if authorized.",
    )
    assert "Visible tools: done" in text
    assert session.policy.grants == frozenset({"control"})
