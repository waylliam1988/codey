"""Bounded non-secret API selection admitted for one durable run."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from codey.runtime.core.reasoning import REASONING_EFFORTS
from codey.types.json_value import (
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)
from codey.types.protocol_window import API_PROTOCOLS


@dataclass(frozen=True)
class ApiRunSelection:
    connection_id: str
    connection_revision: str
    model_id: str
    protocol: str
    native_tools: bool
    context_window_tokens: int = 32768
    context_reserve_tokens: int = 8192
    context_keep_recent_tokens: int = 12000
    thinking_enabled: bool | None = None
    reasoning_effort: str | None = None
    stream: bool = False
    tool_choice: str = "auto"
    output_tokens: int | None = None
    token_counter: str = "estimated"
    budget_source: str = "configuration"
    input_limit_tokens: int | None = None
    text_tool_format: str = "plain"
    supports_images: bool | None = None

    def __post_init__(self) -> None:
        for value in (self.connection_id, self.connection_revision, self.model_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise ValueError("invalid API connection or model identity")
        if self.protocol not in API_PROTOCOLS:
            raise ValueError("unsupported API protocol")
        if self.text_tool_format not in {"plain", "json_schema"}:
            raise ValueError("unsupported text tool format")
        if self.text_tool_format == "json_schema" and (self.protocol != "openai-completions" or self.native_tools
                                                        or self.thinking_enabled is not False):
            raise ValueError("JSON tool format requires compatibility tools and thinking disabled")
        if not isinstance(self.token_counter, str) or not self.token_counter or len(self.token_counter) > 80:
            raise ValueError("unsupported request token counter")
        if not isinstance(self.budget_source, str) or not self.budget_source or len(self.budget_source) > 80:
            raise ValueError("invalid context budget source")
        if type(self.native_tools) is not bool or type(self.stream) is not bool:
            raise ValueError("API capability flags must be booleans")
        if self.supports_images is not None and type(self.supports_images) is not bool:
            raise ValueError("Image capability must be a boolean or unknown")
        if self.thinking_enabled is not None and type(self.thinking_enabled) is not bool:
            raise ValueError("thinking flag must be boolean")
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("unsupported reasoning effort")
        if self.tool_choice not in {"auto", "required"}:
            raise ValueError("unsupported tool choice")
        if any(type(n) is not int or n <= 0 for n in (self.context_window_tokens, self.context_reserve_tokens, self.context_keep_recent_tokens)):
            raise ValueError("invalid API context budget")
        if self.context_window_tokens <= self.context_reserve_tokens or self.context_keep_recent_tokens > self.context_window_tokens - self.context_reserve_tokens:
            raise ValueError("inconsistent API context budget")
        if self.input_limit_tokens is not None and (type(self.input_limit_tokens) is not int or self.input_limit_tokens <= 0):
            raise ValueError("invalid independent input limit")
        if self.output_tokens is not None and (type(self.output_tokens) is not int or self.output_tokens <= 0
                                               or self.output_tokens > self.context_reserve_tokens):
            raise ValueError("invalid API output budget")

    def to_payload(self) -> dict[str, object]:
        raw: object = asdict(self)
        return dict(decode_json_object(raw))

    @classmethod
    def from_payload(cls, payload: object) -> ApiRunSelection:
        if not isinstance(payload, dict) or len(str(payload)) > 8000:
            raise ValueError("invalid persisted API selection")
        row = decode_json_object(payload)
        required = {"connection_id", "connection_revision", "model_id", "protocol", "native_tools"}
        if not required <= row.keys() or row.keys() - {field.name for field in fields(cls)}:
            raise ValueError("invalid persisted API selection fields")

        def optional_int(name: str) -> int | None:
            value = row.get(name)
            return require_json_integer(value, name) if value is not None else None

        def optional_bool(name: str) -> bool | None:
            value = row.get(name)
            return require_json_boolean(value, name) if value is not None else None

        effort = row.get("reasoning_effort")
        return cls(
            connection_id=require_json_string(row["connection_id"], "connection_id"),
            connection_revision=require_json_string(row["connection_revision"], "connection_revision"),
            model_id=require_json_string(row["model_id"], "model_id"),
            protocol=require_json_string(row["protocol"], "protocol"),
            native_tools=require_json_boolean(row["native_tools"], "native_tools"),
            context_window_tokens=require_json_integer(row.get("context_window_tokens", cls.context_window_tokens), "context_window_tokens"),
            context_reserve_tokens=require_json_integer(row.get("context_reserve_tokens", cls.context_reserve_tokens), "context_reserve_tokens"),
            context_keep_recent_tokens=require_json_integer(row.get("context_keep_recent_tokens", cls.context_keep_recent_tokens), "context_keep_recent_tokens"),
            thinking_enabled=optional_bool("thinking_enabled"),
            reasoning_effort=require_json_string(effort, "reasoning_effort") if effort is not None else None,
            stream=require_json_boolean(row.get("stream", cls.stream), "stream"),
            tool_choice=require_json_string(row.get("tool_choice", cls.tool_choice), "tool_choice"),
            output_tokens=optional_int("output_tokens"),
            token_counter=require_json_string(row.get("token_counter", cls.token_counter), "token_counter"),
            budget_source=require_json_string(row.get("budget_source", cls.budget_source), "budget_source"),
            input_limit_tokens=optional_int("input_limit_tokens"),
            text_tool_format=require_json_string(row.get("text_tool_format", cls.text_tool_format), "text_tool_format"),
            supports_images=optional_bool("supports_images"),
        )
