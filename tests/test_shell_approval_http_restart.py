"""Real HTTP regression coverage for approval recovery after a restart."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest import mock

from codey.agents.shell_approval import ShellApprovalRequest
from codey.app import server, shell_service
from codey.app.context import AppContext
from codey.runtime.effects.shell_approval_records import ShellApprovalFact
from tests.operator_http import OperatorHTTPConnection
from tests.support.shell_approval import admit_shell_task


def _seed_requested_approval(state: AppContext, approval_id: str) -> None:
    admit_shell_task(state, session_id="session", run_id="source-run", task="shell approval source")
    assert state.managed_outputs is not None
    artifact = state.managed_outputs.write_json_artifact(
        session_id="session",
        run_id="source-run",
        payload={
            **ShellApprovalRequest(".", "python --version").to_payload(),
            "command": "python --version",
            "cwd": ".",
            "project": str(state.state_home),
            "attached_project": str(state.state_home),
            "intent": "chat",
            "provider": "local",
            "continue_after": True,
            "max_turns": 4,
            "call_id": "",
            "window_ref": None,
        },
    )
    state.record_shell_approval_fact(ShellApprovalFact(
        approval_id=approval_id,
        session_id="session",
        source_run_id="source-run",
        kind="requested",
        command_ref=artifact.handle,
        command_digest=artifact.sha256,
        cwd=".",
    ))


def _http_json(conn: OperatorHTTPConnection, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    conn.request(
        method,
        path,
        body=json.dumps(body) if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else None,
    )
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    return response.status, payload


def test_stop_disk_failure_keeps_approval_queryable_until_durable_retry(tmp_path, monkeypatch):
    from codey.app.shell_approval_state import restore_pending_shell_approvals

    ctx = AppContext(tmp_path)
    _seed_requested_approval(ctx, "stop-disk-failure")
    restore_pending_shell_approvals(ctx)
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    conn = OperatorHTTPConnection(httpd)
    try:
        with mock.patch.object(ctx.shell_approval_records, "append", side_effect=OSError("audit unavailable")):
            status, _ = _http_json(conn, "POST", "/api/stop", {})
        assert status == 200
        status, snapshot = _http_json(conn, "GET", "/api/state")
        assert status == 200
        assert snapshot["pending_event"] is None
        assert ctx.durable.approval("stop-disk-failure").status.value == "cancelled"
        assert _http_json(conn, "GET", "/api/state")[1]["pending_event"] is None
    finally:
        conn.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        ctx.close()


def test_restart_approval_round_trip_uses_real_http_and_is_single_use(tmp_path: Path) -> None:
    approval_id = "approval-http-restart"
    # Observe durable wakeup without allowing a model worker to consume it.
    first = AppContext(tmp_path, start_scheduler=False)
    _seed_requested_approval(first, approval_id)
    original_task = first.durable.task("source-run")
    first.close()

    restarted = AppContext(tmp_path, start_scheduler=False)
    original_state = server.CodeyHTTPServer.app_context
    server.CodeyHTTPServer.app_context = restarted
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = OperatorHTTPConnection(httpd)
        status, state_payload = _http_json(conn, "GET", "/api/state")
        assert status == 200
        assert state_payload["pending_event"]["id"] == approval_id

        with (
            mock.patch.object(
                shell_service,
                "execute_shell_ticket",
                return_value={
                    "ok": True,
                    "status": "exit",
                    "exit_code": 0,
                    "output": "Python 3.x",
                    "truncated": False,
                },
            ),
        ):
            status, payload = _http_json(
                conn,
                "POST",
                "/api/shell_approval",
                {"id": approval_id, "approved": True},
            )
            assert restarted.shell_approval_workflow.wait(5)
        assert status == 202
        status, approval = _http_json(conn, "GET", f"/api/shell_approval?id={approval_id}")
        assert status == 200
        deadline = time.monotonic() + 2
        while approval["continuation"] != "started" and time.monotonic() < deadline:
            time.sleep(0.02)
            status, approval = _http_json(conn, "GET", f"/api/shell_approval?id={approval_id}")
        assert approval["continuation"] == "started"
        assert approval["execution"] == "settled"
        assert approval["continuation_run_id"] == original_task.task_id
        resumed = restarted.durable.task(original_task.task_id)
        assert resumed.submission_id == original_task.submission_id
        assert resumed.status.value == "pending"
        assert resumed.state["resume_input"] == {"kind": "resolve_shell_approval", "approval_id": approval_id}
        assert len(restarted.durable.tasks()) == 1

        status, payload = _http_json(
            conn,
            "POST",
            "/api/shell_approval",
            {"id": approval_id, "approved": False},
        )
        assert status == 409
        assert payload["code"] == "decision_conflict"
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        restarted.close()
        server.CodeyHTTPServer.app_context = original_state
