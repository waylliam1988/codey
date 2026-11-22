"""Small injectable tool surface used by the Agent runtime.

This is intentionally not a plugin registry. It keeps test/probe dependency
injection explicit without changing the Agent's observable serial tool flow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol, TypeAlias, TypedDict

from codey.toolchain import runtime as tool_runtime
from codey.toolchain.runtime import EditBlock, ToolOutcome
from codey.types.json_value import (
    ToolArgs,
    require_json_boolean,
    require_json_integer,
    require_json_string,
    require_json_strings,
)


class ReadOptions(TypedDict, total=False):
    offset: int
    limit: int


class SearchOptions(ReadOptions, total=False):
    literal: bool
    case_sensitive: bool
    include: str
    exclude: list[str]
    before_context: int
    after_context: int


class ReadFile(Protocol):
    def __call__(self, root: Path, rel: str, /, *, offset: int = ..., limit: int = ...) -> ToolOutcome: ...


class SearchFiles(Protocol):
    def __call__(
        self, root: Path, rel: str, query: str, /, *, offset: int = ..., limit: int = ...,
        max_results: int | None = ..., literal: bool = ..., case_sensitive: bool = ..., include: str = ...,
        exclude: tuple[str, ...] | list[str] = ..., before_context: int = ..., after_context: int = ...,
    ) -> ToolOutcome: ...


class WriteFile(Protocol):
    def __call__(
        self, root: Path, rel: str, content: str, /, *, expected_version: str | None = ...,
    ) -> ToolOutcome: ...


class RunCommand(Protocol):
    def __call__(
        self, root: Path, rel: str, command: str, /, *, permission_profile: str, phase: str = ...,
    ) -> ToolOutcome: ...


RunCommandWithContext: TypeAlias = Callable[[Path, str, str, str, str, str], ToolOutcome]


def read_call_options(args: ToolArgs) -> ReadOptions:
    """Narrow the frozen tool schema's optional fields, retaining omitted keys."""
    options: ReadOptions = {}
    if "offset" in args:
        options["offset"] = require_json_integer(args["offset"], "offset")
    if "limit" in args:
        options["limit"] = require_json_integer(args["limit"], "limit")
    return options


def search_call_options(args: ToolArgs) -> SearchOptions:
    options: SearchOptions = {}
    read_options = read_call_options(args)
    if "offset" in read_options:
        options["offset"] = read_options["offset"]
    if "limit" in read_options:
        options["limit"] = read_options["limit"]
    if "literal" in args:
        options["literal"] = require_json_boolean(args["literal"], "literal")
    if "case_sensitive" in args:
        options["case_sensitive"] = require_json_boolean(args["case_sensitive"], "case_sensitive")
    if "include" in args:
        options["include"] = require_json_string(args["include"], "include")
    if "exclude" in args:
        options["exclude"] = require_json_strings(args["exclude"], "exclude")
    if "before_context" in args:
        options["before_context"] = require_json_integer(args["before_context"], "before_context")
    if "after_context" in args:
        options["after_context"] = require_json_integer(args["after_context"], "after_context")
    return options


@dataclass(frozen=True)
class AgentToolFns:
    read_file: ReadFile = tool_runtime.read_file
    list_directory: Callable[[Path, str], ToolOutcome] = tool_runtime.list_directory
    search_files: SearchFiles = tool_runtime.search_files
    find_references: Callable[[Path, str, str], ToolOutcome] = (
        tool_runtime.find_references
    )
    write_file: WriteFile = tool_runtime.write_file
    edit_file: Callable[[Path, str, list[EditBlock]], ToolOutcome] = (
        tool_runtime.edit_file
    )
    run_command: RunCommand = tool_runtime.run_command
    run_command_with_context: RunCommandWithContext | None = None

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
    run_command_with_context: RunCommandWithContext | None = None,
) -> AgentToolFns:
    """Own the workspace discovery resource for one execution tool set."""
    from codey.workspace.file_index import IncrementalFileIndex

    index = IncrementalFileIndex(state_home)
    return AgentToolFns(
        search_files=partial(tool_runtime.search_files, file_index=index),
        run_command_with_context=run_command_with_context,
    )
