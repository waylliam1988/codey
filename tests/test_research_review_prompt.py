"""Review requests carry one frozen basis and their actual completion action."""

import pytest

from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.task_guidance import task_working_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("scoped", [False, True])
def test_review_input_has_one_basis_and_no_workspace_or_done_instructions(native, scoped):
    task = 'Assess the frozen evidence.\nFROZEN BASIS:\n{"unique_evidence":"exact original passage"}'
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.write"})),
                          task_kind="research_review", task_text=task, coding_context_enabled=False)
    prepared = prepare_kernel_turn(session, user_task=task, context_text="", task_guidance="Assess only the frozen input.",
                                   native=native, request_scoped_contract=scoped)
    observations = task_working_context(session)
    assert prepared.snapshot.tool_names == ("submit_research_review",)
    assert observations == ""
    assert prepared.prompt.count("exact original passage") == 1
    assert "call done" not in prepared.prompt and "with done" not in prepared.prompt
    assert "required checks must pass" not in prepared.prompt
    assert "settled submission receipt" in prepared.prompt
