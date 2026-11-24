from pathlib import Path

import pytest

from codey.app import input_queue_routes as routes
from codey.app.context import AppContext
from codey.utils.object_fields import require_object_fields

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


def _item(body: dict[str, object]) -> dict[str, object]:
    return require_object_fields(body["item"])


def _revision(body: dict[str, object]) -> int:
    revision = _item(body)["revision"]
    assert type(revision) is int
    return revision


def _ignore_binding(ctx: AppContext) -> None:
    return None


@pytest.mark.parametrize('mode', ['steer', 'follow_up'])
def test_input_route_preserves_delivery_mode(tmp_path: Path, mode: str) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        ctx.save_ui_state({'active_id': 'chat', 'projects': [], 'sessions': [{'id': 'chat', 'title': 'Chat', 'messages': []}]}, base_revision=0)
        status, payload = routes.input_action_response(ctx, {'action': 'create', 'id': 'one', 'request': {
            'session_id': 'chat', 'run_id': 'run_one', 'task': 'instruction', 'provider': 'local', 'input_mode': mode,
            'requested_capabilities': ['browser.observe', 'browser.interact', 'browser.effect'],
        }})
        assert status == 202
        request = require_object_fields(_item(payload)['request'])
        assert request['input_mode'] == mode
        assert request['requested_capabilities'] == [
            'browser.observe', 'browser.interact', 'browser.effect',
        ]
        assert ctx.durable.store.submission(ctx.durable.task('run_one').submission_id).mode == mode
    finally:
        ctx.close()


def test_input_route_rejects_unknown_modes(tmp_path: Path) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        status, payload = routes.input_action_response(ctx, {'action': 'create', 'id': 'one', 'request': {'input_mode': 'write'}})
        assert status == 409
        assert payload['error'] == 'invalid input mode'
        assert ctx.durable.tasks() == ()
    finally:
        ctx.close()


@pytest.mark.parametrize("mode", [[], {}], ids=["array", "object"])
def test_input_route_rejects_structured_delivery_modes(tmp_path: Path, mode: object) -> None:
    # HTTP JSON can carry arrays/objects. Neither is one of the two delivery
    # modes; rejection must leave the durable admission log unchanged.
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        status, payload = routes.input_action_response(ctx, {
            "action": "create", "id": "one", "request": {"input_mode": mode},
        })
        assert status == 409
        assert payload["error"] == "invalid input mode"
        assert ctx.durable.tasks() == ()
    finally:
        ctx.close()


def test_queued_edit_and_steer_use_the_same_fenced_receipt(tmp_path: Path) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        ctx.save_ui_state({'active_id': 'chat', 'projects': [], 'sessions': [{'id': 'chat', 'title': 'Chat'}]}, base_revision=0)
        ctx.durable.admit_input(conversation_id='chat', request_id='active', task_id='active', text='current', provider_snapshot={})
        ctx.durable.start_task('active', state={})
        status, body = routes.input_action_response(ctx, {'action': 'create', 'id': 'one', 'request': {
            'session_id': 'chat', 'run_id': 'run_one', 'task': 'original', 'provider': 'local',
        }})
        assert status == 202 and _item(body)['state'] == 'queued'
        status, body = routes.input_action_response(ctx, {'action': 'edit', 'id': 'one', 'revision': _revision(body), 'text': 'edited'})
        assert status == 202 and require_object_fields(_item(body)['request'])['task'] == 'edited'
        status, body = routes.input_action_response(ctx, {'action': 'steer', 'id': 'one', 'revision': _revision(body)})
        assert status == 202 and require_object_fields(_item(body)['request'])['input_mode'] == 'steer'
        ctx.durable.commit_generation_boundary('chat', task_id='active', at='post_tools')
        consumed = ctx.input_queue.get('one')
        assert consumed is not None and consumed['state'] == 'consumed'
    finally:
        ctx.close()


def test_status_query_returns_the_durable_record_without_resubmitting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Admission is durable before any worker runs. A directly placeable input
    # is already submitted in the durable projection even while the scheduler
    # is stopped.
    ctx = AppContext(tmp_path, start_scheduler=False)
    monkeypatch.setattr(routes, "bind_input_queue", _ignore_binding)
    try:
        ctx.input_queue.create("one", {"session_id": "chat", "run_id": "run_one", "task": "hello", "provider": "local"})
        status, body = routes.inputs_response(ctx, {"id": ["one"]})
        assert status == 200
        assert _item(body)["state"] == "submitted"
    finally:
        ctx.close()


def test_rejected_stale_resume_never_clears_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = AppContext(tmp_path, start_scheduler=False)
    monkeypatch.setattr(routes, "bind_input_queue", _ignore_binding)
    try:
        item = ctx.input_queue.create("one", {"session_id": "chat", "run_id": "run_one", "task": "hello", "provider": "local"})
        ctx.request_stop()
        status, _ = routes.input_action_response(ctx, {"id": "one", "revision": item["revision"], "action": "resume"})
        assert status == 409
        assert ctx.run_registry.stop_flag.is_set()
    finally:
        ctx.close()
