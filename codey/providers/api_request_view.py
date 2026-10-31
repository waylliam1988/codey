"""Pure request projection over complete codec groups; never writes history."""

from typing import Any

from codey.providers.api_codec import ApiCodec
from codey.providers.base import ProviderToolDefinition


def bounded_history(codec: ApiCodec, messages: list[dict[str, Any]], groups: int,
                    tools: list[ProviderToolDefinition] | None) -> list[dict[str, Any]]:
    spans = codec.closed_spans(messages)
    start = spans[-groups][0] if len(spans) > groups else 0
    if start < len(messages) and messages[start].get("type") == "function_call":
        # Responses can put assistant commentary between its reasoning item
        # and function call. Keep that response's reasoning with the call.
        while start and (messages[start - 1].get("type") == "reasoning"
                         or messages[start - 1].get("role") == "assistant"):
            start -= 1
    view = [*[item for item in messages[:start] if item.get("role") in {"system", "developer"}],
            *messages[start:]]
    codec.prepare([], view, system="", tools=tools)
    return view
