"""The task diff route uses operator authentication and persisted run ownership."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import pytest

from codey.app.context import AppContext
from codey.app.server import CodeyHTTPServer
from codey.runs.task_diff import record_task_changes
from tests.test_operator_auth_blocks_unauthenticated_http import httpd as httpd
from tests.test_operator_auth_blocks_unauthenticated_http import request
from tools.local_model_gate_shell import GateHTTPClient


@pytest.fixture()
def owner(tmp_path: Path, httpd: CodeyHTTPServer) -> Iterator[AppContext]:
    ctx = AppContext(tmp_path / "state", memory_enabled=False)
    httpd.app_context = ctx
    try:
        yield ctx
    finally:
        ctx.close()


def test_run_changes_requires_an_operator_session(httpd: CodeyHTTPServer, owner: AppContext) -> None:
    status, _, _ = request(httpd, "GET", "/api/run_changes?session_id=chat&run_id=run")
    assert status == 401


@pytest.mark.parametrize("query", ["", "?session_id=chat", "?run_id=run"])
def test_run_changes_requires_both_owner_identifiers(
    httpd: CodeyHTTPServer, owner: AppContext, query: str,
) -> None:
    client = GateHTTPClient(httpd)
    status, payload = client.request("/api/run_changes" + query)
    assert status == 400
    assert payload["ok"] is False


def test_run_changes_returns_the_requested_runs_frozen_diff(
    httpd: CodeyHTTPServer, owner: AppContext,
) -> None:
    assert owner.run_ledgers is not None
    assert owner.managed_outputs is not None
    for run_id, text in (("first", "-base\n+first\n"), ("second", "-first\n+second\n")):
        ledger = owner.run_ledgers.open(
            run_id=run_id, session_id="chat", project=None, task="fixture", provider="local", mode="project",
        )
        changes: dict[str, object] = {
            "ok": True, "mode": "snapshot", "changed_count": 1, "diff": text,
            "files": [{"path": "shared.txt", "status": "modified", "additions": 1, "deletions": 1}],
        }
        record_task_changes(ledger, owner.managed_outputs, changes, checks_passed=True, receipt={})

    client = GateHTTPClient(httpd)
    for run_id, expected in (("first", "-base\n+first\n"), ("second", "-first\n+second\n")):
        query = urlencode({"session_id": "chat", "run_id": run_id})
        status, payload = client.request("/api/run_changes?" + query)
        assert status == 200
        assert payload["diff"] == expected
        assert payload["changed_count"] == 1

    for session_id, run_id in (("other", "first"), ("chat", "missing"), ("chat", "first~")):
        query = urlencode({"session_id": session_id, "run_id": run_id})
        status, payload = client.request("/api/run_changes?" + query)
        assert status == 404
        assert payload["ok"] is False
