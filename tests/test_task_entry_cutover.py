"""Production cutover locks for the shared Coding and Research loop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _cutover_parent_policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({
        "control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link",
    }))


@pytest.mark.parametrize("project, expected", [("E:/codey", "project"), (None, "chat")])
def test_opt_in_task_kind_reaches_shared_kernel(project: str | None, expected: str) -> None:
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    from codey.task.kind import resolve_task_kind
    from codey.task.model import TaskSubmission

    request = TaskSubmission("s", project, "task", 2, False, "local", intent="project")
    kind = resolve_task_kind(request)
    assert kind == expected
    # Hybrid still hits the single session without legacy two-phase.
    hybrid_outcome = ModeOutcome({"type": "task_done", "mode": "hybrid"})
    with patch("codey.operations.task_phases.dispatch.run_task_mode", return_value=hybrid_outcome) as single:
        actual = dispatch_run_mode(
            SimpleNamespace(state=SimpleNamespace(), knowledge_store=None, evidence_ledgers=None,
                            search_factory=None, run_research_advisors=None, managed_outputs=None,
                            runtime_mutations=None),
            SimpleNamespace(), SimpleNamespace(),
            SimpleNamespace(claimed_work_item=None), SimpleNamespace(request=request, run_id="r", recovered_tool_outcomes=(), settled_tool_outcomes=()),
            SimpleNamespace(), "hybrid",
            SimpleNamespace(),
        )
    assert actual is hybrid_outcome
    assert single.call_count == 1
    assert single.call_args.kwargs["task_kind"] == "hybrid"


def test_auto_intent_reaches_auto_router_before_single_entry() -> None:
    """Auto intent must hit run_auto_mode before the single task entry.

    Regression lock: the single-entry return for project/research/planning
    must not precede the auto check, otherwise every auto-intent task skips
    the first-call router and runs the project writer directly.
    """
    from unittest.mock import MagicMock

    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    from codey.task.model import TaskSubmission

    request = TaskSubmission("s", "E:/codey", "read only", 2, False, "local", intent="auto")
    frame = SimpleNamespace(
        request=request, run_id="r", recovered_tool_outcomes=(), settled_tool_outcomes=(),
    )
    work = SimpleNamespace(claimed_work_item=None)
    outcome = ModeOutcome({"type": "task_done", "mode": "chat"})
    with (
        patch("codey.operations.task_phases.dispatch.run_auto_mode", return_value=outcome) as auto,
        patch("codey.operations.task_phases.dispatch.run_task_mode") as single,
    ):
        actual = dispatch_run_mode(
            MagicMock(), MagicMock(), MagicMock(), work, frame,
            SimpleNamespace(), "project", MagicMock(),
        )
    assert actual is outcome
    assert auto.call_count == 1
    assert single.call_count == 0


@pytest.mark.parametrize("kind", ["hybrid"])
def test_default_kinds_all_use_shared_kernel(kind: str) -> None:
    """Default hybrid uses one session; no opt-in needed.

    Project/research/planning already drive the same kernel internally
    (writer adapter / research iteration) plus their review/repair phases,
    so only hybrid needs a dispatch cutover from two-phase to one session.
    """
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode

    outcome = ModeOutcome({"type": "task_done", "mode": kind})
    with patch("codey.operations.task_phases.dispatch.run_task_mode", return_value=outcome) as unified:
        actual = dispatch_run_mode(
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
            SimpleNamespace(recovered_tool_outcomes=(), settled_tool_outcomes=()), SimpleNamespace(), kind,
            SimpleNamespace(),
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


def test_task_entry_research_builds_default_search_provider(tmp_path) -> None:
    from codey.operations.task_execution import build_research_tools

    deps = SimpleNamespace(
        knowledge_store=SimpleNamespace(root=tmp_path),
        search_factory=None,
    )
    search = object()
    with patch("codey.research.search_factory.default_research_search_provider", return_value=search):
        tools = build_research_tools(deps, session_id="s", project="")
    assert tools is not None
    assert tools.search is search


def test_task_entry_uses_same_runtime_receipts_and_events(tmp_path) -> None:
    from codey.operations.task_entry import run_entry_kernel
    from codey.operations.task_loop import KernelResult
    from codey.task.model import TaskSubmission

    request = TaskSubmission("s", str(tmp_path), "inspect", 1, False, "deepseek",
                             intent="project", run_id="r")
    provider = SimpleNamespace(send=lambda *_args, **_kwargs: "")
    frame = SimpleNamespace(
        request=request, task_kind="project", run_id="r", provider=provider,
        provider_id="deepseek", project_text=str(tmp_path), handoff="",
        recovered_tool_outcomes=(),
        settled_tool_outcomes=(), recovered_tool_result_batch_id="",
    )
    events: list[object] = []
    hooks = SimpleNamespace(on_event=events.append, on_shell_request=events.append)
    managed = object()
    mutations = SimpleNamespace(mark_writer_running=lambda *_args, **_kwargs: None)
    deps = SimpleNamespace(
        knowledge_store=None, search_factory=None, runtime_mutations=mutations,
        managed_outputs=managed, state=None,
    )
    with (patch("codey.operations.task_loop.run_task_kernel",
                return_value=KernelResult(True, "done", 1, "done")) as kernel,
          patch("codey.operations.task_effects.KernelEffectSink") as sink_type,
          patch("codey.operations.task_effects.KernelRecordedProvider") as recorded):
        result = run_entry_kernel(frame, SimpleNamespace(evidence=None, analysis_run_payloads=[]),
                                  hooks, deps)
    assert result.event["stop_reason"] == "done"
    sink_type.assert_called_once()
    recorded.assert_called_once()
    assert kernel.call_args.kwargs["intent_sink"] is sink_type.return_value
    assert kernel.call_args.kwargs["on_event"] is hooks.on_event
    assert kernel.call_args.kwargs["on_shell_request"] is hooks.on_shell_request
    assert kernel.call_args.kwargs["managed_outputs"] is managed


def test_task_entry_creates_new_authorized_project(tmp_path) -> None:
    from codey.operations.task_entry import run_entry_kernel
    from codey.operations.task_loop import KernelResult
    from codey.task.model import TaskSubmission

    project = tmp_path / "new-project"
    request = TaskSubmission("s", str(project), "create app", 1, False, "local",
                             intent="project", run_id="r")
    frame = SimpleNamespace(
        request=request, task_kind="project", run_id="r",
        provider=SimpleNamespace(send=lambda *_args, **_kwargs: ""),
        provider_id="local", project_text=str(project), handoff="",
        recovered_tool_outcomes=(),
        settled_tool_outcomes=(), recovered_tool_result_batch_id="",
    )
    hooks = SimpleNamespace(on_event=lambda _event: None, on_shell_request=None)
    deps = SimpleNamespace(knowledge_store=None, runtime_mutations=None, state=None)
    with patch("codey.operations.task_loop.run_task_kernel",
               return_value=KernelResult(True, "done", 1, "done")) as kernel:
        run_entry_kernel(frame, SimpleNamespace(evidence=None, analysis_run_payloads=[]),
                         hooks, deps)
    assert project.is_dir()
    assert kernel.call_args.kwargs["project_path"] == project.resolve()


def test_unified_kernel_passes_authoritative_research_ledger_to_gate() -> None:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "web.read"}), strict_research=True),
        task_kind="research", max_turns=1,
    )
    ledger = object()
    class Provider:
        def send(self, _prompt, timeout=None):
            return '{"tool":"done","args":{"summary":"结论 x 来源 y"}}'

    with patch("codey.operations.completion_gate.evaluate") as evaluate:
        evaluate.return_value = SimpleNamespace(complete=False, followup="missing")
        run_task_kernel(session, provider=Provider(), completion_context={"research_ledger": ledger})
    assert evaluate.call_args.kwargs["context"]["research_ledger"] is ledger


def test_explicit_research_prompt_explains_evidence_and_report_contract() -> None:
    from codey.operations.kernel_prompt import kernel_prompt_for_session
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"}),
                          strict_research=True),
        task_kind="research", task_text="question",
    )
    prompt = kernel_prompt_for_session(session)
    for required in ("web_search", "open_result", "knowledge_write", "结论", "关键证据",
                     "反证与限制", "来源质量", "搜索覆盖", "来源"):
        assert required in prompt


def test_web_research_shows_result_and_source_ids_to_model(tmp_path) -> None:
    from codey.knowledge.changes import KnowledgeChanges
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.research.tools import ResearchTools
    from codey.task.model import TaskSubmission

    url = "https://example.com/evidence"

    class Search:
        def search(self, *_args, **_kwargs):
            return [{"title": "Evidence", "url": url, "snippet": "claim"}]

        def fetch(self, target):
            return {"url": target, "title": "Evidence", "text": "claim text"}

    prompts: list[str] = []
    replies = iter([
        '{"tool":"web_search","args":{"query":"claim"}}',
        '{"tool":"open_result","args":{"result_id":"r1"}}',
        '{"tool":"done","args":{"summary":"unfinished"}}',
    ])

    class Provider:
        def send(self, prompt, timeout=None):
            prompts.append(prompt)
            return next(replies)

    store = KnowledgeStore(tmp_path / "knowledge")
    tools = ResearchTools(search=Search(), store=store, changes=KnowledgeChanges(root=store.root))
    policy = build_task_policy(TaskSubmission("s", None, "claim", 3, False, "deepseek"),
                               task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research", max_turns=3)
    run_task_kernel(session, provider=Provider(), research_tools=tools)
    assert "r1" in prompts[1] and url in prompts[1]
    assert "s1" in prompts[2] and url in prompts[2]


def test_search_result_ids_remain_stable_across_searches() -> None:
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})),
                          task_kind="project")
    urls = iter(("https://example.com/a", "https://example.com/b"))
    def execute(_call):
        return f"1. Result\n   {next(urls)}"
    execute_turn(session, [ToolCall("web_search", {"query": "a"})],
                 executors={"web_search": execute}, run_id="r", turn=1)
    execute_turn(session, [ToolCall("web_search", {"query": "b"})],
                 executors={"web_search": execute}, run_id="r", turn=2)
    assert session.search_results == {
        "r1": "https://example.com/a", "r2": "https://example.com/b",
    }


def test_open_hit_uses_source_locator_in_shared_research_kernel() -> None:
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall

    url = "https://example.com/long"
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})),
                          task_kind="research")
    session.record_open(url)
    opened: list[tuple[str, int]] = []

    class Tools:
        def source_search(self, target, _query, _limit):
            assert target == url
            return "source_search results\n1. offset 123: relevant excerpt"

        def open_url(self, target, offset=0, limit=6000, pages=""):
            opened.append((target, offset))
            return SimpleNamespace(model_text="Title: Long\nrelevant excerpt", receipt_text="")

    tools = Tools()
    search = execute_turn(session, [ToolCall("source_search", {"url": url, "query": "relevant"})],
                          research_tools=tools, run_id="r", turn=1)[0]
    assert "h1" in search.model_text
    opened_result = execute_turn(session, [ToolCall("open_hit", {"hit_id": "h1"})],
                                 research_tools=tools, run_id="r", turn=2)[0]
    assert not opened_result.model_text.startswith("ERROR:")
    assert opened == [(url, 123)]


def test_default_project_writer_uses_shared_turn_kernel(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.app import task_submit

    (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
    replies = iter([
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"done","args":{"summary":"read the file"}}',
    ])

    class WebProvider:
        name = "deepseek"

        def new_chat(self, timeout=None):
            return None

        def send(self, prompt, timeout=None):
            return next(replies)

        def close(self):
            return None

    with patch("codey.operations.task_loop.run_task_kernel", wraps=None) as kernel:
        from codey.operations.task_loop import KernelResult

        kernel.return_value = KernelResult(True, "read the file", 2, "done")
        result = task_submit.agent_run(AgentRequest(
            provider=WebProvider(), project=tmp_path, task="read file.txt", max_turns=3,
        ))
    assert result.stop_reason == "done"
    kernel.assert_called_once()


def test_project_writer_can_search_and_read_in_one_model_loop(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    (tmp_path / "file.txt").write_text("local content", encoding="utf-8")
    replies = iter([
        '{"tool":"web_search","args":{"query":"official docs"}}',
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"done","args":{"summary":"compared docs with local content"}}',
    ])
    seen: list[str] = []
    events: list[object] = []

    class WebProvider:
        name = "deepseek"

        def new_chat(self, timeout=None):
            return None

        def send(self, prompt, timeout=None):
            seen.append(str(prompt))
            return next(replies)

    class ResearchTools:
        def web_search(self, query):
            assert query == "official docs"
            return "1. Docs\n   https://example.com/docs"

    result = run(AgentRequest(
        provider=WebProvider(), project=tmp_path, task="查官方文档并读 file.txt",
        max_turns=4, requested_capabilities=("web.read",),
        research_tools=ResearchTools(),
        on_event=events.append,
    ))
    assert result.stop_reason == "done"
    assert result.turns == 3
    assert len(seen) == 3
    assert any(getattr(event, "kind", "") == "tool" for event in events)


def test_project_writer_keeps_bounded_project_context_in_first_prompt(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    (tmp_path / "AGENTS.md").write_text("Follow the local migration rule.", encoding="utf-8")
    prompts: list[str] = []

    class Provider:
        name = "deepseek"

        def new_chat(self, timeout=None):
            return None

        def send(self, prompt, timeout=None):
            prompts.append(prompt)
            return '{"tool":"done","args":{"summary":"context received"}}'

    run(AgentRequest(
        provider=Provider(), project=tmp_path, task="inspect the project",
        max_turns=1, on_event=lambda _event: None,
        project_facts="verified fact 42", research_context="research brief 42",
        project_map="project map 42", work_checkpoint="checkpoint 42",
    ))
    assert len(prompts) == 1
    for expected in ("Follow the local migration rule.", "verified fact 42",
                     "research brief 42", "project map 42", "checkpoint 42"):
        assert expected in prompts[0]


def test_project_writer_prompts_with_selected_verification_candidates(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.completion.verification_policy import VerificationCandidate
    from codey.operations.project_adapter import run

    prompts: list[str] = []

    class Provider:
        name = "deepseek"

        def send(self, prompt, timeout=None):
            prompts.append(prompt)
            return '{"tool":"done","args":{"summary":"inspected"}}'

    run(AgentRequest(
        provider=Provider(), project=tmp_path, task="inspect", max_turns=1,
        fresh_chat=False, on_event=lambda _event: None,
        verification_candidates=(VerificationCandidate("python -m pytest -q", "."),),
    ))
    assert "python -m pytest -q" in prompts[0]


def test_planning_adapter_exposes_only_read_capabilities(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    prompts: list[str] = []

    class Provider:
        name = "deepseek"

        def send(self, prompt, timeout=None):
            prompts.append(prompt)
            return '{"tool":"done","args":{"summary":"plan"}}'

    run(AgentRequest(
        provider=Provider(), project=tmp_path, task="plan the changes",
        permission_profile="planning_readonly", fresh_chat=False,
        max_turns=1, on_event=lambda _event: None,
    ))
    assert "read_file" in prompts[0]
    assert '"tool":"edit"' not in prompts[0]
    assert '"tool":"shell"' not in prompts[0]


def test_web_chat_refresh_failure_stops_before_sending_to_existing_tab(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    class Provider:
        name = "deepseek"

        def new_chat(self):
            raise RuntimeError("browser tab unavailable")

        def send(self, _prompt, timeout=None):
            raise AssertionError("failed fresh chat must not reuse the current tab")

    import pytest

    with pytest.raises(RuntimeError, match="browser tab unavailable"):
        run(AgentRequest(
            provider=Provider(), project=tmp_path, task="continue", max_turns=1,
            fresh_chat=True, on_event=lambda _event: None,
        ))


def test_shared_writer_updates_conversation_window_usage(tmp_path) -> None:
    from codey.agents.handoff import ConversationContext
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    class Provider:
        name = "deepseek"

        def new_chat(self):
            return None

        def send(self, _prompt, timeout=None):
            return '{"tool":"done","args":{"summary":"finished"}}'

    conversation = ConversationContext()
    run(AgentRequest(
        provider=Provider(), project=tmp_path, task="finish", max_turns=1,
        fresh_chat=True, conversation=conversation, on_event=lambda _event: None,
    ))
    assert conversation.initialized
    assert conversation.used_tokens > 0
    assert conversation.project == str(tmp_path)


def test_writer_stops_after_configured_invalid_turn_limit(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    calls: list[str] = []

    class Provider:
        name = "deepseek"

        def send(self, prompt, timeout=None):
            calls.append(prompt)
            return "not a JSON tool call"

    result = run(AgentRequest(
        provider=Provider(), project=tmp_path, task="inspect", max_turns=20,
        stagnant_turns=2, fresh_chat=False, on_event=lambda _event: None,
    ))
    assert result.turns == 2
    assert result.stop_reason == "protocol"
    assert len(calls) == 2


def test_writer_can_create_a_user_selected_new_project(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    project = tmp_path / "new-project"

    class Provider:
        name = "deepseek"

        def send(self, _prompt, timeout=None):
            return '{"tool":"edit","args":{"path":"hello.txt","content":"hello"}}'

    run(AgentRequest(
        provider=Provider(), project=project, task="create hello.txt",
        max_turns=1, fresh_chat=False, on_event=lambda _event: None,
    ))
    assert (project / "hello.txt").read_text(encoding="utf-8") == "hello"


def test_failed_verification_after_pass_blocks_done_at_same_revision(tmp_path) -> None:
    from codey.agents.tools import AgentToolFns
    from codey.operations.completion_gate import evaluate
    from codey.operations.kernel_execution import execute_turn
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
    fns = AgentToolFns(run_command_with_context=lambda *_args: next(outcomes))
    for turn in (1, 2):
        execute_turn(session, [ToolCall("run", {"path": ".", "command": "python -m pytest"})],
                     project_path=tmp_path, tool_fns=fns, run_id="r", turn=turn)
    verdict = evaluate(session, "tests done")
    assert not verdict.complete
    assert any(not row["passed"] for row in session.verifications)


def test_noop_edit_does_not_create_false_freshness(tmp_path) -> None:
    from codey.operations.kernel_execution import execute_turn
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


def test_default_research_iteration_uses_shared_turn_kernel() -> None:
    from codey.operations.research_iteration import run_research_iteration
    from codey.operations.task_loop import KernelResult
    from codey.research.ledger import ResearchLedger

    tools = SimpleNamespace(
        ledger=ResearchLedger(), created_ids=[], updated_ids=[],
        links_created=0, sources_read=set(), search_result_urls=set(),
    )
    deps = SimpleNamespace(knowledge_store=object(), managed_outputs=None, run_research_advisors=None)
    provider = SimpleNamespace(new_chat=lambda: None)
    with patch("codey.operations.task_loop.run_task_kernel", return_value=KernelResult(
        False, "unfinished", 1, "max_turns",
    )) as kernel:
        iteration = run_research_iteration(
            deps, provider=provider, session_id="s", project="", task="research question",
            max_turns=1, on_event=lambda _event: None, stop_flag=None,
            provider_id="deepseek", run_id="r", chat_handoff="",
            trace_recorder=None, search=object(), tools=tools,
        )
    kernel.assert_called_once()
    assert iteration.result.question == "research question"
    assert iteration.tools is tools


def test_research_done_builds_proof_record_from_current_ledger() -> None:
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.ledger import ResearchLedger

    ledger = ResearchLedger()
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "web.read"}), strict_research=True),
        task_kind="research",
    )
    with patch("codey.research.object_model.build_research_record", return_value=object()) as build:
        evaluate(session, "结论 x 来源 y", context={"research_ledger": ledger, "question": "x"})
    build.assert_called_once()


def test_web_only_research_iteration_finishes_with_opened_evidence(tmp_path) -> None:
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_iteration import run_research_iteration

    url = "https://example.com/pipeline"
    answer = (
        "## 结论\n- Pipeline answer depends on the opened source. [1]\n\n"
        "## 关键证据\n- [1] Pipeline source text says the fact.\n\n"
        "## 反证与限制\n- 未找到强反证；需要持续追踪新数据。\n\n"
        "## 来源质量\n- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n- query: pipeline question\n- opened: Pipeline source\n"
        "- skipped: none representative\n\n"
        f"## 来源\n[1] Pipeline source - {url}"
    )
    replies = iter(json.dumps(item, ensure_ascii=False) for item in (
        {"tool": "web_search", "args": {"query": "pipeline question"}},
        {"tool": "open_result", "args": {"result_id": "r1"}},
        {"tool": "knowledge_write", "args": {
            "type": "fact", "title": "Pipeline answer depends on the opened source.",
            "body": "Pipeline source text says the fact.", "sources": [url],
            "evidence": [{"claim": "Pipeline answer depends on the opened source.",
                          "source_url": url, "excerpt": "Pipeline source text says the fact.",
                          "stance": "supports"}],
        }},
        {"tool": "done", "args": {"summary": answer}},
    ))

    class Search:
        def search(self, *_args, **_kwargs):
            return [{"title": "Pipeline source", "url": url, "snippet": "Pipeline source text."}]

        def fetch(self, target):
            return {"url": target, "title": "Pipeline source",
                    "text": "Pipeline source text says the fact.", "truncated": False}

    class Provider:
        name = "deepseek"

        def new_chat(self, timeout=None):
            return None

        def send(self, _prompt, timeout=None):
            return next(replies)

    store = KnowledgeStore(tmp_path / "knowledge")
    iteration = run_research_iteration(
        SimpleNamespace(knowledge_store=store), provider=Provider(), session_id="s",
        project="", task="pipeline question", max_turns=5,
        on_event=lambda _event: None, stop_flag=None, provider_id="deepseek",
        run_id="run-1", chat_handoff="", trace_recorder=None, search=Search(),
    )
    assert iteration.result.stop_reason == "done"
    assert iteration.result.research_record is not None
    assert iteration.tools.ledger.evidence_items


def test_project_shell_requests_approval_in_shared_kernel(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    approvals: list[object] = []

    class Provider:
        name = "deepseek"

        def new_chat(self, timeout=None):
            return None

        def send(self, _prompt, timeout=None):
            return '{"tool":"shell","args":{"path":".","command":"git status"}}'

    result = run(AgentRequest(
        provider=Provider(), project=tmp_path, task="run git status with approval",
        max_turns=2, on_shell_request=approvals.append,
    ))
    assert result.stop_reason == "approval"
    assert len(approvals) == 1
    assert approvals[0].command == "git status"


def test_shell_approval_records_turn_intent_before_notifying_user(tmp_path) -> None:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "shell", 1, False, "deepseek"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    order: list[str] = []

    class Sink:
        def begin_turn(self, items, *, turn, specs=None):
            assert turn == 1 and items[0][1].name == "shell"
            order.append("intent")

    class Provider:
        def send(self, _prompt, timeout=None):
            return '{"tool":"shell","args":{"path":".","command":"git status"}}'

    result = run_task_kernel(
        session, provider=Provider(), project_path=tmp_path, run_id="run",
        intent_sink=Sink(), on_shell_request=lambda _approval: order.append("approval"),
    )
    assert result.stop_reason == "approval"
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


def test_kernel_records_intent_before_project_tool_execution(tmp_path) -> None:
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 2, False, "local"),
                               task_kind="project")
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    observed: list[str] = []

    class Sink:
        def begin_turn(self, _items, *, turn, specs=None):
            observed.append(f"begin:{turn}")

        def has_unsettled(self, _identity):
            return False

        def settle(self, _identity, _ok, *, result=None, exit_code=None):
            observed.append("settle")

    class Provider:
        def send(self, _prompt, timeout=None):
            return '{"tool":"read_file","args":{"path":"file.txt"}}'

    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    run_task_kernel(session, provider=Provider(), project_path=tmp_path,
                    intent_sink=Sink(), on_event=lambda event: observed.append(event.kind))
    assert observed.index("begin:1") < observed.index("settle")


def test_failed_intent_commit_prevents_tool_execution(tmp_path) -> None:
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolCall
    from codey.task.model import TaskSubmission

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "write", 2, False, "local"),
                               task_kind="project")
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path))

    class Sink:
        def begin_turn(self, _items, *, turn, specs=None):
            raise RuntimeError("journal unavailable")

    with pytest.raises(RuntimeError, match="journal unavailable"):
        execute_turn(session, [ToolCall("edit", {"path": "file.txt", "content": "x"})],
                     project_path=tmp_path, intent_sink=Sink())
    assert not (tmp_path / "file.txt").exists()


def test_production_kernel_persists_tool_and_delivery_receipts(tmp_path) -> None:
    from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.task.model import TaskSubmission

    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(session_id="s", run_id="r", project=str(tmp_path),
                          provider_id="deepseek", turn_budget=3, max_repair_rounds=0,
                          task_kind="project")
    line.mark_writer_running("s", "r", provider_id="deepseek")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="deepseek")
    replies = iter([
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"done","args":{"summary":"read file"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, _prompt, timeout=None):
            return next(replies)

    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 3, False, "deepseek"),
                               task_kind="project")
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=3)
    outcome = run_task_kernel(session, provider=KernelRecordedProvider(Provider(), sink),
                              project_path=tmp_path, run_id="r", intent_sink=sink)
    assert outcome.stop_reason == "done"
    effects = RuntimeEffectStore(log).load_effects("s", "r")
    assert any(row.intent.effect_category == "tool_call" and row.settlement is not None for row in effects)
    assert sum(row.intent.effect_category == "provider_send" for row in effects) == 2
    batches = ToolResultDeliveryStore(log).load_batches("s", "r")
    assert len(batches) == 1
    assert batches[0].is_delivered


def test_shared_effect_replay_class_matches_tool_contract(tmp_path) -> None:
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(session_id="s", run_id="r", project="", provider_id="deepseek",
                          turn_budget=2, max_repair_rounds=0, task_kind="research")
    line.mark_writer_running("s", "r", provider_id="deepseek")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="deepseek")
    calls = [ToolCall("web_search", {"query": "q"}),
             ToolCall("open_url", {"url": "https://example.com"}),
             ToolCall("knowledge_write", {"title": "note"})]
    sink.begin_turn([(f"effect-{i}", call, i) for i, call in enumerate(calls)], turn=1)
    rows = {row.intent.tool_name: row.intent.replay_class
            for row in RuntimeEffectStore(log).load_effects("s", "r")}
    assert rows["web_search"] == "safe"
    assert rows["open_url"] == "safe"
    assert rows["knowledge_write"] == "unsafe"


def test_same_run_research_and_writer_effect_slots_do_not_collide(tmp_path) -> None:
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(session_id="s", run_id="r", project=str(tmp_path),
                          provider_id="deepseek", turn_budget=2, max_repair_rounds=0,
                          task_kind="hybrid")
    line.mark_writer_running("s", "r", provider_id="deepseek")
    calls: list[str] = []
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "project.read"}))
    research = TaskSession(policy=policy, task_kind="hybrid")
    writer = TaskSession(policy=policy, task_kind="hybrid")
    for session, scope, name, args in (
        (research, "research:1", "web_search", {"query": "q"}),
        (writer, "writer", "read_file", {"path": "a.txt"}),
    ):
        sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="deepseek")
        execute_turn(
            session, [ToolCall(name, args)],
            executors={name: lambda _call, current=name: calls.append(current) or "ok"},
            run_id="r", effect_scope=scope, turn=1, intent_sink=sink,
        )
        KernelRecordedProvider(SimpleNamespace(send=lambda *_args, **_kwargs: "ok"), sink).send("continue")
    assert calls == ["web_search", "read_file"]
    effects = [row for row in RuntimeEffectStore(log).load_effects("s", "r")
               if row.intent.effect_category == "tool_call"]
    assert len({row.intent.effect_id for row in effects}) == 2


def test_default_writer_records_effects_in_existing_runtime(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
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

        def send(self, _prompt, timeout=None):
            return next(replies)

    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    outcome = run(AgentRequest(
        provider=Provider(), project=tmp_path, task="read", max_turns=3,
        fresh_chat=False, session_id="s", run_id="r", provider_id="deepseek",
        runtime_mutations=line, on_event=lambda _event: None,
    ))
    assert outcome.stop_reason == "done"
    effects = RuntimeEffectStore(log).load_effects("s", "r")
    assert any(row.intent.effect_category == "tool_call" for row in effects)


def test_writer_recovery_delivers_previous_tool_result_without_rerunning(tmp_path) -> None:
    from codey.agents.request import AgentRequest, RecoveredToolOutcome
    from codey.operations.project_adapter import run
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    seen: list[str] = []
    replies = iter([
        '{"tool":"done","args":{"summary":"used recovered result"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, prompt, timeout=None):
            seen.append(str(prompt))
            return next(replies)

    with patch("codey.operations.task_execution.ExecutionDelegate._execute_project") as executed:
        result = run(AgentRequest(
            provider=Provider(), project=tmp_path, task="read", max_turns=3,
            fresh_chat=False, run_id="r", provider_id="deepseek",
            recovered_tool_outcomes=(RecoveredToolOutcome(
                call=ToolCall("read_file", {"path": "missing.txt"}),
                outcome=ToolOutcome("previous content", True), turn=1, tool_index=0,
            ),), on_event=lambda _event: None,
        ))
    assert result.stop_reason == "done"
    executed.assert_not_called()
    # Recovery-first: the original batch is delivered before any new model call.
    assert seen and "previous content" in seen[0]


def test_default_research_iteration_records_effects_in_same_runtime(tmp_path) -> None:
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_iteration import run_research_iteration
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(session_id="s", run_id="r", project="", provider_id="deepseek",
                          turn_budget=2, max_repair_rounds=0, task_kind="research")

    class Search:
        def search(self, query, limit=8):
            return [{"title": "Result", "url": "https://example.com", "snippet": query}]

    class Provider:
        name = "deepseek"

        def new_chat(self):
            return None

        def send(self, _prompt, timeout=None):
            return '{"tool":"web_search","args":{"query":"q"}}'

    store = KnowledgeStore(tmp_path / "knowledge")
    iteration = run_research_iteration(
        SimpleNamespace(knowledge_store=store, runtime_mutations=line),
        provider=Provider(), session_id="s", project="", task="q", max_turns=1,
        on_event=lambda _event: None, stop_flag=None, provider_id="deepseek",
        run_id="r", chat_handoff="", trace_recorder=None, search=Search(),
    )
    effects = RuntimeEffectStore(log).load_effects("s", "r")
    assert any(row.intent.effect_category == "tool_call" for row in effects), iteration.result.summary
    assert any(row.intent.effect_category == "provider_send" for row in effects)
    assert iteration.result.synthesis_id == ""
    assert not iteration.result.notes_created


def test_research_evidence_followup_uses_shared_kernel_with_fresh_url_guard() -> None:
    from codey.operations.evidence_followup import run_evidence_followup
    from codey.research.plan_executor import PlanExecutionResult

    url = "https://example.com/fresh"
    tools = SimpleNamespace(
        ledger=SimpleNamespace(evidence_items=[]), created_ids=[],
    )
    writes: list[dict] = []

    def knowledge_write(args):
        writes.append(args)
        tools.ledger.evidence_items.append(SimpleNamespace(source_url=url, excerpt="fact"))
        tools.created_ids.append("n1")
        return "saved note id=n1"

    tools.knowledge_write = knowledge_write
    replies = iter([
        '{"tool":"knowledge_write","args":{"type":"fact","title":"x","body":"x",'
        '"sources":["https://example.com/other"],"evidence":[{"source_url":"https://example.com/other",'
        '"excerpt":"x","claim":"x","stance":"supports"}]}}',
        '{"tool":"knowledge_write","args":{"type":"fact","title":"x","body":"x",'
        '"sources":["https://example.com/fresh"],"evidence":[{"source_url":"https://example.com/fresh",'
        '"excerpt":"fact","claim":"x","stance":"supports"}]}}',
    ])

    class Provider:
        def send(self, _prompt, timeout=None):
            return next(replies)

    result = run_evidence_followup(
        provider=Provider(), tools=tools,
        plan=SimpleNamespace(plan_ref="p1"),
        material=PlanExecutionResult(fresh_source_urls=(url,), previews=("fact",)),
        question="x", run_id="r", session_id="s", round_index=1,
        parent_policy=_cutover_parent_policy(),
    )
    assert result.has_new_evidence
    assert len(writes) == 1
    assert writes[0]["sources"] == [url]


def test_unified_edit_keeps_existing_file_guard_and_read_before_edit(tmp_path) -> None:
    from codey.operations.kernel_execution import execute_turn
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


def test_unified_writer_notifies_change_tracker_around_edit(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    class Tracker:
        def __init__(self):
            self.calls = []

        def capture_before(self, path):
            self.calls.append(("before", path))

        def capture_after(self, path):
            self.calls.append(("after", path))

    replies = iter([
        '{"tool":"edit","args":{"path":"app.py","content":"print(1)"}}',
        '{"tool":"done","args":{"summary":"created"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, _prompt, timeout=None):
            return next(replies)

    tracker = Tracker()
    run(AgentRequest(provider=Provider(), project=tmp_path, task="create app.py",
                     max_turns=2, fresh_chat=False, change_tracker=tracker,
                     on_event=lambda _event: None))
    assert tracker.calls == [("before", "app.py"), ("after", "app.py")]


def test_native_writer_uses_same_durable_delivery_chain(tmp_path, monkeypatch) -> None:
    from codey.agents.handoff import ConversationContext
    from codey.agents.request import AgentRequest
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.operations.project_adapter import run
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(session_id="s", run_id="r", project=str(tmp_path),
                          provider_id="local", turn_budget=3, max_repair_rounds=0,
                          task_kind="project")
    line.mark_writer_running("s", "r", provider_id="local")
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    sent: list[dict] = []
    conversation = ConversationContext()

    class Provider:
        name = "local"

        def send_turn(self, _prompt, _tools, timeout=None):
            return AssistantTurn(text="", tool_calls=(
                ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),
            ))

        def send_tool_results(self, messages, _tools, timeout=None):
            sent.extend(messages)
            return AssistantTurn(text='{"tool":"done","args":{"summary":"read app.py"}}')

    result = run(AgentRequest(
        provider=Provider(), project=tmp_path, task="read app.py", max_turns=3,
        fresh_chat=False, session_id="s", run_id="r", provider_id="local",
        runtime_mutations=line, on_event=lambda _event: None,
        conversation=conversation,
    ))
    assert result.stop_reason == "done"
    assert sent[0]["tool_call_id"] == "c1"
    assert conversation.used_tokens > 0
    assert ToolResultDeliveryStore(log).load_batches("s", "r")[0].is_delivered


def test_native_final_turn_answers_tool_call_before_budget_stop(tmp_path, monkeypatch) -> None:
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.task.model import TaskSubmission

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 1, False, "local"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    messages: list[dict] = []

    class Provider:
        name = "local"

        def send_turn(self, _prompt, _tools, timeout=None):
            return AssistantTurn(tool_calls=(
                ProviderToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"}),
            ))

        def send_tool_results(self, results, _tools, timeout=None):
            messages.extend(results)
            return AssistantTurn(text="acknowledged")

    outcome = run_task_kernel(session, provider=Provider(), provider_id="local",
                              project_path=tmp_path)
    assert outcome.stop_reason == "max_turns"
    assert messages and messages[0]["tool_call_id"] == "call-1"


def test_native_budget_stop_rejects_following_dangling_tool_call(tmp_path, monkeypatch) -> None:
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.task.model import TaskSubmission

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    (tmp_path / "a.txt").write_text("content", encoding="utf-8")
    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "read", 1, False, "local"))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=1)
    messages: list[list[dict]] = []

    class Provider:
        name = "local"

        def send_turn(self, _prompt, _tools, timeout=None):
            return AssistantTurn(tool_calls=(
                ProviderToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"}),
            ))

        def send_tool_results(self, results, _tools, timeout=None):
            messages.append(results)
            if len(messages) == 1:
                return AssistantTurn(tool_calls=(
                    ProviderToolCall(id="call-2", name="done", arguments={"summary": "x"}),
                ))
            return AssistantTurn(text="acknowledged")

    run_task_kernel(session, provider=Provider(), provider_id="local", project_path=tmp_path)
    assert len(messages) == 2
    assert messages[1][0]["tool_call_id"] == "call-2"
    assert "budget" in messages[1][0]["content"]


def test_unified_writer_keeps_managed_output_receipt_in_event(tmp_path) -> None:
    from codey.agents.request import AgentRequest
    from codey.agents.tools import AgentToolFns
    from codey.operations.project_adapter import run
    from codey.toolchain.runtime import ToolOutcome

    replies = iter([
        '{"tool":"run","args":{"path":".","command":"python -m pytest"}}',
        '{"tool":"done","args":{"summary":"checked"}}',
    ])

    class Provider:
        name = "deepseek"

        def send(self, _prompt, timeout=None):
            return next(replies)

    def command(*_args):
        return ToolOutcome(
            "test output", True, exit_code=0, truncated=True,
            audit={"managed_output": {
                "handle": "out_test", "original_bytes": 100,
                "stored_bytes": 100, "sha256": "a" * 64,
            }},
        )

    events: list[object] = []
    run(AgentRequest(
        provider=Provider(), project=tmp_path, task="run tests", max_turns=2,
        fresh_chat=False, on_event=events.append,
        tool_fns=AgentToolFns(run_command_with_context=command),
    ))
    finished = next(event for event in events if getattr(event, "kind", "") == "tool")
    assert finished.outcome.managed_output()["handle"] == "out_test"


def test_unified_research_externalizes_large_opened_source(tmp_path) -> None:
    from codey.knowledge.changes import KnowledgeChanges
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.research.tools import ResearchTools
    from codey.runtime.core.models import ToolCall
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.task.model import TaskSubmission

    url = "https://example.com/large"

    class Search:
        def fetch(self, target):
            return {"url": target, "title": "Large source", "text": "A" * 30_000,
                    "truncated": False}

    knowledge = KnowledgeStore(tmp_path / "knowledge")
    research_tools = ResearchTools(
        search=Search(), store=knowledge, changes=KnowledgeChanges(root=knowledge.root),
        session_id="s", project="",
    )
    policy = build_task_policy(TaskSubmission("s", None, "research", 2, False, "deepseek"),
                               task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research")
    outputs = ManagedOutputStore(tmp_path / "state")
    result = execute_turn(session, [ToolCall("open_url", {"url": url})],
                          research_tools=research_tools, managed_outputs=outputs,
                          session_id="s", run_id="r", turn=1)[0]
    assert result.audit["managed_output"]["handle"].startswith("out_")
    assert len(result.model_text) < 30_000


def test_research_controller_keeps_authorized_project_read_available(tmp_path) -> None:
    from codey.operations.kernel_protocol import (
        controller_allowed_for_session,
        normalize_turn,
    )
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission

    policy = build_task_policy(TaskSubmission("s", str(tmp_path), "research project", 3,
                                              False, "deepseek", intent="research"),
                               task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research", project=str(tmp_path))
    plan = normalize_turn('{"tool":"read_file","args":{"path":"README.md"}}',
                          policy=policy, controller_allowed=controller_allowed_for_session(session))
    assert not plan.protocol_error
    assert plan.calls[0].name == "read_file"


def test_research_project_verification_runs_without_write_grant(tmp_path) -> None:
    from codey.agents.tools import AgentToolFns
    from codey.operations.kernel_execution import execute_turn
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

    def verify(_root, _path, command, *_rest):
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
