from codey.app import input_queue_routes as routes
from codey.app.context import AppContext


def test_status_query_returns_the_durable_record_without_resubmitting(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    monkeypatch.setattr(routes, "bind_input_queue", lambda ctx: None)
    try:
        ctx.input_queue.create("one", {"session_id": "chat", "run_id": "run_one", "task": "hello", "provider": "local"})
        status, body = routes.inputs_response(ctx, {"id": ["one"]})
        assert status == 200
        assert body["item"]["state"] == "queued"
    finally:
        ctx.close()


def test_rejected_stale_resume_never_clears_stop(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path)
    monkeypatch.setattr(routes, "bind_input_queue", lambda ctx: None)
    try:
        item = ctx.input_queue.create("one", {"session_id": "chat", "run_id": "run_one", "task": "hello", "provider": "local"})
        ctx.request_stop()
        status, _ = routes.input_action_response(ctx, {"id": "one", "revision": item["revision"], "action": "resume"})
        assert status == 409
        assert ctx.run_registry.stop_flag.is_set()
    finally:
        ctx.close()
