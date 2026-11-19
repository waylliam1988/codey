"""Per-run migration seam for the experimental graph; no production changes.

The process owns the stable tool schema. Each admitted kernel request owns its
executor and reads only that session's committed observations. This module does
not install a process-global service or change other executors.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from codey.operations.kernel_contracts import KernelRunRequest
from codey.operations.task_session import TaskSession
from codey.toolchain import tool_spec
from codey.types.json_value import JsonObject
from tools.work_graph_candidate import TOOL_NAME, WorkContextService, production_inputs


def bind_work_context(session: TaskSession, request: KernelRunRequest, *, graph: bool = True,
                      observe: Callable[[JsonObject], None] | None = None) -> KernelRunRequest:
    spec = tool_spec.spec_for_tool(TOOL_NAME)
    if spec is None or spec.grant != "project.read":
        raise ValueError("Register the stable project.read work_context schema first")
    execution = request.execution
    if execution.project_path is None:
        raise ValueError("work_context requires an admitted project")
    executors = dict(execution.executors or {})
    if TOOL_NAME in executors:
        raise ValueError("work_context is already bound to this run")
    service = WorkContextService(execution.project_path, lambda: production_inputs(session, execution),
                                 graph=graph, observe=observe)
    executors[TOOL_NAME] = service.execute
    return replace(request, execution=replace(execution, executors=executors))
