"""Temporary partner wire profile. Declarations never create task grants."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from codey.providers.base import ProviderToolDefinition, tools_from_specs
from codey.toolchain.tool_spec import spec_for_tool, thaw_schema_value

WIRE_NAMES = {"list_dir": "ls", "read_file": "read", "grep": "search", "find_references": "references"}
TRANSPORT_CONTRACT = "unavailable-read-shell-v1"
TEXT_PROMPT_PREFIX = "Reply with text only. The declared tools are unavailable for this request; do not call them.\n\n"
_REQUIRED_NAMES = ("read_file", "shell")
_UNAVAILABLE = "Unavailable for this request. Do not call this tool. Declared for transport compatibility only. "


def prepare_declarations(tools: list[ProviderToolDefinition] | None) -> tuple[list[ProviderToolDefinition], dict[str, str]]:
    """Preserve real declarations, filling only the two required wire slots.

    The original frozen tool list is untouched. Supplemental calls map to
    canonical names and are rejected against the kernel's original snapshot.
    Text consumers reject every call in the connection wrapper.
    """
    declared: list[ProviderToolDefinition] = []
    reverse: dict[str, str] = {}
    for tool in tools or []:
        wire = WIRE_NAMES.get(tool.name, tool.name)
        if wire in reverse:
            raise ValueError(f"Zen tool name collision: {wire}")
        declared.append(replace(tool, name=wire))
        reverse[wire] = tool.name
    for name in _REQUIRED_NAMES:
        wire = WIRE_NAMES.get(name, name)
        if wire in reverse:
            continue
        spec = spec_for_tool(name)
        if spec is None:
            raise ValueError(f"Zen transport definition unavailable: {name}")
        definition = tools_from_specs((spec,))[0]
        declared.append(replace(definition, name=wire, description=_UNAVAILABLE + definition.description))
        reverse[wire] = name
    return declared, reverse


def transport_identity(runtime_identity: str) -> str:
    declared, _ = prepare_declarations(None)
    settings = {"runtime": runtime_identity, "contract": TRANSPORT_CONTRACT, "prefix": TEXT_PROMPT_PREFIX,
                "declarations": [{"name": t.name, "description": t.description,
                                  "parameters": thaw_schema_value(t.parameters)} for t in declared]}
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
