from __future__ import annotations

from pathlib import Path

from codey.agents import context_compaction as compaction
from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.providers import error_classification as errors
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.write.file_mutation_queue import group_tool_calls_for_execution
from codey.toolchain.line_prefix import strip_line_number_prefixes
from codey.toolchain.runtime import (
    EditBlock,
    edit_file,
    retry_replacement_without_line_numbers,
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


def test_receipts_keep_small_results_inline_and_reject_unstored_large_results() -> None:
    import pytest

    from codey.operations.kernel_receipts import result_receipt_fields

    call = ToolCall("read", {"path": "a.py"})
    small = result_receipt_fields(ToolResult(call, "ok", ok=True), store=None, session_id="s", run_id="r", effect_id="e")
    assert small["result_text"] == "ok"
    assert small["result_ref"] == ""
    with pytest.raises(ValueError, match="durable managed receipt"):
        result_receipt_fields(ToolResult(call, "z" * 30_000, ok=True), store=None, session_id="s", run_id="r", effect_id="e")


def test_receipts_persist_complete_large_results(tmp_path: Path) -> None:
    import json

    from codey.operations.kernel_receipts import result_receipt_fields, text_digest
    from codey.storage.managed_outputs import ManagedOutputStore

    store = ManagedOutputStore(tmp_path / "state")
    result = result_receipt_fields(ToolResult(ToolCall("search", {"query": "q"}), "z" * 30_000, ok=True),
                                   store=store, session_id="s", run_id="r", effect_id="e")
    assert result["result_payload"] == ""
    assert result["result_ref"].startswith("out_")
    payload, metadata = store.read_tool_output("s", "r", result["result_ref"])
    assert text_digest(payload) == result["result_payload_sha256"]
    assert json.loads(payload)["model_text"] == "z" * 30_000
    assert metadata["stored_truncated"] is False


def test_edit_line_prefix_retry(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")
    stripped, changed = strip_line_number_prefixes("42 | def foo():\n42 |     return 1")
    assert changed
    assert stripped == "def foo():\n    return 1"
    block = EditBlock(old_string="42 | def foo():", new_string="42 | def bar():")
    retried = retry_replacement_without_line_numbers("def foo():\n", block)
    assert retried is not None
    outcome = edit_file(tmp_path, "app.py", [EditBlock(old_string="1 | def foo():", new_string="1 | def bar():")])
    assert outcome.ok
    assert "def bar():" in target.read_text(encoding="utf-8")
    assert "1 |" not in target.read_text(encoding="utf-8")
    # Ambiguous after strip must refuse.
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    bad = edit_file(tmp_path, "app.py", [EditBlock(old_string="9 | x = 1", new_string="9 | x = 2")])
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
    failed = ToolResult(ok=False, call=call, model_text="ERROR: boom")
    history = [attempt_record(call, failed, turn=turn) for turn in range(3)]
    decision = should_block_or_remind(history)
    assert decision.block
    ok_history = [attempt_record(call, ToolResult(ok=True, call=call, model_text="fine"), turn=0)]
    assert not should_block_or_remind(ok_history).block


def test_registry_snapshot_stable() -> None:
    # Old ToolRegistry removed; single ToolSpec snapshot stays stable.
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import tool_specs, visible_tool_names

    policy = TaskPolicy(
        grants=frozenset({"project.read", "project.write", "project.verify", "control"}),
        strict_research=False,
    )
    names = set(visible_tool_names(policy))
    assert "read_file" in names
    assert "read_file" in tool_specs()
