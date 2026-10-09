"""Provider-neutral token meanings. No connector or wire-field knowledge."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal


@dataclass(frozen=True)
class RequestContextCount:
    value: int | None
    method: Literal["tokenizer", "estimated", "unknown"]

    def __post_init__(self) -> None:
        if self.method not in {"tokenizer", "estimated", "unknown"}:
            raise ValueError("invalid context count method")
        if (self.value is None) != (self.method == "unknown"):
            raise ValueError("unknown context counts must have no value")
        if self.value is not None and (type(self.value) is not int or self.value < 0):
            raise ValueError("context count must be a nonnegative integer")


@dataclass(frozen=True)
class ContextBudget:
    window_tokens: int
    output_tokens: int
    safety_tokens: int
    keep_recent_tokens: int
    source: str = "configuration"

    def __post_init__(self) -> None:
        for value in (self.window_tokens, self.output_tokens, self.keep_recent_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("context budget limits must be positive integers")
        if type(self.safety_tokens) is not int or self.safety_tokens < 0 or self.input_limit <= 0:
            raise ValueError("context budget has no input capacity")

    @property
    def input_limit(self) -> int:
        return self.window_tokens - self.output_tokens - self.safety_tokens


@dataclass(frozen=True)
class ReportedUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("reported usage must contain nonnegative integers or unknown values")
        for detail, total in [(self.cached_input_tokens, self.input_tokens),
                              (self.cache_write_input_tokens, self.input_tokens),
                              (self.reasoning_output_tokens, self.output_tokens)]:
            if detail is not None and total is not None and detail > total:
                raise ValueError("reported usage detail exceeds its inclusive total")


@dataclass(frozen=True)
class ApiExchangeUsage:
    exchange_id: str
    connection_id: str
    model_id: str
    protocol: str
    context: RequestContextCount
    budget: ContextBudget
    usage: ReportedUsage
    usage_status: str
    outcome: str

    def to_payload(self) -> dict[str, object]:
        return asdict(self)
