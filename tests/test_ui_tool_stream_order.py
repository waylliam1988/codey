"""The published tool stream retains call slots through real browser updates."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def event(page, call, kind='codemode', *, pending=True, parent='', error=False, result='Retained result'):
    payload = {'type': 'tool_started' if pending else 'tool', 'schema_version': 1,
               'session_id': 'a', 'run_id': 'order-review', 'turn': 1, 'call_id': call,
               'parent_call_id': parent, 'kind': kind, 'tool_name': kind, 'path': f'{call}.ts',
               'activity': 'Working', 'result': result, 'detail': result, 'ok': not error,
               'status': 'error' if error else 'ok'}
    page.evaluate('handleServerEvent', payload)


def begin(page):
    page.evaluate("""() => {
        addToSession('a', {type:'user',text:'Fix the login form and verify the changes.'});
        handleServerEvent({type:'task_start',schema_version:1,session_id:'a',run_id:'order-review',provider:'local'});
    }""")
    page.locator('.process-summary').click()


def order(page, selector='.process-body'):
    return page.locator(selector).evaluate("e=>Array.from(e.querySelectorAll('[data-call-id]'),n=>n.dataset.callId)")


@pytest.mark.parametrize('kind', ['codemode', 'read', 'search', 'edit', 'write', 'apply_patch', 'run'])
def test_completed_tool_retains_its_slot_before_a_later_running_call(page, kind):
    begin(page)
    event(page, 'first', kind)
    event(page, 'later')
    first = page.locator('[data-call-id="first"]')
    later = page.locator('[data-call-id="later"]')
    initial = first.bounding_box()['y'], later.bounding_box()['y']
    event(page, 'first', kind, pending=False)
    assert order(page) == ['first', 'later']
    assert first.bounding_box()['y'] == pytest.approx(initial[0], abs=1)
    assert later.bounding_box()['y'] == pytest.approx(initial[1], abs=1)
    expect(later.locator('.tool-line')).to_have_class('tool-line pending')


def test_nested_reads_keep_their_group_and_errors_do_not_move_out_of_the_stream(page):
    begin(page)
    event(page, 'parent')
    event(page, 'a', 'read', parent='parent')
    event(page, 'b', 'read', parent='parent')
    group = page.locator('.tool-children .tool-group')
    group.locator('.tool-group-summary').click()
    event(page, 'edit', 'edit', parent='parent')
    event(page, 'c', 'read', parent='parent')
    event(page, 'later')
    initial_order = order(page)
    event(page, 'b', 'read', pending=False, parent='parent')
    event(page, 'a', 'read', pending=False, parent='parent', error=True, result='Read denied')
    event(page, 'edit', 'edit', pending=False, parent='parent')
    event(page, 'c', 'read', pending=False, parent='parent')
    event(page, 'parent', pending=False)
    assert order(page) == initial_order
    # A failed row adds its visible error line; its call and siblings keep their slots.
    expect(group).not_to_have_class('tool-group collapsed')
    expect(group.locator('.tg-count')).to_have_text('2 files · 1 failed')
    assert page.locator('.tool-children .tool-group').count() == 1
    expect(group.locator('.tg-count')).to_contain_text('1 failed')
    expect(page.locator('.process-summary')).to_contain_text('1 failed tool')
    expect(page.locator('.process-alerts [data-call-id]')).to_have_count(0)
    group.locator('[data-call-id="a"] summary').click()
    expect(group.locator('[data-call-id="a"] pre')).to_have_text('Read denied')


def test_command_promotion_and_output_refresh_preserve_order_disclosures_and_reading_position(page):
    begin(page)
    event(page, 'command', 'shell')
    event(page, 'later')
    initial_y = page.locator('[data-call-id="later"]').bounding_box()['y']
    result = {'type': 'shell_result', 'schema_version': 1, 'session_id': 'a', 'run_id': 'order-review',
              'id': 'command-review', 'call_id': 'command', 'command': 'npm run test:unit',
              'approved': True, 'status': 'exit', 'exit_code': 1, 'output': 'One test failed'}
    page.evaluate('handleServerEvent', result)
    assert order(page) == ['command', 'later']
    assert page.locator('[data-call-id="later"]').bounding_box()['y'] == pytest.approx(initial_y, abs=1)
    expect(page.locator('.command-card')).to_have_count(1)
    page.locator('.command-card summary').click()
    expect(page.locator('.command-card details')).to_have_attribute('open', '')
    page.evaluate('handleServerEvent', result)
    expect(page.locator('.command-card details')).to_have_attribute('open', '')
    assert order(page) == ['command', 'later']

    event(page, 'output', 'custom_tool')
    text = '\n'.join(f'Line {index}: retained output' for index in range(180))
    event(page, 'output', 'custom_tool', pending=False, result=text)
    output = page.locator('[data-call-id="output"] pre')
    page.locator('[data-call-id="output"] summary').click()
    output.evaluate('e => {e.scrollTop=100; e.scrollLeft=0}')
    before = output.evaluate('e=>e.scrollTop')
    assert before > 0
    # Refresh the mounted result through the same renderer replacement used by
    # accepted tool updates; duplicate terminal SSE facts are intentionally ignored.
    page.evaluate("""result => Process.replace(document.querySelector('#chat'), {
        type:'tool', kind:'custom_tool', toolName:'custom_tool', pending:false,
        runId:'order-review', callId:'output', toolKey:'order-review:output', result
    })""", text + '\nLast retained line')
    expect(output).to_contain_text('Last retained line')
    expect(page.locator('[data-call-id="output"] details')).to_have_attribute('open', '')
    assert output.evaluate('e=>e.scrollTop') == pytest.approx(before, abs=1)


def test_history_replay_keeps_start_order_after_reverse_completions(page):
    ctx = page.codey_context
    ctx.durable.admit_input(conversation_id='a', request_id='history-input', task_id='history-order',
                            text='Inspect the files', provider_snapshot={'provider_id': 'local', 'model_id': 'test'})
    ctx.durable.start_task('history-order', state={})
    messages = [
        {'type': 'tool_pending', 'pending': True, 'runId': 'history-order', 'callId': name,
         'toolKey': f'history-order:{name}', 'kind': kind, 'toolName': kind, 'turn': 1, 'path': f'{name}.ts'}
        for name, kind in [('a', 'read'), ('b', 'read'), ('edit', 'edit'), ('c', 'read')]
    ]
    retained = '\n'.join(f'Line {index}: retained history output' for index in range(180))
    for index, message in enumerate(messages + [{**row, 'type': 'tool', 'pending': False, 'result': retained}
                                               for row in reversed(messages)]):
        ctx.durable.record_display_message('a', run_id='history-order', identity=f'review:{index}', payload=message)
    page.evaluate("ConversationUI.refreshHistory('a')")
    expect(page.locator('.process-summary')).to_be_visible()
    page.locator('.process-summary').click()
    assert order(page) == ['a', 'b', 'edit', 'c']
    page.locator('.tool-group-summary').click()
    page.locator('[data-call-id="a"] summary').click()
    output = page.locator('[data-call-id="a"] pre')
    output.evaluate('e=>e.scrollTop=90')
    assert output.evaluate('e=>e.scrollTop') > 0
    page.evaluate("ConversationUI.refreshHistory('a')")
    assert order(page) == ['a', 'b', 'edit', 'c']
    expect(page.locator('.tool-group-summary')).to_have_attribute('aria-expanded', 'true')
    assert output.evaluate('e=>e.scrollTop') == pytest.approx(90, abs=1)
