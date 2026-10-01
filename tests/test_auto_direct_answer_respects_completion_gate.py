"""Auto direct answers must pass the common completion gate.

Requirements (open sources, edits, strict research) block a bare answer;
a plain greeting with no requirements still completes in one shot.
"""
from __future__ import annotations

import pytest


def _auto_frame(task, **overrides):
    from codey.operations.context import RunFrame
    from codey.task.model import TaskSubmission

    request = TaskSubmission(
        session_id="s-auto",
        project="/tmp/proj",
        task=task,
        max_turns=8,
        continue_task=False,
        provider_id="web",
        intent="auto",
        run_id="r-auto",
        **overrides,
    )
    return RunFrame(
        request=request,
        project_text="/tmp/proj",
        provider=None,
        provider_id="web",
        run_id="r-auto",
        task_kind="auto",
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


def test_direct_answer_blocked_when_sources_required():
    from codey.operations.task_entry import _direct_answer_outcome

    frame = _auto_frame("hello finished", sources_open_required=True)
    frame.handoff = "finished"
    outcome = _direct_answer_outcome(frame, "chat")
    assert outcome.event.get("stop_reason") != "done"


def test_direct_answer_blocked_when_changes_required():
    from codey.operations.task_entry import _direct_answer_outcome

    frame = _auto_frame("hello finished", project_changes_required=True)
    frame.handoff = "finished"
    outcome = _direct_answer_outcome(frame, "chat")
    assert outcome.event.get("stop_reason") != "done"


def test_direct_answer_blocked_when_strict_research():
    from codey.operations.task_entry import _direct_answer_outcome

    frame = _auto_frame("hello finished", strict_research=True)
    frame.handoff = "finished"
    outcome = _direct_answer_outcome(frame, "chat")
    assert outcome.event.get("stop_reason") != "done"


def test_plain_greeting_direct_answer_completes():
    from codey.operations.task_entry import _direct_answer_outcome

    frame = _auto_frame("hello")
    frame.handoff = "hi there"
    outcome = _direct_answer_outcome(frame, "chat")
    assert outcome.event.get("stop_reason") == "done"
