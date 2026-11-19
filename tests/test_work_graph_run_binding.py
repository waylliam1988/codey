"""Exercise per-run ownership through the real headless production kernel."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV, LOCAL_OPENAI_MODEL_ENV, NATIVE_TOOLS_ENV
from codey.operations import task_loop
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.operations.task_session import TaskSession
from codey.providers.api_provider import ApiProvider
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import tool_spec
from codey.types.json_value import JsonObject
from tests.test_work_graph_candidate import ReplayProvider, fixture
from tools.agent_work_graph_probe import append, records
from tools.work_graph_candidate import TOOL_NAME, WorkContextService, WorkInputs, installed_tool
from tools.work_graph_run_binding import bind_work_context


def test_two_projects_use_run_executor_instead_of_global_service(tmp_path: Path) -> None:
    original = task_loop.run_task_kernel
    observations: dict[str, list[JsonObject]] = {"alpha": [], "beta": []}
    global_calls: list[str] = []

    def unexpected(call: ToolCall) -> ToolResult:
        global_calls.append(call.name)
        raise AssertionError("A run must never use a global graph service")

    def run(session: TaskSession, *, request: KernelRunRequest) -> KernelResult:
        project = request.execution.project_path
        assert project is not None
        name = project.parent.name
        bound = bind_work_context(session, request, observe=observations[name].append)
        assert TOOL_NAME not in (request.execution.executors or {})
        with pytest.raises(ValueError, match="already bound"):
            bind_work_context(session, bound)
        return original(session, request=bound)

    with (installed_tool(WorkContextService(tmp_path, WorkInputs)),
          patch.object(task_loop, "run_task_kernel", run),
          patch.dict("os.environ", {NATIVE_TOOLS_ENV: "1", LOCAL_OPENAI_BASE_URL_ENV: "http://127.0.0.1:1/v1",
                                   LOCAL_OPENAI_MODEL_ENV: "offline-graph-integration"})):
        assert tool_spec.register_custom_executor(TOOL_NAME, unexpected)
        for name in observations:
            directory = tmp_path / name
            project = fixture(directory, "checkout")
            (project / "scope.md").write_text(f"Only PROJECT_{name.upper()}_SENTINEL is in this project.\n", encoding="utf-8")
            provider = ReplayProvider(True, [(TOOL_NAME, {"query": "PROJECT SENTINEL"}),
                                             ("done", {"summary": "Inspected the current project."})])

            def connect(provider_id: str, *, port: int, provider: ApiProvider = provider) -> ApiProvider:
                return provider

            def emit(row: dict[str, object], directory: Path = directory) -> None:
                append(directory / "events.jsonl", row)

            result = run_headless(HeadlessRequest(project, "Inspect project context without editing.", provider_id="local",
                intent="chat", max_turns=4, state_home=directory / "state", sources_open_required=False,
                project_changes_required=False,
                model_selection={"model": "offline-graph-integration", "base_url": "http://127.0.0.1:1/v1"}),
                emit_jsonl=emit, connect_provider=connect)
            assert result.stop_reason == "done", records(directory / "events.jsonl")[-5:]
    assert not global_calls
    for name, rows in observations.items():
        assert len(rows) == 1
        text = json.dumps(rows)
        assert f"PROJECT_{name.upper()}_SENTINEL" in text
        other = "beta" if name == "alpha" else "alpha"
        assert f"PROJECT_{other.upper()}_SENTINEL" not in text
    assert tool_spec.custom_executor_for(TOOL_NAME) is None
