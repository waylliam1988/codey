"""Usage advertised by Local's supported OpenAI-compatible protocols."""
from __future__ import annotations

from typing import Any

from codey.providers.api_metering import UsageParser
from codey.providers.token_accounting import ReportedUsage


def configure_usage(payload: dict[str, Any]) -> None:
    if payload.get("stream") and "messages" in payload:
        payload["stream_options"] = {"include_usage": True}


def parser_for(protocol: str) -> UsageParser:
    if protocol == "openai-completions":
        return parse_chat_usage
    if protocol == "openai-responses":
        return parse_responses_usage
    raise ValueError("unsupported Local usage protocol")


def _details(usage: dict[str, Any], key: str) -> dict[str, Any]:
    details = usage.get(key, {})
    if details is None:
        return {}
    if not isinstance(details, dict):
        raise ValueError("invalid Local usage details")
    return details


def parse_chat_usage(event: dict[str, Any]) -> ReportedUsage | None:
    usage = event.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("invalid Local Chat usage")
    return ReportedUsage(usage.get("prompt_tokens"), usage.get("completion_tokens"),
                         _details(usage, "prompt_tokens_details").get("cached_tokens"),
                         reasoning_output_tokens=_details(usage, "completion_tokens_details").get("reasoning_tokens"))


def parse_responses_usage(event: dict[str, Any]) -> ReportedUsage | None:
    response = event.get("response", event)
    if not isinstance(response, dict):
        raise ValueError("invalid Local Responses usage envelope")
    usage = response.get("usage")
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("invalid Local Responses usage")
    return ReportedUsage(usage.get("input_tokens"), usage.get("output_tokens"),
                         _details(usage, "input_tokens_details").get("cached_tokens"),
                         reasoning_output_tokens=_details(usage, "output_tokens_details").get("reasoning_tokens"))
