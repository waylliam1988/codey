"""Every HTTP route uses an explicitly attached application owner."""

from codey.app import server


def test_http_module_has_no_implicit_execution_owner():
    assert not hasattr(server, "STATE")
    assert not hasattr(server, "get_state")
    assert not hasattr(server, "_submit_task")


def test_handler_requires_the_server_context():
    from types import SimpleNamespace

    handler = object.__new__(server.Handler)
    state = object()
    handler.server = SimpleNamespace(app_context=state)
    assert handler._get_state() is state


def test_draining_cannot_mint_a_shell_execution_ticket(monkeypatch):
    from codey.app import shell_service
    from tests.support.shell_execution import ShellExecutionHarness

    called = []
    monkeypatch.setattr(shell_service, "mint_shell_ticket", lambda **kwargs: called.append(kwargs))
    ctx = ShellExecutionHarness()
    ctx.control_service = type("Control", (), {"draining": True})()
    ctx.approvals = type("Approvals", (), {})()
    assert shell_service.claim_shell_ticket(ctx, "approval", timeout=1, output_limit=100) == (None, None)
    assert not called


def test_native_queue_cannot_admit_after_drain(monkeypatch):
    from types import SimpleNamespace

    from codey.app import input_queue_routes
    from tests.support.shell_execution import ShellExecutionHarness

    admitted = []
    monkeypatch.setattr(input_queue_routes, "bind_input_queue", lambda ctx: None)
    ctx = ShellExecutionHarness()
    ctx.control_service = SimpleNamespace(draining=True)
    ctx.load_ui_state = lambda: {"sessions": [{"id": "c"}], "projects": []}
    ctx.input_queue = SimpleNamespace(create=lambda *args, **kwargs: admitted.append(args))
    status, _ = input_queue_routes.input_action_response(ctx, {"action": "create", "id": "i",
        "request": {"session_id": "c", "run_id": "r", "task": "read", "provider": "deepseek"}})
    assert status == 503
    assert not admitted
