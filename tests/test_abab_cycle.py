from __future__ import annotations

from pathlib import Path

from codey.agents.runaway_guard import (
    attempt_record,
    detect_abab_cycle,
    detect_periodic_cycle,
    should_block_or_remind,
)
from codey.agents.state import ToolAttemptRecord
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


def test_legacy_tuple_history_still_works() -> None:
    call = ToolCall(name="read", args={"path": "a"})
    failed = ToolResult(call=call, model_text="ERROR: boom")
    assert should_block_or_remind([(call, failed)] * 3).block


def test_research_cycle_records_and_flags() -> None:
    from codey.research.native_bridge import record_and_check_cycle

    class _Runner:
        pass

    runner = _Runner()
    pairs = [
        (ToolCall(name="web_search", args={"query": "A"}), ToolResult(call=ToolCall("web_search", {}), model_text="hits")),
        (ToolCall(name="open_url", args={"url": "https://b"}), ToolResult(call=ToolCall("open_url", {}), model_text="page")),
    ]

    class _Plan:
        calls = [p[0] for p in pairs]

    assert record_and_check_cycle(runner, _Plan(), pairs) == ""
    assert record_and_check_cycle(runner, _Plan(), pairs) != ""


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


def test_search_first_page_keeps_legacy_truncation_text(tmp_path: Path) -> None:
    from codey.toolchain import runtime as tool_runtime

    root = tmp_path
    (root / "a.py").write_text("target\ntarget\ntarget\n", encoding="utf-8")
    outcome = tool_runtime.search_files(root, ".", "target", max_results=2)
    assert outcome.truncated
    assert "truncated after 2 matches" in outcome.model_text
    assert "narrow the query" in outcome.model_text


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
    from codey.agents.tool_turn import PlannedToolCall
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
    assert PlannedToolCall is not None


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
