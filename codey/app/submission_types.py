"""Typed HTTP submission callbacks and their keyword arguments."""

from __future__ import annotations

from typing import Protocol, TypedDict

from codey.runtime.core.api_selection import ApiRunSelection
from codey.task.model import TaskInput


class ContinuationOptions(TypedDict, total=False):
    previous_run_id: str
    model_selection: ApiRunSelection
    input: TaskInput
    run_id: str


class TaskSubmitOptions(ContinuationOptions, total=False):
    requested_capabilities: tuple[str, ...]
    strict_research: bool
    sources_open_required: bool
    project_changes_required: bool
    denied_capabilities: tuple[str, ...]
    review_source_run_id: str
    output_format: str
    output_schema: str
    required_tools: tuple[str, ...]
    message_metadata: dict[str, object]


class TaskSubmitter(Protocol):
    def __call__(self, session_id: str, project: str | None, task: str, max_turns: int,
                 continue_task: bool, provider_id: str, intent: str, /, *,
                 requested_capabilities: tuple[str, ...] = (), strict_research: bool = False,
                 sources_open_required: bool = False, project_changes_required: bool = False,
                 denied_capabilities: tuple[str, ...] = (), previous_run_id: str = "",
                 message_metadata: dict[str, object] | None = None, review_source_run_id: str = "",
                 model_selection: ApiRunSelection | None = None, run_id: str = "", input: TaskInput | None = None,
                 output_format: str = "text", output_schema: str = "",
                 required_tools: tuple[str, ...] = ()) -> str | None: ...


class ContinuationSubmitter(Protocol):
    def __call__(self, session_id: str, project: str | None, task: str, max_turns: int,
                 continue_task: bool, provider_id: str, intent: str, /, *, previous_run_id: str = "",
                 model_selection: ApiRunSelection | None = None, run_id: str = "",
                 input: TaskInput | None = None) -> str | None: ...
