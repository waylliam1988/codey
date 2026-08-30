from __future__ import annotations

from codey.protocols.json_codec import JsonToolCodec


def test_parse_ignores_json_inside_think_blocks() -> None:
    plan = JsonToolCodec().parse(
        '<think>{"tool":"run","args":{"command":"python -m pytest","path":"."}}</think>\n'
        '{"tool":"read_file","args":{"path":"app.py"}}'
    )

    assert [call.name for call in plan.calls] == ["read"]
    assert plan.calls[0].args == {"path": "app.py"}


def test_parse_deduplicates_repeated_tool_calls() -> None:
    plan = JsonToolCodec().parse(
        '{"tool":"read_file","args":{"path":"app.py"}}\n'
        '{"args":{"path":"app.py"},"tool":"read_file"}'
    )

    assert [call.name for call in plan.calls] == ["read"]
    assert plan.control is not None
    assert plan.control.kind == "continue"


def test_parse_preserves_literal_think_tags_inside_json_strings() -> None:
    plan = JsonToolCodec().parse(
        '{"tool":"edit","args":{"path":"docs/<think>x</think>.md",'
        '"old_string":"old","new_string":"<think>keep</think>"}}'
    )

    assert [call.name for call in plan.calls] == ["edit"]
    assert plan.calls[0].args == {
        "path": "docs/<think>x</think>.md",
        "replacements": [{"search": "old", "replace": "<think>keep</think>"}],
    }
