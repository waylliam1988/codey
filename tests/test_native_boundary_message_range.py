from types import SimpleNamespace

from codey.operations.kernel_transport import _native_tool_results
from codey.providers import api_chat, api_responses
from codey.providers.base import ProviderToolResult


def test_native_boundary_input_is_carried_after_tool_result_in_one_message_range() -> None:
    result = SimpleNamespace(
        call=SimpleNamespace(call_id="call-1", name="read_file"),
        model_text="tool output", truncated=False,
    )
    messages = _native_tool_results(
        [result], SimpleNamespace(_memory_results={}, search_results={}), boundary_prompt="steer now",
    )

    assert messages == [
        ProviderToolResult("call-1", "tool output", user_content="steer now"),
    ]
    assert api_chat.encode_results(messages) == [
        {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
        {"role": "user", "content": "steer now"},
    ]
    assert api_responses.encode_results(messages) == [
        {"type": "function_call_output", "call_id": "call-1", "output": "tool output"},
        {"type": "message", "role": "user", "content": "steer now"},
    ]
