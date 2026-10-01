"""Direct-answer gate failure must never complete.

Covers: gate raises -> blocked (fail-closed, with reason preserved),
gate returns None/illegal -> blocked, plain greeting still completes.
"""
from __future__ import annotations


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


def test_direct_answer_gate_exception_never_completes(monkeypatch):
    import codey.operations.task_entry as entry

    frame = _auto_frame("must open sources", sources_open_required=True)
    frame.handoff = "already done"

    def broken_gate(*args, **kwargs):
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(entry, "_direct_answer_gate", broken_gate)

    outcome = entry._direct_answer_outcome(frame, "chat")

    assert outcome.event["stop_reason"] != "done"
    assert outcome.event["stop_reason"] == "blocked"
    # Failure reason must be preserved, not collapsed to generic "not done".
    summary = str(outcome.event.get("summary", "") or "")
    assert "gate" in summary.lower() or "check failed" in summary.lower() or "unavailable" in summary.lower()


def test_direct_answer_none_verdict_never_completes(monkeypatch):
    import codey.operations.task_entry as entry

    frame = _auto_frame("must open sources", sources_open_required=True)
    frame.handoff = "already done"

    monkeypatch.setattr(entry, "_direct_answer_gate", lambda *a, **k: None)

    outcome = entry._direct_answer_outcome(frame, "chat")

    assert outcome.event["stop_reason"] != "done"
    assert outcome.event["stop_reason"] == "blocked"


def test_direct_answer_illegal_verdict_never_completes(monkeypatch):
    import codey.operations.task_entry as entry

    frame = _auto_frame("must open sources", sources_open_required=True)
    frame.handoff = "already done"

    class NotAVerdict:
        pass

    monkeypatch.setattr(entry, "_direct_answer_gate", lambda *a, **k: NotAVerdict())

    outcome = entry._direct_answer_outcome(frame, "chat")

    assert outcome.event["stop_reason"] != "done"
    assert outcome.event["stop_reason"] == "blocked"


def test_plain_greeting_still_completes():
    import codey.operations.task_entry as entry

    frame = _auto_frame("hello")
    frame.handoff = "hi there"
    outcome = entry._direct_answer_outcome(frame, "chat")
    assert outcome.event.get("stop_reason") == "done"
