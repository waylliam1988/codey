"""One composer action, with durable steering and immediate draft handoff."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from codey.app import input_queue_routes
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def run_state(page, running=True):
    page.evaluate('applyRunState', {'busy': running, 'run_id': 'active', 'session_id': 'a',
                                  'run_status': 'running' if running else 'done', 'task_states': [{
                                      'run_id': 'active', 'session_id': 'a', 'task_revision': 1 if running else 2,
                                      'task_status': 'running' if running else 'completed'}]})


def action(page, expected):
    expect(page.locator('.action-slot button:visible')).to_have_count(1)
    expect(page.locator(f'#{expected}')).to_be_visible()
    assert page.locator('.action-slot').bounding_box()['width'] == pytest.approx(34, abs=1)


@pytest.mark.parametrize('width', [1280, 480])
def test_draft_and_current_run_choose_exactly_one_action(page, width):
    page.set_viewport_size({'width': width, 'height': 800})
    action(page, 'send')
    expect(page.locator('#send')).to_be_disabled()
    run_state(page)
    action(page, 'stop')
    page.locator('#task').fill('change the validation')
    action(page, 'send')
    expect(page.locator('#send')).to_have_attribute('aria-label', 'Steer current task')
    expect(page.locator('#send-hint')).to_have_text('Steer')
    page.locator('#task').fill('   ')
    action(page, 'stop')
    page.locator('#task').fill('retained draft')
    run_state(page, False)
    action(page, 'send')
    expect(page.locator('#send')).to_have_attribute('aria-label', 'Send message')
    expect(page.locator('#task')).to_have_value('retained draft')
    run_state(page)
    page.evaluate("switchSession('b')")
    action(page, 'send')
    page.locator('#task').fill('independent draft')
    expect(page.locator('#send')).to_be_enabled()
    page.evaluate("switchSession('a')")
    action(page, 'send')
    expect(page.locator('#task')).to_have_value('retained draft')


def delayed_delivery(page):
    run_state(page)
    page.evaluate("""() => {
        const original=fetch;
        window.steerCalls=[];
        window.fetch=(url, options)=> url==='/api/inputs' && options?.method==='POST'
            ? (steerCalls.push(JSON.parse(options.body)), new Promise((resolve,reject)=>{
                window.confirmSteer=()=>resolve(new Response(JSON.stringify({item:{
                    id:steerCalls[0].id, revision:1, state:'queued', request:steerCalls[0].request
                }})));
                window.loseSteer=()=>reject(new TypeError('lost response'));
            })) : original(url,options);
    }""")


def test_steer_handoff_clears_immediately_and_late_ack_preserves_new_drafts(page):
    delayed_delivery(page)
    page.locator('#task').fill('change the validation')
    page.locator('#send').click()
    expect(page.locator('#task')).to_have_value('')
    expect(page.locator('.pending-inputs')).to_contain_text('change the validation')
    expect(page.locator('.pending-inputs')).to_contain_text('Steer · Sending…')
    action(page, 'stop')
    assert page.evaluate('steerCalls[0].request.input_mode') == 'steer'
    page.locator('#task').press('Enter')
    assert page.evaluate('steerCalls.length') == 1
    page.locator('#task').fill('newer alpha draft')
    action(page, 'send')
    expect(page.locator('#send')).to_be_disabled()
    page.evaluate("switchSession('b')")
    page.locator('#task').fill('beta draft')
    page.evaluate('confirmSteer()')
    expect(page.locator('#task')).to_have_value('beta draft')
    page.evaluate("switchSession('a')")
    expect(page.locator('#task')).to_have_value('newer alpha draft')
    expect(page.locator('.pending-inputs')).to_contain_text('Steer at next boundary')
    expect(page.locator('#send')).to_be_enabled()
    action(page, 'send')


def test_unconfirmed_steer_keeps_one_delivery_and_exposes_only_stop_for_empty_input(page):
    delayed_delivery(page)
    page.locator('#task').fill('retained correction')
    page.locator('#task').press('Enter')
    expect(page.locator('#task')).to_have_value('')
    page.evaluate('loseSteer()')
    expect(page.locator('.pending-inputs')).to_contain_text('Delivery unconfirmed')
    expect(page.locator('.pending-inputs')).to_contain_text('retained correction')
    expect(page.get_by_role('button', name='Check status', exact=True)).to_be_visible()
    action(page, 'stop')
    page.locator('#task').fill('later text')
    action(page, 'send')
    expect(page.locator('#send')).to_be_disabled()
    page.locator('#task').press('Enter')
    assert page.evaluate('steerCalls.length') == 1


def test_storage_failure_keeps_the_draft_and_never_sends_a_steer(page):
    delayed_delivery(page)
    page.evaluate("""() => {
        const original=Storage.prototype.setItem;
        Storage.prototype.setItem=function(key,value){
            if(key==='codey:pending-input-delivery') throw new Error('storage unavailable');
            return original.call(this,key,value);
        };
    }""")
    page.locator('#task').fill('keep the correction')
    page.locator('#send').click()
    expect(page.locator('.pending-inputs')).to_contain_text('message was not sent')
    expect(page.locator('#task')).to_have_value('keep the correction')
    action(page, 'send')
    assert page.evaluate('steerCalls.length') == 0


@pytest.mark.usefixtures('scripted_local_api_connection')
@pytest.mark.parametrize('finish_before_admission', [False, True])
def test_default_steer_reaches_the_real_durable_boundary_or_next_task(page, finish_before_admission):
    ctx = page.codey_context
    ctx.durable.admit_input(conversation_id='a', request_id='current', task_id='active',
                           text='current task', provider_snapshot={'provider_id': 'local', 'model_id': 'test'})
    ctx.durable.start_task('active', state={})
    run_state(page)
    deliveries = []

    def receive(route):
        if route.request.method != 'POST':
            route.fallback()
            return
        body = route.request.post_data_json
        deliveries.append(body)
        if finish_before_admission:
            ctx.durable.complete_task('active', 'Finished before admission', state={})
        status, result = input_queue_routes.input_action_response(ctx, body)
        route.fulfill(status=status, json=result)

    page.route('**/api/inputs', receive)
    page.locator('#task').fill('Use the revised validation')
    page.locator('#send').click()
    expect(page.locator('#task')).to_have_value('')
    expect(page.locator('#send')).to_be_hidden()
    page.wait_for_function("!localStorage.getItem('codey:pending-input-delivery') || JSON.parse(localStorage.getItem('codey:pending-input-delivery')).length===0")
    assert len(deliveries) == 1
    item = ctx.input_queue.get(deliveries[0]['id'])
    assert item['request']['input_mode'] == 'steer'
    if finish_before_admission:
        assert item['state'] == 'submitted'
    else:
        assert item['state'] == 'queued'
        ctx.durable.commit_generation_boundary('a', task_id='active', at='post_tools')
        item = ctx.input_queue.get(item['id'])
        assert item['state'] == 'consumed'
        assert item['consumed_by_run_id'] == 'active'
        page.evaluate('handleServerEvent', {'type':'conversation_input', 'schema_version':1, 'session_id':'a', 'input':item})
        expect(page.locator('.pending-inputs')).to_be_hidden()
    assert sum(entry.payload.get('text') == 'Use the revised validation' for entry in ctx.durable.context('a')) == 1
