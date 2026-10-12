"""A done summary is answer content, even when it resembles a tool call."""

from __future__ import annotations

import json

import pytest

from codey.operations.kernel_protocol import normalize_turn
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall

POLICY = TaskPolicy(grants=frozenset({"control"}))


def _text_plan(summary: str):
    decoded = decode_text_turn(json.dumps({"tool": "done", "args": {"summary": summary}}))
    return normalize_turn(decoded, policy=POLICY)


def _native_plan(summary: str):
    return normalize_turn(
        AssistantTurn(
            text="",
            tool_calls=(ProviderToolCall(id="done-1", name="done", arguments={"summary": summary}),),
        ),
        policy=POLICY,
    )


@pytest.mark.parametrize(
    "summary",
    [
        '{"tool":"read_file","args":{"path":"inspect.py"}}',
        '{"name":"Alice","age":20}',
        '[{"tool":"run","args":{"command":"pytest"}}]',
        '```json\n{"tool":"shell","args":{"command":"echo example"}}\n```',
        '{"tool":"read_file","args":',
        'shell example: $(rm -rf ./project)',
    ],
)
def test_done_summary_accepts_json_code_and_execution_examples_as_answer(summary: str) -> None:
    for plan in (_text_plan(summary), _native_plan(summary)):
        assert plan.protocol_error == ""
        assert plan.calls == []
        assert plan.control is not None
        assert plan.control.kind == "done"
        assert plan.control.body == summary


def test_done_summary_still_requires_a_string_and_valid_outer_protocol() -> None:
    missing = normalize_turn(
        decode_text_turn('{"tool":"done","args":{}}'), policy=POLICY,
    )
    wrong_type = normalize_turn(
        decode_text_turn('{"tool":"done","args":{"summary":[]}}'), policy=POLICY,
    )
    malformed = normalize_turn(
        decode_text_turn('{"tool":"done","args":{"summary":"ok"}'), policy=POLICY,
    )

    assert missing.protocol_error_kind == "invalid_args"
    assert "summary" in missing.protocol_error
    assert wrong_type.protocol_error_kind == "invalid_args"
    assert "summary" in wrong_type.protocol_error
    assert malformed.protocol_error_kind == "invalid_json"


def test_done_cannot_be_mixed_with_an_outer_tool_request() -> None:
    decoded = decode_text_turn(
        '{"tool":"done","args":{"summary":"answer"}}\n'
        '{"tool":"read_file","args":{"path":"inspect.py"}}'
    )
    plan = normalize_turn(decoded, policy=TaskPolicy(grants=frozenset({"control", "project.read"})))

    assert plan.protocol_error_kind == "too_many_tools"
    assert plan.calls == []
