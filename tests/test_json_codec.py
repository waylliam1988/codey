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
