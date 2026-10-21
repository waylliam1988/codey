"""Real builder/kernel/API HTTP boundary; synthetic inputs, no model required."""
from __future__ import annotations

import json
import threading
from contextlib import closing
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from codey.agents.request import AgentRequest
from codey.app.context import AppContext
from codey.operations.project_adapter import _project_context, _project_ghost_sources
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.runs.trace import RunTraceStore
from codey.utils.refs import digest_text
from codey.workspace.context_admission import request_scope
from codey.workspace.context_epoch import context_epoch_id
from tests.test_chat_workspace_tools_and_answer_completion import execute_frame, workspace_frame


@pytest.fixture
def capture_endpoint():
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payloads.append(payload)
            message = {"role": "assistant", "content": '{"tool":"done","args":{"summary":"done"}}'}
            if payload.get("tools"):
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "done-1", "type": "function", "function": {
                        "name": "done", "arguments": '{"summary":"done"}'}}]}
            body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", payloads
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("native", [False, True], ids=["text", "native"])
@pytest.mark.parametrize("fault", [
    "none", "off", "scope", "global_first_scope", "global_then_project", "session_scope", "budget", "expiry",
    "invalid_expiry", "missing_bytes", "disabled", "duplicate_key", "trace_failure", "scope_trace_failure", "empty_scope",
])
def test_builder_kernel_api_wire_admission(tmp_path, capture_endpoint, native, fault):
    endpoint, payloads = capture_endpoint
    provider = ApiProvider(endpoint, "synthetic-admission", native_tools=native, timeout=5,
                           temperature=0, output_tokens=128)
    request = AgentRequest(provider=provider, project=tmp_path, task="Say done.",
                           session_id="session-a", ghost_directive="Prefer Chinese answers.",
                           permission_profile="planning_readonly", admit_hints=fault != "off")
    context = _project_context(request)
    sources = _project_ghost_sources(request)
    if fault in {"scope", "global_first_scope", "scope_trace_failure"}:
        sources = (replace(sources[0], scope=request_scope("project-other")),)
        if fault == "global_first_scope":
            sources = (replace(sources[0], key="global", scope="global"), *sources)
    elif fault == "global_then_project":
        sources = (replace(sources[0], key="global", scope="global"), *sources)
    elif fault == "session_scope":
        sources = (replace(sources[0], scope=request_scope(session_id="session-other")),)
    elif fault == "budget":
        sources = (replace(sources[0], budget=1),)
    elif fault in {"expiry", "invalid_expiry"}:
        sources = (replace(sources[0], expires_at="2000-01-01T00:00:00Z" if fault == "expiry" else "invalid"),)
    elif fault == "missing_bytes":
        context = "No candidate bytes."
    elif fault == "duplicate_key":
        sources = (*sources, sources[0])

    admissions, boundaries = [], []
    recorder = RunTraceStore(tmp_path / "trace").open(
        run_id="scope-capture", session_id="session-a", project=tmp_path,
        mode_initial="planning", provider_initial="local",
    )

    def record_sources(rows, **kwargs):
        if "trace_failure" in fault:
            raise RuntimeError("trace sink unavailable")
        admissions.append((rows, kwargs))
        recorder.record_context_sources(rows, **kwargs)

    def record_boundary(row, **_kwargs):
        if "trace_failure" in fault:
            raise RuntimeError("trace sink unavailable")
        boundaries.append(row)
        recorder.record_provider_prompt_boundary(row)

    trace = SimpleNamespace(record_context_sources=record_sources, record_provider_prompt_boundary=record_boundary)
    outcome = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1),
        request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, context_text=context,
                                          context_scope="" if fault == "empty_scope" else request_scope(tmp_path, "session-a"),
                                          ghost_sources=sources, admit_hints=fault != "disabled"),
            execution=KernelExecutionDeps(project_path=tmp_path, session_id="session-a"),
            observation=KernelObservationDeps(trace_recorder=trace),
        ),
    )
    allowed = fault in {"none", "off", "global_then_project", "trace_failure"}
    assert len(payloads) == int(allowed), (fault, outcome)
    assert outcome.stop_reason == ("done" if allowed else "provider_failure"), outcome
    if allowed:
        outbound = payloads[0]["messages"][-1]["content"]
        assert (request.ghost_directive in outbound) == (fault != "off")
        assert context in outbound
        if fault != "trace_failure":
            assert len(boundaries) == 1
            assert boundaries[0]["text"] == outbound
            assert boundaries[0]["epoch_id"] == context_epoch_id(outbound)
            if sources:
                assert admissions == [(sources, {"epoch_id": context_epoch_id(outbound),
                                                 "admission_reason": "provider_turn_boundary"})]
                for source in sources:
                    section = next(row for row in recorder.manifest.prompt_sections if row.name == source.key)
                    assert section.digest == digest_text(source.text)
                    assert section.scope == source.scope
                    assert section.budget == source.budget
                    assert section.epoch_id == context_epoch_id(outbound)
                    assert section.source_refs == (f"context_source:{source.key}",)
            else:
                assert admissions == []
            outbound_section = next(row for row in recorder.manifest.prompt_sections
                                    if row.name == "workspace_outbound_prompt")
            assert outbound_section.digest == digest_text(outbound)
            assert outbound_section.epoch_id == context_epoch_id(outbound)
            assert request.ghost_directive not in repr(recorder.manifest.to_payload())
    else:
        assert admissions == []
        assert boundaries == []
        expected_reason = {
            "scope": "scope_mismatch", "scope_trace_failure": "scope_mismatch",
            "global_first_scope": "scope_mismatch", "session_scope": "scope_mismatch",
            "budget": "over_budget", "expiry": "expired", "invalid_expiry": "invalid_expiry",
            "missing_bytes": "not_in_provider_payload", "disabled": "hints_disabled",
            "duplicate_key": "invalid_or_duplicate_key", "empty_scope": "missing_request_scope",
        }[fault]
        assert f":{expected_reason}" in outcome.summary


