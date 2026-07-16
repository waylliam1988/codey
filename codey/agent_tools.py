"""Small injectable tool surface used by the Agent runtime.

This is intentionally not a plugin registry. It keeps test/probe dependency
injection explicit without changing the Agent's observable serial tool flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from codey import tool_runtime
from codey.tool_runtime import EditBlock, ToolOutcome


@dataclass(frozen=True)
class AgentToolFns:
    read_file: Callable[..., ToolOutcome] = tool_runtime.read_file
    list_directory: Callable[[Path, str], ToolOutcome] = tool_runtime.list_directory
    search_files: Callable[..., ToolOutcome] = tool_runtime.search_files
    find_references: Callable[[Path, str, str], ToolOutcome] = (
        tool_runtime.find_references
    )
    write_file: Callable[[Path, str, str], ToolOutcome] = tool_runtime.write_file
    edit_file: Callable[[Path, str, list[EditBlock]], ToolOutcome] = (
        tool_runtime.edit_file
    )
    run_command: Callable[[Path, str, str], ToolOutcome] = tool_runtime.run_command


DEFAULT_TOOL_FNS = AgentToolFns()
