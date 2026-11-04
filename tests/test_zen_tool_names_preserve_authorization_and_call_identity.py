"""Zen wire aliases preserve admitted definitions and exact call identity."""
import pytest

from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult
from codey.providers.zen.connection import ZenProvider


@pytest.mark.parametrize("required", [False, True])
def test_zen_names_are_wire_only_and_preserve_exact_call_ids(required):
    class Runtime:
        def send_turn(self, prompt, tools, timeout=None, *, tool_call_observations="",
                      tool_call_history_groups=0, output_token_limit=0, require_tool_call=False):
            assert require_tool_call is required
            assert (tool_call_observations, tool_call_history_groups, output_token_limit) == ("observed", 2, 128)
            assert [tool.name for tool in tools] == ["read", "done", "shell"]
            assert "unavailable" in tools[-1].description.lower()
            assert tools[0].parameters == {"type": "object"}
            return AssistantTurn(tool_calls=(ProviderToolCall("exact", "read", {"path": "fixture"}),))

        def send_tool_results(self, results, tools, timeout=None, *, tool_call_observations="",
                              tool_call_history_groups=0, output_token_limit=0, require_tool_call=False):
            assert require_tool_call is required
            assert (tool_call_observations, tool_call_history_groups, output_token_limit) == ("receipt", 1, 64)
            assert results == [ProviderToolResult("exact", "content")]
            assert [tool.name for tool in tools] == ["read", "done", "shell"]
            return AssistantTurn(text="received")

        def send_tool_results_bound(self, results, window_ref, tools, timeout=None, delivery_commit=None, **options):
            assert window_ref == "original-window" and delivery_commit == {"receipt": "original"}
            return self.send_tool_results(results, tools, timeout=timeout, **options)

    provider = ZenProvider(Runtime())
    tools = [ProviderToolDefinition("read_file", "read", {"type": "object"}), ProviderToolDefinition("done", "done", {})]
    turn = provider.send_turn("read", tools, tool_call_observations="observed",
                              tool_call_history_groups=2, output_token_limit=128, require_tool_call=required)
    assert turn.tool_calls == (ProviderToolCall("exact", "read_file", {"path": "fixture"}),)
    assert provider.send_tool_results([ProviderToolResult("exact", "content")], tools,
                                     tool_call_observations="receipt", tool_call_history_groups=1,
                                     output_token_limit=64, require_tool_call=required).text == "received"
    assert provider.send_tool_results_bound([ProviderToolResult("exact", "content")], "original-window", tools,
        delivery_commit={"receipt": "original"}, tool_call_observations="receipt", tool_call_history_groups=1,
        output_token_limit=64, require_tool_call=required).text == "received"
    assert [tool.name for tool in tools] == ["read_file", "done"]
