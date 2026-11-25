"""Production cutover locks for the shared Coding and Research loop."""

from __future__ import annotations

import json
from collections.abc import Generator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.agents.shell_approval import ShellApprovalRequest
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.operations.context import RunHooks, RunWork
from codey.operations.provider_session import adapt_provider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import KernelExecutionDeps, KernelObservationDeps, KernelRunRequest, KernelTransportDeps
from codey.policies.capabilities import BROWSER_CAPABILITIES
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolDefinition, ProviderToolResult
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchToolOutput
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.storage.managed_outputs import ManagedOutputStore
from codey.toolchain.runtime import ToolOutcome
from codey.toolchain.tool_spec import ToolSpec
from codey.types.json_value import JsonObject, decode_json_object
from tests.support.kernel_fixture import runtime
from tests.support.task_entry_helpers import entry_frame


class EmptyProvider:
    def new_chat(self) -> None:
        pass

    def send(self, prompt: str, timeout: float | None = None) -> str:
        return "ok"


def test_turn_budget_exhaustion_labels_the_rejected_draft_as_incomplete() -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.tool_requirement import require_tool_success
    from tests.support.kernel_fixture import run_task_kernel

    class Provider:
        def send(self, prompt: str, timeout: float | None = None) -> str:
            return json.dumps({"tool": "done", "args": {"summary": "All edits complete and checks passed."}})

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"}),
        required_checks=(require_tool_success("read_file"),)), max_turns=1)
    result = run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(adapt_provider(Provider()))))
    assert not result.completed and result.stop_reason == "max_turns", result
    assert result.summary.startswith("max turns reached; the task is incomplete.")
    assert "Unaccepted draft:\nAll edits complete and checks passed." in result.summary


@contextmanager
def observed_runtime(kind: type[KernelEffectSink]) -> Generator[KernelEffectSink, None, None]:
    with runtime(run_id="run") as base:
        yield kind(base.mutations, session_id=base.session_id, run_id=base.run_id, provider_id=base.provider_id,
                   durable=base.durable, managed_outputs=base.managed_outputs)


def entry_hooks() -> RunHooks:
    return RunHooks(lambda _event: None, lambda _request: None, lambda _update: None,
                    lambda _id, _failure: None, lambda _append: None, lambda: (), None)


def _cutover_parent_policy() -> TaskPolicy:
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({
        "control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link",
    }))


@pytest.mark.parametrize("project, expected", [("E:/codey", "project"), (None, "chat")])
def test_opt_in_task_kind_reaches_shared_kernel(tmp_path: Path, project: str | None, expected: str) -> None:
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    from codey.task.kind import resolve_task_kind
    from codey.task.model import TaskSubmission

    request = TaskSubmission("s", project, "task", 2, False, "local", intent="project", run_id="r")
    kind = resolve_task_kind(request)
    assert kind == expected
    # Hybrid still hits the single session without legacy two-phase.
    hybrid_outcome = ModeOutcome({"type": "task_done", "mode": "hybrid"})
    from codey.operations.task_phases.dispatch import project_completion_deps, review_flow_deps
    from codey.workspace.config import ProjectConfigLoadResult
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state, patch(
            "codey.operations.task_phases.dispatch.run_task_mode", return_value=hybrid_outcome) as single:
        from tests.support.kernel_fixture import admit
        admit(state.durable, run_id="r", session_id="s", task="task", provider_id="local", max_turns=2)
        deps = build_task_deps(state)
        actual = dispatch_run_mode(
            deps, project_completion_deps(deps), review_flow_deps(deps), RunWork([], ExecutionEvidence()),
            entry_frame(request), entry_hooks(), "hybrid", ProjectConfigLoadResult(),
        )
    assert actual is hybrid_outcome
    assert single.call_count == 1
    assert single.call_args.kwargs["task_kind"] == "hybrid"


@pytest.mark.parametrize("kind", ["hybrid"])
def test_default_kinds_all_use_shared_kernel(tmp_path: Path, kind: str) -> None:
    """Default hybrid uses one session; no opt-in needed.

    Project/research/planning already drive the same kernel internally
    (writer adapter / research iteration) plus their review/repair phases,
    so only hybrid needs a dispatch cutover from two-phase to one session.
    """
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode

    outcome = ModeOutcome({"type": "task_done", "mode": kind})
    from codey.operations.task_phases.dispatch import project_completion_deps, review_flow_deps
    from codey.task.model import TaskSubmission
    from codey.workspace.config import ProjectConfigLoadResult
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state, patch(
            "codey.operations.task_phases.dispatch.run_task_mode", return_value=outcome) as unified:
        from tests.support.kernel_fixture import admit
        admit(state.durable, run_id="r", session_id="s", task="task", provider_id="local", max_turns=2)
        deps = build_task_deps(state)
        actual = dispatch_run_mode(
            deps, project_completion_deps(deps), review_flow_deps(deps), RunWork([], ExecutionEvidence()),
            entry_frame(TaskSubmission("s", str(tmp_path), "task", 2, False, "local", run_id="r")), entry_hooks(), kind,
            ProjectConfigLoadResult(),
        )
    assert actual is outcome
    assert unified.call_count == 1
    assert unified.call_args.kwargs["task_kind"] == kind


def test_unified_alias_removed_project_uses_project_runtime_modes() -> None:
    from codey.task.kind import (
        conversation_mode,
        startup_failover_mode,
        trace_mode,
        ui_mode,
        writer_failover_mode,
    )

    # Cold-start alias deleted: no unified projection remains.
    assert startup_failover_mode("unified") == "unified"
    # Project modes still project correctly.
    assert startup_failover_mode("project") == "project"
    assert writer_failover_mode("project") == "project"
    assert conversation_mode("project", "E:/codey") == "project"
    assert conversation_mode("project", None) == "chat"
    assert ui_mode("project", "E:/codey") == "agent"
    assert trace_mode("project", "E:/codey") == "project"


def test_task_entry_research_builds_default_search_provider(tmp_path: Path) -> None:
    from codey.operations.task_execution import build_research_tools
    from codey.research.web_sources import WebSourceProvider

    search = WebSourceProvider()
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state, patch(
            "codey.research.search_factory.default_research_search_provider", return_value=search):
        tools = build_research_tools(build_task_deps(state), session_id="s", project="")
    assert tools is not None
    assert tools.search is search


