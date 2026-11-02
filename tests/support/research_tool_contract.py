"""Legacy research protocol projections used only by test adapters.

Schemas and examples still come from the production TOOL_CONTRACTS; the live
kernel builds its tools from frozen ToolSpecs instead of these renderers.
"""

from __future__ import annotations

import copy
from typing import Any

from codey.research.tool_contract import TOOL_CONTRACTS


def research_tool_names(*, include_source_search: bool = True) -> tuple[str, ...]:
    base = (
        "research_plan",
        "web_search",
        "open_url",
        "knowledge_search",
        "knowledge_read",
        "save_finding",
        "save_inference",
        "knowledge_write",
        "knowledge_link",
        "knowledge_commit",
        "done",
    )
    if not include_source_search:
        return base
    # source_search is inserted after open_url to match the system prompt order
    return (
        "research_plan",
        "web_search",
        "open_url",
        "source_search",
        "knowledge_search",
        "knowledge_read",
        "save_finding",
        "save_inference",
        "knowledge_write",
        "knowledge_link",
        "knowledge_commit",
        "done",
    )


def render_research_tool_contract_text(*, include_source_search: bool = True) -> str:
    lines: list[str] = []
    for name in research_tool_names(include_source_search=include_source_search):
        contract = TOOL_CONTRACTS[name]
        example = contract.example
        description = contract.description
        if description:
            lines.append(f"- {example}  {description}")
        else:
            lines.append(f"- {example}")
    return "\n".join(lines)


def research_tool_contract_hash(*, include_source_search: bool = True) -> str:
    from codey.toolchain.tool_prompt import model_visible_contract_hash

    return model_visible_contract_hash(
        "research_tool_contract",
        render_research_tool_contract_text(include_source_search=include_source_search),
    )


def openai_parameters_schema(tool: str) -> dict[str, Any]:
    contract = TOOL_CONTRACTS.get(tool)
    return copy.deepcopy(dict(contract.input_schema)) if contract else {
        "type": "object", "properties": {}, "additionalProperties": False}


def render_openai_tools(*, include_source_search: bool = True) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for name in research_tool_names(include_source_search=include_source_search):
        contract = TOOL_CONTRACTS[name]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": contract.description or contract.example,
                    "parameters": openai_parameters_schema(name),
                },
            }
        )
    def tool_name(item: dict[str, object]) -> str:
        function = item.get("function")
        return str(function.get("name") or "") if isinstance(function, dict) else ""

    tools.sort(key=tool_name)
    return tools


def tool_example(tool: str) -> str:
    # Single source: the static Tools: block and all dynamic repair /
    # controller allowed-action examples render from ToolContract examples.
    # Unknown tools fall back to the web_search shape (explicit repair policy).
    contract = TOOL_CONTRACTS.get(tool)
    if contract is not None:
        return contract.example
    return TOOL_CONTRACTS["web_search"].example
