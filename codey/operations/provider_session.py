"""Finite provider wrappers and validated reply accounting."""

from __future__ import annotations

import contextlib
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Protocol

from codey.providers.base import AssistantTurn, ProviderToolDefinition, ProviderToolResult
from codey.providers.compat_transport import (
    declared_callable,
    execute_compat_request,
    optional_callable,
    provider_attribute,
    require_callable,
)
from codey.providers.request_codec import decode_provider_request, encode_provider_request
from codey.providers.requests import (
    AcknowledgeToolResultsBoundRequest,
    AcknowledgeToolResultsRequest,
    ProviderRequest,
    ProviderRequestOptions,
    SendTextRequest,
    SendToolResultsBoundRequest,
    SendToolResultsRequest,
    SendTurnRequest,
)
from codey.providers.response_record import decode_reply, encode_reply, json_value
from codey.providers.text_result_views import TextResultSpan
from codey.providers.token_accounting import ApiExchangeUsage, ContextBudget
from codey.providers.transport import ProviderTransport
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.types.json_value import JsonObject


class ConversationRecorder(Protocol):
    def begin_window(self, provider_id: str, mode: str, project: str) -> None: ...
    def record_exchange(self, prompt: str, reply: str) -> None: ...


BeforeSend = Callable[[ProviderRequest], None]


def bind_api_usage(provider: object, connection_id: str, trace: object) -> None:
    if isinstance(provider_attribute(provider, "context_budget"), ContextBudget):
        sink = FailOpenPromptTrace(trace)

        def record_usage(record: ApiExchangeUsage) -> None:
            sink.call("record_api_usage", record)

        bind = optional_callable(provider, "bind_usage")
        if bind is None:
            raise TypeError("provider context budget requires usage binding")
        bind(connection_id, record_usage)


def reply_text_for_accounting(reply: object) -> str:
    if isinstance(reply, str):
        return reply
    if not isinstance(reply, AssistantTurn):
        raise ValueError("provider returned an unsupported response type")
    # Validate the external object, including nested arguments, before accounting.
    decode_reply(encode_reply(reply))
    return json.dumps({"text": reply.text, "tool_calls": [
        {"name": call.name, "args": call.arguments, "call_id": call.id} for call in reply.tool_calls
    ]}, ensure_ascii=False)


def _explicit_reply_normalizer(provider: object) -> Callable[..., object] | None:
    return declared_callable(provider, "normalize_reply", forwarding_owner=ProviderAdapter)


def normalize_provider_reply(provider: object, reply: object) -> AssistantTurn | str:
    """Apply a declared hook exactly once, then validate and detach its reply."""
    normalize = _explicit_reply_normalizer(provider)
    value = normalize(reply) if normalize is not None else reply
    return decode_reply(encode_reply(value))


def provider_reasoning(provider: object, reply: str) -> str:
    hook = declared_callable(provider, "reasoning_for_reply", forwarding_owner=ProviderAdapter)
    value = hook(reply) if hook is not None else ""
    return value.strip() if isinstance(value, str) else ""


def request_text_for_accounting(request: ProviderRequest) -> str:
    if isinstance(request, (SendTextRequest, SendTurnRequest)):
        return request.prompt
    return json.dumps(json_value(request.results), ensure_ascii=False)