def test_task_entry_uses_same_runtime_receipts_and_events(tmp_path: Path) -> None:
    from codey.app.context import AppContext
    from codey.operations.task_entry import run_entry_kernel
    from codey.operations.task_loop import KernelResult
    from codey.task.model import TaskSubmission

    request = TaskSubmission("s", str(tmp_path), "inspect", 1, False, "deepseek",
                             intent="project", run_id="r")
    provider = adapt_provider(EmptyProvider())
    frame = entry_frame(request)
    frame.provider = provider
    events: list[RunEvent] = []
    from dataclasses import replace
    hooks = replace(entry_hooks(), on_event=events.append)
    state = AppContext(tmp_path / ".codey")
    from tests.support.kernel_fixture import admit

    admit(state.durable, run_id=frame.run_id, session_id=frame.request.session_id,
          task=frame.request.task, provider_id=frame.provider_id, max_turns=frame.request.max_turns)
    managed = state.managed_outputs
    deps = build_task_deps(state)
    from codey.operations import project_adapter
    from codey.operations.task_effects import KernelEffectSink

    # Import the adapter outside the constructor patch: otherwise its module
    # captures the fake sink and contaminates every subsequent writer test.
    assert project_adapter is not None and KernelEffectSink is not None
    with (patch("codey.operations.task_loop.run_task_kernel",
                return_value=KernelResult(True, "done", 1, "done")) as kernel,
          patch("codey.operations.task_effects.KernelEffectSink") as sink_type):
        result = run_entry_kernel(frame, RunWork(recent_events=[], evidence=ExecutionEvidence()),
                                  hooks, deps)
    assert result.event["stop_reason"] == "done"
    sink_type.assert_called_once()
    request = kernel.call_args.kwargs["request"]
    assert request.execution.intent_sink is sink_type.return_value
    assert request.observation.on_event is hooks.on_event
    assert request.observation.on_shell_request is hooks.on_shell_request
    assert sink_type.call_args.kwargs["managed_outputs"] is managed
    state.close()


def test_task_entry_creates_new_authorized_project(tmp_path: Path) -> None:
    from codey.app.context import AppContext
    from codey.operations.task_entry import run_entry_kernel
    from codey.operations.task_loop import KernelResult
    from codey.task.model import TaskSubmission

    project = tmp_path / "new-project"
    request = TaskSubmission("s", str(project), "create app", 1, False, "local",
                             intent="project", run_id="r")
    frame = entry_frame(request)
    frame.provider = adapt_provider(EmptyProvider())
    hooks = entry_hooks()
    state = AppContext(tmp_path / "state")
    deps = build_task_deps(state)
    from tests.support.kernel_fixture import admit

    admit(state.durable, run_id=frame.run_id, session_id=frame.request.session_id,
          task=frame.request.task, provider_id=frame.provider_id, max_turns=frame.request.max_turns)
    with patch("codey.operations.task_loop.run_task_kernel",
               return_value=KernelResult(True, "done", 1, "done")) as kernel:
        run_entry_kernel(frame, RunWork(recent_events=[], evidence=ExecutionEvidence()),
                         hooks, deps)
    assert project.is_dir()
    state.close()
    assert kernel.call_args.kwargs["request"].execution.project_path == project.resolve()


def test_unified_kernel_passes_authoritative_research_ledger_to_gate(tmp_path: Path, public_source_dns: None) -> None:
    from dataclasses import replace

    from codey.knowledge.store import KnowledgeStore
    from codey.operations.completion_gate import evaluate as real_evaluate
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.research.tools import ResearchTools
    from tests.support.kernel_fixture import run_task_kernel
    from tests.test_unified_task_capabilities import Source

    URL = "https://example.com/reference"
    store = KnowledgeStore(tmp_path / "knowledge")
    tools = ResearchTools(Source(), store, None)
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.write"}),
        strict_research=True, required_checks=("research_submission",)), task_kind="research", task_text="Explain writer concurrency.")
    session.policy = replace(session.policy, strict_research=True)
    session.max_turns = 1

    class Provider:
        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return json.dumps({"tool": "submit_research_answer", "args": {
                "state": "answered", "markdown": "Only one writer can write at a time [e1]."}})

    try:
        execute_turn(session, [ToolCall("open_url", {"url": URL})], research_tools=tools)
        with patch("codey.operations.completion_gate.evaluate", wraps=real_evaluate) as evaluate:
            result = run_task_kernel(session, request=KernelRunRequest(
                transport=KernelTransportDeps(provider=adapt_provider(Provider()), user_task=session.task_text),
                execution=KernelExecutionDeps(research_tools=tools),
                observation=KernelObservationDeps(completion_context={"research_ledger": tools.ledger})))
        assert evaluate.call_count == 1
        assert evaluate.call_args.kwargs["context"].research_ledger is tools.ledger
        assert result.stop_reason == "done", result.summary
        assert "Only one writer" in result.summary and "[e1]" in result.summary
    finally:
        store.close()


def _prepared_research_prompt(*, native: bool=False) -> str:
    from codey.operations.kernel_preparation import prepare_kernel_turn
    from codey.operations.task_guidance import task_guidance_for_policy
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.answer_context import answer_context
    from codey.research.answer_contract import answer_scope

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                          strict_research=True),
        task_kind="research", task_text="Explain writers and version conditions in Chinese.",
    )
    prepared = prepare_kernel_turn(session, user_task=session.task_text, context_text="",
                                   task_guidance=task_guidance_for_policy(session.policy, task_kind=session.task_kind),
                                   native=native)
    # The kernel appends the current task/evidence view after base preparation.
    return prepared.prompt + "\n\n" + answer_context(session, ResearchLedger(), answer_scope(session))


@pytest.mark.parametrize("native", [False, True])
def test_explicit_research_prompt_explains_evidence_and_report_contract(native: bool) -> None:
    from codey.research.answer_contract import ANSWER_GUIDANCE

    prompt = _prepared_research_prompt(native=native)
    assert prompt.count(ANSWER_GUIDANCE) == 1
    for required in ("web_search", "open_url", "submit_research_answer", "knowledge_updates",
                     "complete original task", "condition", "[eN]"):
        assert required in prompt
    for obsolete in ("report_sections.coverage", "task_refs", "research_plan", "question_ref=qN"):
        assert obsolete not in prompt


@pytest.mark.parametrize("native", [False, True])
def test_strict_research_prompt_frontloads_completion_recovery_steps(native: bool) -> None:
    prompt = _prepared_research_prompt(native=native)
    for required in ("Do not invent facts or gaps", "state=answered", "partial", "unresolved",
                     "complete answer", "publishes that exact submission", "not proof of correctness"):
        assert required in prompt
    assert "After rejection edit the specified field in existing rows" not in prompt


@pytest.mark.usefixtures("public_source_dns")
def test_web_research_shows_result_and_source_ids_to_model(tmp_path: Path) -> None:
    from codey.knowledge.changes import KnowledgeChanges
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.research.tools import ResearchTools
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import run_task_kernel

    url = "https://example.com/evidence"

    class Search:
        def search(self, query: str, *, limit: int=8) -> list[JsonObject]:
            return [{"title": "Evidence", "url": url, "snippet": "claim"}]

        def fetch(self, url: str) -> dict[str, object]:
            return {"status": "ok", "url": url, "title": "Evidence", "text": "claim text"}

    prompts: list[str] = []
    replies = iter([
        '{"tool":"web_search","args":{"query":"claim"}}',
        '{"tool":"open_result","args":{"result_id":"r1"}}',
        json.dumps({"tool": "source_search", "args": {"url": url, "query": "claim"}}),
    ])

    class Provider:
        def send(self, prompt: str, timeout: float | None=None) -> str:
            prompts.append(prompt)
            return next(replies)

    store = KnowledgeStore(tmp_path / "knowledge")
    tools = ResearchTools(search=Search(), store=store, changes=KnowledgeChanges(root=store.root))
    policy = build_task_policy(TaskSubmission("s", None, "claim", 3, False, "deepseek", denied_capabilities=BROWSER_CAPABILITIES),
                               task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research", max_turns=3, task_text="claim")
    run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=adapt_provider(Provider()), user_task="claim",
            ),
            execution=KernelExecutionDeps(
                research_tools=tools,
            ),
        ),
    )
    assert "r1" in prompts[1] and url in prompts[1]
    assert "s1" in prompts[2] and url in prompts[2]
    assert "[e1]" in prompts[2] and "claim text" in prompts[2]
    store.close()


