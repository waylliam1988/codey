from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, cast


@dataclass(frozen=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ProviderToolResult:
    """Model delivery only; execution authority remains in the task ledger."""

    call_id: str
    content: str


@dataclass(frozen=True)
class ProviderToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, object]


def tools_from_specs(specs: tuple[Any, ...]) -> list[ProviderToolDefinition]:
    from codey.toolchain.tool_spec import _freeze_schema_value, _schema_for_spec

    return sorted([
        ProviderToolDefinition(spec.name, spec.description or "\n".join(spec.json_examples),
                               cast(Mapping[str, object], _freeze_schema_value(_schema_for_spec(spec))))
        for spec in specs if spec.name not in {"parallel", "read_files"}
    ], key=lambda tool: tool.name)


class TurnFinish(Enum):
    COMPLETE = "complete"
    OUTPUT_LIMIT = "output_limit"


@dataclass(frozen=True)
class AssistantTurn:
    text: str = ""
    tool_calls: tuple[ProviderToolCall, ...] = ()
    raw: Mapping[str, object] = field(default_factory=dict)
    reasoning: str = ""
    finish: TurnFinish = TurnFinish.COMPLETE


class ChatProvider(Protocol):
    name: str

    @property
    def location(self) -> str:
        """Return a human-readable provider location or page URL."""

    def new_chat(self, timeout: float | None = None) -> None:
        """Start a fresh remote conversation."""

    def send(self, text: str, timeout: float | None = None) -> str:
        """Send one message and return the completed assistant response."""

    def close(self) -> None | bool:
        """Release the local provider connection."""
