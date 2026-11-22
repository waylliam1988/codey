"""Constructor resources and decoded request shapes obey the HTTP boundary."""

from pathlib import Path

import pytest

from codey.app import server
from codey.app.context import AppContext
from codey.types.json_value import parse_json_object
from tests.test_operator_auth_blocks_unauthenticated_http import httpd as httpd
from tests.test_operator_auth_blocks_unauthenticated_http import request


@pytest.mark.parametrize("body", ["null", "true", "42", '"task"', "[]", '["task"]'])
def test_run_http_rejects_non_object_json_as_the_submission_boundary_requires(
    httpd: server.CodeyHTTPServer, tmp_path: Path, body: str,
) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    httpd.app_context = ctx
    cookie = httpd.operator_auth.exchange(httpd.operator_auth.issue_bootstrap())
    assert cookie is not None
    try:
        status, _, encoded = request(httpd, "POST", "/api/run", body=body, headers={"Cookie": cookie})
        assert status == 400, "invalid input became an internal server failure"
        assert parse_json_object(encoded.decode("utf-8"))["error"] == "invalid json"
        assert not ctx.active_runs()
    finally:
        ctx.close()


@pytest.mark.parametrize("body", ["", "{}"])
def test_empty_run_body_keeps_the_required_task_error(
    httpd: server.CodeyHTTPServer, tmp_path: Path, body: str,
) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    httpd.app_context = ctx
    cookie = httpd.operator_auth.exchange(httpd.operator_auth.issue_bootstrap())
    assert cookie is not None
    try:
        status, _, encoded = request(httpd, "POST", "/api/run", body=body, headers={"Cookie": cookie})
        assert status == 400
        assert parse_json_object(encoded.decode("utf-8"))["error"] == "task required"
    finally:
        ctx.close()


def test_unknown_post_route_keeps_its_not_found_response(httpd: server.CodeyHTTPServer) -> None:
    cookie = httpd.operator_auth.exchange(httpd.operator_auth.issue_bootstrap())
    assert cookie is not None
    status, _, _ = request(httpd, "POST", "/api/unknown", body="null", headers={"Cookie": cookie})
    assert status == 404
