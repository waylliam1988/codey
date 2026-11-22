"""New information cannot prove an identical call/result loop."""

import pytest

from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.runtime.core.models import ToolCall, ToolResult


@pytest.mark.parametrize("tool", ["read", "read_file", "search"])
@pytest.mark.parametrize("texts", [("first", "second", "third"), ("first", "first", "changed")])
def test_same_call_with_new_output_is_not_a_proven_repeat(tool: str, texts: tuple[str, ...]) -> None:
    call = ToolCall(tool, {"path": "log.txt"})
    records = [attempt_record(call, ToolResult(call, text, ok=True), turn=turn)
               for turn, text in enumerate(texts)]

    assert not should_block_or_remind(records).block


@pytest.mark.parametrize("tool", ["read", "read_file", "search"])
def test_identical_call_and_result_still_triggers_the_guard(tool: str) -> None:
    call = ToolCall(tool, {"path": "log.txt"})
    records = [attempt_record(call, ToolResult(call, "unchanged", ok=True), turn=turn) for turn in range(3)]

    assert should_block_or_remind(records).block
