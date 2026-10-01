"""Direct-answer gate failure must never complete.

Covers: gate raises -> blocked (fail-closed, with reason preserved),
gate returns None/illegal -> blocked, plain greeting still completes.
"""
from __future__ import annotations

from types import SimpleNamespace

from codey.operations.auto_loop import run_auto_mode
from tests.test_auto_direct_answer_continues_to_kernel import _auto_deps, _SeqProvider
from tests.test_auto_direct_answer_continues_to_kernel import _auto_frame as make_frame


def _auto_frame(task, **overrides):
    return make_frame(task, _SeqProvider(["finished"]), **overrides)


def run_candidate(frame):
    return run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(), _auto_deps(SimpleNamespace(), SimpleNamespace()))


def test_direct_answer_gate_exception_never_completes(monkeypatch):
    import codey.operations.task_entry as entry

    frame = _auto_frame("must open sources", sources_open_required=True)
    frame.handoff = "already done"

    def broken_gate(*args, **kwargs):
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(entry, "evaluate_direct_answer_candidate", broken_gate)

    outcome = run_candidate(frame)

    assert outcome.event["stop_reason"] != "done"
    assert outcome.event["stop_reason"] == "blocked"
    # Failure reason must be preserved, not collapsed to generic "not done".
    summary = str(outcome.event.get("summary", "") or "")
    assert "gate" in summary.lower() or "check failed" in summary.lower() or "unavailable" in summary.lower()


def test_direct_answer_none_verdict_never_completes(monkeypatch):
    import codey.operations.task_entry as entry

    frame = _auto_frame("must open sources", sources_open_required=True)
    frame.handoff = "already done"

    monkeypatch.setattr(entry, "evaluate_direct_answer_candidate", lambda *a, **k: None)

    outcome = run_candidate(frame)

    assert outcome.event["stop_reason"] != "done"
    assert outcome.event["stop_reason"] == "blocked"


def test_direct_answer_illegal_verdict_never_completes(monkeypatch):
    import codey.operations.task_entry as entry

    frame = _auto_frame("must open sources", sources_open_required=True)
    frame.handoff = "already done"

    class NotAVerdict:
        pass

    monkeypatch.setattr(entry, "evaluate_direct_answer_candidate", lambda *a, **k: NotAVerdict())

    outcome = run_candidate(frame)

    assert outcome.event["stop_reason"] != "done"
    assert outcome.event["stop_reason"] == "blocked"


def test_plain_greeting_still_completes():

    frame = _auto_frame("hello")
    frame.handoff = "hi there"
    outcome = run_candidate(frame)
    assert outcome.event.get("stop_reason") == "done"