def test_search_result_ids_remain_stable_across_searches() -> None:
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.tools import ResearchTools
    from codey.runtime.core.models import ToolCall

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})),
                          task_kind="project")
    urls = iter(("https://example.com/a", "https://example.com/b"))

    class Search:
        def search(self, query: str, *, limit: int=8) -> list[JsonObject]:
            return [{"title": "Result", "url": next(urls), "snippet": ""}]

        def fetch(self, url: str) -> dict[str, object]:
            raise AssertionError("this test only searches")

    tools = ResearchTools(search=Search(), store=None, changes=None)
    execute_turn(session, [ToolCall("web_search", {"query": "a"})],
                 research_tools=tools, run_id="r", turn=1)
    execute_turn(session, [ToolCall("web_search", {"query": "b"})],
                 research_tools=tools, run_id="r", turn=2)
    assert session.search_results == {
        "r1": "https://example.com/a", "r2": "https://example.com/b",
    }


def test_open_hit_uses_source_locator_in_shared_research_kernel() -> None:
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.passages import describe_window
    from codey.research.source_observation import source_observation
    from codey.research.tools import ResearchToolOutput
    from codey.runtime.core.models import ToolCall

    url = "https://example.com/long"
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})),
                          task_kind="research")
    session.record_open(url)
    opened: list[tuple[str, int]] = []

    from codey.research.tools import ResearchTools

    class Tools(ResearchTools):
        def __init__(self) -> None:
            super().__init__(search=None, store=None, changes=None)

        def source_search(self, url: str, query: str, limit: object=6) -> ResearchToolOutput:
            assert url == "https://example.com/long"
            self.ledger.record_source_search(url, query, [{"offset": 123, "page": None,
                                                              "snippet": "relevant excerpt"}])
            return ResearchToolOutput("source_search results\n1. offset 123: relevant excerpt", ok=True)

        def read_source(self, observation: JsonObject, *, offset: int=0, limit: int=6000, pages: str="") -> ResearchToolOutput:
            opened.append((url, offset))
            source, text, _pages = source_observation(observation)
            return ResearchToolOutput(model_text="Title: Long\nrelevant excerpt",
                                      read_observation=observation,
                                      read_passages=tuple(describe_window(source.final_url, source.text_hash,
                                                                          text, offset, limit)), ok=True)

    tools = Tools()
    source_text = "x" * 123 + "relevant excerpt"
    tools.ledger.record_open(url, url, "Long", source_text)
    search = execute_turn(session, [ToolCall("source_search", {"url": url, "query": "relevant"})],
                          research_tools=tools, run_id="r", turn=1)[0]
    assert "h1" in search.model_text
    opened_result = execute_turn(session, [ToolCall("open_hit", {"hit_id": "h1"})],
                                 research_tools=tools, run_id="r", turn=2)[0]
    assert opened_result.ok is True
    assert opened == [(url, 123)]


def test_default_project_writer_uses_shared_turn_kernel(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest

    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    replies = iter([
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"done","args":{"summary":"read the file"}}',
    ])

    class WebProvider:
        name = "deepseek"

        def new_chat(self, timeout: float | None=None) -> None:
            return None

        def send(self, prompt: str, timeout: float | None=None) -> str:
            return next(replies)

        def close(self) -> None:
            return None

    with patch("codey.operations.task_loop.run_task_kernel", wraps=None) as kernel:
        from codey.operations.task_loop import KernelResult

        kernel.return_value = KernelResult(True, "read the file", 2, "done")
        from tests.support.kernel_fixture import run_agent

        result = run_agent(AgentRequest(
            provider=adapt_provider(WebProvider()), project=tmp_path, task="read file.txt", max_turns=3,
        ))
    assert result.stop_reason == "done"
    kernel.assert_called_once()


def test_project_writer_can_search_and_read_in_one_model_loop(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from codey.policies.task_policy import TaskPolicy
    from codey.research.tools import ResearchTools
    from tests.support.kernel_fixture import run_agent as run

    (tmp_path / "file.txt").write_text("local content", encoding="utf-8")
    replies = iter([
        '{"tool":"web_search","args":{"query":"official docs"}}',
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"done","args":{"summary":"compared docs with local content"}}',
    ])
    seen: list[str] = []
    events: list[RunEvent] = []

    class WebProvider:
        name = "deepseek"

        def new_chat(self, timeout: float | None=None) -> None:
            return None

        def send(self, prompt: str, timeout: float | None=None) -> str:
            seen.append(str(prompt))
            return next(replies)

    class Search:
        def search(self, query: str, *, limit: int=8) -> list[JsonObject]:
            assert query == "official docs"
            return [{"title": "Docs", "url": "https://example.com/docs", "snippet": "Official docs"}]

        def fetch(self, url: str) -> dict[str, object]:
            raise AssertionError("this test only searches")

    result = run(AgentRequest(
        provider=adapt_provider(WebProvider()), project=tmp_path, task="查官方文档并读 file.txt",
        max_turns=4, task_policy=TaskPolicy(grants=frozenset({"control", "web.read", "project.read"})),
        research_tools=ResearchTools(search=Search(), store=None, changes=None),
        on_event=events.append,
    ))
    assert result.stop_reason == "done"
    assert result.turns == 3
    assert len(seen) == 3
    assert any(getattr(event, "kind", "") == "tool" for event in events)


def test_project_writer_keeps_bounded_project_context_in_first_prompt(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    (tmp_path / "AGENTS.md").write_text("Follow the local migration rule.", encoding="utf-8")
    prompts: list[str] = []

    class Provider:
        name = "deepseek"

        def new_chat(self, timeout: float | None=None) -> None:
            return None

        def send(self, prompt: str, timeout: float | None=None) -> str:
            prompts.append(prompt)
            return '{"tool":"done","args":{"summary":"context received"}}'

    run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="inspect the project",
        max_turns=1, on_event=lambda _event: None,
        project_facts="verified fact 42", research_context="research brief 42",
        project_map="project map 42", work_checkpoint="checkpoint 42",
    ))
    assert len(prompts) == 1
    for expected in ("Follow the local migration rule.", "verified fact 42",
                     "research brief 42", "project map 42", "checkpoint 42"):
        assert expected in prompts[0]


def test_project_writer_prompts_with_selected_verification_candidates(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from codey.completion.verification_policy import VerificationCandidate
    from tests.support.kernel_fixture import run_agent as run

    prompts: list[str] = []

    class Provider:
        name = "deepseek"

        def send(self, prompt: str, timeout: float | None=None) -> str:
            prompts.append(prompt)
            return '{"tool":"done","args":{"summary":"inspected"}}'

    run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="inspect", max_turns=1,
        fresh_chat=False, on_event=lambda _event: None,
        verification_candidates=(VerificationCandidate("python -m pytest -q", "."),),
    ))
    assert "python -m pytest -q" in prompts[0]


