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


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    output: str
