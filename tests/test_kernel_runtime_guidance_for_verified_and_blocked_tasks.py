"""Model guidance preserves completion gates, requested output and fresh evidence."""
import json

import pytest

from codey.operations.kernel_prompt import _result_context, kernel_prompt_for_session, working_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult


def observations(text):
    return json.loads(text.splitlines()[1])


def test_complete_result_is_available_without_ordering_a_redundant_receipt_read():
    session = TaskSession(policy=None)
    result = ToolResult(ToolCall("run", {"command": "python -m unittest discover -v"}), "2 tests passed", ok=True)
    session._memory_results["exec-1"] = result
    text = _result_context(result, session)
    assert "Stored result: exec-1" in text
    assert "Read it with read_tool_result" not in text
    assert "If more detail" in text


def test_truncated_result_guides_literal_search_instead_of_reexecution():
    session = TaskSession(policy=None)
    result = ToolResult(ToolCall("run", {}), "bounded preview", ok=False, truncated=True)
    session._memory_results["exec-1"] = result
    text = _result_context(result, session)
    assert "query" in text and "literal" in text
    assert "do not repeat execution" in text


@pytest.mark.parametrize("native", [False, True])
def test_readonly_task_can_report_blocked_in_user_requested_format(native):
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.verify"}),
        denied_capabilities=frozenset({"project.write"})), task_kind="project",
        task_text='Diagnose only; report JSON {"status":"blocked","reason":"..."} if edits would be required.')
    prompt = kernel_prompt_for_session(session, native=native)
    assert "cannot be completed within" in prompt and "blocked" in prompt
    assert "requested final format" in prompt and "summary" in prompt
    assert "only after the required checks have passed" not in prompt


@pytest.mark.parametrize("fingerprint,passed,status", [("sha256:" + "a" * 64, True, "current_pass"),
    ("sha256:" + "b" * 64, True, "stale"), ("sha256:" + "a" * 64, False, "current_fail")])
def test_verification_facts_are_explicit_without_claiming_task_completion(fingerprint, passed, status):
    session = TaskSession(policy=None, workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
    session.record_verification("python -m unittest discover -v", 1, passed, exit_code=0 if passed else 1,
        workspace_revision=1, workspace_fingerprint=fingerprint)
    text = working_context(session)
    data = observations(text)
    assert data["verifications"][0]["state"] == status
    assert data["verifications"][0]["command"] == "python -m unittest discover -v"
    assert "If the user's requirements are satisfied" in text
    assert "call done" in text
    assert "automatically complete" not in text


def test_missing_fingerprint_is_not_projected_as_current_passing_verification():
    session = TaskSession(policy=None)
    session.record_verification("python -m unittest discover -v", 0, True)
    assert observations(working_context(session))["verifications"][0]["state"] == "unknown"


@pytest.mark.parametrize("exit_code,passed,status", [(None, True, "unknown"), (1, True, "current_fail"),
    (0, False, "current_fail")])
def test_missing_or_conflicting_exit_status_cannot_become_passing_guidance(exit_code, passed, status):
    session = TaskSession(policy=None, workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
    session.record_verification("python -m unittest discover -v", 1, passed, exit_code=exit_code,
        workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
    assert observations(working_context(session))["verifications"][0]["state"] == status


def test_matching_fingerprint_with_changed_revision_is_stale():
    session = TaskSession(policy=None, workspace_revision=2, workspace_fingerprint="sha256:" + "a" * 64)
    session.record_verification("python -m unittest discover -v", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
    assert observations(working_context(session))["verifications"][0]["state"] == "stale"


def test_latest_unknown_verification_replaces_prior_pass_for_the_same_command():
    session = TaskSession(policy=None, workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
    session.record_verification("python -m unittest discover -v", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint=session.workspace_fingerprint)
    session.record_verification("python -m unittest discover -v", 2, False,
        workspace_revision=1, workspace_fingerprint=session.workspace_fingerprint)
    rows = observations(working_context(session))["verifications"]
    assert len(rows) == 1 and rows[0]["state"] == "unknown"


def test_readonly_work_observations_explain_how_to_finish_a_failed_diagnosis():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.verify"})),
        task_kind="project", task_text="Diagnose the defect without changing files")
    text = working_context(session)
    assert observations(text)["can_modify_project"] is False
    assert "diagnostic findings" in text and "blocked" in text and "done" in text
    assert "rerun unchanged checks" in text
