"""Stateless Responses items. Call identity and reasoning are replayed verbatim."""
from __future__ import annotations

from collections.abc import Callable

from codey.providers.api_codec import GenerationSettings
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.providers.error_classification import OutputLengthError, RequestPrepError
from codey.toolchain.tool_spec import thaw_schema_value
from codey.types.json_value import decode_json_object, parse_json_object

endpoint = "responses"


def encode_context(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Translate canonical tool pairs into Responses function items."""
    from codey.providers.api_chat import encode_context as canonical_messages

    result: list[dict[str, object]] = []
    for message in canonical_messages(messages):
        if message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise RequestPrepError("durable tool result has no call ID")
            result.append({"type": "function_call_output", "call_id": call_id, "output": message.get("content", "")})
        elif message.get("role") == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                result.append({"role": "assistant", "content": message["content"]})
            raw_calls = message["tool_calls"]
            if not isinstance(raw_calls, list):
                raise RequestPrepError("durable function calls are malformed")
            for raw_call in raw_calls:
                call = decode_json_object(raw_call)
                function = decode_json_object(call.get("function", {}))
                if (not call.get("id") or not function.get("name") or not isinstance(function.get("arguments"), str)):
                    raise RequestPrepError("durable function call is malformed")
                result.append({"type": "function_call", "call_id": call["id"], "name": function["name"],
                               "arguments": function["arguments"]})
        else:
            result.append(message)
    return result


def pending_calls(items: list[dict[str, object]]) -> tuple[str, ...]:
    open_calls: list[str] = []
    for item in items:
        kind = item.get("type")
        call_id = item.get("call_id")
        if kind == "function_call" and isinstance(call_id, str) and call_id:
            open_calls.append(call_id)
        elif kind == "function_call_output" and isinstance(call_id, str) and call_id in open_calls:
            open_calls.remove(call_id)
    return tuple(open_calls)


def encode_tools(tools: list[ProviderToolDefinition] | None) -> list[dict[str, object]]:
    return [{"type": "function", "name": t.name, "description": t.description,
             "parameters": thaw_schema_value(t.parameters), "strict": False} for t in tools or []]


def encode_results(results: list[ProviderToolResult]) -> list[dict[str, object]]:
    encoded: list[dict[str, object]] = []
    for result in results:
        encoded.append({"type": "function_call_output", "call_id": result.call_id, "output": result.content})
        if result.user_content:
            encoded.append({"type": "message", "role": "user", "content": result.user_content})
    return encoded


def prepare(history: list[dict[str, object]], pending: list[dict[str, object]], *, system: str,
            tools: list[ProviderToolDefinition] | None) -> list[dict[str, object]]:
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
            if not isinstance(identity, str) or identity not in calls:
                raise RequestPrepError("Responses history has an unpaired result")
            calls.remove(identity)
        elif calls and kind != "reasoning" and not (kind == "message" and item.get("role") == "assistant"):
            raise RequestPrepError("Responses history has unanswered calls")
    if calls:
        raise RequestPrepError("Responses history has unanswered calls")
    return candidate


def validate_view(items: list[dict[str, object]]) -> None:
    calls = {item.get("call_id") for item in items if item.get("type") == "function_call"}
    answered = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    additions = [{"type": "function_call_output", "call_id": identity, "output": ""}
                 for identity in calls - answered]
    prepare(items, additions, system="", tools=None)


def closed_spans(items: list[dict[str, object]]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    calls: set[str] = set()
    for index, item in enumerate(items):
        kind = item.get("type")
        if kind == "function_call":
            identity = item.get("call_id")
            if not isinstance(identity, str) or not identity or identity in calls:
                raise RequestPrepError("Responses history has invalid call IDs")
            calls.add(identity)
        elif kind == "function_call_output":
            identity = item.get("call_id")
            if not isinstance(identity, str) or identity not in calls:
                raise RequestPrepError("Responses history has unpaired results")
            calls.remove(identity)
        if not calls and kind != "reasoning":
            if items[start].get("role") not in {"system", "developer"}:
                spans.append((start, index + 1))
            start = index + 1
    return spans


def checkpoint_item(text: str) -> dict[str, object]:
    return {"role": "assistant", "content": "Current project state:\n" + text}


def build_payload(items: list[dict[str, object]], tools: list[ProviderToolDefinition] | None,
                  settings: GenerationSettings) -> dict[str, object]:
    body: dict[str, object] = {"model": settings.model, "input": items, "stream": settings.stream, "store": False,
                            "include": ["reasoning.encrypted_content"]}
    if settings.effort is not None:
        body["reasoning"] = {"effort": "none" if settings.effort == "off" else settings.effort}
    if settings.output_tokens is not None:
        body["max_output_tokens"] = settings.output_tokens
    if tools:
        body.update(tools=encode_tools(tools), tool_choice=settings.choice, parallel_tool_calls=False)
    return body


def decode_exchange(body: dict[str, object], *, text_only: bool,
                    text_decoder: Callable[[str], str | AssistantTurn] | None,
                    ) -> tuple[AssistantTurn, list[dict[str, object]]]:
    status = body.get("status")
    details = body.get("incomplete_details")
    limited = status == "incomplete" and isinstance(details, dict) and details.get("reason") == "max_output_tokens"
    if status != "completed" and not limited:
        raise RuntimeError(f"Responses generation did not complete: {status}: {body.get('error')}")
    output = body.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise RuntimeError("Responses returned malformed output items")
    decoded_output: list[dict[str, object]] = [dict(decode_json_object(item)) for item in output]
    calls: list[ProviderToolCall] = []
    text: list[str] = []
    reasoning: list[str] = []
    for item in decoded_output:
        kind = item.get("type")
        if not limited and "status" in item and item["status"] != "completed":
            raise RuntimeError(f"Responses output item is not complete: {item['status']}")
        if kind == "function_call":
            if limited:
                raise OutputLengthError("Responses tool output truncated; refusing partial tool execution")
            call_id, name = item.get("call_id"), item.get("name")
            try:
                arguments = parse_json_object(str(item.get("arguments", "")))
            except (ValueError, TypeError, RecursionError) as exc:
                raise RuntimeError("Responses returned invalid function arguments") from exc
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or not isinstance(arguments, dict):
                raise RuntimeError("Responses returned malformed function call")
            if any(call.id == call_id for call in calls):
                raise RuntimeError("Responses returned duplicate call IDs")
            calls.append(ProviderToolCall(call_id, name, arguments))
        elif kind == "message":
            content = item.get("content", [])
            if not isinstance(content, list):
                raise RuntimeError("Responses returned malformed message content")
            for raw_part in content:
                part = decode_json_object(raw_part)
                if part.get("type") == "output_text":
                    text.append(str(part.get("text", "")))
                elif part.get("type") == "refusal":
                    raise RuntimeError("Responses model refused the request")
        elif kind == "reasoning":
            summary = item.get("summary", [])
            if not isinstance(summary, list):
                raise RuntimeError("Responses returned malformed reasoning summary")
            reasoning.extend(str(decode_json_object(part).get("text", "")) for part in summary)
        else:
            raise RuntimeError(f"unsupported Responses output item: {kind}")
    turn = AssistantTurn(text="\n".join(text), tool_calls=tuple(calls), reasoning="\n".join(reasoning),
                         finish=TurnFinish.OUTPUT_LIMIT if limited else TurnFinish.COMPLETE)
    return turn, decoded_output
