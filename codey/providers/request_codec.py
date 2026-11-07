"""Decode legacy frozen invocations into finite requests; encode stable v1 wire.

Provider artifact envelopes own the persisted format version. Legacy invocation
fields are decoded here, never carried into the domain as args/kwargs bags.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields
from typing import cast

from codey.providers.base import ProviderToolDefinition, ProviderToolResult
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
from codey.providers.response_record import json_value
from codey.providers.text_result_views import decode_text_result_spans
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.toolchain.tool_spec import _freeze_schema_value
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)

_COMMON = frozenset({"timeout", "tool_call_observations", "tool_call_history_groups", "output_token_limit"})
_TEXT = _COMMON | {"tool_call_json", "tool_call_schema", "tool_call_contract", "tool_call_results"}
_NATIVE = _COMMON | {"require_tool_call"}
_POSITIONAL = {
    "send": ("text", "timeout"),
    "send_turn": ("prompt", "tools", "timeout"),
    "send_tool_results": ("results", "tools", "timeout"),
    "acknowledge_tool_results": ("results", "declared_tools", "timeout"),
    "send_tool_results_bound": ("results", "window_ref", "tools", "timeout", "delivery_commit",
        "tool_call_observations", "tool_call_history_groups", "output_token_limit", "require_tool_call"),
    "acknowledge_tool_results_bound": ("results", "window_ref", "declared_tools", "timeout"),
}


def _tools(value: object) -> tuple[ProviderToolDefinition, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("frozen provider declarations are not a list")
    tools = []
    for raw in value:
        row = decode_json_object(raw)
        if set(row) != {"name", "description", "parameters"}:
            raise ValueError("frozen provider declaration is invalid")
        schema = decode_json_object(row["parameters"])
        tools.append(ProviderToolDefinition(require_json_string(row["name"], "name"),
            require_json_string(row["description"], "description"),
            cast(Mapping[str, object], _freeze_schema_value(schema))))
    return tuple(tools)


def _results(value: object) -> tuple[ProviderToolResult, ...]:
    if not isinstance(value, list):
        raise ValueError("frozen provider results are not a list")
    results = []
    for raw in value:
        row = decode_json_object(raw)
        if set(row) != {"call_id", "content", "user_content"}:
            raise ValueError("frozen provider result is invalid")
        user = row["user_content"]
        results.append(ProviderToolResult(require_json_string(row["call_id"], "call_id"),
            require_json_string(row["content"], "content"),
            None if user is None else require_json_string(user, "user_content")))
    return tuple(results)


def decode_provider_options(value: object, *, method: str, prompt: str = "") -> ProviderRequestOptions:
    if method not in _POSITIONAL:
        raise ValueError("unsupported provider method")
    row = decode_json_object(value)
    allowed = (_TEXT if method == "send" else {"timeout"} if method.startswith("acknowledge_")
               else _NATIVE | {"delivery_commit"} if method == "send_tool_results_bound" else _NATIVE)
    if set(row) - allowed:
        raise ValueError(f"unsupported {method} options: {sorted(set(row) - allowed)}")
    timeout = row.get("timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("provider timeout must be positive finite seconds or null")
        try:
            finite = math.isfinite(timeout)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("provider timeout must be positive finite seconds or null")
    history = require_json_integer(row.get("tool_call_history_groups", 0), "tool_call_history_groups")
    output = require_json_integer(row.get("output_token_limit", 0), "output_token_limit")
    if not 0 <= history <= 2**63 - 1 or not 0 <= output <= 2**63 - 1:
        raise ValueError("provider counts must fit nonnegative int64")
    schema = row.get("tool_call_schema")
    commit = row.get("delivery_commit")
    spans = row.get("tool_call_results")
    return ProviderRequestOptions(
        supplied=frozenset(row), timeout=timeout,
        tool_call_observations=require_json_string(row.get("tool_call_observations", ""), "tool_call_observations"),
        tool_call_history_groups=history, output_token_limit=output,
        require_tool_call=require_json_boolean(row.get("require_tool_call", False), "require_tool_call"),
        tool_call_json=require_json_boolean(row.get("tool_call_json", False), "tool_call_json"),
        tool_call_schema=None if schema is None else decode_json_object(schema),
        tool_call_contract=require_json_string(row.get("tool_call_contract", ""), "tool_call_contract"),
        tool_call_results=None if spans is None else decode_text_result_spans(prompt, spans),
        delivery_commit=None if commit is None else decode_json_object(commit))


def provider_options_payload(options: ProviderRequestOptions) -> JsonObject:
    names = {field.name for field in fields(ProviderRequestOptions)} - {"supplied"}
    if options.supplied - names:
        raise ValueError("unknown provider option presence")
    # Finite field lookup belongs to the codec; the kernel consumes named fields.
    return {name: json_value(getattr(options, name)) for name in sorted(options.supplied)}


def decode_provider_request(payload: object) -> ProviderRequest:
    row = decode_json_object(payload)
    if "schema_version" in row and (type(row["schema_version"]) is not int or row["schema_version"] != 1):
        raise ValueError("unknown provider invocation version")
    method = require_json_string(row.get("method"), "method")
    args, kwargs = row.get("args"), row.get("kwargs")
    if method not in _POSITIONAL or not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ValueError("frozen provider invocation is invalid")
    names = _POSITIONAL[method]
    if len(args) > len(names):
        raise ValueError("too many provider positional arguments")
    arguments = dict(kwargs)
    for name, value in zip(names, args, strict=False):
        if name in arguments:
            raise ValueError(f"duplicate provider argument: {name}")
        arguments[name] = value
    prompt = require_json_string(arguments.pop("text" if method == "send" else "prompt", None), "prompt") \
        if method in {"send", "send_turn"} else ""
    tools = None if method == "send" else _tools(arguments.pop(
        "declared_tools" if method.startswith("acknowledge_") else "tools", None))
    results = _results(arguments.pop("results", None)) if method not in {"send", "send_turn"} else ()
    window = ProtocolWindowRef.from_payload(arguments.pop("window_ref", None)) if method.endswith("_bound") else None
    options = decode_provider_options(arguments, method=method, prompt=prompt)
    if method == "send":
        return SendTextRequest(prompt, options)
    if method == "send_turn":
        return SendTurnRequest(prompt, tools, options)
    if method == "send_tool_results":
        return SendToolResultsRequest(results, tools, options)
    if method == "send_tool_results_bound":
        assert window is not None
        return SendToolResultsBoundRequest(results, window, tools, options)
    if tools is None:
        raise ValueError("acknowledgement requires declared_tools")
    if method == "acknowledge_tool_results":
        return AcknowledgeToolResultsRequest(results, tools, options)
    assert window is not None
    return AcknowledgeToolResultsBoundRequest(results, window, tools, options)


def encode_provider_request(request: ProviderRequest) -> JsonObject:
    """Canonical legacy-v1 shape; does not rewrite an existing frozen artifact."""
    args: list[JsonValue]
    if isinstance(request, SendTextRequest):
        args = [request.prompt]
    elif isinstance(request, SendTurnRequest):
        args = [request.prompt, json_value(request.tools)]
    elif isinstance(request, (SendToolResultsBoundRequest, AcknowledgeToolResultsBoundRequest)):
        args = [json_value(request.results), json_value(request.window_ref), json_value(request.tools)]
    else:
        args = [json_value(request.results), json_value(request.tools)]
    return {"method": request.method, "args": args, "kwargs": provider_options_payload(request.options)}
