"""Published SSE and headless events must describe the same active run."""

import pytest

from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessAppContext


@pytest.mark.parametrize("kind", ["status", "tool_started", "tool", "task_done"])
@pytest.mark.parametrize("explicit_run", [False, True])
def test_headless_jsonl_and_bus_share_active_run_identity(tmp_path, kind, explicit_run):
    rows = []
    context = HeadlessAppContext(tmp_path, port=0, emit_jsonl=rows.append)
    queue = context.subscribe()
    try:
        context.reserve_run(session_id="session", project=None, task="inspect", provider_id="local", run_id="run")
        event = {"type": kind, "status": "running", "kind": "read", "tool_name": "read_file", "ok": False}
        if explicit_run:
            event["run_id"] = "run"
        context.emit(event)
        published = queue.get_nowait()
        assert len(rows) == 1
        for payload in (published, rows[0]):
            assert payload["run_id"] == "run"
            assert payload["session_id"] == "session"
            assert payload["schema_version"] == 1
        assert "session_id" not in event, "emitting must not modify the caller's event"
    finally:
        context.close()


def test_explicit_foreign_run_is_not_stamped_with_active_session(tmp_path):
    context = AppContext(tmp_path)
    queue = context.subscribe()
    try:
        context.reserve_run(session_id="session", project=None, task="inspect", provider_id="local", run_id="run")
        context.emit({"type": "tool", "run_id": "foreign", "session_id": "other", "ok": False})
        payload = queue.get_nowait()
        assert (payload["run_id"], payload["session_id"]) == ("foreign", "other")
    finally:
        context.close()


@pytest.mark.parametrize("invalid", [None, False, True, "0", 0.0])
def test_ui_and_jsonl_never_invent_an_exit_code(tmp_path, invalid):
    rows = []
    context = HeadlessAppContext(tmp_path, port=0, emit_jsonl=rows.append)
    queue = context.subscribe()
    try:
        context.emit({"type": "tool", "tool_name": "run", "kind": "run", "ok": False, "exit_code": invalid})
        for payload in (queue.get_nowait(), rows[0]):
            assert "exit_code" not in payload
            assert payload["ok"] is False
    finally:
        context.close()


@pytest.mark.parametrize("missing", [None, ""])
def test_explicit_empty_session_is_filled_from_active_run(tmp_path, missing):
    context = AppContext(tmp_path)
    queue = context.subscribe()
    try:
        context.reserve_run(session_id="session", project=None, task="inspect", provider_id="local", run_id="run")
        context.emit({"type": "tool", "run_id": "run", "session_id": missing, "ok": False})
        assert queue.get_nowait()["session_id"] == "session"
    finally:
        context.close()


def test_sse_wire_and_jsonl_share_canonical_tool_fields(tmp_path):
    import io
    import json
    from types import SimpleNamespace

    from codey.app.cli import _human_cli_line
    from codey.app.http_plumbing import write_sse_event

    rows = []
    context = HeadlessAppContext(tmp_path, port=0, emit_jsonl=rows.append)
    queue = context.subscribe()
    try:
        context.reserve_run(session_id="session", project=None, task="inspect", provider_id="local", run_id="run")
        context.emit({"type": "tool", "kind": "read", "tool_name": "read_file", "path": "a.py", "ok": True})
        published = queue.get_nowait()
        handler = SimpleNamespace(wfile=io.BytesIO())
        assert write_sse_event(handler, published, event_id=17)
        wire = handler.wfile.getvalue().decode()
        assert wire.startswith("id: 17\n")
        payload = json.loads(wire.split("data: ", 1)[1])
        for key in ("type", "run_id", "session_id", "schema_version", "tool_name", "ok", "path"):
            assert payload[key] == rows[0][key]
        assert "read_file" in _human_cli_line(rows[0])
    finally:
        context.close()
