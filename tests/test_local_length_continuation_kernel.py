from __future__ import annotations

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.providers.base import AssistantTurn, ProviderToolCall


class _ContinuableProvider:
    name = "local"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def send_turn(self, prompt, tools=None, timeout=None):
        del tools, timeout
        self.prompts.append(str(prompt))
        if len(self.prompts) == 1:
            return AssistantTurn(
                text="partial reasoning",
                raw={"finish_reason": "length", "continuable_length": True},
            )
        return AssistantTurn(
            tool_calls=(ProviderToolCall(
                id="done-1", name="done", arguments={"summary": "finished"},
            ),),
        )

    def send_tool_results(self, results, tools=None, timeout=None):
        assert results and results[0]["tool_call_id"] == "done-1"
        return AssistantTurn()


class _AlwaysLengthProvider:
    name = "local"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def send_turn(self, prompt, tools=None, timeout=None):
        del tools, timeout
        self.prompts.append(str(prompt))
        return AssistantTurn(
            text="Tests passed; task complete.",
            raw={"finish_reason": "length", "continuable_length": True},
        )

    def send_tool_results(self, results, tools=None, timeout=None):
        del results, tools, timeout
        raise AssertionError("length probe must not execute tools")


def test_kernel_consumes_one_local_length_continuation_before_done():
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    provider = _ContinuableProvider()
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control"})),
        task_kind="project",
        project="",
        max_turns=3,
    )
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=provider,
                run_id="local-length-continuation",
                provider_id="local",
                user_task="already complete",
            ),
            execution=KernelExecutionDeps(
                executors={},
            ),
        ),
    )
    assert result.stop_reason == "done"
    assert len(provider.prompts) == 2
    assert "truncated" in provider.prompts[1]


def test_non_local_native_provider_does_not_get_local_length_continuation(monkeypatch):
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    monkeypatch.setenv("NATIVE_TOOLS", "1")
    provider = _AlwaysLengthProvider()
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=2)
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=provider,
                provider_id="web",
                run_id="web-length",
            ),
        ),
    )
    assert not result.completed
    assert all("Your previous response was truncated" not in prompt for prompt in provider.prompts)


def test_second_local_length_stop_is_provider_failure() -> None:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    provider = _AlwaysLengthProvider()
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control"})),
        task_kind="project",
        project="",
        max_turns=4,
    )
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=provider,
                run_id="local-double-length",
                provider_id="local",
                user_task="already complete",
            ),
            execution=KernelExecutionDeps(
                executors={},
            ),
        ),
    )

    assert result.completed is False
    assert result.stop_reason == "provider_failure"
    assert len(provider.prompts) == 2


def test_plain_text_after_length_never_completes() -> None:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    class Provider(_ContinuableProvider):
        def send_turn(self, prompt, tools=None, timeout=None):
            del tools, timeout
            self.prompts.append(str(prompt))
            if len(self.prompts) == 1:
                return AssistantTurn(text="partial", raw={"continuable_length": True})
            return AssistantTurn(text="Tests passed. Task complete.")

    provider = Provider()
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control"})),
        task_kind="project",
        project="",
        max_turns=2,
    )
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=provider,
                run_id="local-plain-after-length",
                provider_id="local",
                user_task="already complete",
            ),
            execution=KernelExecutionDeps(
                executors={},
            ),
        ),
    )

    assert result.completed is False
    assert result.stop_reason != "done"