def test_planning_adapter_exposes_only_read_capabilities(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    prompts: list[str] = []

    class Provider:
        name = "deepseek"

        def send(self, prompt: str, timeout: float | None=None) -> str:
            prompts.append(prompt)
            return '{"tool":"done","args":{"summary":"plan"}}'

    run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="plan the changes",
        permission_profile="planning_readonly", fresh_chat=False,
        max_turns=1, on_event=lambda _event: None,
    ))
    assert "read_file" in prompts[0]
    assert '"tool":"edit"' not in prompts[0]
    assert '"tool":"shell"' not in prompts[0]


def test_web_chat_refresh_failure_stops_before_sending_to_existing_tab(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    class Provider:
        name = "deepseek"

        def new_chat(self) -> None:
            raise RuntimeError("browser tab unavailable")

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            raise AssertionError("failed fresh chat must not reuse the current tab")

    import pytest

    with pytest.raises(RuntimeError, match="browser tab unavailable"):
        run(AgentRequest(
            provider=adapt_provider(Provider()), project=tmp_path, task="continue", max_turns=1,
            fresh_chat=True, on_event=lambda _event: None,
        ))


def test_shared_writer_updates_conversation_window_usage(tmp_path: Path) -> None:
    from codey.agents.handoff import ConversationContext
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    class Provider:
        name = "deepseek"

        def new_chat(self) -> None:
            return None

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return '{"tool":"done","args":{"summary":"finished"}}'

    conversation = ConversationContext()
    run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="finish", max_turns=1,
        fresh_chat=True, conversation=conversation, on_event=lambda _event: None,
    ))
    assert conversation.initialized
    assert conversation.estimated_context_tokens is not None and conversation.estimated_context_tokens > 0
    assert conversation.project == str(tmp_path)


def test_writer_stops_after_configured_invalid_turn_limit(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    calls: list[str] = []

    class Provider:
        name = "deepseek"

        def send(self, prompt: str, timeout: float | None=None) -> str:
            calls.append(prompt)
            return '{"tool":"read_file","args":'

    result = run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="inspect", max_turns=20,
        stagnant_turns=2, fresh_chat=False, on_event=lambda _event: None,
    ))
    assert result.turns == 2
    assert result.stop_reason == "protocol"
    assert len(calls) == 2


def test_writer_can_create_a_user_selected_new_project(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    project = tmp_path / "new-project"

    class Provider:
        name = "deepseek"

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return '{"tool":"edit","args":{"path":"hello.txt","content":"hello"}}'

    run(AgentRequest(
        provider=adapt_provider(Provider()), project=project, task="create hello.txt",
        max_turns=1, fresh_chat=False, on_event=lambda _event: None,
    ))
    assert (project / "hello.txt").read_text(encoding="utf-8") == "hello"


def test_failed_verification_after_pass_blocks_done_at_same_revision(tmp_path: Path) -> None:
    from codey.agents.tools import AgentToolFns
    from codey.operations.completion_gate import evaluate
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolCall
    from codey.task.model import TaskSubmission
    from codey.toolchain.runtime import ToolOutcome

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "edit and test", 3,
                                              False, "deepseek"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path))
    session.record_edit("app.py")
    outcomes = iter((ToolOutcome("1 passed", True, exit_code=0),
                     ToolOutcome("1 failed", False, exit_code=1)))
    def command(*args: object) -> ToolOutcome:
        return next(outcomes)

    fns = AgentToolFns(run_command_with_context=command)
    for turn in (1, 2):
        execute_turn(session, [ToolCall("run", {"path": ".", "command": "python -m pytest"})],
                     project_path=tmp_path, tool_fns=fns, run_id="r", turn=turn)
    verdict = evaluate(session, "tests done")
    assert not verdict.complete
    assert any(not row["passed"] for row in session.verifications)


def test_noop_edit_does_not_create_false_freshness(tmp_path: Path) -> None:
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolCall
    from codey.task.model import TaskSubmission

    (tmp_path / "a.txt").write_text("same", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "edit", 3, False, "deepseek"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path))
    execute_turn(session, [ToolCall("read_file", {"path": "a.txt"})],
                 project_path=tmp_path, run_id="r", turn=1)
    result = execute_turn(session, [ToolCall("edit", {
        "path": "a.txt", "replacements": [{"old_string": "same", "new_string": "same"}],
    })], project_path=tmp_path, run_id="r", turn=2)[0]
    assert "no changes" in result.model_text
    assert not session.edited_files




def test_research_done_builds_proof_record_from_current_ledger(tmp_path: Path) -> None:
    """Legacy node id: plain text cannot replace a settled whole submission."""
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy

    ledger = ResearchLedger()
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "web.read"}), strict_research=True,
                          required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS),
        task_kind="research", task_text="x",
    )
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        with patch("codey.research.object_model.build_research_record", return_value=object()) as build:
            verdict = evaluate(session, "结论 x 来源 y",
                context={"research_ledger": ledger, "knowledge_store": store, "question": "x"})
    finally:
        store.close()
    build.assert_not_called()
    assert not verdict.complete
    assert verdict.proof is None and verdict.research_report is None
    assert "whole research answer has not been submitted" in verdict.followup




def test_project_shell_requests_approval_in_shared_kernel(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    approvals: list[ShellApprovalRequest] = []

    class Provider:
        name = "deepseek"

        def new_chat(self, timeout: float | None=None) -> None:
            return None

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return '{"tool":"shell","args":{"path":".","command":"git status"}}'

    result = run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="run git status with approval",
        max_turns=2, on_shell_request=approvals.append,
    ))
    assert result.stop_reason == "approval", result.summary
    assert len(approvals) == 1
    assert approvals[0].command == "git status"


def test_shell_approval_records_turn_intent_before_notifying_user(tmp_path: Path) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import approval_notifier, run_task_kernel

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "shell", 1, False, "deepseek"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    order: list[str] = []

    class Sink(KernelEffectSink):
        def begin_turn(self, items: list[tuple[str, ToolCall, int]], *, turn: int, specs: Mapping[str, ToolSpec] | None=None, scope: str="") -> None:
            assert turn == 1 and items[0][1].name == "shell"
            super().begin_turn(items, turn=turn, specs=specs, scope=scope)
            order.append("intent")

    class Provider:
        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return '{"tool":"shell","args":{"path":".","command":"git status"}}'

    with observed_runtime(Sink) as sink:
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=adapt_provider(Provider()),
                    run_id=sink.run_id, provider_id=sink.provider_id,
                ),
                execution=KernelExecutionDeps(
                    project_path=tmp_path,
                ),
                observation=KernelObservationDeps(
                    intent_sink=sink,
                    on_shell_request=approval_notifier(sink.durable, sink.managed_outputs, sink.session_id,
                        sink.run_id, lambda _approval: order.append("approval")),
                ),
            ),
        )
    assert result.stop_reason == "approval", result.summary
    assert order == ["intent", "approval"]


