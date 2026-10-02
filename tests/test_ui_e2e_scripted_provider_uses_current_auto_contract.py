"""The browser fixture must answer current entry prompts, not retired ones."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from codey.operations.auto_loop import build_auto_first_prompt, parse_auto_first_output
from codey.operations.kernel_prompt import _format_results
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from tools.ui_e2e import TASK, ScriptedWriter


def test_plain_auto_question_has_a_plain_answer_without_project_tools():
    reply = ScriptedWriter().send(build_auto_first_prompt("Explain box breathing without project access."))
    assert reply == "Box breathing uses equal inhale, hold, exhale, and hold phases."


def test_project_discussion_is_a_plain_answer_without_done_or_edit():
    prior_context = "Local Context:\n- Recent focus: Explain box breathing without project access.\n\n"
    reply = ScriptedWriter().send(prior_context + build_auto_first_prompt("Discuss a breathing app without changing files.", project="project"))
    assert reply.startswith("Start with one guided breathing rhythm.")
    assert not reply.startswith("{")


def test_write_task_requests_the_current_auto_action_before_kernel_tools():
    old_focus = "\n\nLocal experience: Discuss a breathing app without changing files."
    reply = ScriptedWriter().send(build_auto_first_prompt(TASK, project="project") + old_focus)
    assert parse_auto_first_output(reply).kind == "project"


def test_reload_probe_returns_plain_completion_from_auto():
    writer = ScriptedWriter()
    assert not writer.reload_release.is_set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer.send, build_auto_first_prompt("Stay active across one UI reload.", project="project"))
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
        result = ToolResult(call=ToolCall(name=name, args={}), model_text=text)
        assert json.loads(writer.send(_format_results([result], session)))["tool"] == expected
