"""Usage advertised by Local's supported OpenAI-compatible protocols."""
from __future__ import annotations

from codey.providers.api_metering import UsageParser
from codey.providers.token_accounting import ReportedUsage
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer


def configure_usage(payload: dict[str, object], *, protocol: str = "openai-completions") -> None:
    if protocol == "openai-completions" and payload.get("stream") and "messages" in payload:
        payload["stream_options"] = {"include_usage": True}


def parser_for(protocol: str) -> UsageParser:
    if protocol == "openai-completions":
        return parse_chat_usage
    if protocol == "openai-responses":
        return parse_responses_usage
    if protocol == "anthropic-messages":
        return parse_messages_usage
    raise ValueError("unsupported Local usage protocol")


def _details(usage: JsonObject, key: str) -> JsonObject:
    details = usage.get(key, {})
    if details is None:
        return {}
    if not isinstance(details, dict):
        raise ValueError("invalid Local usage details")
    return decode_json_object(details)


def _token(usage: JsonObject, key: str) -> int | None:
    value = usage.get(key)
    return require_json_integer(value, key) if value is not None else None


def parse_chat_usage(event: dict[str, object]) -> ReportedUsage | None:
    usage = event.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("invalid Local Chat usage")
    data = decode_json_object(usage)
    return ReportedUsage(_token(data, "prompt_tokens"), _token(data, "completion_tokens"),
                         _token(_details(data, "prompt_tokens_details"), "cached_tokens"),
                         reasoning_output_tokens=_token(_details(data, "completion_tokens_details"), "reasoning_tokens"))


def parse_responses_usage(event: dict[str, object]) -> ReportedUsage | None:
    response = event.get("response", event)
    if not isinstance(response, dict):
        raise ValueError("invalid Local Responses usage envelope")
    usage = response.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("invalid Local Responses usage")
    data = decode_json_object(usage)
    return ReportedUsage(_token(data, "input_tokens"), _token(data, "output_tokens"),
                         _token(_details(data, "input_tokens_details"), "cached_tokens"),
                         reasoning_output_tokens=_token(_details(data, "output_tokens_details"), "reasoning_tokens"))


def parse_messages_usage(event: dict[str, object]) -> ReportedUsage | None:
    if event.get("type") != "message" or event.get("usage") is None:
        return None
    data = decode_json_object(event["usage"])
    fresh = _token(data, "input_tokens")
    cached = _token(data, "cache_read_input_tokens")
    written = _token(data, "cache_creation_input_tokens")
    total = fresh + (cached or 0) + (written or 0) if fresh is not None else None
    return ReportedUsage(total, _token(data, "output_tokens"), cached, written)
