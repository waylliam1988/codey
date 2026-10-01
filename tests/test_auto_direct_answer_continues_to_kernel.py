"""Auto direct answer must continue to the kernel when the gate rejects.

- Sources task: first reply 'done' blocked -> same run continues and completes.
- Greeting: single legal reply completes with no extra tool rounds.
- Proof identity: gate uses frame.run_id (not empty request.run_id).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock


def _auto_frame(task, provider, **overrides):
    from codey.agents.handoff import ConversationContext
    from codey.operations.context import RunFrame
    from codey.task.model import TaskSubmission

    request_run_id = overrides.pop("request_run_id", "r-auto")
    frame_run_id = overrides.pop("frame_run_id", "r-auto")
    request = TaskSubmission(
        session_id="s-auto",
        project="/tmp/proj",
        task=task,
        max_turns=8,
        continue_task=False,
        provider_id="web",
        intent="auto",
        run_id=request_run_id,
        **overrides,
    )
    return RunFrame(
        request=request,
        project_text="/tmp/proj",
        provider=provider,
        provider_id="web",
        run_id=frame_run_id,
        task_kind="auto",
        conversation=ConversationContext(),
        fresh_chat=False,
        handoff="",
        research_handoff="",
        prior_snapshot=None,
        recovered_owner_prompt="",
        provider_session_changed=False,
        preflight_tried=set(),
        preflight_switches=0,
        trace=None,
        recovered_tool_outcomes=(),
        recovered_tool_result_batch_id="",
        entry_policy=None,
    )


class _SeqProvider:
    def __init__(self, replies):
        self._replies = list(replies)
        self.sends = 0

    def new_chat(self):
        pass

    def send(self, text):
        self.sends += 1
        return self._replies[min(self.sends - 1, len(self._replies) - 1)]

    def close(self):
        pass


def _auto_deps(state, mode_deps):
    from codey.operations.auto_loop import AutoRunDeps

    return AutoRunDeps(
        state=state,
        review_task=getattr(mode_deps, "review", None),
        acquire_writer=lambda _p: True,
        release_writer=lambda _p: None,
        open_ledger_for=lambda _k: None,
    )


def test_sources_task_continues_same_task_callback():
    from dataclasses import replace

    from codey.operations.auto_loop import run_auto_mode
    from codey.operations.result import ModeOutcome

    frame = _auto_frame("must open sources", _SeqProvider(["finished"]), sources_open_required=True)
    modes = SimpleNamespace(project=mock.Mock(), research=mock.Mock(), planning=mock.Mock(), review=mock.Mock())
    work, hooks = SimpleNamespace(), SimpleNamespace()
    calls = []
    def continuation(active_frame, active_work, active_hooks, *, followup):
        assert active_frame is frame and active_work is work and active_hooks is hooks
        assert active_frame.entry_session.policy.sources_open_required is True
        assert active_frame.entry_session.turn == 1
        assert active_frame.fresh_chat is False
        assert followup
        calls.append(active_frame.entry_session)
        return ModeOutcome({"stop_reason": "blocked"})
    deps = replace(_auto_deps(SimpleNamespace(), modes), continue_task=continuation)
    outcome = run_auto_mode(frame, work, hooks, deps)
    assert len(calls) == 1
    assert outcome.event["stop_reason"] == "blocked"
    modes.research.assert_not_called()
    modes.project.assert_not_called()


def test_greeting_stays_single_shot():
    from codey.operations.auto_loop import run_auto_mode

    provider = _SeqProvider(["hi there"])
    frame = _auto_frame("hello", provider)
    mode_deps = SimpleNamespace(
        project=mock.Mock(), research=mock.Mock(),
        planning=mock.Mock(), review=mock.Mock(),
    )
    deps = _auto_deps(mock.Mock(), mode_deps)
    outcome = run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(), deps)

    assert outcome.event.get("stop_reason") == "done"
    assert provider.sends == 1
    mode_deps.project.assert_not_called()
    mode_deps.research.assert_not_called()


def test_direct_gate_uses_frame_run_id_not_request():
    import codey.operations.task_entry as entry

    seen = {}
    from codey.operations import completion_gate as gate

    real_evaluate = gate.evaluate

    def spy(session, answer, context=None):
        seen["run_id"] = (context or {}).get("run_id") if isinstance(context, dict) else getattr(context, "run_id", None)
        return real_evaluate(session, answer, context=context)

    import codey.operations.auto_loop as auto

    frame = _auto_frame("hello", _SeqProvider(["hi"]), request_run_id="", frame_run_id="real-123")
    # Ensure project texts diverge too: frame has real, request has /tmp/proj (same here),
    # the key check is run_id.
    import unittest.mock as umock

    with umock.patch.object(gate, "evaluate", side_effect=spy):
        # task_entry path
        frame.handoff = "hi there"
        verdict = entry.evaluate_direct_answer_candidate(frame, "hi there")
        assert verdict.complete is True
        assert seen.get("run_id") == "real-123", f"task_entry gate must use frame.run_id, got {seen.get('run_id')!r}"

    seen.clear()
    with umock.patch("codey.operations.completion_gate.evaluate", side_effect=spy):
        # auto_loop path via run_auto_mode greeting
        provider = _SeqProvider(["hi there"])
        frame2 = _auto_frame("hello", provider, request_run_id="", frame_run_id="real-456")
        mode_deps = SimpleNamespace(
            project=mock.Mock(), research=mock.Mock(),
            planning=mock.Mock(), review=mock.Mock(),
        )
        deps = _auto_deps(mock.Mock(), mode_deps)
        outcome2 = auto.run_auto_mode(frame2, SimpleNamespace(), SimpleNamespace(), deps)
        assert outcome2.event.get("stop_reason") == "done"
        assert seen.get("run_id") == "real-456", f"auto gate must use frame.run_id, got {seen.get('run_id')!r}"
