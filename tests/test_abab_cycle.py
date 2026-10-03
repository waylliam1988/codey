from __future__ import annotations

from pathlib import Path

from codey.agents.runaway_guard import (
    attempt_record,
    detect_abab_cycle,
    detect_periodic_cycle,
    should_block_or_remind,
)
from codey.agents.state import ToolAttemptRecord
from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.runtime.core.models import ToolCall, ToolResult


def _ok(name: str, args: dict, text: str, *, turn: int = 0) -> ToolAttemptRecord:
    return attempt_record(ToolCall(name=name, args=args), ToolResult(call=ToolCall(name, args), model_text=text), turn=turn)


def test_single_lookback_does_not_trigger() -> None:
    records = [
        _ok("search", {"query": "A"}, "hits A"),
        _ok("read", {"path": "B"}, "body B"),
        _ok("search", {"query": "A"}, "hits A"),
    ]
    assert not should_block_or_remind(records).block
    assert not detect_abab_cycle(records).block


def test_abab_second_cycle_triggers_remind() -> None:
    records = [
        _ok("search", {"query": "A"}, "hits A"),
        _ok("read", {"path": "B"}, "body B"),
        _ok("search", {"query": "A"}, "hits A"),
        _ok("read", {"path": "B"}, "body B"),
    ]
    decision = should_block_or_remind(records)
    assert decision.block
    assert decision.action == "remind"
    assert "search" in decision.reason and "read" in decision.reason


def test_changing_results_do_not_trigger() -> None:
    records = [
        _ok("search", {"query": "A"}, "hits v1"),
        _ok("read", {"path": "B"}, "body B"),
        _ok("search", {"query": "A"}, "hits v2"),
        _ok("read", {"path": "B"}, "body B"),
    ]
    assert not should_block_or_remind(records).block


def test_file_change_breaks_cycle() -> None:
    from dataclasses import replace

    records = [
        _ok("search", {"query": "A"}, "hits A"),
        _ok("read", {"path": "B"}, "body B"),
        replace(_ok("search", {"query": "A"}, "hits A"), changed=True),
        _ok("read", {"path": "B"}, "body B"),
    ]
    assert not should_block_or_remind(records).block


def test_abcabc_triggers_periodic() -> None:
    records = [
        _ok("search", {"query": "A"}, "a"),
        _ok("read", {"path": "B"}, "b"),
        _ok("run", {"command": "c"}, "c"),
        _ok("search", {"query": "A"}, "a"),
        _ok("read", {"path": "B"}, "b"),
        _ok("run", {"command": "c"}, "c"),
    ]
    assert detect_periodic_cycle(records).block
    assert not detect_abab_cycle(records).block


def test_edit_cycle_demands_reread() -> None:
    records = [
        _ok("read", {"path": "F"}, "body"),
        _ok("edit", {"path": "F"}, "ERROR: no match"),
        _ok("read", {"path": "F"}, "body"),
        _ok("edit", {"path": "F"}, "ERROR: no match"),
    ]
    decision = should_block_or_remind(records)
    assert decision.block
    assert decision.action == "require_reread"


def test_shell_cycle_stops() -> None:
    records = [
        _ok("shell", {"command": "deploy"}, "needs approval"),
        _ok("read", {"path": "F"}, "body"),
        _ok("shell", {"command": "deploy"}, "needs approval"),
        _ok("read", {"path": "F"}, "body"),
    ]
    decision = should_block_or_remind(records)
    assert decision.block
    assert decision.action == "stop"


def test_runaway_record_failure_is_visible(tmp_path: Path) -> None:
    # 进度记账失败不得打断循环：当前链经 KernelProgress.observe 容错，
    # 故障结果计为无进展（安全方向：可能早停，绝不空转，更不崩溃）。
    from unittest import mock

    from codey.operations.kernel_progress import KernelProgress
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
        task_kind="project", project=str(tmp_path), max_turns=4,
    )
    progress = KernelProgress(stagnant_turns=4)
    result = ToolResult(
        call=ToolCall(name="read", args={"path": "app.py"}, call_id="c1"),
        model_text="hello",
    )
    with mock.patch(
        "codey.operations.kernel_progress.attempt_record", side_effect=RuntimeError("boom"),
    ):
        stopped = progress.observe([result], session)
    assert stopped is False
    assert list(progress.attempts) == []


def test_runaway_guard_failure_is_visible(tmp_path: Path) -> None:
    # Old loop runaway guard deleted with the old loop; stagnation via the new
    # single entry (invalid_turns >= stagnant_turns) is locked in
    # test_runtime_helper_boundaries (new entry stagnant). This keeps the behavior
    # category (no infinite loops) via the new entry.
    from unittest.mock import patch

    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(grants=frozenset({"control"}))
    session = TaskSession(policy=policy, task_kind="project", project="", max_turns=3, task_text="hi")

    class _BadProvider:
        def send(self, prompt: str, timeout: object = None) -> str:
            return "not json"

    with patch("codey.operations.kernel_transport.provider_uses_native", return_value=False):
        result = run_task_kernel(
            session,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=_BadProvider(),
                    run_id="r-runaway",
                    stagnant_turns=2,
                ),
                execution=KernelExecutionDeps(
                    executors={},
                ),
            ),
        )
    assert result.stop_reason == "protocol"


