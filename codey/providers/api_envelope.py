"""Build the API request envelope from its codec and connection configuration."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from codey.providers import api_messages
from codey.providers.api_codec import ApiCodec, GenerationSettings
from codey.providers.api_request_policy import RequestContext
from codey.providers.base import ProviderToolDefinition
from codey.types.json_value import decode_json_object


class RequestConfiguration(Protocol):
    @property
    def _codec(self) -> ApiCodec: ...
    @property
    def api_protocol(self) -> str: ...
    @property
    def api_key(self) -> str: ...
    @property
    def base_url(self) -> str: ...
    @property
    def request_headers(self) -> Callable[[str], Mapping[str, str]] | None: ...
    @property
    def request_context_headers(self) -> Callable[[RequestContext], Mapping[str, str]] | None: ...
    @property
    def configure_request(self) -> Callable[[dict[str, object]], None]: ...


def payload(self: RequestConfiguration, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None,
             settings: GenerationSettings, *, clock: str | None = None) -> dict[str, object]:
    from codey.providers.request_context import clock_context

    view = messages
    if settings.tool_history_groups:
        from codey.providers.api_request_view import bounded_history

        # Select complete protocol groups, including native call/result
        # identities and Responses reasoning. The original history and
        # durable receipts remain intact; only this request is projected.
        view = bounded_history(self._codec, messages, settings.tool_history_groups, tools)
    payload = self._codec.build_payload(view, tools, settings)
    key = "input" if self.api_protocol == "openai-responses" else "messages"
    from codey.providers.text_result_views import TEXT_RESULTS_KEY

    # View provenance is local metadata, never an API message property.
    raw_items = payload.get(key)
    if not isinstance(raw_items, list):
        raise ValueError("API codec did not produce its history array")
    items: list[dict[str, object]] = [
        {name: value for name, value in decode_json_object(item).items()
         if name not in {TEXT_RESULTS_KEY, "_codey_attachment_entries"}} for item in raw_items]
    from codey.providers.task_prompt import project_retry_tasks

    project_retry_tasks(items)
    if settings.text_tool_contract:
        # Frozen request declarations, like native tools, belong to this
        # payload only. Never append obsolete tool catalogs to history.
        if items and items[0].get("role") == "system" and isinstance(content := items[0].get("content"), str):
            items[0]["content"] = content + "\n\n" + settings.text_tool_contract
        else:
            items.insert(0, {"role": "system", "content": settings.text_tool_contract})
    if settings.text_tool_observations:
        # A receipt-derived snapshot describes this request. Keep it
        # separate from history and before the current result or repair.
        index = len(items) - 1 if items and items[-1].get("role") == "user" else len(items)
        items.insert(index, {"role": "user", "content": settings.text_tool_observations})
    items.append({"role": "user", "content": clock if clock is not None else clock_context()})
    if self.api_protocol == "anthropic-messages":
        api_messages.finalize_messages(payload, items)
    else:
        payload[key] = items
    if settings.text_tool_json and self.api_protocol != "anthropic-messages":
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "text_tool_call", "strict": True, "schema": settings.text_tool_schema or {"type": "object", "properties": {
                "tool": {"type": "string"}, "args": {"type": "object"}},
                "required": ["tool", "args"], "additionalProperties": False}}}
    self.configure_request(payload)
    return payload


def headers(self: RequestConfiguration, context: RequestContext | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if self.api_protocol == "anthropic-messages":
        headers["anthropic-version"] = "2023-06-01"
    if self.api_key:
        if self.api_protocol == "anthropic-messages":
            headers["x-api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
    if self.request_headers is not None:
        headers.update(self.request_headers(self.base_url))
    if context is not None and self.request_context_headers is not None:
        headers.update(self.request_context_headers(context))
    return headers