def test_research_write_requires_project_writer_lease() -> None:
    from codey.operations.task_run import requires_project_writer_lease
    from codey.task.model import TaskSubmission

    read_only = TaskSubmission("s", "E:/codey", "research", 3, False, "local",
                               intent="research", strict_research=True)
    writable = TaskSubmission("s", "E:/codey", "research and edit", 3, False, "local",
                              intent="research", strict_research=True,
                              requested_capabilities=("project.write",))
    assert not requires_project_writer_lease(read_only, "research")
    assert requires_project_writer_lease(writable, "research")
    project_task = TaskSubmission("s", "E:/codey", "edit", 3, False, "local", intent="project")
    assert requires_project_writer_lease(project_task, "project")


def test_kernel_records_intent_before_project_tool_execution(tmp_path: Path) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import run_task_kernel

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 2, False, "local"),
                               task_kind="project")
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    observed: list[str] = []

    class Sink(KernelEffectSink):
        def begin_turn(self, items: list[tuple[str, ToolCall, int]], *, turn: int, specs: Mapping[str, ToolSpec] | None=None, scope: str="") -> None:
            super().begin_turn(items, turn=turn, specs=specs, scope=scope)
            observed.append(f"begin:{turn}")

        def settle(self, identity: str, ok: bool, *, result: ToolResult | None=None, exit_code: int | None=None) -> None:
            super().settle(identity, ok, result=result, exit_code=exit_code)
            observed.append("settle")

    class Provider:
        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return '{"tool":"read_file","args":{"path":"file.txt"}}'

    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    with observed_runtime(Sink) as sink:
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=adapt_provider(Provider()), run_id=sink.run_id, provider_id=sink.provider_id,
                ),
                execution=KernelExecutionDeps(
                    project_path=tmp_path,
                ),
                observation=KernelObservationDeps(
                    intent_sink=sink,
                    on_event=lambda event: observed.append(event.kind),
                ),
            ),
        )
    assert result.stop_reason == "max_turns", result.summary
    assert observed.index("begin:1") < observed.index("settle")


def test_failed_intent_commit_prevents_tool_execution(tmp_path: Path) -> None:
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolCall
    from codey.task.model import TaskSubmission

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "write", 2, False, "local"),
                               task_kind="project")
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path))

    class Sink(KernelEffectSink):
        def begin_turn(self, items: list[tuple[str, ToolCall, int]], *, turn: int, specs: Mapping[str, ToolSpec] | None=None, scope: str="") -> None:
            raise RuntimeError("journal unavailable")

    with observed_runtime(Sink) as sink, pytest.raises(RuntimeError, match="journal unavailable"):
        execute_turn(session, [ToolCall("edit", {"path": "file.txt", "content": "x"})],
                     project_path=tmp_path, intent_sink=sink)
    assert not (tmp_path / "file.txt").exists()


def test_production_kernel_persists_tool_and_delivery_receipts(tmp_path: Path) -> None:
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import run_task_kernel

    durable = DurableSession(SQLiteDurableStore(tmp_path / "state" / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text="read",
                        provider_snapshot={"provider_id": "deepseek", "model_id": "test"})
    durable.start_task("r", state={"task_kind": "project"})
    durable.advance_task(task_id="r", phase="running", state={"task_kind": "project"})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", project=str(tmp_path),
                          provider_id="deepseek", turn_budget=3, max_repair_rounds=0,
                          task_kind="project")
    line.mark_writer_running("s", "r", provider_id="deepseek")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="deepseek", durable=durable, managed_outputs=ManagedOutputStore(Path(tmp_path)))
    replies = iter([
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"done","args":{"summary":"read file"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return next(replies)

    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 3, False, "deepseek"),
                               task_kind="project")
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=3)
    outcome = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=adapt_provider(Provider()),
                run_id="r",
            ),
            execution=KernelExecutionDeps(
                project_path=tmp_path,
            ),
            observation=KernelObservationDeps(
                intent_sink=sink,
            ),
        ),
    )
    assert outcome.stop_reason == "done"
    effects = RuntimeEffectStore(durable=durable).load_effects("s", "r")
    assert any(row.intent.effect_category == "tool_call" and row.settlement is not None for row in effects)
    assert sum(row.intent.effect_category == "provider_send" for row in effects) == 2
    batches = ToolResultDeliveryStore(durable=durable).load_batches("s", "r")
    assert len(batches) == 1
    assert batches[0].is_delivered
    durable.close()


def test_shared_effect_replay_class_matches_tool_contract(tmp_path: Path) -> None:
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    durable = DurableSession(SQLiteDurableStore(tmp_path / "state" / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text="research",
                        provider_snapshot={"provider_id": "deepseek", "model_id": "test"})
    durable.start_task("r", state={"task_kind": "research"})
    durable.advance_task(task_id="r", phase="running", state={"task_kind": "research"})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", project="", provider_id="deepseek",
                          turn_budget=2, max_repair_rounds=0, task_kind="research")
    line.mark_writer_running("s", "r", provider_id="deepseek")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="deepseek", durable=durable, managed_outputs=ManagedOutputStore(Path(tmp_path)))
    calls = [ToolCall("web_search", {"query": "q"}),
             ToolCall("open_url", {"url": "https://example.com"}),
             ToolCall("knowledge_write", {"title": "note"})]
    sink.begin_turn([(f"effect-{i}", call, i) for i, call in enumerate(calls)], turn=1)
    rows = {row.intent.tool_name: row.intent.replay_class
            for row in RuntimeEffectStore(durable=durable).load_effects("s", "r")}
    assert rows["web_search"] == "unsafe"
    assert rows["open_url"] == "unsafe"
    assert rows["knowledge_write"] == "unsafe"
    durable.close()


def test_same_run_research_and_writer_effect_slots_do_not_collide(tmp_path: Path) -> None:
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.kernel.kernel_driver import drive_provider_request
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.provider_request import prepare_provider_request
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.requests import SendTextRequest
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    durable = DurableSession(SQLiteDurableStore(tmp_path / "state" / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text="hybrid",
                        provider_snapshot={"provider_id": "deepseek", "model_id": "test"})
    durable.start_task("r", state={"task_kind": "hybrid"})
    durable.advance_task(task_id="r", phase="running", state={"task_kind": "hybrid"})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", project=str(tmp_path),
                          provider_id="deepseek", turn_budget=2, max_repair_rounds=0,
                          task_kind="hybrid")
    line.mark_writer_running("s", "r", provider_id="deepseek")
    calls: list[str] = []
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "project.read"}))
    research = TaskSession(policy=policy, task_kind="hybrid")
    writer = TaskSession(policy=policy, task_kind="hybrid")
    cases: tuple[tuple[TaskSession, str, str, JsonObject], ...] = (
        (research, "research:1", "web_search", {"query": "q"}),
        (writer, "writer", "read_file", {"path": "a.txt"}),
    )
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research.tools import ResearchTools
    from tests.test_unified_task_capabilities import Source

    delegate = ExecutionDelegate(session=research, research_tools=ResearchTools(Source(), None, None))

    def execute(call: ToolCall) -> ToolResult:
        calls.append(call.name)
        return delegate.execute(call)[0] if call.name == "web_search" else ToolResult(call, "ok", ok=True)

    for session, scope, name, args in cases:
        sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="deepseek", durable=durable, managed_outputs=ManagedOutputStore(Path(tmp_path)))
        execute_turn(
            session, [ToolCall(name, args)],
            executors={name: execute},
            run_id="r", effect_scope=scope, turn=1, intent_sink=sink,
        )
        reference = prepare_provider_request(sink, SendTextRequest("continue"), counters=None)
        drive_provider_request(sink, adapt_provider(EmptyProvider()), reference)
    assert calls == ["web_search", "read_file"]
    effects = [row for row in RuntimeEffectStore(durable=durable).load_effects("s", "r")
               if row.intent.effect_category == "tool_call"]
    assert len({row.intent.effect_id for row in effects}) == 2
    durable.close()


