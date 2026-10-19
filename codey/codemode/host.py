from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from codey.codemode.types import ToolCatalogEntry
from codey.runtime.core.models import ToolCall, ToolResult


class CodeModeHostProtocol(Protocol):
    def invoke(self, name: str, args: dict[str, object]) -> object:
        """Invoke one frozen, host-mediated child tool."""


class CodeModeHost:
    """Translate sandbox RPC calls into the existing kernel tool callback."""

    def __init__(
        self,
        *,
        execution_id: str,
        catalog: Sequence[ToolCatalogEntry],
        execute: Callable[[ToolCall], object],
        transform_value: Callable[[object], object] | None = None,
        allow_writes: bool = False,
    ) -> None:
        self.execution_id = str(execution_id or "").strip()
        if not self.execution_id:
            raise ValueError("execution_id must be non-empty")
        if any(not entry.read_only for entry in catalog) and not allow_writes:
            raise ValueError("CodeMode read-only host requires allow_writes=True for write tools")
        if any(not entry.read_only and entry.grant != "project.write" for entry in catalog):
            raise ValueError("CodeMode only permits project.write tools")
        self._allow_writes = bool(allow_writes)
        self._allowed = frozenset(entry.name for entry in catalog)
        self._execute = execute
        self._transform_value = transform_value
        self._next_child = 0

    def invoke(self, name: str, args: dict[str, object]) -> object:
        tool_name = str(name or "").strip().lower()
        if tool_name == "codemode" or tool_name.startswith("codemode."):
            raise ValueError("recursive codemode invocation is denied")
        if tool_name not in self._allowed:
            raise ValueError(f"tool is not in the frozen catalog: {tool_name}")
        if not isinstance(args, dict):
            raise ValueError("tool args must be an object")
        call = ToolCall(tool_name, dict(args), call_id=f"{self.execution_id}:child:{self._next_child}")
        self._next_child += 1
        raw = self._execute(call)
        if isinstance(raw, ToolResult):
            value = raw.canonical.get("value", raw.model_text)
            if self._transform_value is not None:
                value = self._transform_value(value)
            return {"ok": raw.ok, "value": value}
        if isinstance(raw, dict):
            value = dict(raw)
            if self._transform_value is not None and "value" in value:
                value["value"] = self._transform_value(value["value"])
            return value
        value = self._transform_value(raw) if self._transform_value is not None else raw
        return {"ok": True, "value": value}
