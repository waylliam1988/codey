"""Finite provider operations, independent of Python calling conventions."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import ClassVar, Literal, TypeAlias

from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.text_result_views import TextResultSpan
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.types.json_value import JsonObject

ProviderMethod: TypeAlias = Literal[
    "send", "send_turn", "send_tool_results", "acknowledge_tool_results",
    "send_tool_results_bound", "acknowledge_tool_results_bound",
]


@dataclass(frozen=True)
class ProviderRequestOptions:
    """Presence is explicit: omitted, null, false, zero and empty differ on wire."""

    supplied: frozenset[str] = frozenset()
    timeout: float | int | None = None
    tool_call_observations: str = ""
    tool_call_history_groups: int = 0
    output_token_limit: int = 0
    require_tool_call: bool = False
    tool_call_json: bool = False
    tool_call_schema: JsonObject | None = None
    tool_call_contract: str = ""
    tool_call_results: tuple[TextResultSpan, ...] | None = None
    delivery_commit: JsonObject | None = None

    def __post_init__(self) -> None:
        # Typed construction supplies non-default values automatically. The
        # decoder additionally records explicit null/false/zero/empty fields.
        changed = {item.name for item in fields(self)
                   if item.name != "supplied" and getattr(self, item.name) != item.default}
        object.__setattr__(self, "supplied", self.supplied | changed)


@dataclass(frozen=True)
class SendTextRequest:
    prompt: str
    options: ProviderRequestOptions = field(default_factory=ProviderRequestOptions)
    method: ClassVar[Literal["send"]] = "send"


@dataclass(frozen=True)
class SendTurnRequest:
    prompt: str
    tools: tuple[ProviderToolDefinition, ...] | None
    options: ProviderRequestOptions = field(default_factory=ProviderRequestOptions)
    method: ClassVar[Literal["send_turn"]] = "send_turn"


@dataclass(frozen=True)
class SendToolResultsRequest:
    results: tuple[ProviderToolResult, ...]
    tools: tuple[ProviderToolDefinition, ...] | None
    options: ProviderRequestOptions = field(default_factory=ProviderRequestOptions)
    method: ClassVar[Literal["send_tool_results"]] = "send_tool_results"


@dataclass(frozen=True)
class AcknowledgeToolResultsRequest:
    results: tuple[ProviderToolResult, ...]
    tools: tuple[ProviderToolDefinition, ...]
    options: ProviderRequestOptions = field(default_factory=ProviderRequestOptions)
    method: ClassVar[Literal["acknowledge_tool_results"]] = "acknowledge_tool_results"


@dataclass(frozen=True)
class SendToolResultsBoundRequest:
    results: tuple[ProviderToolResult, ...]
    window_ref: ProtocolWindowRef
    tools: tuple[ProviderToolDefinition, ...] | None
    options: ProviderRequestOptions = field(default_factory=ProviderRequestOptions)
    method: ClassVar[Literal["send_tool_results_bound"]] = "send_tool_results_bound"


@dataclass(frozen=True)
class AcknowledgeToolResultsBoundRequest:
    results: tuple[ProviderToolResult, ...]
    window_ref: ProtocolWindowRef
    tools: tuple[ProviderToolDefinition, ...]
    options: ProviderRequestOptions = field(default_factory=ProviderRequestOptions)
    method: ClassVar[Literal["acknowledge_tool_results_bound"]] = "acknowledge_tool_results_bound"


ProviderRequest: TypeAlias = (
    SendTextRequest | SendTurnRequest | SendToolResultsRequest | AcknowledgeToolResultsRequest
    | SendToolResultsBoundRequest | AcknowledgeToolResultsBoundRequest
)
