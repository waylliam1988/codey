"""Real kernel tool observations retain the same facts in SSE and headless."""
import io
import json
from types import SimpleNamespace

import pytest

from codey.app.headless_runner import headless_event_payload
from codey.app.http_plumbing import write_sse_event
from codey.operations.kernel_events import _emit_tool_results
from codey.operations.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.observe.events import run_event_ui_payload


@pytest.mark.parametrize("name,ok,text,audit", [
    ("read_file", True, "ERROR: example in source", {}),
    ("read_file", False, "权限不足", {}),
    ("run", True, "tests passed", {"exit_code": 0}),
    ("run", False, "unknown", {}),
])
def test_real_tool_execution_has_identical_stream_semantics(name, ok, text, audit):
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.verify"})))
    call = ToolCall(name, {"path": "a.txt"} if name == "read_file" else {"command": "pytest", "path": "."}, "c1")
    events = []
    results = execute_turn(session, [call], executors={name: lambda c: ToolResult(c, text, ok=ok, audit=audit)},
                           run_id="r", turn=1)
    _emit_tool_results(events.append, session, results, run_id="r", turn=1)
    payloads = [run_event_ui_payload("r", "s", event) for event in events]
    ui = next(payload for payload in payloads if payload and payload["type"] == "tool")
    handler = SimpleNamespace(wfile=io.BytesIO())
    assert write_sse_event(handler, ui, event_id=1)
    line = next(line for line in handler.wfile.getvalue().decode().splitlines() if line.startswith("data: "))
    sse = json.loads(line[6:])
    headless = headless_event_payload(ui)
    for key in ("run_id", "session_id", "tool_id", "tool_name", "ok", "status", "changed", "truncated", "result"):
        assert headless[key] == sse[key]
    assert headless["ok"] is ok
    assert headless.get("exit_code") == sse.get("exit_code") == audit.get("exit_code")
