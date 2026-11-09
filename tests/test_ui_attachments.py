"""Shipped composer with real attachment routes and durable queue ownership."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, expect

from codey.app import attachment_routes, input_queue_routes
from codey.runtime.core.api_selection import ApiRunSelection
from tests.test_input_attachments import PNG
from tests.test_ui_composer_primary_action import action, run_state
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def routes(page):
    ctx = page.codey_context
    def responder(function):
        def respond(route):
            if route.request.method != "POST":
                status, result = input_queue_routes.inputs_response(ctx, parse_qs(urlsplit(route.request.url).query))
            else:
                status, result = function(ctx, route.request.post_data_json)
            route.fulfill(status=status, json=result)
        return respond
    for path, function in [("attachments", attachment_routes.upload_response),
                           ("attachments/read", attachment_routes.read_response),
                           ("attachments/capabilities", attachment_routes.capabilities_response),
                           ("inputs", input_queue_routes.input_action_response)]:
        page.route("**/api/" + path + ('*' if path == 'inputs' else ''), responder(function))
    return ctx


def attach(
    page: Page,
    name: str = "notes.txt",
    data: bytes = b"Retain the API response shape.",
    mime: str = "text/plain",
) -> None:
    page.locator('.composer-box').evaluate('''(node, file) => {
      const data = new DataTransfer();
      data.items.add(new File([new Uint8Array(file.bytes)], file.name, {type: file.mime}));
      node.dispatchEvent(new DragEvent('dragover', {bubbles: true, cancelable: true, dataTransfer: data}));
      node.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: data}));
    }''', {"name": name, "mime": mime, "bytes": list(data)})
    expect(page.locator('.composer-attachments .spinner')).to_have_count(0)


def quiet(page):
    expect(page.locator('.attachment-error')).to_be_hidden()
    expect(page.locator('.reference-task-context, .reference-next-model')).to_have_count(0)
    assert "Attachment pending" not in page.locator('body').inner_text()
    assert "Current task ·" not in page.locator('body').inner_text()
    assert "Steering applies to the current task" not in page.locator('body').inner_text()


def composer_size(page: Page) -> tuple[float, float]:
    bounds = page.locator('.composer-box').bounding_box()
    assert bounds is not None
    return bounds["width"], bounds["height"]


@pytest.mark.parametrize("width", [1280, 480])
def test_empty_composer_matches_layout_without_attachment_row_and_collapses_after_removal(page: Page, width: int) -> None:
    routes(page)
    page.set_viewport_size({"width": width, "height": 800})
    area = page.locator('.composer-attachments')
    expect(page.get_by_role('button', name='Attach files', exact=True)).to_have_count(0)
    expect(page.locator('input[type=file]')).to_have_count(0)
    expect(area).to_be_hidden()
    empty_size = composer_size(page)
    # Measure the same input without the new row, rather than hard-coding font/platform pixels.
    area.evaluate("node => node.remove()")
    assert composer_size(page) == empty_size
    page.reload()
    expect(page.locator('#task')).to_be_enabled()
    assert composer_size(page) == empty_size
    evidence = Path('.e2e-artifacts/attachments-compact-ui')
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / f'empty-{width}.png'))

    attach(page, 'screen.png', PNG, 'image/png')
    expect(area).to_be_visible()
    expect(area.locator('img')).to_be_visible()
    expect(area).to_contain_text('screen.png')
    expect(area.locator('.attachment-error')).to_have_text(attachment_routes.IMAGE_UNSUPPORTED)
    assert composer_size(page)[0] == empty_size[0]
    assert composer_size(page)[1] > empty_size[1]
    expect(page.locator('.composer-box.attachment-dragging')).to_have_count(0)
    page.reload()
    expect(area.locator('img')).to_be_visible()
    expect(area.locator('.attachment-error')).to_have_text(attachment_routes.IMAGE_UNSUPPORTED)
    page.screenshot(path=str(evidence / f'image-{width}.png'))
    page.get_by_role('button', name='Remove screen.png', exact=True).click()
    expect(area).to_be_hidden()
    assert composer_size(page) == empty_size
    page.screenshot(path=str(evidence / f'removed-{width}.png'))


def test_empty_attachment_validation_error_stays_visible_without_a_file_chip(page: Page) -> None:
    routes(page)
    attach(page, 'empty.png', b'', 'image/png')
    expect(page.locator('.composer-attachments')).to_be_visible()
    expect(page.locator('.composer-attachments .attachment-chip')).to_have_count(0)
    expect(page.locator('.attachment-error')).to_have_text('File must contain between 1 byte and 2 MiB.')


@pytest.mark.parametrize("width", [1280, 480])
def test_image_error_is_the_only_attachment_hint_and_primary_action_stays_single(page, width):
    routes(page)
    page.set_viewport_size({"width": width, "height": 800})
    run_state(page)
    attach(page, "screen.png", PNG, "image/png")
    expect(page.locator('.attachment-error')).to_have_text(attachment_routes.IMAGE_UNSUPPORTED)
    expect(page.locator('.attachment-chip img')).to_be_visible()
    action(page, 'send')
    expect(page.locator('#send')).to_be_disabled()
    evidence = Path('.e2e-artifacts/attachments-ui')
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / f'image-error-{width}.png'))
    page.get_by_role('button', name='Remove screen.png', exact=True).click()
    action(page, 'stop')
    quiet(page)


def test_file_draft_is_scoped_and_survives_reload_without_normal_status_copy(page):
    routes(page)
    attach(page)
    expect(page.locator('.composer-attachments')).to_contain_text('notes.txt')
    expect(page.locator('#send')).to_be_enabled()
    quiet(page)
    page.evaluate("switchSession('b')")
    expect(page.locator('.composer-attachments .attachment-chip')).to_have_count(0)
    page.evaluate("switchSession('a')")
    expect(page.locator('.composer-attachments')).to_contain_text('notes.txt')
    page.reload()
    expect(page.locator('#task')).to_be_enabled()
    expect(page.locator('.composer-attachments')).to_contain_text('notes.txt')
    quiet(page)


def test_upload_failure_retains_file_and_retry_uses_the_same_selected_bytes(page):
    routes(page)
    page.route('**/api/attachments', lambda route: route.fulfill(status=503, json={"error": "Upload unavailable. Retry."}))
    attach(page)
    expect(page.locator('.attachment-error')).to_have_text('Upload unavailable. Retry.')
    expect(page.locator('#send')).to_be_disabled()
    page.unroute('**/api/attachments')
    ctx = page.codey_context
    page.route('**/api/attachments', lambda route: route.fulfill(status=201, json=attachment_routes.upload_response(ctx, route.request.post_data_json)[1]))
    page.locator('.attachment-chip').get_by_role('button', name='Retry', exact=True).click()
    expect(page.locator('#send')).to_be_enabled()
    quiet(page)


def test_paste_and_drop_use_real_file_uploads(page):
    routes(page)
    page.locator('#task').evaluate('''(node, bytes) => {
      const data=new DataTransfer();data.items.add(new File([new Uint8Array(bytes)],'pasted.png',{type:'image/png'}));
      node.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:data}));
    }''', list(PNG))
    expect(page.locator('.attachment-error')).to_have_text(attachment_routes.IMAGE_UNSUPPORTED)
    page.get_by_role('button', name='Remove pasted.png', exact=True).click()
    page.locator('.composer-box').evaluate('''node => {
      const data=new DataTransfer();data.items.add(new File(['attached source'],'dropped.ts',{type:'text/plain'}));
      node.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:data}));
    }''')
    expect(page.locator('.composer-attachments')).to_contain_text('dropped.ts')
    expect(page.locator('#send')).to_be_enabled()
    quiet(page)


def test_rejected_admission_keeps_files_recoverable_and_preserves_a_newer_draft(page):
    routes(page)
    pending = []
    page.route('**/api/inputs', lambda route: pending.append(route))
    attach(page)
    page.locator('#task').fill('Read the attached file')
    page.locator('#send').click()
    expect(page.locator('.composer-attachments .attachment-chip')).to_have_count(0)
    expect(page.locator('.pending-inputs .message-attachments')).to_contain_text('notes.txt')
    page.locator('#task').fill('Newer draft')
    page.wait_for_function('true')
    assert len(pending) == 1
    assert pending[0].request.post_data_json['request']['attachments'][0]['name'] == 'notes.txt'
    pending[0].fulfill(status=400, json={'error': 'Fixture admission rejected', 'code': 'attachment_invalid'})
    expect(page.locator('.pending-inputs')).to_contain_text('Fixture admission rejected')
    page.get_by_role('button', name='Return to draft', exact=True).click()
    expect(page.locator('.composer-attachments')).to_contain_text('notes.txt')
    expect(page.locator('#task')).to_have_value('Newer draft\n\nRead the attached file')
    quiet(page)


def test_attached_steer_uses_the_running_model_and_survives_a_lost_delivery_reply(page):
    ctx = routes(page)
    selection = ApiRunSelection('local', 'fixture', 'vision-fixture', 'openai-completions', True, supports_images=True)
    ctx.reserve_run(session_id='a', project=None, task='Working', provider_id='local', run_id='active')
    ctx.run_registry.bind_api_selection('active', selection)
    ctx.durable.admit_input(conversation_id='a', request_id='current', task_id='active', text='Working', provider_snapshot={'provider_id':'local'})
    ctx.durable.start_task('active', state={})
    run_state(page)
    attach(page, 'screen.png', PNG, 'image/png')
    expect(page.locator('#send')).to_be_enabled()
    quiet(page)

    page.evaluate('''() => {
      const original=window.fetch;
      window.fetch=async (url, options) => {
        const response=await original(url, options);
        if(url==='/api/inputs' && options?.method==='POST')throw new TypeError('Lost delivery reply');
        return response;
      };
    }''')
    page.locator('#send').click()
    expect(page.locator('.pending-inputs')).to_contain_text('Delivery unconfirmed')
    expect(page.locator('.pending-inputs .attachment-chip')).to_contain_text('screen.png')
    expect(page.locator('.composer-attachments .attachment-chip')).to_have_count(0)
    action(page, 'stop')
    page.get_by_role('button', name='Check status', exact=True).click()
    expect(page.locator('.pending-inputs')).to_contain_text('Steer at next boundary')
    item = next(item for item in ctx.input_queue.list('a') if item['request'].get('attachments'))
    assert item['request']['provider'] == 'local'
    assert item['request']['model_selection']['supports_images'] is True
    ctx.durable.commit_generation_boundary('a', task_id='active', at='post_tools')
    item = ctx.input_queue.get(item['id'])
    assert item['state'] == 'consumed'
    page.evaluate('handleServerEvent', {'type':'conversation_input','schema_version':1,'session_id':'a','input':item})
    expect(page.locator('.pending-inputs')).to_be_hidden()
    expect(page.locator('#chat .message-attachments .attachment-chip')).to_have_count(1)
    quiet(page)


def test_idle_attachment_stop_targets_the_retained_delivery_identity(page):
    routes(page)
    pending, stops = [], []
    page.route('**/api/inputs', lambda route: pending.append(route))
    def stop(route):
        stops.append(route.request.post_data_json)
        route.fulfill(json={'ok': True})
    page.route('**/api/stop', stop)
    attach(page)
    page.locator('#send').click()
    action(page, 'stop')
    page.locator('#stop').click()
    assert len(pending) == len(stops) == 1
    body = pending[0].request.post_data_json
    assert stops[0]['run_id'] == body['request']['run_id']
    pending[0].fulfill(status=202, json={'item': {'id': body['id'], 'revision': 1, 'state': 'cancelled', 'request': body['request']}})
    expect(page.locator('.pending-inputs')).to_be_hidden()
