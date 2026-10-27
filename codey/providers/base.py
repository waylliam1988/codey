from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, cast


@dataclass(frozen=True)
class ResultMetadata:
    result_ref: str
    tool: str
    ok: bool

    def render(self) -> str:
        return "Result metadata:\n" + json.dumps(asdict(self), ensure_ascii=True, separators=(",", ":"))


def render_result_metadata(result_ref: str, *, tool: str, ok: bool) -> str:
    """One model-facing receipt-reference format; the task owns the receipt."""
    return ResultMetadata(result_ref, tool, ok).render()


def result_metadata_from_text(text: str) -> ResultMetadata | None:
    head, separator, tail = text.rpartition("Result metadata:\n")
    if not separator or (head and not head.endswith("\n")):
        return None
    encoded = tail.partition("\n")[0]
    try:
        value = json.loads(encoded)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or set(value) != {"result_ref", "tool", "ok"}:
        return None
    ref, tool, ok = value["result_ref"], value["tool"], value["ok"]
    if not isinstance(ref, str) or not ref or not isinstance(tool, str) or not tool or type(ok) is not bool:
        return None
    metadata = ResultMetadata(ref, tool, ok)
    return metadata if metadata.render() == separator + encoded else None


def result_ref_from_metadata(text: str) -> str | None:
    metadata = result_metadata_from_text(text)
    return metadata.result_ref if metadata is not None else None


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
    # Optional user input admitted at the same provider generation boundary.
    # It is encoded after this tool result by native protocol codecs, keeping
    # the call/result pair intact while avoiding a separate provider turn.
    user_content: str | None = None


@dataclass(frozen=True)
class ProviderToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, object]


def tools_from_specs(specs: tuple[Any, ...]) -> list[ProviderToolDefinition]:
    from codey.toolchain.tool_prompt import model_tool_description
    from codey.toolchain.tool_spec import _freeze_schema_value, _schema_for_spec

    return sorted([
        ProviderToolDefinition(spec.name, model_tool_description(spec),
                               cast(Mapping[str, object], _freeze_schema_value(_schema_for_spec(spec))))
        for spec in specs
    ], key=lambda tool: tool.name)


class TurnFinish(Enum):
    COMPLETE = "complete"
    OUTPUT_LIMIT = "output_limit"
    PROTOCOL_ERROR = "protocol_error"


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
