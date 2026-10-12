"""Stable protocol error kinds shared by repair and execution boundaries."""

PROTOCOL_NO_JSON = "no_json"
PROTOCOL_UNKNOWN_TOOL = "unknown_tool"
PROTOCOL_INVALID_ARGS = "invalid_args"
PROTOCOL_DIRECT_ANSWER = "direct_answer"
PROTOCOL_NATIVE_TOOL_DENIAL = "native_tool_denial"
PROTOCOL_NESTED_TOOL_IN_DONE = "nested_tool_in_done"
PROTOCOL_DISALLOWED_TOOL = "disallowed_tool"

__all__ = [
    "PROTOCOL_DIRECT_ANSWER",
    "PROTOCOL_DISALLOWED_TOOL",
    "PROTOCOL_INVALID_ARGS",
    "PROTOCOL_NATIVE_TOOL_DENIAL",
    "PROTOCOL_NESTED_TOOL_IN_DONE",
    "PROTOCOL_NO_JSON",
    "PROTOCOL_UNKNOWN_TOOL",
]
