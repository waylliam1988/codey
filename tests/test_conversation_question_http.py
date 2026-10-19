"""Both native protocols pause, accept a choice, and resume the same history."""
from __future__ import annotations

import json
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import GateHTTPClient, gate_application, wait_for_run


@contextmanager
def question_peer(protocol):
    requests, errors = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply({"data": [{"id": "question-fixture"}]})

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                assert len(requests) <= 3, "unexpected repair or repeated generation"
                if len(requests) == 1:
                    names = [tool.get("name", tool.get("function", {}).get("name")) for tool in body["tools"]]
                    assert "ask_user" in names
                    name, identity, args = "ask_user", "ask-original", {"question": "检查范围？", "options": ["只读文档", "运行测试"]}
                else:
                    items = body.get("messages", body.get("input"))
                    receipts = [item for item in items if item.get("tool_call_id", item.get("call_id")) == "ask-original" and (item.get("role") == "tool" or item.get("type") == "function_call_output")]
                    assert len(receipts) == 1, "original question receipt is missing or duplicated"
                    assert "No execution authorized" in str(receipts[0])
                    assert "只读文档" in json.dumps(items, ensure_ascii=False)
                    if len(requests) == 2:
                        name, identity, args = "read_file", "read-once", {"path": "pyproject.toml"}
                    else:
                        name, identity, args = "done", "done-once", {"summary": "已完成只读文档检查，没有执行命令或修改文件。"}
                if protocol == "openai-responses":
                    self.reply({"status": "completed", "output": [{"type": "function_call", "call_id": identity, "name": name, "arguments": json.dumps(args)}]})
                else:
                    self.reply({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "id": identity, "function": {"name": name, "arguments": json.dumps(args)}}]}}]})
            except Exception as exc:
                errors.append(str(exc))
                self.reply({"error": {"message": str(exc)}}, 400)

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
@pytest.mark.parametrize("intent", ["chat", "project"])
@pytest.mark.parametrize("restart", [False, True])
def test_question_answer_resumes_without_repeating_the_original_call(tmp_path, protocol, intent, restart):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname="question-test"\n', encoding="utf-8")
    with question_peer(protocol) as (url, requests, errors):
        target = GateTarget(url, "question-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=15)
        with ExitStack() as stack:
            httpd, ctx, events = stack.enter_context(gate_application(target, tmp_path / "gate"))
            client = GateHTTPClient(httpd)
            source = client.post("/api/run", {"session_id": "question-chat", "project": str(project), "intent": intent,
                "input": {"kind": "start_task", "text": "先询问检查范围，再只读分析 pyproject.toml；不要修改文件，不要运行命令。"},
                "provider": "local", "model_selection": {"model": target.model, "base_url": url}, "max_turns": 5})
            terminal = wait_for_run(client, source["run_id"], 15)
            assert terminal["stop_reason"] == "question", (terminal, errors)
            question = ctx.user_questions.pending("question-chat")
            assert question is not None
            if restart:
                stack.close()
                httpd, ctx, events = stack.enter_context(gate_application(target, tmp_path / "gate"))
                client = GateHTTPClient(httpd)
                assert ctx.user_questions.pending("question-chat")["id"] == question["id"]
            accepted = client.post("/api/questions", {"id": question["id"], "revision": question["revision"], "answer": "只读文档"})
            assert accepted["question"]["run_id"] == source["run_id"]
            deadline = time.monotonic() + 15
            while ctx.durable.task(source["run_id"]).phase not in {"completed", "failed", "cancelled"}:
                assert time.monotonic() < deadline, (ctx.durable.task(source["run_id"]), errors)
                time.sleep(0.02)
            final = wait_for_run(client, source["run_id"], 15)
            assert final["stop_reason"] == "done", (final, errors)
            client.post("/api/questions", {"id": question["id"], "revision": question["revision"], "answer": "只读文档"})
            assert not errors
            assert len(requests) == 3
            assert len([event for event in events if event.get("type") == "tool" and event.get("tool_name") == "read_file"]) == 1
            assert len(ctx.durable.tasks()) == 1
