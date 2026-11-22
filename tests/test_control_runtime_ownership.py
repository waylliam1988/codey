"""Every HTTP route uses an explicitly attached application owner."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.app import input_queue_routes, server, shell_service
from codey.app.context import AppContext
from codey.app.control_service import ControlService
from codey.app.http_response import response_pair
from codey.runtime.core.api_selection import ApiRunSelection


@pytest.fixture()
def context(tmp_path: Path) -> Iterator[AppContext]:
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    control = ControlService(ctx, allowed_roots=())
    ctx.control_service = control
    try:
        yield ctx
    finally:
        control.close()
        ctx.close()


def test_http_module_has_no_implicit_execution_owner() -> None:
    assert not hasattr(server, "STATE")
    assert not hasattr(server, "get_state")
    assert not hasattr(server, "_submit_task")


def test_handler_requires_the_server_context(context: AppContext) -> None:
    handler = object.__new__(server.Handler)
    with server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler) as httpd:
        handler.server = httpd
        with pytest.raises(RuntimeError, match="attached application context"):
            handler._get_state()
        httpd.app_context = context
        assert handler._get_state() is context


def test_draining_cannot_mint_a_shell_execution_ticket(
    monkeypatch: pytest.MonkeyPatch, context: AppContext,
) -> None:
    called: list[str] = []

    def mint(
        approval_id: str, *, timeout: int, output_limit: int,
    ) -> tuple[dict[str, object] | None, shell_service.ShellExecutionTicket | None]:
        called.append(approval_id)
        return None, None

    monkeypatch.setattr(context, "claim_shell_ticket", mint)
    assert context.control_service is not None
    context.control_service.draining = True
    assert shell_service.claim_shell_ticket(context, "approval", timeout=1, output_limit=100) == (None, None)
    assert not called


def test_native_queue_cannot_admit_after_drain(
    monkeypatch: pytest.MonkeyPatch, context: AppContext,
) -> None:
    admitted: list[str] = []

    def bind(_ctx: AppContext) -> None:
        pass

    def create(
        identity: str, request: dict[str, object], *, model_selection: ApiRunSelection | None = None,
        message_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        admitted.append(identity)
        return {}

    monkeypatch.setattr(input_queue_routes, "bind_input_queue", bind)
    monkeypatch.setattr(context.input_queue, "create", create)
    assert context.control_service is not None
    context.control_service.draining = True
    body: dict[str, object] = {"action": "create", "id": "i",
        "request": {"session_id": "c", "run_id": "r", "task": "read", "provider": "deepseek"}}
    result: object = input_queue_routes.input_action_response(context, body)
    status, _ = response_pair(result)
    assert status == 503
    assert not admitted
