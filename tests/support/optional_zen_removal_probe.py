"""Subprocess-only absence probe using real Local factories and a loopback API."""
import contextlib
import importlib.abc
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


class MissingZen(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.attempted = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "codey.providers.zen" or fullname.startswith("codey.providers.zen."):
            self.attempted.append(fullname)
            raise ModuleNotFoundError("Zen package is absent")
        return None


def probe(root):
    missing = MissingZen()
    sys.meta_path.insert(0, missing)
    from codey.providers.catalog import API_CONNECTIONS, PROVIDER_LABELS

    API_CONNECTIONS.pop("zen", None)
    PROVIDER_LABELS.pop("zen", None)
    from codey.app.api import api_models_response, provider_catalog_response
    from codey.app.cli import main as cli_main
    from codey.app.context import AppContext
    from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers import local_config
    from codey.providers.api_connections import capture_selection, open_selection
    from codey.providers.registry import connect_provider
    from codey.runs.details import load_run_details
    from codey.runs.ledger import RunLedgerStore
    from codey.runs.trace import RunTraceStore
    from codey.storage.ui_state_store import UiStateStore
    from tests.support.model_preferences import enable_models
    from tools.local_model_gate_attempts import GateTarget
    from tools.local_model_release_gate import _metadata

    for name in tuple(os.environ):
        if name.startswith("LOCAL_OPENAI_") or name == "NATIVE_TOOLS":
            del os.environ[name]
    project = root / "project"
    project.mkdir()
    source = project / "fixture.txt"
    source.write_text("READ_MARKER", encoding="utf8")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.respond({"data": [{"id": "fixture"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            history = body.get("messages", body.get("input", []))
            results = [r for r in history if r.get("role") == "tool" or r.get("type") == "function_call_output"]
            call = None
            if body.get("tools"):
                call = ("read-id", "read_file", {"path": "fixture.txt"}) if not results else (
                    "done-id", "done", {"summary": "Read the fixture."})
            if self.path.endswith("/responses"):
                output = [{"type": "function_call", "id": "item-id", "call_id": call[0], "name": call[1],
                           "arguments": json.dumps(call[2])}] if call else [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "received"}]}]
                self.respond({"status": "completed", "output": output, "usage": {"input_tokens": 10, "output_tokens": 3}})
            else:
                message = {"content": "received"}
                if call:
                    message["tool_calls"] = [{"id": call[0], "type": "function", "function": {
                        "name": call[1], "arguments": json.dumps(call[2])}}]
                self.respond({"choices": [{"finish_reason": "stop", "message": message}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    protocols = ["openai-completions", "openai-responses"]
    try:
        with patch.object(local_config, "DEFAULT_STATE_HOME", root):
            for protocol in protocols:
                local_config.save_local_config(local_config.LocalProviderConfig(
                    base_url=base, model="fixture", api_protocol=protocol,
                    native_tools_mode="on", connection_revision="fixture-revision"))
                selection = capture_selection("local", {"base_url": base, "model": "fixture"})
                provider = open_selection(selection)
                trace = RunTraceStore(root).open(run_id=protocol, session_id="usage", project=None,
                                                 mode_initial="project", provider_initial="local")
                provider.bind_usage("local", trace.record_api_usage)
                try:
                    start = len(requests)
                    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})),
                                          task_kind="planning_readonly", project=str(project), max_turns=3)
                    result = run_task_kernel(session, request=KernelRunRequest(
                        transport=KernelTransportDeps(provider=provider, provider_id="local", run_id=protocol,
                                                      user_task="Read fixture.txt. Do not modify it."),
                        execution=KernelExecutionDeps(project_path=project)))
                    assert result.completed, result
                    assert session.read_files == {"fixture.txt"}
                    assert source.read_text(encoding="utf8") == "READ_MARKER"
                    assert len(requests) - start == 2
                    totals = trace.manifest.to_payload()["api_usage_totals"]
                    assert totals == {"requests": 2, "known_input_tokens": 20, "known_output_tokens": 6, "incomplete_requests": 0}
                    provider._codec.validate_view(provider.context_ledger.view)
                    results = requests[start + 1].get("messages", requests[start + 1].get("input"))
                    assert any("READ_MARKER" in str(r) for r in results)
                finally:
                    provider.close()
            enable_models(root, local=["fixture"])
            ctx = AppContext(root)
            status, bootstrap = api_models_response(ctx)
            ctx.close()
            assert status == 200 and [r["id"] for r in bootstrap["connections"]] == ["local"]
            assert bootstrap["connections"][0]["models"]
            status, catalog = provider_catalog_response()
            assert status == 200
            assert not any(item["id"] == "zen" for item in catalog["providers"])

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                cli_main(["--help"])
            except SystemExit as exc:
                assert exc.code == 0

        ui = UiStateStore(root)
        ui.save({"sessions": [{"id": "old", "provider": "zen", "messages": [{"role": "assistant", "text": "History"}]}],
                 "active_id": "old", "projects": []}, base_revision=0)
        assert UiStateStore(root).load()["sessions"][0]["provider"] == "zen"
        ledger = RunLedgerStore(root)
        writer = ledger.open(run_id="old-run", session_id="old", project=project, task="Read", provider="zen", mode="chat")
        writer.finish(summary="Read", stop_reason="done", turns=1, max_turns=3, provider="zen")
        assert load_run_details(run_ledgers=RunLedgerStore(root), run_traces=RunTraceStore(root),
                                session_id="old", run_id="old-run").available
        try:
            connect_provider("zen")
        except ValueError:
            pass
        else:
            raise AssertionError("removed Zen connection was silently replaced")
        metadata = _metadata(GateTarget(base, "fixture", 32768, 8192, 12000))
        assert not any("providers/zen/" in path for path in metadata["production_hashes"])
        assert not missing.attempted
        return {"local_protocols": protocols, "bootstrap": True, "cli": True, "old_history": True,
                "old_connection_rejected": True, "local_gate": True, "zen_imports": missing.attempted, "usage_without_zen": True}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


if __name__ == "__main__":
    print(json.dumps(probe(Path(sys.argv[1]))))
