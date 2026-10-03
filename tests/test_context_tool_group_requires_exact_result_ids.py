"""Compaction must not hide missing results behind duplicate or foreign IDs."""
import pytest

from codey.agents.context_compaction import find_safe_cut, is_tool_group_complete


@pytest.mark.parametrize("calls,results", [
    (["a", "b"], ["a", "a"]), (["a"], ["foreign"]),
    (["a"], ["a", "a"]), (["a", "a"], ["a", "a"]),
    ([""], [""]), ([1], [1]), (["a", "b"], ["a"]),
])
def test_invalid_pairing_is_not_complete_or_compactable(calls, results):
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "assistant", "tool_calls": [{"id": x} for x in calls]},
        *[{"role": "tool", "tool_call_id": x, "content": "x" * 200} for x in results],
        {"role": "user", "content": "continue"},
    ]
    assert is_tool_group_complete(messages, range(1, len(messages) - 1)) is False
    assert find_safe_cut(messages, reserve_tokens=100, keep_recent_tokens=1, context_window_tokens=1) == 0


def test_out_of_order_unique_results_are_complete():
    messages = [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
                {"role": "tool", "tool_call_id": "b"}, {"role": "tool", "tool_call_id": "a"}]
    assert is_tool_group_complete(messages, range(3)) is True


def test_stray_tool_result_is_not_a_complete_group():
    assert is_tool_group_complete([{"role": "tool", "tool_call_id": "a"}], [0]) is False
