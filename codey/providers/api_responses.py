"""Stateless Responses items. Call identity and reasoning are replayed verbatim."""
from __future__ import annotations

import json
from typing import Any

from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.providers.error_classification import ContextOverflowError, OutputLengthError, RequestPrepError
from codey.toolchain.tool_spec import thaw_schema_value


def encode_tools(tools: list[ProviderToolDefinition] | None) -> list[dict[str, Any]]:
    return [{"type": "function", "name": t.name, "description": t.description,
             "parameters": thaw_schema_value(t.parameters), "strict": False} for t in tools or []]


def encode_results(results: list[ProviderToolResult]) -> list[dict[str, Any]]:
    return [{"type": "function_call_output", "call_id": r.call_id, "output": r.content} for r in results]


def prepare(history: list[dict[str, Any]], pending: list[dict[str, Any]], *, system: str,
            tools: list[ProviderToolDefinition] | None, budget: tuple[int, int, int]) -> list[dict[str, Any]]:
    from codey.agents.handoff import estimate_tokens

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
    window, reserve, keep = budget
    declarations_size = estimate_tokens(json.dumps(encode_tools(tools)))

    def size(items: list[dict[str, Any]]) -> int:
        return estimate_tokens(json.dumps(items, ensure_ascii=False)) + declarations_size

    if size(candidate) + reserve <= window:
        return candidate
    # A user request begins one indivisible exchange (including its chained
    # reasoning, calls, and results). Never trim inside the current exchange.
    starts = [i for i, item in enumerate(candidate) if item.get("role") == "user"]
    prefix = [item for item in candidate[:starts[0] if starts else 0] if item.get("role") in {"system", "developer"}]
    for start in starts[1:]:
        compacted = prefix + candidate[start:]
        if size(compacted) <= min(keep + declarations_size, window - reserve):
            return compacted
    if starts and size(prefix + candidate[starts[-1]:]) + reserve <= window:
        return prefix + candidate[starts[-1]:]
    if size(candidate) + reserve > window:
        raise ContextOverflowError("Responses context overflow before send")
    return candidate


def payload(items: list[dict[str, Any]], tools: list[ProviderToolDefinition] | None, *, model: str,
            stream: bool, effort: str | None, choice: str, output_tokens: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "input": items, "stream": stream, "store": False,
                            "include": ["reasoning.encrypted_content"]}
    if effort is not None:
        body["reasoning"] = {"effort": "none" if effort == "off" else effort}
    if output_tokens is not None:
        body["max_output_tokens"] = output_tokens
    if tools:
        body.update(tools=encode_tools(tools), tool_choice=choice, parallel_tool_calls=False)
    return body


def decode(body: dict[str, Any]) -> tuple[AssistantTurn, list[dict[str, Any]]]:
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
