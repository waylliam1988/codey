"""Entry sessions must initialize task requirements from the user task.

Same request via unified entry and project adapter must agree on
verification_forbidden and project_changes_required. Model hints must not
rewrite user requirements.
"""
from __future__ import annotations


def _frame(task, model_hint=""):

    from codey.operations.context import RunFrame
    from codey.task.model import TaskSubmission

    request = TaskSubmission(
        session_id="s1",
        project="/tmp/proj",
        task=task,
        max_turns=8,
        continue_task=False,
        provider_id="web",
        intent="auto",
        run_id="r1",
        model_hint=model_hint,
        project_changes_required=False,
    )
    return RunFrame(
        request=request,
        project_text="/tmp/proj",
        provider=None,
        provider_id="web",
        run_id="r1",
        task_kind="project",
        conversation=None,
        fresh_chat=False,
        handoff="",
        research_handoff="",
        prior_snapshot=None,
        recovered_owner_prompt="",
        provider_session_changed=False,
        preflight_tried=set(),
        preflight_switches=0,
    )


def _work():
    from codey.operations.context import RunWork
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    return RunWork(recent_events=[], evidence=ExecutionEvidence())


def test_entry_inits_verification_forbidden_from_user_task():
    from codey.operations.task_entry import _create_entry_session, build_task_policy_for_entry

    frame = _frame("Please edit a.py; do not run tests.")

    work = _work()
    policy = build_task_policy_for_entry(frame.request, "project")
    session = _create_entry_session(frame, work, policy, "project")
    assert session.verification_forbidden is True
    assert session.project_changes_required is False


def test_model_hint_cannot_clear_user_forbids():
    from codey.operations.task_entry import _create_entry_session, build_task_policy_for_entry

    frame = _frame("Please edit a.py; do not run tests.", model_hint="please run pytest now")
    work = _work()
    policy = build_task_policy_for_entry(frame.request, "project")
    session = _create_entry_session(frame, work, policy, "project")
    # User requirement wins; model hint is execution text only.
    assert session.verification_forbidden is True


def test_model_hint_cannot_invent_forbids():
    from codey.operations.task_entry import _create_entry_session, build_task_policy_for_entry

    frame = _frame("Please edit a.py and run pytest.", model_hint="do not run tests")
    work = _work()
    policy = build_task_policy_for_entry(frame.request, "project")
    session = _create_entry_session(frame, work, policy, "project")
    assert session.verification_forbidden is False
