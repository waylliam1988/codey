"""A successful connection must publish running for SSE replay as well as state."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.app import provider_services
from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessAppContext


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_connection_event_stream_agrees_with_authoritative_state(tmp_path, monkeypatch, headless, failed):
    def connect(*args, **kwargs):
        if failed:
            raise OSError("connection failed")
        return SimpleNamespace(close=lambda: None)

    if headless:
        context = HeadlessAppContext(tmp_path, port=0, emit_jsonl=lambda row: None, connect_provider=connect)
    else:
        context = AppContext(tmp_path)
        monkeypatch.setattr(provider_services, "connect_provider", connect)
        monkeypatch.setattr(provider_services, "provider_status_update", lambda *args: [])
    events = context.subscribe()
    try:
        if failed:
            with pytest.raises(OSError, match="connection failed"):
                context.get_provider("deepseek")
        else:
            context.get_provider("deepseek")
        statuses = []
        while not events.empty():
            event = events.get_nowait()
            if event["type"] == "status":
                statuses.append(event["status"])
        assert statuses == (["connecting"] if failed else ["connecting", "running"])
        assert context.run_status() == statuses[-1]
    finally:
        context.close()


@pytest.mark.parametrize("status,text,kind", [("connecting", "Connecting to browser…", "warn"),
                                               ("running", "Running", "run")])
def test_replayed_connection_status_keeps_running_display(status, text, kind):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js unavailable")
    html = (Path(__file__).parents[1] / "codey/web/index.html").read_text(encoding="utf-8")
    start = html.index("function handleServerEvent(data)")
    # Execute the real event dispatcher through its status branch; unrelated
    # message rendering is outside this status-only scenario.
    source = html[start:html.index("  if (data.type === 'providers')", start)] + "}\n"
    sse = (Path(__file__).parents[1] / "codey/web/assets/sse.js").read_text(encoding="utf-8")
    script = """
const assert = require('node:assert/strict');
let runningRunId = 'run';
const calls = [];
const setStatus = (...args) => calls.push(args);
const window = {};
""" + sse + "\nwindow.CodeySse.init({setStatus});\n" + source + f"""
handleServerEvent({{type: 'status', run_id: 'run', status: {json.dumps(status)}}});
assert.deepEqual(calls, [[{json.dumps(text)}, {json.dumps(kind)}]]);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
