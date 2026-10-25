from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from codey.codemode.identity import rpc_child_index
from codey.codemode.types import ToolCatalogEntry
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.result_schema import validate_output


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
        execute: Callable[[ToolCall], ToolResult],
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
        self._catalog = {entry.name: entry for entry in catalog}
        self._execute = execute
        self._next_child = 0

    def invoke(self, name: str, args: dict[str, object]) -> object:
        tool_name = str(name or "").strip().lower()
        if tool_name == "codemode" or tool_name.startswith("codemode."):
            raise ValueError("recursive codemode invocation is denied")
        if tool_name not in self._catalog:
            raise ValueError(f"tool is not in the frozen catalog: {tool_name}")
        if not isinstance(args, dict):
            raise ValueError("tool args must be an object")
        index = rpc_child_index.get()
        if index is None:
            index = self._next_child
            self._next_child += 1
        call = ToolCall(tool_name, dict(args), call_id=f"{self.execution_id}:child:{index}")
        raw = self._execute(call)
        if not isinstance(raw, ToolResult):
            raise TypeError("host executor must return a ToolResult")
        if not raw.ok:
            return {"ok": False, "error": raw.model_text}
        schema = self._catalog[tool_name].output_schema
        if schema.get("type") == "string":
            value: object = raw.model_text
        else:
            if "value" not in raw.canonical:
                raise ValueError(f"missing structured result for {tool_name}")
            value = raw.canonical["value"]
        validate_output(tool_name, schema, value)
        return {"ok": True, "value": value}