class ProviderAdapter(ProviderTransport):
    """Explicit legacy facade; wrappers intercept one finite request port."""

    def __init__(self, provider: object) -> None:
        self.provider = provider

    def execute_request(self, request: ProviderRequest) -> object:
        return execute_compat_request(self.provider, request)

    def _validated_request(self, request: ProviderRequest) -> object:
        return self.execute_request(decode_provider_request(encode_provider_request(request)))

    def send(self, text: str, timeout: float | None = None, *, tool_call_json: bool = False,
             tool_call_schema: JsonObject | None = None, tool_call_contract: str = "",
             tool_call_results: Sequence[TextResultSpan] | None = None, tool_call_observations: str = "",
             tool_call_history_groups: int = 0, output_token_limit: int = 0) -> object:
        return self._validated_request(SendTextRequest(text, ProviderRequestOptions(
            timeout=timeout, tool_call_json=tool_call_json, tool_call_schema=tool_call_schema,
            tool_call_contract=tool_call_contract,
            tool_call_results=None if tool_call_results is None else tuple(tool_call_results),
            tool_call_observations=tool_call_observations, tool_call_history_groups=tool_call_history_groups,
            output_token_limit=output_token_limit)))

    def send_turn(self, prompt: str, tools: Sequence[ProviderToolDefinition] | None = None,
                  timeout: float | None = None, *, tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> object:
        return self._validated_request(SendTurnRequest(prompt, None if tools is None else tuple(tools),
            ProviderRequestOptions(timeout=timeout, tool_call_observations=tool_call_observations,
                tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
                require_tool_call=require_tool_call)))

    def send_tool_results(self, results: Sequence[ProviderToolResult], tools: Sequence[ProviderToolDefinition] | None = None,
                          timeout: float | None = None, *, tool_call_observations: str = "",
                          tool_call_history_groups: int = 0, output_token_limit: int = 0,
                          require_tool_call: bool = False) -> object:
        return self._validated_request(SendToolResultsRequest(tuple(results), None if tools is None else tuple(tools),
            ProviderRequestOptions(timeout=timeout, tool_call_observations=tool_call_observations,
                tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
                require_tool_call=require_tool_call)))

    def acknowledge_tool_results(self, results: Sequence[ProviderToolResult], declared_tools: Sequence[ProviderToolDefinition],
                                 timeout: float | None = None) -> object:
        return self._validated_request(AcknowledgeToolResultsRequest(tuple(results), tuple(declared_tools),
            ProviderRequestOptions(timeout=timeout)))

    def send_tool_results_bound(self, results: Sequence[ProviderToolResult], window_ref: ProtocolWindowRef,
                                tools: Sequence[ProviderToolDefinition] | None = None, timeout: float | None = None,
                                delivery_commit: JsonObject | None = None, tool_call_observations: str = "",
                                tool_call_history_groups: int = 0, output_token_limit: int = 0,
                                require_tool_call: bool = False) -> object:
        return self._validated_request(SendToolResultsBoundRequest(tuple(results), window_ref,
            None if tools is None else tuple(tools), ProviderRequestOptions(timeout=timeout,
                delivery_commit=delivery_commit, tool_call_observations=tool_call_observations,
                tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
                require_tool_call=require_tool_call)))

    def acknowledge_tool_results_bound(self, results: Sequence[ProviderToolResult], window_ref: ProtocolWindowRef,
                                       declared_tools: Sequence[ProviderToolDefinition], timeout: float | None = None) -> object:
        return self._validated_request(AcknowledgeToolResultsBoundRequest(tuple(results), window_ref, tuple(declared_tools),
            ProviderRequestOptions(timeout=timeout)))

    def new_chat(self, timeout: float | None = None) -> object:
        call = require_callable(self.provider, "new_chat")
        return call() if timeout is None else call(timeout=timeout)

    def close(self) -> object:
        return require_callable(self.provider, "close")()

    def normalize_reply(self, reply: object) -> AssistantTurn | str:
        return normalize_provider_reply(self.provider, reply)

    def reasoning_for_reply(self, reply: str) -> str:
        return provider_reasoning(self.provider, reply)


class ConversationProvider(ProviderAdapter):
    def __init__(self, provider: object, conversation: ConversationRecorder, *,
                 fresh_window: tuple[str, str, str] | None = None) -> None:
        super().__init__(provider)
        self.conversation = conversation
        self.fresh_window = fresh_window

    def new_chat(self, timeout: float | None = None) -> object:
        result = super().new_chat(timeout)
        if self.fresh_window is not None:
            self.conversation.begin_window(*self.fresh_window)
        return result

    def execute_request(self, request: ProviderRequest) -> object:
        reply = super().execute_request(request)
        with contextlib.suppress(Exception):
            self.conversation.record_exchange(request_text_for_accounting(request), reply_text_for_accounting(reply))
        return reply


class DeadlineProvider(ProviderAdapter):
    """One episode deadline applies to all six protocols, including positional timeout."""

    def __init__(self, provider: object, *, timeout: float) -> None:
        try:
            valid = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("provider episode timeout must be positive finite seconds")
        super().__init__(provider)
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout

    def execute_request(self, request: ProviderRequest) -> object:
        remaining = min(self.timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("provider episode deadline exceeded")
        requested = request.options.timeout
        timeout = min(remaining, requested) if requested is not None else remaining
        return super().execute_request(replace(request, options=replace(request.options,
            timeout=timeout, supplied=request.options.supplied | {"timeout"})))


class ObservedProvider(ProviderAdapter):
    """Observe the typed actual request, after durable intent creation."""

    def __init__(self, provider: object, before_send: BeforeSend) -> None:
        super().__init__(provider)
        self.before_send = before_send

    def execute_request(self, request: ProviderRequest) -> object:
        self.before_send(request)
        return super().execute_request(request)


def adapt_provider(provider: object) -> ProviderTransport:
    return provider if isinstance(provider, ProviderTransport) else ProviderAdapter(provider)
