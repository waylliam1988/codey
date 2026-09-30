"""planning 只做收窄与上下文，不再自建任务准备。

共同准备（策略一次确定、run 身份、授权资源、恢复行）必须完整传入
AgentRequest：复用 frame.entry_policy；run/session 身份齐全；持久运行
资源（mutations/managed_outputs/恢复行）齐全；允许联网时具备 Research
执行器。
"""

from __future__ import annotations

from types import SimpleNamespace


def _planning_request(*, requested=(), entry_policy=None, recovered=(), knowledge_store=None):
    from unittest import mock

    from codey.operations.planning_flow import PlanningFlowDeps, run_planning_readonly_mode
    from codey.runtime.core.run_result import RunResult

    captured: dict = {}

    def agent_run(request):
        captured["request"] = request
        return RunResult(summary="plan", stop_reason="done", turns=1)

    request = SimpleNamespace(
        session_id="s-plan", project="E:\\codey", task="plan this",
        max_turns=3, provider_id="local", intent="planning",
        requested_capabilities=tuple(requested),
        strict_research=False, sources_open_required=False,
        project_changes_required=False,
    )
    from codey.agents.handoff import ConversationSnapshot

    conversation = SimpleNamespace(
        snapshot=ConversationSnapshot(mode="chat"),
        update_snapshot=lambda _s: None,
    )
    frame = SimpleNamespace(
        request=request, run_id="r-plan", task_kind="planning",
        provider=SimpleNamespace(new_chat=lambda: None, close=lambda: None),
        provider_id="local", project_text="E:\\codey",
        conversation=conversation, fresh_chat=False, handoff="",
        provider_session_changed=False, preflight_switches=0,
        trace=SimpleNamespace(call=lambda *a, **k: None),
        recovered_tool_outcomes=tuple(recovered),
        recovered_tool_result_batch_id="",
        entry_policy=entry_policy,
    )
    state = SimpleNamespace(
        run_registry=SimpleNamespace(stop_flag=SimpleNamespace(is_set=lambda: False)),
        runtime_mutations="MUT",
        managed_outputs="OUT",
        emit=lambda _e: None,
        set_provider_session=lambda *a, **k: None,
    )
    work = SimpleNamespace(ledger=None, record_agent_events_in_ledger=False)
    deps = PlanningFlowDeps(
        state=state, agent_run=agent_run, knowledge_store=knowledge_store,
    )
    with mock.patch(
        "codey.operations.planning_flow.ProjectTaskContextBuilder",
    ) as builder:
        context = SimpleNamespace(
            verified_facts="", research_context="", project_map="",
            project_config_warnings="",
        )
        builder.return_value.build.return_value = context
        run_planning_readonly_mode(deps, frame, work)
    return captured["request"]


def test_planning_reuses_entry_policy_and_full_run_identity():
    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    req = _planning_request(entry_policy=policy)
    assert req.task_policy is policy, "不得重新构造 planning policy"
    assert req.session_id == "s-plan"
    assert req.run_id == "r-plan"
    assert req.runtime_mutations == "MUT"
    assert req.managed_outputs == "OUT"
    assert req.permission_profile == "planning_readonly"
    assert req.task_policy.allows("project.write") is False


def test_planning_forwards_recovery_rows_and_denied_capabilities():
    from codey.agents.request import RecoveredToolOutcome
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    row = RecoveredToolOutcome(
        call=ToolCall("read_file", {"path": "a.py"}, "c1"),
        outcome=ToolOutcome("x", True), turn=0, tool_index=0, effect_id="e1",
    )
    req = _planning_request(recovered=(row,))
    assert tuple(req.recovered_tool_outcomes) == (row,)


def test_planning_with_web_read_gets_research_executors():
    import tempfile
    from pathlib import Path

    from codey.knowledge.store import KnowledgeStore
    from codey.policies.task_policy import TaskPolicy

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        root = Path(tmp.name)
        store = KnowledgeStore(root / "knowledge")
        policy = TaskPolicy(grants=frozenset({"control", "project.read", "web.read"}))
        req = _planning_request(
            requested=("web.read",), entry_policy=policy, knowledge_store=store,
        )
        assert req.research_tools is not None, "允许联网的规划必须有对应执行器"
    finally:
        tmp.cleanup()


def test_planning_narrows_parent_policy_without_losing_denials_or_requirements():
    from codey.policies.task_policy import TaskPolicy

    parent = TaskPolicy(grants=frozenset({"control", "project.write", "project.verify", "web.read"}),
                        denied_capabilities=frozenset({"web.read"}), required_checks=("custom_requirement",))
    request = _planning_request(entry_policy=parent)
    assert not request.task_policy.allows("project.write")
    assert not request.task_policy.allows("project.verify")
    assert not request.task_policy.allows("web.read")
    assert request.task_policy.required_checks == parent.required_checks
    assert parent.allows("project.write")


def test_planning_does_not_create_web_resources_when_parent_denies_web():
    from unittest import mock

    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(grants=frozenset({"control", "web.read"}),
                        denied_capabilities=frozenset({"web.read"}))
    with mock.patch("codey.operations.task_execution.build_research_tools") as build:
        request = _planning_request(requested=("web.read",), entry_policy=policy)
    build.assert_not_called()
    assert request.research_tools is None


def test_research_resource_construction_failure_closes_search_and_propagates(tmp_path):
    from unittest import mock

    import pytest

    from codey.operations.task_execution import build_research_tools

    search = mock.Mock()
    deps = SimpleNamespace(knowledge_store=SimpleNamespace(root=tmp_path), search_factory=lambda: search)
    with (
        mock.patch("codey.research.tools.ResearchTools", side_effect=RuntimeError("broken adapter")),
        pytest.raises(RuntimeError, match="broken adapter"),
    ):
        build_research_tools(deps, session_id="s", project="")
    search.close.assert_called_once_with()


def test_planning_closes_owned_search_after_agent_returns():
    from unittest import mock

    from codey.policies.task_policy import TaskPolicy

    tools = SimpleNamespace(search=mock.Mock())
    policy = TaskPolicy(grants=frozenset({"control", "web.read"}))
    with mock.patch("codey.operations.task_execution.build_research_tools", return_value=tools):
        _planning_request(entry_policy=policy)
    tools.search.close.assert_called_once_with()


def test_writer_reuses_one_authorized_adapter_for_repair_attempts():
    from unittest import mock

    from codey.operations.project_writer_phase import _writer_research_tools
    from codey.policies.task_policy import TaskPolicy

    ctx = SimpleNamespace(frame=SimpleNamespace(
        entry_policy=TaskPolicy(grants=frozenset({"control", "web.read"})), project_text="project"),
        request=SimpleNamespace(session_id="s", requested_capabilities=()),
        deps=SimpleNamespace(persistence=object()), research_tools=None)
    tools = object()
    with mock.patch("codey.operations.project_writer_phase._build_research_tools", return_value=tools) as build:
        assert _writer_research_tools(ctx) is tools
        assert _writer_research_tools(ctx) is tools
    build.assert_called_once()
