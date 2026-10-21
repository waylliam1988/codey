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
    import threading
    from types import SimpleNamespace

    from codey.app import shell_service

    called = []
    monkeypatch.setattr(shell_service, "mint_shell_ticket", lambda **kwargs: called.append(kwargs))
    ctx = SimpleNamespace(_shell_spawn_gate=threading.RLock(), control_service=SimpleNamespace(draining=True),
                          lock=threading.RLock(), approvals={}, run_registry=object())
    assert shell_service.claim_shell_ticket(ctx, "approval", timeout=1, output_limit=100) == (None, None)
    assert not called


def test_native_queue_cannot_admit_after_drain(monkeypatch):
    import threading
    from types import SimpleNamespace

    from codey.app import input_queue_routes

    admitted = []
    monkeypatch.setattr(input_queue_routes, "bind_input_queue", lambda ctx: None)
    ctx = SimpleNamespace(_shell_spawn_gate=threading.RLock(), control_service=SimpleNamespace(draining=True),
                          load_ui_state=lambda: {"sessions": [{"id": "c"}], "projects": []},
                          input_queue=SimpleNamespace(create=lambda *args, **kwargs: admitted.append(args)))
    status, _ = input_queue_routes.input_action_response(ctx, {"action": "create", "id": "i",
        "request": {"session_id": "c", "run_id": "r", "task": "read", "provider": "deepseek"}})
    assert status == 503
    assert not admitted
