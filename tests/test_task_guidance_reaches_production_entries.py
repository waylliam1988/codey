"""Formal task entry, project adapter, and Research iteration inject domain guidance."""

from types import SimpleNamespace

import pytest


class CaptureProvider:
    name = "deepseek"

    def __init__(self):
        self.sent = []

    def new_chat(self):
        pass

    def send(self, text, timeout=None):
        self.sent.append(text)
        return '{"tool":"done","args":{"summary":"inspected"}}'


@pytest.mark.parametrize("strict", [False, True])
def test_project_adapter_supplies_guidance_only_when_policy_requires_it(tmp_path, strict):
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    provider = CaptureProvider()
    run(AgentRequest(provider=provider, project=tmp_path, task="inspect", max_turns=1,
                     strict_research=strict, on_event=lambda _event: None))
    assert len(provider.sent) == 1
    assert ("Research evidence rules" in provider.sent[0]) is strict


@pytest.mark.parametrize("strict", [False, True])
def test_formal_task_entry_supplies_guidance_from_original_policy(tmp_path, strict):
    from codey.app.context import AppContext
    from codey.operations.task_entry import run_entry_kernel
    from codey.task.model import TaskSubmission

    provider = CaptureProvider()
    submission = TaskSubmission("s", str(tmp_path), "inspect", 1, False, "deepseek",
                                intent="project", run_id="r", strict_research=strict)
    frame = SimpleNamespace(request=submission, task_kind="project", run_id="r", provider=provider,
                            provider_id="deepseek", project_text=str(tmp_path), handoff="",
                            recovered_tool_outcomes=(), settled_tool_outcomes=(), recovered_tool_result_batch_id="",
                            entry_initial_turn=None)
    hooks = SimpleNamespace(on_event=lambda _event: None, on_shell_request=None)
    deps = SimpleNamespace(knowledge_store=None, runtime_mutations=None, state=AppContext(tmp_path / "state"))
    outcome = run_entry_kernel(frame, SimpleNamespace(evidence=None, analysis_run_payloads=[]), hooks, deps)
    assert len(provider.sent) == 1
    assert ("Research evidence rules" in provider.sent[0]) is strict
    if strict:
        assert outcome.event["stop_reason"] != "done"


def test_research_iteration_supplies_guidance_through_actual_shared_kernel():
    from codey.operations.research_iteration import run_research_iteration
    from codey.research.ledger import ResearchLedger

    provider = CaptureProvider()
    tools = SimpleNamespace(ledger=ResearchLedger(), created_ids=[], updated_ids=[],
                            links_created=0, sources_read=set(), search_result_urls=set())
    result = run_research_iteration(
        SimpleNamespace(knowledge_store=object(), managed_outputs=None, run_research_advisors=None),
        provider=provider, session_id="s", project="", task="research question", max_turns=1,
        on_event=lambda _event: None, stop_flag=None, provider_id="deepseek", run_id="r",
        chat_handoff="", trace_recorder=None, search=object(), tools=tools,
    )
    assert len(provider.sent) == 1
    assert "Research evidence rules" in provider.sent[0]
    assert "Strict research completion checklist" in provider.sent[0]
    assert result.result.stop_reason != "done"
