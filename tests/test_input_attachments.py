"""Real attachment storage, durable admission, and provider wire projection."""
from __future__ import annotations

import base64
import json
from functools import partial
from unittest import mock

import pytest

from codey.app import api, attachment_routes, task_submit
from codey.app.context import AppContext
from codey.providers.api_provider import ApiProvider
from codey.providers.cline.capabilities import parse_capability
from codey.storage.input_attachments import InputAttachmentStore
from codey.task.attachments import IMAGE_UNSUPPORTED

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


@pytest.fixture
def ctx(tmp_path):
    state = AppContext(tmp_path / "state", start_scheduler=False)
    state.save_ui_state({"active_id": "chat", "projects": [], "sessions": [
        {"id": "chat", "title": "Files", "provider": "deepseek"},
        {"id": "other", "title": "Other", "provider": "deepseek"},
    ]}, base_revision=0)
    yield state
    state.close()


def upload(ctx, name="source.py", data=b"print('attached file')"):
    status, result = attachment_routes.upload_response(ctx, {"session_id": "chat", "name": name, "data": base64.b64encode(data).decode()})
    assert status == 201, result
    return result["attachment"]


def admit(ctx, refs, *, identity="task"):
    items = [InputAttachmentStore(ctx.state_home, "chat").read(ref["id"]) for ref in refs]
    ctx.durable.admit_input(conversation_id="chat", request_id=identity, task_id=identity, text="Read the attachment",
                            provider_snapshot={"provider_id": "fixture"}, task_state={"message_metadata": {"attachments": items}})
    return items


def test_store_is_immutable_deduplicated_and_scoped(ctx):
    ref = upload(ctx)
    assert upload(ctx) == ref
    assert "text" not in ref and "data_url" not in ref
    original = InputAttachmentStore(ctx.state_home, "chat").read(ref["id"])
    assert original["text"] == "print('attached file')"
    assert attachment_routes.read_response(ctx, {"session_id": "other", "id": ref["id"]})[0] == 404
    assert attachment_routes.read_response(ctx, {"session_id": "missing", "id": ref["id"]})[0] == 404
    assert attachment_routes.read_response(ctx, {"session_id": "chat", "id": "../../outside"})[0] == 404


@pytest.mark.parametrize(("name", "data"), [("../escape.txt", b"text"), ("empty.txt", b""), ("binary.dat", b"\0data"),
                                           ("invalid.txt", b"\xff\xfe"), ("vector.svg", b"<svg/>"), ("large.txt", b"x" * (2 * 1024 * 1024 + 1))],
                         ids=["path", "empty", "binary", "encoding", "svg", "size"])
def test_bad_files_fail_without_becoming_durable_inputs(ctx, name, data):
    status, result = attachment_routes.upload_response(ctx, {"session_id": "chat", "name": name, "data": base64.b64encode(data).decode()})
    assert status == 400 and result["error"]
    assert not ctx.durable.has_conversation("chat")


def test_corrupt_stored_bytes_are_rejected(ctx):
    ref = upload(ctx)
    store = InputAttachmentStore(ctx.state_home, "chat")
    path = store.root / (ref["id"] + ".json")
    payload = json.loads(path.read_text())
    payload["text"] = "modified outside admission"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="corrupted"):
        store.read(ref["id"])


def test_admission_uses_stored_content_not_client_metadata_and_history_is_bounded(ctx):
    ref = upload(ctx)
    body = {"session_id": "chat", "task": "Read this file", "provider": "deepseek", "run_id": "run_" + "a" * 32,
            "attachments": [{**ref, "name": "forged.txt", "text": "forged instruction"}]}
    with mock.patch.object(task_submit, "submit_browser_task", return_value=True):
        status, result = api.run_submit_response(body, partial(task_submit.submit_task, get_state=lambda: ctx), ctx=ctx)
    assert status == 200, result
    entry = ctx.durable.context("chat")[0]
    assert entry.payload["attachments"][0]["name"] == "source.py"
    assert entry.payload["attachments"][0]["text"] == "print('attached file')"
    item = ctx.durable.history_page("chat")["items"][0]
    assert item["attachments"] == [ref]
    assert "attached file" not in json.dumps(item)


def test_non_image_model_rejects_images_before_reserving_a_run(ctx):
    ref = upload(ctx, "screen.png", PNG)
    submit = mock.Mock()
    status, result = api.run_submit_response({"session_id": "chat", "task": "Describe this", "provider": "deepseek", "attachments": [ref]}, submit, ctx=ctx)
    assert status == 400 and result["error"] == IMAGE_UNSUPPORTED
    submit.assert_not_called()


