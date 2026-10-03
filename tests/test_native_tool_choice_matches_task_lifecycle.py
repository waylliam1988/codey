"""Active native turns require calls; terminal receipts withdraw tools."""
from __future__ import annotations

import json
from threading import Event

import pytest

from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import local_openai
from codey.providers.local_openai import LocalOpenAIProvider


class _Response:
    def __init__(self, choice):
        self.data = json.dumps({"choices": [choice]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.data[:size]


def _call(name, args, call_id="c1"):
    return {"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}}


TOOLS = [{"type": "function", "function": {"name": "done", "parameters": {
    "type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
}}}]


@pytest.mark.parametrize("method", ["send_turn", "send_tool_results"])
def test_active_native_http_request_requires_a_tool(monkeypatch, method):
    seen = []

    def reply(request, timeout=None):
        seen.append(json.loads(request.data))
        return _Response(_call("done", {"summary": "finished"}))

    monkeypatch.setattr(local_openai.urllib.request, "urlopen", reply)
    provider = LocalOpenAIProvider("http://model.test/v1", "test")
    if method == "send_turn":
        turn = provider.send_turn("finish", TOOLS)
    else:
        provider._messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "prior", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]})
        turn = provider.send_tool_results([{"tool_call_id": "prior", "content": "read"}], TOOLS)
    assert seen[0]["tool_choice"] == "required"
    assert seen[0]["parallel_tool_calls"] is False
    assert seen[0]["tools"] == TOOLS
    assert turn.tool_calls[0].name == "done"


def test_plain_text_and_terminal_receipt_do_not_force_tools(monkeypatch):
    seen = []

    def reply(request, timeout=None):
        seen.append(json.loads(request.data))
        return _Response({"finish_reason": "stop", "message": {"content": "ok"}})

    monkeypatch.setattr(local_openai.urllib.request, "urlopen", reply)
    provider = LocalOpenAIProvider("http://model.test/v1", "test")
    assert provider.send("hello") == "ok"
    provider.send_tool_results([{"tool_call_id": "done1", "content": "OK: done accepted"}], [])
    assert all("tools" not in payload and "tool_choice" not in payload
               and "parallel_tool_calls" not in payload for payload in seen)
    assert "max_tokens" not in seen[0]
    assert seen[1]["max_tokens"] == 1  # Only acknowledgement, never the user answer.


def test_gate_records_the_actual_required_request(monkeypatch, tmp_path):
    from tools.local_model_gate_attempts import GateTarget, RecordingProvider

    wire = []

    def reply(request, timeout=None):
        wire.append(json.loads(request.data))
        return _Response(_call("done", {"summary": "finished"}))

    monkeypatch.setattr(local_openai.urllib.request, "urlopen", reply)
    target = GateTarget("http://model.test/v1", "test", 32768, 8192, 12000)
    provider = RecordingProvider(target, tmp_path)
    provider.send_turn("finish", TOOLS)
    rows = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text().splitlines()]
    assert rows[0]["payload"] == wire[0]
    assert rows[0]["payload"]["tool_choice"] == "required"
    provider.send_tool_results([{"tool_call_id": "c1", "content": "OK: done accepted"}], [])
    rows = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text().splitlines()]
    assert rows[2]["payload"] == wire[1]
    assert rows[2]["payload"]["max_tokens"] == 1


def test_gate_metadata_includes_the_provider_and_transport_being_tested(monkeypatch):
    from tools import local_model_release_gate as gate
    from tools.local_model_gate_attempts import GateTarget

    monkeypatch.setattr(gate, "_server_observations", lambda url: {})
    metadata = gate._metadata(GateTarget("http://model.test/v1", "test", 32768, 8192, 12000))
    assert {"codey/providers/local_openai.py", "codey/operations/kernel_transport.py",
            "codey/operations/task_loop.py"} <= metadata["production_hashes"].keys()


@pytest.mark.parametrize("tools", [None, []])
def test_plain_native_turn_without_tools_keeps_its_answer_budget(monkeypatch, tools):
    seen = []

    def reply(request, timeout=None):
        seen.append(json.loads(request.data))
        return _Response({"finish_reason": "stop", "message": {"content": "full answer"}})

    monkeypatch.setattr(local_openai.urllib.request, "urlopen", reply)
    provider = LocalOpenAIProvider("http://model.test/v1", "test")
    assert provider.send_turn("explain", tools).text == "full answer"
    assert "max_tokens" not in seen[0]


