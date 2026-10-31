"""Constrain compatibility output using the task's already frozen tool contract.

The grammar assists tool selection; the kernel still validates arguments and
authorizes every proposal. No native declarations or mutable registry enter
this path. Argument objects stay unordered: grammar samplers commonly reorder
required and optional properties, which can prevent a later evidence field.
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
    # A single tool has no selection ambiguity. Its complete frozen contract
    # also constrains arguments, notably the bounded research assessment.
    args = _sampling_schema(thaw_schema_value(tools[0].parameters)) if len(tools) == 1 else {"type": "object"}
    # Separate allOf components preserve tool-before-args order even in servers
    # that sort object keys before compiling JSON Schema into a grammar.
    return {"allOf": [
        {"type": "object", "properties": {"tool": {"type": "string", "enum": [tool.name for tool in tools]}},
         "required": ["tool"]},
        {"type": "object", "properties": {"args": args}, "required": ["args"]},
    ], "unevaluatedProperties": False}
