"""Auto direct answers must pass the common completion gate.

Requirements (open sources, edits, strict research) block a bare answer;
a plain greeting with no requirements still completes in one shot.
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


def test_direct_answer_blocked_when_sources_required():

    frame = _auto_frame("hello finished", sources_open_required=True)
    frame.handoff = "finished"
    outcome = run_candidate(frame)
    assert outcome.event.get("stop_reason") != "done"


def test_direct_answer_blocked_when_changes_required():

    frame = _auto_frame("hello finished", project_changes_required=True)
    frame.handoff = "finished"
    outcome = run_candidate(frame)
    assert outcome.event.get("stop_reason") != "done"


def test_direct_answer_blocked_when_strict_research():

    frame = _auto_frame("hello finished", strict_research=True)
    frame.handoff = "finished"
    outcome = run_candidate(frame)
    assert outcome.event.get("stop_reason") != "done"


def test_plain_greeting_direct_answer_completes():

    frame = _auto_frame("hello")
    frame.handoff = "hi there"
    outcome = run_candidate(frame)
    assert outcome.event.get("stop_reason") == "done"