def test_fork_inherits_only_its_visible_attachment_content(ctx):
    ref = upload(ctx, "screen.png", PNG)
    admit(ctx, [ref])
    parent = ctx.durable.context("chat")[0]
    ctx.durable.fork("chat", parent.entry_id, child_conversation_id="fork")
    current = ctx.load_ui_state()
    current["sessions"].append({"id": "fork", "title": "Fork", "provider": "deepseek"})
    ctx.save_ui_state(current, base_revision=current["revision"])
    status, result = attachment_routes.read_response(ctx, {"session_id": "fork", "id": ref["id"]})
    assert status == 200 and result["data_url"].endswith(base64.b64encode(PNG).decode())
    assert attachment_routes.read_response(ctx, {"session_id": "other", "id": ref["id"]})[0] == 404


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_image_and_file_reach_real_codec_once_and_private_metadata_never_reaches_wire(ctx, monkeypatch, protocol):
    image = upload(ctx, "screen.png", PNG)
    text = upload(ctx)
    admit(ctx, [image, text])
    provider = ApiProvider("http://127.0.0.1:9/v1", "vision-fixture", supports_images=True, api_protocol=protocol)
    provider.bind_context("chat")
    provider.bind_durable_context(ctx.durable, "chat")
    provider.bind_input_attachments(ctx.durable, "chat", "task")
    wires = []
    def generate(messages, tools=None, **kwargs):
        wires.append(provider._payload(messages, tools, provider._settings()))
        return ({"choices": [{"message": {"content": "received"}, "finish_reason": "stop"}]} if protocol == "openai-completions"
                else {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "received"}]}]})
    monkeypatch.setattr(provider, "_generate", generate)
    monkeypatch.setattr("codey.providers.api_context.schedule_maintenance", lambda *args: None)
    assert provider.send_turn("Read the attachment").text == "received"
    provider.send_turn("Continue checking")
    first = wires[0]["messages" if protocol == "openai-completions" else "input"][0]
    assert isinstance(first["content"], list)
    assert "print('attached file')" in first["content"][0]["text"]
    assert first["content"][1]["type"] == ("image_url" if protocol == "openai-completions" else "input_image")
    assert "_codey_attachment_entries" not in json.dumps(wires)
    last_user = [m for m in provider._messages if m.get("role") == "user"][-1]
    assert last_user["content"] == "Continue checking"


def test_cold_context_preserves_files_and_gates_historical_images(ctx):
    admit(ctx, [upload(ctx), upload(ctx, "screen.png", PNG)])
    context = ctx.durable.context_snapshot("chat", token_budget=32768)
    provider = ApiProvider("http://127.0.0.1:9/v1", "text-fixture")
    provider.load_durable_context([dict(message) for message in context.messages])
    content = provider._messages[0]["content"]
    assert "attached file" in content and "not available to this model" in content


def test_capability_comes_from_explicit_modalities_not_model_name():
    value = {"id": "vision-in-the-name", "context_length": 32768, "top_provider": {"max_completion_tokens": 4096}, "supported_parameters": ["tools"]}
    assert parse_capability(value).images is False
    value["architecture"] = {"input_modalities": ["text", "image"]}
    assert parse_capability(value).images is True


def test_text_file_reaches_the_browser_provider_send(ctx, monkeypatch):
    from types import SimpleNamespace

    from codey.providers.web_provider import DeepSeekWebProvider

    admit(ctx, [upload(ctx)])
    provider = DeepSeekWebProvider(SimpleNamespace(page=SimpleNamespace(url="fixture")))
    provider.bind_input_attachments(ctx.durable, "chat", "task")
    sent = []
    monkeypatch.setattr("codey.providers.web_provider.run_web_send", lambda owner, **kwargs: kwargs["func"]())
    monkeypatch.setattr(provider.spec.driver, "chat", lambda page, text, **kwargs: sent.append(text) or "received")
    provider.send("Read this file")
    provider.send("Continue")
    assert "print('attached file')" in sent[0] and "untrusted data" in sent[0]
    assert "print('attached file')" not in sent[1]


def test_idle_attachment_input_dispatches_without_changing_its_durable_metadata(ctx):
    from codey.app import input_queue_routes

    ref = upload(ctx)
    body = {"action": "create", "id": "input-files", "request": {"session_id": "chat", "run_id": "run_" + "b" * 32,
            "task": "Review the attached files.", "provider": "deepseek", "input_mode": "follow_up", "attachments": [ref]}}
    with mock.patch.object(task_submit, "submit_browser_task", return_value=True):
        status, result = input_queue_routes.input_action_response(ctx, body)
        assert status == 202, result
        # Exercise the production admission callback without a model/browser worker.
        checkpoint = ctx.durable.task(body['request']['run_id'])
        assert task_submit.dispatch_durable_task(checkpoint, state=ctx)
        assert ctx.input_queue.get('input-files')['state'] == 'submitted'
        assert ctx.durable.context("chat")[0].payload["attachments"][0]["text"] == "print('attached file')"
        ctx.input_queue.close()


def test_another_tasks_attachment_is_not_injected_into_the_current_request(ctx):
    from codey.providers.input_attachments import pending_attachments

    admit(ctx, [upload(ctx)], identity='current')
    future = upload(ctx, 'future.txt', b'belongs to the next task')
    admit(ctx, [future], identity='future')
    items, _ = pending_attachments(ctx.durable, 'chat', 'current', set())
    assert [item['name'] for item in items] == ['source.py']


def test_browser_file_input_is_bounded_before_any_site_send():
    from codey.providers.input_attachments import browser_attachment_text

    with pytest.raises(ValueError, match='input budget'):
        browser_attachment_text('deepseek', 'Read', [{'name':'large.txt','media_type':'text/plain','text':'x' * 2_000_000}])
