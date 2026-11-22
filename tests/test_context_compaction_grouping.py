"""Compaction groups cover each message once and keep adjacent tool replies together."""

from typing import Literal

import pytest

from codey.agents.context_compaction import group_messages_for_compaction

MessageKind = Literal["system", "user", "assistant", "call", "tool"]


@pytest.mark.parametrize("kinds,expected", [
    ((), []),
    (("system", "user", "assistant"), [[0], [1], [2]]),
    (("call", "tool", "tool", "user"), [[0, 1, 2], [3]]),
    (("tool", "assistant", "tool"), [[0], [1], [2]]),
    (("call", "user", "tool"), [[0], [1], [2]]),
    (("call", "tool", "call", "tool"), [[0, 1], [2, 3]]),
])
def test_tool_grouping_preserves_atomic_replies_and_stray_messages(
    kinds: tuple[MessageKind, ...], expected: list[list[int]],
) -> None:
    messages: list[dict[str, object]] = []
    for kind in kinds:
        message: dict[str, object] = {"role": "assistant" if kind == "call" else kind}
        if kind == "call":
            message["tool_calls"] = [{"id": "call-id"}]
        messages.append(message)

    groups = group_messages_for_compaction(messages)

    assert groups == expected
    assert [index for group in groups for index in group] == list(range(len(messages)))
