from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Control:
    kind: str
    body: str


@dataclass(frozen=True)
class ToolPlan:
    calls: list[ToolCall]
    control: Control | None
    protocol_error: str = ""
    protocol_error_kind: str = ""


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    output: str
    truncated: bool = False
    output_handle: str = ""
    output_bytes: int = 0
    output_stored_bytes: int = 0
    output_sha256: str = ""
