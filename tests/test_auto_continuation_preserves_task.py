"""Direct candidates retain policy, facts, leases, window and total budget."""
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.operations.auto_loop import run_auto_mode
from codey.operations.completion_gate import evaluate
from codey.operations.context import RunWork
from codey.operations.result import ModeOutcome
from codey.operations.task_entry import evaluate_direct_answer_candidate, run_entry_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.test_auto_direct_answer_continues_to_kernel import _auto_deps, _auto_frame, _SeqProvider


def test_direct_candidate_keeps_original_required_check():
    frame = _auto_frame("hello", _SeqProvider(["hi"]))
    frame.entry_policy = TaskPolicy(grants=frozenset({"control"}), required_checks=("custom_requirement",))
    assert evaluate_direct_answer_candidate(frame, "hi").complete is False


def test_direct_candidate_keeps_existing_unknown_verification():
    frame = _auto_frame("finished", _SeqProvider(["hi"]))
    frame.project_text = ""
    frame.entry_policy = TaskPolicy(grants=frozenset({"control"}))
    session = TaskSession(policy=frame.entry_policy)
    session.edited_files = {"a.py": 1}
    session.record_verification("pytest", 1, False)
    frame.entry_session = session
    assert evaluate(session, "done").complete is False
    assert evaluate_direct_answer_candidate(frame, "done").complete is False


@pytest.mark.parametrize("fault", [False, RuntimeError("lease unavailable")])
def test_failed_writer_lease_never_executes_or_releases(fault):
    frame = _auto_frame("fix a.py", _SeqProvider(["finished"]), project_changes_required=True)
    frame.entry_policy = TaskPolicy(grants=frozenset({"control", "project.write"}))
    calls = []

    def acquire(project):
        if isinstance(fault, Exception):
            raise fault
        return fault

    def execute(*args, **kwargs):
        calls.append("execute")
        return ModeOutcome({"stop_reason": "done"})

    deps = replace(
        _auto_deps(SimpleNamespace(), SimpleNamespace(project=execute)),
        acquire_writer=acquire, release_writer=lambda _: calls.append("release"),
        continue_task=execute,
    )
    result = run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(), deps)
    assert result.event["stop_reason"] != "done"
    assert calls == []


@pytest.mark.parametrize("first_reply", ["finished", "ACTION: research\nPLAN: open docs and fix a.py"])
@pytest.mark.parametrize("finish_with_edit", [False, True])
def test_mixed_direct_rejection_uses_real_kernel_same_session(tmp_path, monkeypatch, finish_with_edit, first_reply):
    from codey.agents.tools import DEFAULT_TOOL_FNS
    from codey.app.context import AppContext
    from codey.research.ledger import ResearchLedger
    from codey.workspace.changes import collect_changes

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    url = "https://example.com/a"
    replies = [first_reply, json.dumps({"tool": "open_url", "args": {"url": url}})]
    if finish_with_edit:
        replies += [
            json.dumps({"tool": "read_file", "args": {"path": "a.py"}}),
            json.dumps({"tool": "edit", "args": {"path": "a.py", "replacements": [{"old_string": "x = 1", "new_string": "x = 2"}]}}),
            json.dumps({"tool": "run", "args": {"command": "python -m py_compile a.py", "path": "."}}),
        ]
    replies += [json.dumps({"tool": "done", "args": {"summary": "finished"}})]

    class Provider(_SeqProvider):
        def __init__(self):
            super().__init__(replies)
            self.windows = 0

        def new_chat(self):
            self.windows += 1

    provider = Provider()
    frame = _auto_frame("open docs and modify a.py", provider, sources_open_required=True,
                        project_changes_required=True, requested_capabilities=("web.read",))
    frame.request = replace(frame.request, project=str(project), max_turns=8)
    frame.project_text = str(project)
    frame.entry_policy = TaskPolicy(
        grants=frozenset({"control", "project.read", "project.write", "project.verify", "web.read"}),
        sources_open_required=True, required_checks=("research_sources_opened",),
    )
    ledger = ResearchLedger()

    def opened(url, **kwargs):
        ledger.record_open(url, url, "A", "body")
        return SimpleNamespace(model_text="Title: A\nbody")

    tools = SimpleNamespace(open_url=opened, ledger=ledger)
    monkeypatch.setattr("codey.operations.task_entry._entry_executors",
                        lambda *args: (project, DEFAULT_TOOL_FNS, tools))
    work = RunWork(recent_events=[], evidence=ExecutionEvidence())
    hooks = SimpleNamespace(on_event=lambda _: None, on_shell_request=None)
    app_state = AppContext(tmp_path / "state")
    runtime = SimpleNamespace(state=app_state, workspace_revisions=app_state.workspace_revisions,
                              collect_changes=collect_changes)
    seen = []

    def continuation(active_frame, active_work, active_hooks, *, followup):
        before = active_frame.entry_session
        result = run_entry_kernel(active_frame, active_work, active_hooks, runtime,
                                  task_kind="project", continuation_followup=followup)
        assert active_frame.entry_session is before
        assert before.policy is frame.entry_policy
        seen.append(before)
        return result

    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace()), continue_task=continuation)
    outcome = run_auto_mode(frame, work, hooks, deps)
    assert len(seen) == 1
    assert (outcome.event["stop_reason"] == "done") is finish_with_edit
    assert provider.windows == 0
    assert provider.sends <= 8
    assert outcome.event["turns"] == provider.sends
    if finish_with_edit:
        assert (project / "a.py").read_text(encoding="utf-8") == "x = 2\n"


def test_first_auto_send_exhausts_one_turn_budget():
    frame = _auto_frame("fix a.py", _SeqProvider(["finished"]), project_changes_required=True)
    frame.request = replace(frame.request, max_turns=1)
    frame.entry_policy = TaskPolicy(grants=frozenset({"control", "project.write"}))
    calls = []
    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace(project=lambda *a, **k: calls.append("execute"))))
    outcome = run_auto_mode(frame, SimpleNamespace(), SimpleNamespace(), deps)
    assert outcome.event["stop_reason"] == "max_turns"
    assert calls == []


@pytest.mark.parametrize("action", ["project", "research", "planning_readonly"])
def test_action_marker_continues_original_kernel_without_mode_selection(action):
    provider = _SeqProvider([f"ACTION: {action}\nPLAN: inspect official docs"])
    frame = _auto_frame("inspect docs", provider)
    frame.task_kind = "hybrid"
    policy = TaskPolicy(frozenset({"control", "project.read", "web.read"}),
                        required_checks=("third_task_check",))
    frame.entry_policy = policy
    seen = []
    def wrong_mode(*args, **kwargs):
        raise AssertionError("auto must not redispatch tool tasks by model mode")
    modes = SimpleNamespace(project=wrong_mode, research=wrong_mode, planning=wrong_mode)
    def continued(active_frame, work, hooks, *, followup):
        assert active_frame is frame
        assert active_frame.task_kind == "hybrid"
        assert active_frame.entry_session.policy is policy
        assert active_frame.entry_session.turn == 1
        assert active_frame.fresh_chat is False
        assert "inspect official docs" in followup
        seen.append(active_frame.entry_session)
        return ModeOutcome({"stop_reason": "blocked"})
    deps = replace(_auto_deps(SimpleNamespace(), modes), continue_task=continued)
    result = run_auto_mode(frame, RunWork([], ExecutionEvidence()), SimpleNamespace(), deps)
    assert result.event["stop_reason"] == "blocked"
    assert len(seen) == 1
    assert provider.sends == 1
