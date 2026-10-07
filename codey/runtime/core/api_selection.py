"""Bounded non-secret API selection admitted for one durable run."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


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
    tool_choice: str = "required"
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in (self.connection_id, self.connection_revision, self.model_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise ValueError("invalid API connection or model identity")
        if self.protocol not in {"openai-completions", "openai-responses"}:
            raise ValueError("unsupported API protocol")
        if type(self.native_tools) is not bool or type(self.stream) is not bool:
            raise ValueError("API capability flags must be booleans")
        if self.thinking_enabled is not None and type(self.thinking_enabled) is not bool:
            raise ValueError("thinking flag must be boolean")
        if self.reasoning_effort is not None and self.reasoning_effort not in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("unsupported reasoning effort")
        if self.tool_choice not in {"auto", "required"}:
            raise ValueError("unsupported tool choice")
        if any(type(n) is not int or n <= 0 for n in (self.context_window_tokens, self.context_reserve_tokens, self.context_keep_recent_tokens)):
            raise ValueError("invalid API context budget")
        if self.context_window_tokens <= self.context_reserve_tokens or self.context_keep_recent_tokens > self.context_window_tokens - self.context_reserve_tokens:
            raise ValueError("inconsistent API context budget")
        if self.output_tokens is not None and (type(self.output_tokens) is not int or self.output_tokens <= 0):
            raise ValueError("invalid API output budget")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: object) -> ApiRunSelection:
        if not isinstance(payload, dict) or len(str(payload)) > 8000:
            raise ValueError("invalid persisted API selection")
        try:
            return cls(**payload)
        except TypeError as exc:
            raise ValueError("invalid persisted API selection fields") from exc
