import pytest

from codey.app import input_queue_routes as routes
from codey.app.context import AppContext

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


@pytest.mark.parametrize('mode', ['steer', 'follow_up'])
def test_input_route_preserves_delivery_mode(tmp_path, mode):
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        ctx.save_ui_state({'active_id': 'chat', 'projects': [], 'sessions': [{'id': 'chat', 'title': 'Chat', 'messages': []}]}, base_revision=0)
        status, payload = routes.input_action_response(ctx, {'action': 'create', 'id': 'one', 'request': {
            'session_id': 'chat', 'run_id': 'run_one', 'task': 'instruction', 'provider': 'local', 'input_mode': mode,
        }})
        assert status == 202
        assert payload['item']['request']['input_mode'] == mode
        assert ctx.durable.store.submission(ctx.durable.task('run_one').submission_id).mode == mode
    finally:
        ctx.close()


def test_input_route_rejects_unknown_modes(tmp_path):
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        status, payload = routes.input_action_response(ctx, {'action': 'create', 'id': 'one', 'request': {'input_mode': 'write'}})
        assert status == 409
        assert payload['error'] == 'invalid input mode'
        assert ctx.durable.tasks() == ()
    finally:
        ctx.close()


def test_status_query_returns_the_durable_record_without_resubmitting(tmp_path, monkeypatch):
    # Admission is durable before any worker runs. A directly placeable input
    # is already submitted in the durable projection even while the scheduler
    # is stopped.
    ctx = AppContext(tmp_path, start_scheduler=False)
    monkeypatch.setattr(routes, "bind_input_queue", lambda ctx: None)
    try:
        ctx.input_queue.create("one", {"session_id": "chat", "run_id": "run_one", "task": "hello", "provider": "local"})
        status, body = routes.inputs_response(ctx, {"id": ["one"]})
        assert status == 200
        assert body["item"]["state"] == "submitted"
    finally:
        ctx.close()


def test_rejected_stale_resume_never_clears_stop(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path, start_scheduler=False)
    monkeypatch.setattr(routes, "bind_input_queue", lambda ctx: None)
    try:
        item = ctx.input_queue.create("one", {"session_id": "chat", "run_id": "run_one", "task": "hello", "provider": "local"})
        ctx.request_stop()
        status, _ = routes.input_action_response(ctx, {"id": "one", "revision": item["revision"], "action": "resume"})
        assert status == 409
        assert ctx.run_registry.stop_flag.is_set()
    finally:
        ctx.close()
