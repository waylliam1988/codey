"""The browser fixture must answer current entry prompts, not retired ones."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from codey.operations.kernel_prompt import _format_results, kernel_prompt_for_session
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from tools.ui_e2e import TASK, ScriptedWriter


def workspace_prompt(task, *, project=""):
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.write", "project.verify", "shell.approval"})),
                          task_kind="chat", project=project)
    return kernel_prompt_for_session(session, user_task=task, tool_names=build_turn_snapshot(session).tool_names)


def test_plain_workspace_question_has_a_plain_answer():
    reply = ScriptedWriter().send(workspace_prompt("Explain box breathing without project access."))
    assert reply == "Box breathing uses equal inhale, hold, exhale, and hold phases."


def test_project_discussion_is_a_plain_answer_without_done_or_edit():
    prior_context = "Local Context:\n- Recent focus: Explain box breathing without project access.\n\n"
    reply = ScriptedWriter().send(prior_context + workspace_prompt("Discuss a breathing app without changing files.", project="project"))
    assert reply.startswith("Start with one guided breathing rhythm.")
    assert not reply.startswith("{")


def test_previous_shell_denial_in_context_cannot_intercept_new_shell_request():
    prompt = workspace_prompt("Request a shell command for git status --short and wait for approval.")
    prompt += "\n\nPrior context: The user denied this shell command; it was not executed: git status"
    reply = ScriptedWriter().send(prompt)
    assert json.loads(reply)["tool"] == "shell"


def test_write_task_directly_requests_kernel_edit():
    old_focus = "\n\nLocal experience: Discuss a breathing app without changing files."
    reply = ScriptedWriter().send(workspace_prompt(TASK, project="project") + old_focus)
    assert json.loads(reply)["tool"] == "edit"


def test_reload_probe_returns_plain_workspace_completion():
    writer = ScriptedWriter()
    assert not writer.reload_release.is_set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer.send, workspace_prompt("Stay active across one UI reload.", project="project"))
        try:
            assert writer.reload_entered.wait(5)
            assert not future.done(), "reload must remain active until the browser confirms restored UI"
        finally:
            writer.reload_release.set()
        assert future.result(timeout=5) == "reload completed"


def test_unrecognized_fixture_prompt_fails_instead_of_inventing_an_edit():
    with pytest.raises(AssertionError, match="unrecognized browser fixture prompt"):
        ScriptedWriter().send("unknown prompt")


def test_kernel_fixture_keeps_real_edit_run_done_sequence():
    writer = ScriptedWriter()
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    assert json.loads(writer.send(TASK))["tool"] == "edit"
    for name, text, expected in (("edit", "changed", "run"), ("run", "exit 0", "done")):
        result = ToolResult(ok=True, call=ToolCall(name=name, args={}), model_text=text)
        assert json.loads(writer.send(_format_results([result], session)))["tool"] == expected