def test_session_scope_uses_request_session_not_candidate():
    sent = []
    from codey.workspace.context_admission import sources_from_values

    sources = sources_from_values(directive="Synthetic preference", scope=request_scope(session_id="other"))
    result = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1),
        request=KernelRunRequest(
            transport=KernelTransportDeps(provider=SimpleNamespace(send=lambda prompt: sent.append(prompt)),
                                          context_scope=request_scope(session_id="current"),
                                          context_text=sources[0].text, ghost_sources=sources),
            execution=KernelExecutionDeps(session_id="current"),
        ),
    )
    assert result.stop_reason == "provider_failure"
    assert sent == []


@pytest.mark.parametrize("kind", ["project", "session"])
def test_scope_identity_does_not_alias_long_shared_prefixes(kind):
    prefix = "shared-" + "x" * 160
    def scope(suffix):
        return request_scope(prefix + suffix) if kind == "project" else request_scope(session_id=prefix + suffix)

    assert scope("a") != scope("b")
    assert scope("a") == scope("a")
    assert len(scope("a")) <= 160


@pytest.mark.parametrize("control", ["enabled", "disabled", "scoped_error"])
def test_scoped_hint_control_reaches_entry_api_http(tmp_path, capture_endpoint, monkeypatch, control):
    endpoint, payloads = capture_endpoint
    hint = "Synthetic reviewed preference"
    monkeypatch.setattr("codey.operations.ghost_context.ghost_directive",
                        lambda *args, **kwargs: SimpleNamespace(text=hint))
    calls = []

    def read(name, **scope):
        if name == "admit_hints":
            calls.append(scope)
            if control == "scoped_error" and scope:
                raise TypeError("scoped read failed")
            return control != "disabled"
        return True

    provider = ApiProvider(endpoint, "synthetic-admission", native_tools=True, timeout=5,
                           temperature=0, output_tokens=128)
    frame = workspace_frame("", provider)
    with closing(AppContext(tmp_path / "state")) as state:
        monkeypatch.setattr(state.ghost_inbox, "control_enabled", read)
        result, _ = execute_frame(frame, state)
    assert result.event["stop_reason"] == "done", result.event
    assert len(payloads) == 1
    assert (hint in json.dumps(payloads[0])) == (control == "enabled")
    assert calls == [{"project": "", "session_id": frame.request.session_id}]