def test_search_pagination_next_offset(tmp_path: Path) -> None:
    from codey.toolchain import runtime as tool_runtime

    root = tmp_path
    (root / "a.py").write_text("\n".join(f"target line {i}" for i in range(10)) + "\n", encoding="utf-8")
    first = tool_runtime.search_files(root, ".", "target", limit=4)
    assert first.ok
    assert "next offset=5" in first.model_text
    assert '"offset":5' in first.model_text
    second = tool_runtime.search_files(root, ".", "target", offset=5, limit=4)
    assert second.ok
    assert "results 5-8" in second.model_text
    assert "next offset=9" in second.model_text
    last = tool_runtime.search_files(root, ".", "target", offset=9, limit=4)
    assert last.ok
    assert "end of matches" in last.model_text


def test_search_first_page_uses_unified_page_footer(tmp_path: Path) -> None:
    from codey.toolchain import runtime as tool_runtime

    root = tmp_path
    (root / "a.py").write_text("target\ntarget\ntarget\n", encoding="utf-8")
    outcome = tool_runtime.search_files(root, ".", "target", max_results=2)
    assert outcome.truncated
    assert "[grep page: results 1-2; next offset=3" in outcome.model_text
    assert "next call:" in outcome.model_text


def test_search_rejects_bad_pagination() -> None:
    from codey.toolchain.constants import SEARCH_PAGE_MAX_RESULTS
    from codey.toolchain.tool_args_repair import ToolArgsRepairError, normalize_tool_args

    assert SEARCH_PAGE_MAX_RESULTS == 100
    repaired = normalize_tool_args("search", {"query": "x", "offset": 3, "limit": 10})
    assert repaired.args == {"query": "x", "path": ".", "offset": 3, "limit": 10}
    try:
        normalize_tool_args("search", {"query": "x", "offset": 0})
    except ToolArgsRepairError:
        pass
    else:
        raise AssertionError("offset=0 must be rejected")
    try:
        normalize_tool_args("search", {"query": "x", "limit": SEARCH_PAGE_MAX_RESULTS + 1})
    except ToolArgsRepairError:
        pass
    else:
        raise AssertionError("over-cap limit must be rejected")


def test_mutation_groups_drive_execution_order(tmp_path: Path) -> None:
    from codey.runtime.write.file_mutation_queue import group_tool_calls_for_execution

    calls = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="read", args={"path": "b.py"}),
        ToolCall(name="edit", args={"path": "a.py"}),
    ]
    groups = group_tool_calls_for_execution(calls, str(tmp_path))
    flat = [i for group in groups for i in group]
    assert sorted(flat) == [0, 1, 2]
    # read b.py touches no written file, so it batches with the first edit;
    # the second edit of a.py still serializes after it.
    assert groups == [[0, 1], [2]]


def test_local_context_defaults_come_from_capability() -> None:
    from codey.providers.capabilities import capability_for
    from codey.providers.local_openai import LocalOpenAIProvider

    capability = capability_for("local")
    assert capability.context_window_tokens == 32_768
    assert capability.context_reserve_tokens == 8_192
    assert capability.context_keep_recent_tokens == 12_000
    provider = LocalOpenAIProvider(
        base_url="http://127.0.0.1:9/v1",
        model="qwen-test",
        context_window_tokens=capability.context_window_tokens,
        context_reserve_tokens=capability.context_reserve_tokens,
        context_keep_recent_tokens=capability.context_keep_recent_tokens,
    )
    assert provider.context_window_tokens == 32_768
    assert provider.context_keep_recent_tokens == 12_000


def test_conversation_plan_applies_capability_budgets() -> None:
    from codey.agents.handoff import ConversationContext
    from codey.operations.conversation_plan import build_conversation_plan

    class _State:
        def provider_session_changed(self, provider_id: str, session_id: str) -> bool:
            return False

        def visible_session_excerpt(self, session_id: str, current_request: str = "") -> str:
            return ""

    conversation = ConversationContext()
    plan = build_conversation_plan(
        state=_State(),  # type: ignore[arg-type]
        session_id="s",
        provider_id="local",
        provider=None,
        conversation=conversation,
        task_kind="project",
        project=None,
        task="do work",
        continue_task=False,
        trace=None,
    )
    assert conversation.hard_limit == 32_768 - 8_192
    assert conversation.reserve_tokens == 8_192
    assert conversation.keep_recent_tokens == 12_000
    assert plan.conversation is conversation
