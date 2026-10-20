"""Real approval HTTP routes and native history, with a deterministic model peer."""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import SHELL_CASES, probe_command, run_shell_case


@contextmanager
def model_peer(protocol, case):
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
                if len(requests) == 1:
                    name, call_id, args = "shell", "original-shell-call", {"command": probe_command(case), "path": "."}
                else:
                    assert len(requests) == 2, "unexpected new generation/retry"
                    if protocol == "openai-responses":
                        calls = [item for item in items if item.get("type") == "function_call"]
                        results = [item for item in items if item.get("type") == "function_call_output"]
                        assert [item["call_id"] for item in calls] == ["original-shell-call"]
                        assert [item["call_id"] for item in results] == ["original-shell-call"]
                        content = results[0]["output"]
                    else:
                        calls = [call for item in items for call in item.get("tool_calls", [])]
                        results = [item for item in items if item.get("role") == "tool"]
                        assert [item["id"] for item in calls] == ["original-shell-call"]
                        assert [item["tool_call_id"] for item in results] == ["original-shell-call"]
                        content = results[0]["content"]
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
def test_http_approval_returns_original_result_without_another_user_message(tmp_path, protocol, case):
    with model_peer(protocol, case) as (url, requests, errors):
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