def test_default_writer_records_effects_in_existing_runtime(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from tests.support.kernel_fixture import run_agent as run

    durable = DurableSession(SQLiteDurableStore(tmp_path / "state" / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text="read",
                        provider_snapshot={"provider_id": "deepseek", "model_id": "test"})
    durable.start_task("r", state={"task_kind": "project"})
    durable.advance_task(task_id="r", phase="running", state={"task_kind": "project"})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", project=str(tmp_path),
                          provider_id="deepseek", turn_budget=3, max_repair_rounds=0,
                          task_kind="project")
    line.mark_writer_running("s", "r", provider_id="deepseek")
    replies = iter([
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"done","args":{"summary":"read file"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return next(replies)

    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    outcome = run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="read", max_turns=3,
        fresh_chat=False, session_id="s", run_id="r", provider_id="deepseek",
        runtime_mutations=line, durable=durable, on_event=lambda _event: None,
    ))
    assert outcome.stop_reason == "done"
    effects = RuntimeEffectStore(durable=durable).load_effects("s", "r")
    assert any(row.intent.effect_category == "tool_call" for row in effects)
    durable.close()


def test_writer_recovery_delivers_previous_tool_result_without_rerunning(tmp_path: Path) -> None:
    from dataclasses import replace

    from codey.durable.model import CommitResult, DurableMutation
    from codey.operations.task_session import TaskSession
    from codey.runtime.write.checkpoint import operation_from_checkpoint
    from tests.support.kernel_fixture import run_task_kernel

    class SimulatedExit(BaseException):
        pass

    executions: list[ToolCall] = []
    prompts: list[str] = []

    class Provider:
        def send(self, prompt: str, timeout: float | None = None) -> str:
            prompts.append(prompt)
            return ('{"tool":"read_file","args":{"path":"a.py"}}' if len(prompts) == 1 else
                    '{"tool":"done","args":{"summary":"finished"}}')

    def execute(call: ToolCall) -> ToolResult:
        executions.append(call)
        return ToolResult(call, "ORIGINAL_RESULT", ok=True)

    policy = TaskPolicy(frozenset({"control", "project.read"}))
    with runtime(run_id="r", session_id="s") as sink:
        armed = True

        def committed(_result: CommitResult, _mutation: DurableMutation) -> None:
            nonlocal armed
            state = operation_from_checkpoint(sink.durable.task("r"), "s")
            if armed and state is not None and state.leaf == "tool_delivery_pending":
                armed = False
                raise SimulatedExit()

        sink.durable.subscribe_commits(committed)
        request = KernelRunRequest(transport=KernelTransportDeps(adapt_provider(Provider()), run_id="r"),
            execution=KernelExecutionDeps(intent_sink=sink, executors={"read_file": execute}))
        with pytest.raises(SimulatedExit):
            run_task_kernel(TaskSession(policy, max_turns=4), request=request)
        resumed = KernelEffectSink(sink.mutations, session_id="s", run_id="r", provider_id=sink.provider_id,
            durable=sink.durable, managed_outputs=sink.managed_outputs)
        result = run_task_kernel(TaskSession(policy, max_turns=4),
            request=replace(request, execution=replace(request.execution, intent_sink=resumed)))
        assert result.completed, result
        assert len(executions) == 1 and "ORIGINAL_RESULT" in prompts[-1]




def test_unified_edit_keeps_existing_file_guard_and_read_before_edit(tmp_path: Path) -> None:
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolCall
    from codey.task.model import TaskSubmission

    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "edit", 4, False, "deepseek"),
                               task_kind="project")
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path))
    overwrite = execute_turn(session, [ToolCall("edit", {"path": "app.py", "content": "new\n"})],
                             project_path=tmp_path, run_id="r", turn=1)[0]
    assert overwrite.model_text.startswith("ERROR:")
    assert target.read_text(encoding="utf-8") == "old\n"
    replace = ToolCall("edit", {"path": "app.py", "replacements": [
        {"old_string": "old", "new_string": "new"},
    ]})
    unseen = execute_turn(session, [replace], project_path=tmp_path, run_id="r", turn=2)[0]
    assert unseen.model_text.startswith("ERROR:")
    assert target.read_text(encoding="utf-8") == "old\n"
    execute_turn(session, [ToolCall("read_file", {"path": "app.py"})],
                 project_path=tmp_path, run_id="r", turn=3)
    from codey.workspace.revision import WorkspaceRevisionStore

    seen = execute_turn(session, [replace], project_path=tmp_path, run_id="r", turn=4,
                        workspace_revision_store=WorkspaceRevisionStore(tmp_path / ".codey"))[0]
    assert not seen.model_text.startswith("ERROR:")
    assert target.read_text(encoding="utf-8") == "new\n"


