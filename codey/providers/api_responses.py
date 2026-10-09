"""Stateless Responses items. Call identity and reasoning are replayed verbatim."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from codey.providers.api_codec import GenerationSettings
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.providers.error_classification import OutputLengthError, RequestPrepError
from codey.toolchain.tool_spec import thaw_schema_value

endpoint = "responses"


def encode_tools(tools: list[ProviderToolDefinition] | None) -> list[dict[str, Any]]:
    return [{"type": "function", "name": t.name, "description": t.description,
             "parameters": thaw_schema_value(t.parameters), "strict": False} for t in tools or []]


def encode_results(results: list[ProviderToolResult]) -> list[dict[str, Any]]:
    return [{"type": "function_call_output", "call_id": r.call_id, "output": r.content} for r in results]


def prepare(history: list[dict[str, Any]], pending: list[dict[str, Any]], *, system: str,
            tools: list[ProviderToolDefinition] | None) -> list[dict[str, Any]]:
    candidate = [dict(item) for item in history]
    if not candidate and system:
        candidate.append({"role": "system", "content": system})
    candidate.extend(pending)
    calls: set[str] = set()
    for item in candidate:
        kind, identity = item.get("type"), item.get("call_id")
        if kind == "function_call":
            if not isinstance(identity, str) or not identity or identity in calls:
                raise RequestPrepError("Responses history has duplicate or missing call IDs")
            calls.add(identity)
        elif kind == "function_call_output":
            if identity not in calls:
                raise RequestPrepError("Responses history has an unpaired result")
            calls.remove(identity)
        elif calls and kind != "reasoning" and not (kind == "message" and item.get("role") == "assistant"):
            raise RequestPrepError("Responses history has unanswered calls")
    if calls:
        raise RequestPrepError("Responses history has unanswered calls")
    return candidate


def compact(items: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    starts = [i for i, item in enumerate(items) if item.get("role") == "user"]
    if len(starts) < 2:
        return None
    prefix = [item for item in items[:starts[0]] if item.get("role") in {"system", "developer"}]
    return prefix + items[starts[1]:]


def build_payload(items: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None,
                  settings: GenerationSettings) -> dict[str, Any]:
    body: dict[str, Any] = {"model": settings.model, "input": items, "stream": settings.stream, "store": False,
                            "include": ["reasoning.encrypted_content"]}
    if settings.effort is not None:
        body["reasoning"] = {"effort": "none" if settings.effort == "off" else settings.effort}
    if settings.output_tokens is not None:
        body["max_output_tokens"] = settings.output_tokens
    if tools:
        body.update(tools=encode_tools(tools), tool_choice=settings.choice, parallel_tool_calls=False)
    return body


def decode_exchange(body: dict[str, Any], *, text_only: bool,
                    text_decoder: Callable[[str], str | AssistantTurn] | None,
                    ) -> tuple[AssistantTurn, list[dict[str, Any]]]:
    status = body.get("status")
    limited = status == "incomplete" and (body.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
    if status != "completed" and not limited:
        raise RuntimeError(f"Responses generation did not complete: {status}: {body.get('error')}")
    output = body.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise RuntimeError("Responses returned malformed output items")
    calls: list[ProviderToolCall] = []
    text: list[str] = []
    reasoning: list[str] = []
    for item in output:
        kind = item.get("type")
        if not limited and "status" in item and item["status"] != "completed":
            raise RuntimeError(f"Responses output item is not complete: {item['status']}")
        if kind == "function_call":
            if limited:
                raise OutputLengthError("Responses tool output truncated; refusing partial tool execution")
            call_id, name = item.get("call_id"), item.get("name")
            try:
                arguments = json.loads(item.get("arguments", ""))
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Responses returned invalid function arguments") from exc
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or not isinstance(arguments, dict):
                raise RuntimeError("Responses returned malformed function call")
            if any(call.id == call_id for call in calls):
                raise RuntimeError("Responses returned duplicate call IDs")
            calls.append(ProviderToolCall(call_id, name, arguments))
        elif kind == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text.append(str(part.get("text", "")))
                elif part.get("type") == "refusal":
                    raise RuntimeError("Responses model refused the request")
        elif kind == "reasoning":
            reasoning.extend(str(part.get("text", "")) for part in item.get("summary", []))
        else:
            raise RuntimeError(f"unsupported Responses output item: {kind}")
    turn = AssistantTurn(text="\n".join(text), tool_calls=tuple(calls), reasoning="\n".join(reasoning),
                         finish=TurnFinish.OUTPUT_LIMIT if limited else TurnFinish.COMPLETE)
    return turn, output
