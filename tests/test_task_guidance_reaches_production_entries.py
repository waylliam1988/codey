"""Formal task entry, project adapter, and Research iteration inject domain guidance."""

from contextlib import closing
from pathlib import Path

import pytest

from codey.operations.context import RunHooks, RunWork
from codey.research.answer_contract import ANSWER_GUIDANCE
from tests.support.task_entry_helpers import entry_frame


class CaptureProvider:
    name = "deepseek"
    location = ""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        self.sent.append(text)
        return '{"tool":"done","args":{"summary":"inspected"}}'


@pytest.mark.parametrize("strict", [False, True])
def test_project_adapter_supplies_guidance_only_when_policy_requires_it(tmp_path: Path, strict: bool) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    provider = CaptureProvider()
    run(AgentRequest(provider=provider, project=tmp_path, task="inspect", max_turns=1,
                     strict_research=strict, on_event=lambda _event: None))
    assert len(provider.sent) == 1
    assert (ANSWER_GUIDANCE in provider.sent[0]) is strict


@pytest.mark.parametrize("strict", [False, True])
def test_formal_task_entry_supplies_guidance_from_original_policy(tmp_path: Path, strict: bool) -> None:
    from codey.app.context import AppContext
    from codey.app.task_services import build_task_deps
    from codey.operations.task_entry import run_entry_kernel
    from codey.operations.task_phases.lifecycle import _ensure_durable_task
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.task.model import TaskSubmission

    provider = CaptureProvider()
    submission = TaskSubmission("s", str(tmp_path), "inspect", 1, False, "deepseek",
                                intent="project", run_id="r", strict_research=strict)
    frame = entry_frame(submission)
    frame.provider = provider
    hooks = RunHooks(lambda _event: None, lambda _request: None, lambda _update: None,
                     lambda _id, _failure: None, lambda _append: None, lambda: (), None)
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state:
        assert _ensure_durable_task(state, submission)
        checkpoint = state.durable.task(frame.run_id)
        state.durable.start_task(frame.run_id, state=dict(checkpoint.state))
        state.runtime_mutations.accept_operation(session_id="s", run_id="r", provider_id="deepseek",
            turn_budget=submission.max_turns, max_repair_rounds=1, task_kind="project")
        outcome = run_entry_kernel(frame, RunWork(recent_events=[], evidence=ExecutionEvidence()), hooks, build_task_deps(state))
    assert len(provider.sent) == 1
    assert (ANSWER_GUIDANCE in provider.sent[0]) is strict
    if strict:
        assert outcome.event["stop_reason"] != "done"


def test_research_iteration_supplies_guidance_through_actual_shared_kernel(tmp_path: Path) -> None:
    from codey.app.context import AppContext
    from codey.operations.research_flow import ResearchFlowDeps
    from codey.operations.research_iteration import run_research_iteration
    from codey.policies.capabilities import BROWSER_CAPABILITIES
    from codey.policies.task_policy import build_task_policy
    from codey.research.tools import ResearchTools
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import admit

    provider = CaptureProvider()
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state:
        mutations = admit(state.durable, run_id="r", session_id="s", task="research question",
                          provider_id="deepseek", max_turns=1, task_kind="research")
        tools = ResearchTools(search=None, store=state.knowledge_store, changes=None)
        deps = ResearchFlowDeps(state, state.knowledge_store, None, lambda: None,
                                state.managed_outputs, mutations)
        result = run_research_iteration(deps,
            provider=provider, session_id="s", project="", task="research question", max_turns=1,
            on_event=lambda _event: None, stop_flag=None, provider_id="deepseek", run_id="r",
            chat_handoff="", trace_recorder=None, task_policy=build_task_policy(
                TaskSubmission("s", None, "research", 1, False, "deepseek", denied_capabilities=BROWSER_CAPABILITIES),
                task_kind="research", strict_research=True), search=object(), tools=tools,
        )
    assert len(provider.sent) == 1
    assert provider.sent[0].count(ANSWER_GUIDANCE) == 1
    assert "Register 1..8 questions with research_plan" not in provider.sent[0]
    assert "Submit only report_sections.coverage" not in provider.sent[0]
    visible = provider.sent[0].split("Visible tools: ", 1)[1].splitlines()[0]
    assert "submit_research_answer" in visible
    assert "research_plan" not in visible
    assert "save_finding" in visible
    assert "browser_navigate" not in visible
    assert "done" in visible
    assert result.result.stop_reason != "done"
