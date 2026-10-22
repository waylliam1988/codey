"""Real approval HTTP routes and native history, with a deterministic model peer."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import (
    SHELL_CASES,
    GateHTTPClient,
    gate_application,
    probe_command,
    run_shell_case,
    wait_for_run,
)


@contextmanager
def model_peer(protocol, case, tool_name="shell", repeat=False):
    requests = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, status=200):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.reply({"data": [{"id": "shell-fixture"}]} if self.path == "/v1/models" else {})

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                assert body["model"] == "shell-fixture"
                items = body.get("input", body.get("messages"))
                expected_id = f"original-shell-call-{(len(requests) - 1) // 2}" if repeat else "original-shell-call"
                if len(requests) == 1 or (repeat and len(requests) % 2 == 1):
                    name, call_id, args = tool_name, expected_id, {"command": probe_command(case), "path": "."}
                else:
                    assert repeat or len(requests) == 2, "unexpected new generation/retry"
                    if protocol == "openai-responses":
                        calls = [item for item in items if item.get("type") == "function_call"]
                        results = [item for item in items if item.get("type") == "function_call_output"]
                        if not repeat:
                            assert len(calls) == len(results) == 1
                        assert calls[-1]["call_id"] == results[-1]["call_id"] == expected_id
                        assert calls[-1]["name"] == tool_name
                        content = results[-1]["output"]
                    else:
                        calls = [call for item in items for call in item.get("tool_calls", [])]
                        results = [item for item in items if item.get("role") == "tool"]
                        if not repeat:
                            assert len(calls) == len(results) == 1
                        assert calls[-1]["id"] == results[-1]["tool_call_id"] == expected_id
                        assert calls[-1]["function"]["name"] == tool_name
                        content = results[-1]["content"]
                    assert ("Denied by user" if case == "shell_deny" else "SHELL_GATE_RESULT") in content
                    name, call_id, args = "done", "done-call", {"summary": "Shell result received; " + content}
                if protocol == "openai-responses":
                    self.reply({"status": "completed", "output": [{"type": "function_call", "status": "completed",
                        "call_id": call_id, "name": name, "arguments": json.dumps(args)}]})
                else:
                    self.reply({"choices": [{"finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": "", "tool_calls": [{"type": "function", "id": call_id,
                        "function": {"name": name, "arguments": json.dumps(args)}}]}}]})
            except Exception as exc:
                errors.append(str(exc))
                self.reply({"error": {"message": "fixture assertion: " + str(exc)}}, 400)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/v1", requests, errors
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("case", SHELL_CASES)
@pytest.mark.parametrize("tool_name", ["shell", "run"])
def test_http_approval_returns_original_result_without_another_user_message(tmp_path, protocol, case, tool_name):
    with model_peer(protocol, case, tool_name) as (url, requests, errors):
        target = GateTarget(url, "shell-fixture", 32768, 8192, 12000,
                            api_protocol=protocol, request_timeout=10)
        proof = run_shell_case(case, target, tmp_path)
        assert not errors
        assert len(requests) == 2
        assert proof["ok"] is True
        assert proof["stop_reason"] == "done"
    # Approval settlement resumes the original durable task checkpoint; no
    # second task identity or synthetic user submission is created.
    assert proof["source_run_id"] == proof["continuation_run_id"]
    assert "Shell result received" in proof["summary"]


@pytest.mark.parametrize('protocol', ['openai-completions', 'openai-responses'])
def test_remembered_run_approval_automatically_resumes_next_task(protocol, tmp_path):
    with model_peer(protocol, 'shell_approve', 'run', repeat=True) as (url, requests, errors):
        target = GateTarget(url, 'shell-fixture', 32768, 8192, 12000, api_protocol=protocol, request_timeout=10)
        with gate_application(target, tmp_path) as (httpd, ctx, events):
            client = GateHTTPClient(httpd)
            for attempt in range(2):
                submitted = client.post('/api/run', {
                    'session_id': 'remembered-chat', 'input': {'kind': 'start_task', 'text': 'Run the same harmless command'},
                    'provider': 'local', 'model_selection': {'model': target.model, 'base_url': url},
                    'intent': 'chat', 'max_turns': 6,
                })
                run_id = submitted['run_id']
                if attempt == 0:
                    assert wait_for_run(client, run_id, 10)['stop_reason'] == 'approval'
                    approval_id = next(iter(ctx.pending_shell_approvals()))
                    client.post('/api/shell_approval', {'id': approval_id, 'approved': True, 'scope': 'session'})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if any(event.get('type') == 'task_done' and event.get('run_id') == run_id
                           and event.get('stop_reason') == 'done' for event in events) and not ctx.is_busy():
                        break
                    time.sleep(.02)
                else:
                    pytest.fail(f'remembered task did not finish: {ctx.shell_approval_workflow.list_statuses()}')
                assert ctx.shell_approval_workflow.for_source('remembered-chat', run_id)['execution'] == 'settled'
            assert len(requests) == 4 and not errors


@pytest.mark.parametrize('protocol', ['openai-completions', 'openai-responses'])
def test_zen_wrapper_restores_approved_call_before_native_continuation(protocol, tmp_path, monkeypatch):
    from codey.providers import local_connection
    from codey.providers.zen.connection import ZenProvider

    original = local_connection.open_selection
    monkeypatch.setattr(local_connection, 'open_selection', lambda selection: ZenProvider(original(selection)))
    with model_peer(protocol, 'shell_approve') as (url, requests, errors):
        target = GateTarget(url, 'shell-fixture', 32768, 8192, 12000, api_protocol=protocol, request_timeout=5)
        proof = run_shell_case('shell_approve', target, tmp_path)
        assert proof['stop_reason'] == 'done'
        assert len(requests) == 2 and not errors
