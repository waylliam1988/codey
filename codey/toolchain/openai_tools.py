"""OpenAI native tool schemas derived from canonical ToolDefinitions.

Single source of truth stays in ``codey.toolchain.definition``. This module
only lowers it to the OpenAI ``tools`` wire format. JSON-text prompts are
untouched.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from codey.toolchain import definition as tool_defs

# Wrapper tools are a JSON-text batching idiom. Native tool calling already
# batches via multiple ``tool_calls`` in one assistant message, so exposing
# them natively would create a double concurrency language.
NATIVE_EXCLUDED_NAMES = frozenset({"parallel", "read_files"})


def _native_function_name(definition: tool_defs.ToolDefinition) -> str:
    """Wire name for one native function.

    ``done`` is a control tool with no runtime alias, so it keeps its model
    name. Every other native tool uses its runtime name.
    """
    if definition.runtime_name is not None:
        return str(definition.runtime_name)
    return str(definition.name)


def _native_definitions(
    definitions: Sequence[tool_defs.ToolDefinition] | None,
) -> tuple[tool_defs.ToolDefinition, ...]:
    if definitions is None:
        definitions = tool_defs.TOOL_DEFINITIONS
    return tuple(
        d
        for d in definitions
        if d.name not in NATIVE_EXCLUDED_NAMES
        and (d.runtime_name is not None or d.name == "done")
    )


def render_openai_tools(
    definitions: Sequence[tool_defs.ToolDefinition] | None = None,
) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for definition in _native_definitions(definitions):
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": _native_function_name(definition),
                    "description": definition.description,
                    "parameters": tool_defs.openai_parameters_schema(definition),
                },
            }
        )
    tools.sort(key=lambda item: str(((item.get("function") or {}).get("name")) or ""))
    return tools


def openai_tool_contract_hash(
    definitions: Sequence[tool_defs.ToolDefinition] | None = None,
) -> str:
    payload = json.dumps(render_openai_tools(definitions), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def native_tool_names(
    definitions: Sequence[tool_defs.ToolDefinition] | None = None,
) -> tuple[str, ...]:
    return tuple(_native_function_name(d) for d in _native_definitions(definitions))


__all__ = [
    "NATIVE_EXCLUDED_NAMES",
    "native_tool_names",
    "openai_tool_contract_hash",
    "render_openai_tools",
]
