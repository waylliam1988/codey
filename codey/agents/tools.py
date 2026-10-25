"""Small injectable tool surface used by the Agent runtime.

This is intentionally not a plugin registry. It keeps test/probe dependency
injection explicit without changing the Agent's observable serial tool flow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from codey.toolchain import runtime as tool_runtime
from codey.toolchain.runtime import EditBlock, ToolOutcome


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
    run_command: Callable[..., ToolOutcome] = tool_runtime.run_command
    run_command_with_context: Callable[..., ToolOutcome] | None = None

    def execute_run_command(
        self,
        root: Path,
        rel: str,
        command: str,
        *,
        permission_profile: str,
        phase: str = "writer",
        tool_id: str = "",
    ) -> ToolOutcome:
        if self.run_command_with_context is not None:
            return self.run_command_with_context(
                root,
                rel,
                command,
                tool_id,
                permission_profile,
                phase,
            )
        return self.run_command(
            root,
            rel,
            command,
            permission_profile=permission_profile,
            phase=phase,
        )


DEFAULT_TOOL_FNS = AgentToolFns()


def build_project_tool_fns(
    *, state_home: Path | None,
    run_command_with_context: Callable[..., ToolOutcome] | None = None,
) -> AgentToolFns:
    """Own the workspace discovery resource for one execution tool set."""
    from codey.workspace.file_index import IncrementalFileIndex

    index = IncrementalFileIndex(state_home)
    return AgentToolFns(
        search_files=partial(tool_runtime.search_files, file_index=index),
        run_command_with_context=run_command_with_context,
    )