def test_real_kernel_does_not_enter_optional_answer_branch(monkeypatch):
    """Counterfactual server branches match the observed auto/required replay."""
    seen = []

    def reply(request, timeout=None):
        payload = json.loads(request.data)
        seen.append(payload)
        if not payload.get("tools"):
            return _Response({"finish_reason": "stop", "message": {"content": "ack"}})
        if payload.get("tool_choice") == "required":
            return _Response(_call("done", {"summary": "finished"}))
        return _Response({"finish_reason": "length", "message": {
            "content": "<|channel>thought\n<channel|>" * 383,
        }})

    monkeypatch.setenv("NATIVE_TOOLS", "1")
    monkeypatch.setattr(local_openai.urllib.request, "urlopen", reply)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=3)
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=LocalOpenAIProvider("http://model.test/v1", "test"),
                provider_id="local",
                run_id="required-tool-choice",
            ),
        ),
    )
    assert result.completed is True
    assert len(seen) == 2
    assert seen[0]["tool_choice"] == "required"
    assert "Call exactly one native tool per turn" in seen[0]["messages"][0]["content"]
    assert "wait for its result" in seen[0]["messages"][0]["content"]
    assert "tools" not in seen[1] and "tool_choice" not in seen[1]
    assert seen[1]["messages"][-1]["tool_call_id"] == "c1"


@pytest.mark.parametrize("terminal", ["done", "cancel", "budget"])
def test_terminal_kernel_receipts_close_ids_without_advertising_tools(monkeypatch, tmp_path, terminal):
    seen = []
    stop = Event()
    executed = []
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    def reply(request, timeout=None):
        payload = json.loads(request.data)
        seen.append(payload)
        if len(seen) == 1:
            if terminal == "cancel":
                stop.set()
            return _Response(_call("done", {"summary": "finished"}) if terminal == "done"
                             else _call("read_file", {"path": "a.py"}))
        assert "tools" not in payload and "tool_choice" not in payload
        # Even an endpoint ignoring the withdrawn tools must get its id closed.
        if len(seen) == 2:
            return _Response(_call("read_file", {"path": "a.py"}, "late"))
        return _Response({"finish_reason": "stop", "message": {"content": "ack"}})

    monkeypatch.setenv("NATIVE_TOOLS", "1")
    monkeypatch.setattr(local_openai.urllib.request, "urlopen", reply)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
                          project=str(tmp_path), max_turns=1)
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=LocalOpenAIProvider("http://model.test/v1", "test"),
                provider_id="local",
                run_id=f"terminal-{terminal}",
                stop_flag=stop,
            ),
            execution=KernelExecutionDeps(
                project_path=tmp_path,
                executors={"read_file": lambda call: executed.append(call.name) or "x = 1"},
            ),
        ),
    )
    assert result.stop_reason == {"done": "done", "cancel": "stopped", "budget": "max_turns"}[terminal]
    assert executed == (["read_file"] if terminal == "budget" else [])
    assert len(seen) == 3
    assert [payload["messages"][-1]["tool_call_id"] for payload in seen[1:]] == ["c1", "late"]


@pytest.mark.parametrize("first_tool", ["done", "unknown_tool"])
def test_rejected_reply_at_last_turn_still_closes_the_followup_id(monkeypatch, tmp_path, first_tool):
    seen = []
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    def reply(request, timeout=None):
        payload = json.loads(request.data)
        seen.append(payload)
        if len(seen) == 1:
            args = {"summary": "I edited a.py"} if first_tool == "done" else {}
            return _Response(_call(first_tool, args, "early"))
        if len(seen) == 2:
            assert payload["tool_choice"] == "required"  # Rejected done remains active.
            assert payload["messages"][-1]["tool_call_id"] == "early"
            assert payload["messages"][-1]["content"].startswith("ERROR:")
            return _Response(_call("edit", {"path": "a.py", "content": "x = 2\n"}, "late-edit"))
        assert "tools" not in payload and "tool_choice" not in payload
        assert payload["messages"][-1]["tool_call_id"] == "late-edit"
        assert "not executed" in payload["messages"][-1]["content"]
        return _Response({"finish_reason": "stop", "message": {"content": "ack"}})

    monkeypatch.setenv("NATIVE_TOOLS", "1")
    monkeypatch.setattr(local_openai.urllib.request, "urlopen", reply)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write"}),
                                           required_checks=("project_changes_required",)),
                          project=str(tmp_path), max_turns=1)
    executed = []
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=LocalOpenAIProvider("http://model.test/v1", "test"),
                provider_id="local",
                run_id="reject-last-turn",
            ),
            execution=KernelExecutionDeps(
                project_path=tmp_path,
                executors={"edit": lambda call: executed.append(call.name)},
            ),
        ),
    )
    assert result.completed is False
    assert result.stop_reason == "max_turns"
    assert executed == []
    assert (tmp_path / "a.py").read_text() == "x = 1\n"
    assert len(seen) == 3
