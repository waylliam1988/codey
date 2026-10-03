"""One task loop, ordinary complexity gates, and exact completion acceptance."""

import ast
import inspect
from types import SimpleNamespace

import pytest

from codey.operations import completion_gate, task_loop
from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


def test_kernel_has_one_loop_without_complexity_exemptions():
    source = inspect.getsource(task_loop.run_task_kernel)
    assert "noqa" not in source
    assert sum(isinstance(node, (ast.For, ast.While)) for node in ast.walk(ast.parse(source))) == 1


@pytest.mark.parametrize("value", [1, "true", [], None])
def test_kernel_requires_exact_true_from_completion_gate(monkeypatch, value):
    monkeypatch.setattr(completion_gate, "evaluate", lambda *args, **kwargs: SimpleNamespace(
        complete=value, proof=None, followup="completion blocked",
    ))
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1)
    provider = SimpleNamespace(send=lambda prompt: '{"tool":"done","args":{"summary":"finished"}}')
    result = task_loop.run_task_kernel(
                 session,
                 request=KernelRunRequest(
                     transport=KernelTransportDeps(
                         provider=provider,
                         run_id='exact-done',
                     ),
                 ),
             )
    assert result.completed is False


def test_kernel_does_not_render_discarded_initial_prompt_after_each_result(monkeypatch):
    from codey.runtime.core.models import ToolResult

    rendered = []
    real_render = task_loop._prompt.kernel_prompt_for_session

    def render(*args, **kwargs):
        rendered.append(True)
        return real_render(*args, **kwargs)

    monkeypatch.setattr(task_loop._prompt, "kernel_prompt_for_session", render)
    replies = iter(['{"tool":"read_file","args":{"path":"a.py"}}',
                    '{"tool":"done","args":{"summary":"finished"}}'])
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read"})), max_turns=2)
    result = task_loop.run_task_kernel(
                 session,
                 request=KernelRunRequest(
                     transport=KernelTransportDeps(
                         provider=SimpleNamespace(send=lambda prompt: next(replies)),
                         run_id='render-once',
                     ),
                     execution=KernelExecutionDeps(
                         executors={'read_file': lambda call: ToolResult(call=call, model_text='file text')},
                     ),
                 ),
             )
    assert result.completed
    assert len(rendered) == 1
