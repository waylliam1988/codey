from __future__ import annotations

from pathlib import Path

from codey.agents import context_compaction as compaction
from codey.agents.runaway_guard import should_block_or_remind
from codey.providers import error_classification as errors
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.hooks import RuntimeHooks, call_hooks
from codey.runtime.write.file_mutation_queue import group_tool_calls_for_execution
from codey.toolchain.runtime import (
    EditBlock,
    edit_file,
    retry_replacement_without_line_numbers,
    strip_line_number_prefixes,
)


def test_compaction_never_splits_tool_group() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do work " + ("x" * 5000)},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "contents " + ("y" * 5000)},
        {"role": "user", "content": "follow up"},
    ]
    groups = compaction.group_messages_for_compaction(messages)
    assert any(len(g) == 2 and g[0] == 2 for g in groups)
    cut = compaction.find_safe_cut(
        messages, reserve_tokens=100_000, keep_recent_tokens=500,
        context_window_tokens=1000, tools_tokens=0,
    )
    # Cut must not land between assistant tool_calls (2) and its tool result (3).
    assert cut not in (3,)
    assert compaction.is_tool_group_complete(messages, [2, 3])


def test_compact_in_place_keeps_system_and_tail() -> None:
    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old " + ("a" * 3000)},
        {"role": "assistant", "content": "old answer " + ("b" * 3000)},
        {"role": "user", "content": "latest request"},
    ]
    summary = compaction.compact_openai_messages_in_place(
        messages, context_window_tokens=1000, reserve_tokens=500, keep_recent_tokens=100,
    )
    assert summary
    assert messages[0]["role"] == "system"
    assert messages[-1]["content"] == "latest request"


def test_compaction_prefix_appears_exactly_once() -> None:
    from codey.agents.context_compaction import SUMMARY_PREFIX_TEXT

    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old " + ("a" * 3000)},
        {"role": "assistant", "content": "old answer " + ("b" * 3000)},
        {"role": "user", "content": "latest request"},
    ]
    summary = compaction.compact_openai_messages_in_place(
        messages, context_window_tokens=1000, reserve_tokens=500, keep_recent_tokens=100,
    )
    assert summary
    assert SUMMARY_PREFIX_TEXT not in summary
    joined = "\n".join(str(m.get("content") or "") for m in messages)
    assert joined.count(SUMMARY_PREFIX_TEXT) == 1


def test_overflow_classification() -> None:
    assert errors.classify_http_error(401, "unauthorized") == errors.ProviderErrorKind.AUTH
    assert errors.classify_http_error(400, "This model's maximum context length is 128k") == (
        errors.ProviderErrorKind.CONTEXT_OVERFLOW
    )
    assert errors.classify_openai_choice({"finish_reason": "length", "message": {"content": ""}}) == (
        errors.ProviderErrorKind.OUTPUT_LENGTH
    )
    assert errors.is_context_overflow_message("context window exceeded")


def _ephemeral_session() -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        request=SimpleNamespace(managed_outputs=None),
        session_id="",
        run_id="",
        profile=SimpleNamespace(name="coding_writer"),
    )


def test_externalize_clips_only_oversize() -> None:
    from codey.agents.tool_execution import maybe_externalize_large_tool_output
    from codey.toolchain.runtime import ToolOutcome

    session = _ephemeral_session()
    small = ToolOutcome("ok", True)
    assert maybe_externalize_large_tool_output(session, ToolCall(name="read", args={}), small, turn=1, tool_index=0) is small
    big = ToolOutcome("z" * 30_000, True)
    clipped = maybe_externalize_large_tool_output(session, ToolCall(name="read", args={}), big, turn=1, tool_index=0)
    assert clipped.truncated
    assert "externalized" in clipped.model_text
    assert len(clipped.model_text.encode("utf-8")) < 30_000


def test_externalize_persists_durable_receipt(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from codey.agents.tool_execution import maybe_externalize_large_tool_output
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.toolchain.runtime import ToolOutcome

    store = ManagedOutputStore(tmp_path / "state")
    session = SimpleNamespace(
        request=SimpleNamespace(managed_outputs=store),
        session_id="session-1",
        run_id="run-1",
        profile=SimpleNamespace(name="coding_writer"),
    )
    outcome = maybe_externalize_large_tool_output(
        session, ToolCall(name="search", args={"query": "q"}), ToolOutcome("z" * 30_000, True),
        turn=2, tool_index=0,
    )
    assert outcome.truncated
    managed = outcome.managed_output()
    assert managed["handle"].startswith("out_")
    assert managed["original_bytes"] == 30_000
    assert len(managed["sha256"]) == 64
    assert store.path_for("session-1", "run-1", str(managed["handle"])).is_file()


def test_edit_line_prefix_retry(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")
    stripped, changed = strip_line_number_prefixes("42 | def foo():\n42 |     return 1")
    assert changed
    assert stripped == "def foo():\n    return 1"
    block = EditBlock(search="42 | def foo():", replace="42 | def bar():")
    retried = retry_replacement_without_line_numbers("def foo():\n", block)
    assert retried is not None
    outcome = edit_file(tmp_path, "app.py", [EditBlock(search="1 | def foo():", replace="1 | def bar():")])
    assert outcome.ok
    assert "def bar():" in target.read_text(encoding="utf-8")
    assert "1 |" not in target.read_text(encoding="utf-8")
    # Ambiguous after strip must refuse.
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    bad = edit_file(tmp_path, "app.py", [EditBlock(search="9 | x = 1", replace="9 | x = 2")])
    assert not bad.ok


def test_mutation_queue_serializes_same_file(tmp_path: Path) -> None:
    calls = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="read", args={"path": "a.py"}),
    ]
    groups = group_tool_calls_for_execution(calls, str(tmp_path))
    assert groups[0] == [0]
    assert groups[1] == [1]
    assert groups[2] == [2]
    other = [
        ToolCall(name="edit", args={"path": "a.py"}),
        ToolCall(name="read", args={"path": "b.py"}),
    ]
    batched = group_tool_calls_for_execution(other, str(tmp_path))
    assert batched == [[0, 1]]


def test_runaway_blocks_exact_repeat() -> None:
    call = ToolCall(name="read", args={"path": "a"})
    failed = ToolResult(call=call, model_text="ERROR: boom")
    history = [(call, failed)] * 3
    decision = should_block_or_remind(history)
    assert decision.block
    ok_history = [(call, ToolResult(call=call, model_text="fine"))]
    assert not should_block_or_remind(ok_history).block


def test_hooks_fail_open() -> None:
    seen: list[str] = []

    def bad(**payload: object) -> None:
        raise RuntimeError("hook boom")

    def good(**payload: object) -> None:
        seen.append("good")

    hooks = RuntimeHooks(before_tool_call=(bad, good))
    call_hooks(hooks, "before_tool_call")
    assert seen == ["good"]


def test_registry_snapshot_stable() -> None:
    from codey.toolchain.registry import ToolRegistry

    snapshot = ToolRegistry().snapshot(profile_name="coding_writer", mode="coding")
    assert "read_file" in snapshot.names
    assert "read" in snapshot.runtime_names
