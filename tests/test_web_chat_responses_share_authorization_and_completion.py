"""The same denied-write/read/done task runs through all three transports."""
import json

import pytest

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from tests.test_responses_protocol_tool_history_and_stream_completion import response, service, wire


@pytest.mark.parametrize("transport", ["web", "chat-json", "chat-sse", "responses-json", "responses-sse"])
def test_three_transports_deny_write_then_complete_from_same_read_fact(tmp_path, transport):
    marker = "same-authorized-fact"
    (tmp_path / "probe.txt").write_text(marker, encoding="utf8")
    calls = [("edit", {"path": "forbidden.txt", "content": "bad"}),
             ("read_file", {"path": "probe.txt"}), ("done", {"summary": marker})]
    writes = []
    streams = transport.endswith("sse")

    def run(provider):
        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "control"})),
                              task_kind="project", project=str(tmp_path), max_turns=5)
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, provider_id="fixture", run_id="r", user_task="read probe.txt"),
            execution=KernelExecutionDeps(project_path=tmp_path, executors={"edit": lambda call: writes.append(call)})))
        assert result.completed and result.stop_reason == "done"
        assert result.summary == marker
        assert result.proof.satisfied
        assert not writes and not (tmp_path / "forbidden.txt").exists()
        assert "probe.txt" in session.read_files

    if transport == "web":
        class Web:
            def __init__(self):
                self.replies = iter(json.dumps({"tool": name, "args": args}) for name, args in calls)

            def send(self, prompt):
                return next(self.replies)
        run(Web())
        return
    replies = []
    for index, (name, args) in enumerate(calls):
        if transport.startswith("responses"):
            replies.append(wire(response([{"type": "function_call", "call_id": f"c{index}", "id": f"item{index}",
                                           "name": name, "arguments": json.dumps(args)}]), streams))
        else:
            message = {"tool_calls": [{"id": f"c{index}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
            choice = {"finish_reason": "tool_calls", "message": message}
            if streams:
                delta = {"choices": [{"delta": {"tool_calls": [{**message["tool_calls"][0], "index": 0}]}, "finish_reason": "tool_calls"}]}
                replies.append(("text/event-stream", ("data: " + json.dumps(delta) + "\n\ndata: [DONE]\n\n").encode()))
            else:
                replies.append(("application/json", json.dumps({"choices": [choice]}).encode()))
    replies.append(wire(response([]), streams) if transport.startswith("responses") else
                   ("application/json", b'{"choices":[{"finish_reason":"stop","message":{"content":"received"}}]}'))
    with service(replies) as (url, requests):
        run(ApiProvider(url, "fixture", native_tools=True, stream=streams,
                        api_protocol="openai-responses" if transport.startswith("responses") else "openai-completions"))
        first = requests[0][2]["tools"]
        names = {tool.get("name") or tool["function"]["name"] for tool in first}
        assert "read_file" in names and "done" in names and "edit" not in names
