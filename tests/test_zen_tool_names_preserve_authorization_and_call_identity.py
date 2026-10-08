"""Zen wire aliases preserve admitted definitions and exact call identity."""
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult
from codey.providers.zen.connection import ZenProvider


def test_zen_names_are_wire_only_and_preserve_exact_call_ids():
    class Runtime:
        def send_turn(self, prompt, tools, timeout=None):
            assert [tool.name for tool in tools] == ["read", "done", "shell"]
            assert "unavailable" in tools[-1].description.lower()
            assert tools[0].parameters == {"type": "object"}
            return AssistantTurn(tool_calls=(ProviderToolCall("exact", "read", {"path": "fixture"}),))

        def send_tool_results(self, results, tools, timeout=None):
            assert results == [ProviderToolResult("exact", "content")]
            assert [tool.name for tool in tools] == ["read", "done", "shell"]
            return AssistantTurn(text="received")

    provider = ZenProvider(Runtime())
    tools = [ProviderToolDefinition("read_file", "read", {"type": "object"}), ProviderToolDefinition("done", "done", {})]
    turn = provider.send_turn("read", tools)
    assert turn.tool_calls == (ProviderToolCall("exact", "read_file", {"path": "fixture"}),)
    assert provider.send_tool_results([ProviderToolResult("exact", "content")], tools).text == "received"
    assert [tool.name for tool in tools] == ["read_file", "done"]
