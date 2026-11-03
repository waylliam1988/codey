"""Zen wire aliases preserve admitted definitions and exact call identity."""
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult
from codey.providers.zen.connection import ZenProvider


def test_zen_names_are_wire_only_and_preserve_exact_call_ids():
    class Runtime:
        def send_turn(self, prompt, tools, timeout=None, *, tool_call_observations="",
                      tool_call_history_groups=0, output_token_limit=0):
            assert (tool_call_observations, tool_call_history_groups, output_token_limit) == ("observed", 2, 128)
            assert [tool.name for tool in tools] == ["read", "done", "shell"]
            assert "unavailable" in tools[-1].description.lower()
            assert tools[0].parameters == {"type": "object"}
            return AssistantTurn(tool_calls=(ProviderToolCall("exact", "read", {"path": "fixture"}),))

        def send_tool_results(self, results, tools, timeout=None, *, tool_call_observations="",
                              tool_call_history_groups=0, output_token_limit=0):
            assert (tool_call_observations, tool_call_history_groups, output_token_limit) == ("receipt", 1, 64)
            assert results == [ProviderToolResult("exact", "content")]
            assert [tool.name for tool in tools] == ["read", "done", "shell"]
            return AssistantTurn(text="received")

    provider = ZenProvider(Runtime())
    tools = [ProviderToolDefinition("read_file", "read", {"type": "object"}), ProviderToolDefinition("done", "done", {})]
    turn = provider.send_turn("read", tools, tool_call_observations="observed",
                              tool_call_history_groups=2, output_token_limit=128)
    assert turn.tool_calls == (ProviderToolCall("exact", "read_file", {"path": "fixture"}),)
    assert provider.send_tool_results([ProviderToolResult("exact", "content")], tools,
                                     tool_call_observations="receipt", tool_call_history_groups=1,
                                     output_token_limit=64).text == "received"
    assert [tool.name for tool in tools] == ["read_file", "done"]
