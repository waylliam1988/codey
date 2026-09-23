from __future__ import annotations

from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.protocols import JsonToolCodec
from codey.research.tool_contract import render_openai_tools


def test_research_native_web_search() -> None:
    codec = JsonToolCodec()
    turn = AssistantTurn(
        text="",
        tool_calls=(ProviderToolCall(id="c1", name="web_search", arguments={"query": "helium"}),),
    )
    plan = codec.parse_turn(turn)
    assert not plan.protocol_error
    assert [c.name for c in plan.calls] == ["web_search"]
    assert plan.calls[0].call_id == "c1"


def test_research_native_done() -> None:
    codec = JsonToolCodec()
    turn = AssistantTurn(
        text="",
        tool_calls=(ProviderToolCall(id="c2", name="done", arguments={"answer": "report"}),),
    )
    plan = codec.parse_turn(turn)
    assert plan.control is not None and plan.control.kind == "done"


def test_research_openai_tools_closed_schema() -> None:
    tools = render_openai_tools()
    by_name = {str(t["function"]["name"]): t["function"] for t in tools}
    assert {"web_search", "open_url", "done"} <= set(by_name)
    assert by_name["web_search"]["parameters"]["additionalProperties"] is False
    assert "query" in by_name["web_search"]["parameters"]["required"]


def test_research_tool_messages_roundtrip() -> None:
    from codey.runtime.core.models import ToolCall, ToolResult

    codec = JsonToolCodec()
    result = ToolResult(call=ToolCall(name="web_search", args={"query": "x"}, call_id="c1"), model_text="hits")
    messages = codec.tool_messages([result])
    assert messages == [{"role": "tool", "tool_call_id": "c1", "content": "hits"}]
