"""Constrain compatibility output using the task's already frozen tool contract.

The grammar binds each selected tool to its frozen argument schema; the kernel
still validates and authorizes every proposal. No mutable registry enters this
path and ordinary prose requests are unaffected.
"""
from __future__ import annotations

from typing import Any

from codey.providers.base import ProviderToolDefinition
from codey.toolchain.tool_spec import thaw_schema_value


def _sampling_schema(value: Any) -> Any:
    # Finite string repetitions expand large grammar graphs in local samplers;
    # KoboldCpp 1.117.1 can silently discard the entire grammar. Execution still
    # checks lengths against the original frozen tool schema. Token budgets
    # bound generation; this transport projection never authorizes arguments.
    if isinstance(value, dict):
        return {key: _sampling_schema(item) for key, item in value.items()
                if not (key in {"minLength", "maxLength"} and value.get("type") == "string")}
    if isinstance(value, list):
        return [_sampling_schema(item) for item in value]
    return value


def text_tool_schema(tools: list[ProviderToolDefinition]) -> dict[str, Any]:
    if not tools:
        raise ValueError("a constrained tool request requires declared tools")
    # Separate allOf components preserve tool-before-args order even in servers
    # that sort object keys before compiling JSON Schema into a grammar.
    variants = [{"allOf": [
        {"type": "object", "properties": {"tool": {"type": "string", "enum": [tool.name]}},
         "required": ["tool"]},
        {"type": "object", "properties": {"args": _sampling_schema(thaw_schema_value(tool.parameters))},
         "required": ["args"]},
    ], "unevaluatedProperties": False} for tool in tools]
    return variants[0] if len(variants) == 1 else {"oneOf": variants}