def test_unified_writer_notifies_change_tracker_around_edit(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from tests.support.kernel_fixture import run_agent as run

    class Tracker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def capture_before(self, rel: str) -> None:
            self.calls.append(("before", rel))

        def capture_after(self, rel: str) -> None:
            self.calls.append(("after", rel))

    replies = iter([
        '{"tool":"edit","args":{"path":"app.py","content":"print(1)"}}',
        '{"tool":"done","args":{"summary":"created"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return next(replies)

    tracker = Tracker()
    run(AgentRequest(provider=adapt_provider(Provider()), project=tmp_path, task="create app.py",
                     max_turns=2, fresh_chat=False, change_tracker=tracker,
                     on_event=lambda _event: None))
    assert tracker.calls == [("before", "app.py"), ("after", "app.py")]


def test_native_writer_uses_same_durable_delivery_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.agents.handoff import ConversationContext
    from codey.agents.request import AgentRequest
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from tests.support.kernel_fixture import run_agent as run

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state" / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text="read app.py",
                        provider_snapshot={"provider_id": "local", "model_id": "test"})
    durable.start_task("r", state={"task_kind": "project"})
    durable.advance_task(task_id="r", phase="running", state={"task_kind": "project"})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", project=str(tmp_path),
                          provider_id="local", turn_budget=3, max_repair_rounds=0,
                          task_kind="project")
    line.mark_writer_running("s", "r", provider_id="local")
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    sent: list[ProviderToolResult] = []
    conversation = ConversationContext()

    class Provider:
        name = "local"

        def send_turn(self, _prompt: str, _tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            return AssistantTurn(text="", tool_calls=(
                ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),
            ))

        def send_tool_results(self, messages: list[ProviderToolResult], _tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            sent.extend(messages)
            return AssistantTurn(text='{"tool":"done","args":{"summary":"read app.py"}}')

        def acknowledge_tool_results(self, results: list[ProviderToolResult], declared_tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            return self.send_tool_results(results, [])

    result = run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="read app.py", max_turns=3,
        fresh_chat=False, session_id="s", run_id="r", provider_id="local",
        runtime_mutations=line, durable=durable, on_event=lambda _event: None,
        conversation=conversation,
    ))
    assert result.stop_reason == "done"
    assert sent[0].call_id == "c1"
    assert conversation.estimated_context_tokens is not None and conversation.estimated_context_tokens > 0
    assert ToolResultDeliveryStore(durable=durable).load_batches("s", "r")[0].is_delivered
    durable.close()


def test_native_final_turn_answers_tool_call_before_budget_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import run_task_kernel

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 1, False, "local"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    messages: list[ProviderToolResult] = []

    class Provider:
        name = "local"

        def send_turn(self, _prompt: str, _tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            return AssistantTurn(tool_calls=(
                ProviderToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"}),
            ))

        def send_tool_results(self, results: list[ProviderToolResult], _tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            messages.extend(results)
            return AssistantTurn(text="acknowledged")

        def acknowledge_tool_results(self, results: list[ProviderToolResult], declared_tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            return self.send_tool_results(results, [])

    outcome = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=adapt_provider(Provider()),
                provider_id="local",
            ),
            execution=KernelExecutionDeps(
                project_path=tmp_path,
            ),
        ),
    )
    assert outcome.stop_reason == "max_turns"
    assert messages and messages[0].call_id == "call-1"


def test_native_budget_stop_rejects_following_dangling_tool_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import run_task_kernel

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 1, False, "local"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    messages: list[list[ProviderToolResult]] = []

    class Provider:
        name = "local"

        def send_turn(self, _prompt: str, _tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            return AssistantTurn(tool_calls=(
                ProviderToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"}),
            ))

        def send_tool_results(self, results: list[ProviderToolResult], _tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            messages.append(results)
            if len(messages) == 1:
                return AssistantTurn(tool_calls=(
                    ProviderToolCall(id="call-2", name="done", arguments={"summary": "x"}),
                ))
            return AssistantTurn(text="acknowledged")

        def acknowledge_tool_results(self, results: list[ProviderToolResult], declared_tools: list[ProviderToolDefinition], timeout: float | None=None) -> AssistantTurn:
            return self.send_tool_results(results, [])

    run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=adapt_provider(Provider()),
                provider_id="local",
            ),
            execution=KernelExecutionDeps(
                project_path=tmp_path,
            ),
        ),
    )
    assert len(messages) == 2
    assert messages[1][0].call_id == "call-2"
    assert "max turns" in messages[1][0].content


