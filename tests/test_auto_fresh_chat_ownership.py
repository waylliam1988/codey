"""Auto and its task kernel own exactly one window and one total budget.

This replaces the former two-window handoff expectations. The assertions
observe real kernel sends and file reads rather than mimicking an executor
with a handwritten PROJECT_INTRO string.
"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.operations.auto_loop import run_auto_mode
from codey.operations.context import RunWork
from codey.operations.task_entry import run_entry_kernel
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.test_auto_direct_answer_continues_to_kernel import _auto_deps, _auto_frame, _SeqProvider


@pytest.mark.parametrize("action", ["project", "research", "planning_readonly"])
def test_auto_action_sends_shared_protocol_and_reads_file_in_one_window(tmp_path, monkeypatch, action):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    class Provider(_SeqProvider):
        def __init__(self):
            super().__init__([f"ACTION: {action}\nPLAN: inspect a.py",
                              '{"tool":"read_file","args":{"path":"a.py"}}',
                              '{"tool":"done","args":{"summary":"read"}}'])
            self.windows = 0
            self.prompts = []
        def new_chat(self):
            self.windows += 1
        def send(self, text):
            self.prompts.append(text)
            return super().send(text)
    provider = Provider()
    frame = _auto_frame("inspect a.py", provider)
    frame.request = replace(frame.request, project=str(tmp_path))
    frame.project_text = str(tmp_path)
    frame.task_kind = "project"
    frame.fresh_chat = True
    frame.entry_policy = TaskPolicy(frozenset({"control", "project.read"}))
    monkeypatch.setattr("codey.operations.task_entry._entry_executors", lambda *args: (tmp_path, DEFAULT_TOOL_FNS, None))
    runtime = SimpleNamespace(state=SimpleNamespace())
    acquire = []
    def continued(active_frame, work, hooks, *, followup):
        return run_entry_kernel(active_frame, work, hooks, runtime, task_kind="project", continuation_followup=followup)
    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace()), continue_task=continued,
                   acquire_writer=lambda _: acquire.append(True) or True)
    outcome = run_auto_mode(frame, RunWork([], ExecutionEvidence()),
                            SimpleNamespace(on_event=lambda _: None, on_shell_request=None), deps)
    assert outcome.event["stop_reason"] == "done"
    assert provider.windows == 1
    assert provider.sends == 3
    assert outcome.event["turns"] == 3
    assert "read_file" in provider.prompts[1]
    assert frame.entry_session.read_files == {"a.py"}
    assert acquire == []


def test_auto_first_prompt_receives_project_scoped_context():
    provider = _SeqProvider(["hello"])
    frame = _auto_frame("hello", provider)
    seen = []
    def context(*, session_id, project):
        seen.append((session_id, project))
        return SimpleNamespace(text="Project continuity")
    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace()), ghost_directive_fn=context)
    run_auto_mode(frame, RunWork([], ExecutionEvidence()), SimpleNamespace(), deps)
    assert seen == [(frame.request.session_id, frame.request.project)]
    assert provider.sends == 1


def test_initial_reset_failure_never_executes_or_continues():
    class Provider(_SeqProvider):
        def new_chat(self):
            raise RuntimeError("reset failed")
    provider = Provider(["ACTION: project\nPLAN: edit"])
    frame = _auto_frame("edit", provider)
    frame.fresh_chat = True
    continued = []
    deps = replace(_auto_deps(SimpleNamespace(), SimpleNamespace()),
                   continue_task=lambda *a, **k: continued.append(True))
    with pytest.raises(RuntimeError, match="reset failed"):
        run_auto_mode(frame, RunWork([], ExecutionEvidence()), SimpleNamespace(), deps)
    assert provider.sends == 0
    assert continued == []


def test_auto_has_no_mode_dispatch_plumbing():
    from dataclasses import fields

    from codey.operations.auto_loop import AutoRunDeps
    assert not {"mode_deps", "config_result"} & {item.name for item in fields(AutoRunDeps)}