def test_unified_writer_keeps_managed_output_receipt_in_event(tmp_path: Path) -> None:
    from codey.agents.request import AgentRequest
    from codey.agents.tools import AgentToolFns
    from codey.toolchain.runtime import ToolOutcome
    from tests.support.kernel_fixture import run_agent as run

    replies = iter([
        '{"tool":"run","args":{"path":".","command":"python -m pytest","run_in_background":false}}',
        '{"tool":"done","args":{"summary":"checked"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return next(replies)

    outputs = ManagedOutputStore(tmp_path / "outputs")
    managed = outputs.write_tool_output(session_id="s", run_id="r", tool_id="verification",
        permission_profile="coding_writer", tool_name="run", display_ref=".", text="test output")
    assert managed is not None

    def command(*_args: object) -> ToolOutcome:
        return ToolOutcome(
            "test output", True, exit_code=0, truncated=True,
            audit={"managed_output": {
                "handle": managed.handle, "original_bytes": managed.original_bytes,
                "stored_bytes": managed.stored_bytes, "sha256": managed.sha256,
            }},
        )

    events: list[RunEvent] = []
    run(AgentRequest(
        provider=adapt_provider(Provider()), project=tmp_path, task="run tests", max_turns=2,
        fresh_chat=False, on_event=events.append, managed_outputs=outputs, session_id="s", run_id="r",
        tool_fns=AgentToolFns(run_command_with_context=command),
    ))
    finished = next(event for event in events if event.kind == "tool")
    assert finished.outcome is not None
    assert finished.outcome.managed_output()["handle"] == managed.handle


@pytest.mark.usefixtures("public_source_dns")
def test_unified_research_externalizes_large_opened_source(tmp_path: Path) -> None:
    from codey.knowledge.changes import KnowledgeChanges
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.research.tools import ResearchTools
    from codey.runtime.core.models import ToolCall
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.task.model import TaskSubmission

    url = "https://example.com/large"

    class Search:
        def search(self, query: str, *, limit: int=8) -> list[JsonObject]:
            raise AssertionError("this test only fetches")

        def fetch(self, url: str) -> dict[str, object]:
            return {"status": "ok", "url": url, "title": "Large source", "text": "A" * 30_000,
                    "truncated": False}

    knowledge = KnowledgeStore(tmp_path / "knowledge")
    research_tools = ResearchTools(
        search=Search(), store=knowledge, changes=KnowledgeChanges(root=knowledge.root),
        session_id="s", project="",
    )
    policy = build_task_policy(TaskSubmission("s", None, "research", 2, False, "deepseek"),
                               task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research", task_text="research")
    outputs = ManagedOutputStore(tmp_path / "state")
    try:
        result = execute_turn(session, [ToolCall("open_url", {"url": url, "limit": 12000})],
                              research_tools=research_tools, managed_outputs=outputs,
                              session_id="s", run_id="r", turn=1)[0]
        assert result.ok, result.model_text
        assert str(decode_json_object(result.audit["managed_output"])["handle"]).startswith("out_")
        assert len(result.model_text) < 30_000
        assert result.canonical["read_passages"]
    finally:
        knowledge.close()


def test_research_controller_keeps_authorized_project_read_available(tmp_path: Path) -> None:
    from codey.operations.kernel.kernel_protocol import normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.operations.turn_snapshot import controller_allowed_for_session
    from codey.policies.task_policy import build_task_policy
    from codey.protocols.text_turn import decode_text_turn
    from codey.task.model import TaskSubmission

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "research project", 3,
                                              False, "deepseek", intent="research"),
                               task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research", project=str(tmp_path))
    plan = normalize_turn(decode_text_turn('{"tool":"read_file","args":{"path":"README.md"}}'),
                          policy=policy, controller_allowed=controller_allowed_for_session(session))
    assert not plan.protocol_error
    assert plan.calls[0].name == "read_file"


def test_research_project_verification_runs_without_write_grant(tmp_path: Path) -> None:
    from codey.agents.tools import AgentToolFns
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolCall
    from codey.task.model import TaskSubmission
    from codey.toolchain.runtime import ToolOutcome

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "research and verify", 2,
                                              False, "deepseek", intent="research"),
                               task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research", project=str(tmp_path))
    calls: list[str] = []

    def verify(_root: Path, _path: str, command: str, *_rest: object) -> ToolOutcome:
        calls.append(command)
        return ToolOutcome("1 passed", True, exit_code=0)

    result = execute_turn(
        session, [ToolCall("run", {"path": ".", "command": "python -m pytest"})],
        project_path=tmp_path, permission_profile="research",
        tool_fns=AgentToolFns(run_command_with_context=verify), run_id="r", turn=1,
    )[0]
    assert not result.model_text.startswith("ERROR:")
    assert calls == ["python -m pytest"]
    assert not policy.allows("project.write")


def test_default_research_iteration_uses_shared_turn_kernel(tmp_path: Path) -> None:
    from codey.operations.research_flow import ResearchFlowDeps
    from codey.operations.research_iteration import run_research_iteration
    from codey.operations.task_loop import KernelResult
    from codey.policies.task_policy import build_task_policy
    from codey.research.tools import ResearchTools
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import admit

    tools = ResearchTools(search=None, store=None, changes=None)
    state = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    admit(state.durable, run_id="r", session_id="s", task="research question", provider_id="deepseek", max_turns=1, task_kind="research")
    deps = ResearchFlowDeps(state, None, lambda: None, managed_outputs=state.managed_outputs, runtime_mutations=state.runtime_mutations)
    provider = adapt_provider(EmptyProvider())
    with patch("codey.operations.task_loop.run_task_kernel", return_value=KernelResult(
        False, "unfinished", 1, "max_turns",
    )) as kernel:
        iteration = run_research_iteration(
            deps, provider=provider, session_id="s", project="", task="research question",
            max_turns=1, on_event=lambda _event: None, stop_flag=None,
            provider_id="deepseek", run_id="r", chat_handoff="",
            trace_recorder=None, task_policy=build_task_policy(
            TaskSubmission("s", None, "research", 3, False, "deepseek", denied_capabilities=BROWSER_CAPABILITIES),
            task_kind="research", strict_research=True), search=object(), tools=tools,
        )
    kernel.assert_called_once()
    assert iteration.result.question == "research question"
    assert iteration.tools is tools
    state.close()


def test_web_only_research_iteration_finishes_with_opened_evidence(tmp_path: Path) -> None:
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_flow import ResearchFlowDeps
    from codey.operations.research_iteration import run_research_iteration
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission
    from tests.support.kernel_fixture import admit

    url = "https://example.com/pipeline"
    markdown = "Pipeline source text says the fact. [e1]"
    replies = iter(json.dumps(item, ensure_ascii=False) for item in (
        {"tool": "web_search", "args": {"query": "pipeline question"}},
        {"tool": "open_result", "args": {"result_id": "r1"}},
        {"tool": "submit_research_answer", "args": {"markdown": markdown, "state": "answered"}},
    ))

    class Search:
        def search(self, query: str, *, limit: int=8) -> list[JsonObject]:
            return [{"title": "Pipeline source", "url": url, "snippet": "Pipeline source text."}]

        def fetch(self, url: str) -> dict[str, object]:
            return {"status": "ok", "url": url, "title": "Pipeline source",
                    "text": "Pipeline source text says the fact.", "truncated": False}

    class Provider:
        name = "deepseek"

        def new_chat(self, timeout: float | None=None) -> None:
            return None

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return next(replies)

    store = KnowledgeStore(tmp_path / "knowledge")
    state = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    admit(state.durable, run_id="run-1", session_id="s", task="pipeline question", provider_id="deepseek", max_turns=3, task_kind="research")
    iteration = run_research_iteration(
        ResearchFlowDeps(state, store, Search, runtime_mutations=state.runtime_mutations, managed_outputs=state.managed_outputs),
        provider=adapt_provider(Provider()), session_id="s",
        project="", task="pipeline question", max_turns=3,
        on_event=lambda _event: None, stop_flag=None, provider_id="deepseek",
        run_id="run-1", chat_handoff="", trace_recorder=None, task_policy=build_task_policy(
            TaskSubmission("s", None, "research", 3, False, "deepseek", denied_capabilities=BROWSER_CAPABILITIES),
            task_kind="research", strict_research=True), search=Search(),
    )
    assert iteration.result.stop_reason == "done", iteration.result.summary
    assert iteration.result.research_record is not None
    assert iteration.tools is not None and iteration.tools.ledger.read_passages
    report = iteration.result.accepted_report
    assert report is not None and report.markdown == markdown
    assert report.evidence_bindings
    for row in report.evidence_bindings:
        evidence = store.repository.read(row["evidence_id"], row["evidence_revision"])
        assert evidence is not None
        assert evidence.value.content == "Pipeline source text says the fact."
    store.close()
    state.close()


def test_default_research_iteration_records_effects_in_same_runtime(tmp_path: Path) -> None:
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_flow import ResearchFlowDeps
    from codey.operations.research_iteration import run_research_iteration
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.task.model import TaskSubmission

    state = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    durable = state.durable
    durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text="q",
                        provider_snapshot={"provider_id": "deepseek", "model_id": "test"})
    durable.start_task("r", state={"task_kind": "research"})
    durable.advance_task(task_id="r", phase="running", state={"task_kind": "research"})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", project="", provider_id="deepseek",
                          turn_budget=2, max_repair_rounds=0, task_kind="research")

    class Search:
        def search(self, query: str, *, limit: int=8) -> list[JsonObject]:
            return [{"title": "Result", "url": "https://example.com", "snippet": query}]

        def fetch(self, url: str) -> dict[str, object]:
            raise AssertionError("this test only searches")

    replies = iter([
        '{"tool":"web_search","args":{"query":"q"}}',
        '{"tool":"web_search","args":{"query":"q details"}}',
    ])

    class Provider:
        name = "deepseek"

        def new_chat(self) -> None:
            return None

        def send(self, _prompt: str, timeout: float | None=None) -> str:
            return next(replies)

    store = KnowledgeStore(tmp_path / "knowledge")
    iteration = run_research_iteration(
        ResearchFlowDeps(state, store, Search, runtime_mutations=line, managed_outputs=state.managed_outputs),
        provider=adapt_provider(Provider()), session_id="s", project="", task="q", max_turns=2,
        on_event=lambda _event: None, stop_flag=None, provider_id="deepseek",
        run_id="r", chat_handoff="", trace_recorder=None, task_policy=build_task_policy(
            TaskSubmission("s", None, "research", 3, False, "deepseek", denied_capabilities=BROWSER_CAPABILITIES),
            task_kind="research", strict_research=True), search=Search(),
    )
    effects = RuntimeEffectStore(durable=durable).load_effects("s", "r")
    calls = [row for row in effects if row.intent.effect_category == "tool_call"]
    assert len(calls) == 2, iteration.result.summary
    assert all(row.intent.tool_name == "web_search" for row in calls)
    assert any(row.intent.effect_category == "provider_send" for row in effects)
    assert iteration.result.synthesis_id == ""
    assert not iteration.result.notes_created
    store.close()
    state.close()
