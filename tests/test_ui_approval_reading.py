"""Approval controls remain usable without scrolling the application shell."""
import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def prepare(page, lines=1):
    page.evaluate("""lines => {
        addToSession('a',{type:'user',text:'Inspect this project'});
        addToSession('a',{type:'asst',text:'Reading earlier results. '.repeat(300),runId:'run',
            streamId:'stream',streamSequence:1,finalAnswer:false});
        window.savedAnswer=document.querySelector('.msg.asst');
        handleServerEvent({type:'shell_request',id:'pending',session_id:'a',run_id:'run',
            command:'python inspect.py\\n'.repeat(lines),cwd:'.'});
    }""", lines)
    expect(page.get_by_role('button', name='Allow once', exact=True)).to_be_visible()


@pytest.mark.parametrize('lines', [1, 100])
def test_approval_reveal_keeps_topbar_and_composer_fixed(page, lines):
    prepare(page, lines)
    state = page.evaluate("""() => ({main:document.querySelector('main').scrollTop,
        body:document.scrollingElement.scrollTop,
        top:document.querySelector('.topbar').getBoundingClientRect().top,
        bottom:document.querySelector('.composer-wrap').getBoundingClientRect().bottom,
        height:innerHeight})""")
    assert state['main'] == 0, state
    assert state['body'] == 0, state
    assert abs(state['bottom'] - state['height']) < 2, state
    for delta in (-300, 300, -300):
        page.locator('#chat-area').hover()
        page.mouse.wheel(0, delta)
        assert page.locator('main').evaluate('e => e.scrollTop') == 0
        assert page.locator('.composer-wrap').evaluate('e => Math.abs(e.getBoundingClientRect().bottom-innerHeight)<2')


def test_allow_updates_during_a_live_reply_and_cannot_be_submitted_twice(page):
    prepare(page)
    page.evaluate("""() => {
        handleServerEvent({type:'shell_request',id:'other',session_id:'a',run_id:'run',command:'python other.py',cwd:'.'});
        window.otherCard=document.querySelector('[data-approval-id="other"]');
    }""")
    requests = []

    def approval(route):
        requests.append(route.request.method)
        route.fulfill(json={'type':'shell_approval_status','id':'pending','session_id':'a','run_id':'run',
                            'decision':'allow','execution':'queued','continuation':'waiting_for_result','revision':2})

    page.route('**/api/shell_approval', approval)
    page.locator('[data-approval-id="pending"] .approval-allow').click()
    expect(page.get_by_role('button', name='Allow once', exact=True)).to_have_count(1)
    expect(page.locator('[data-approval-id="pending"] .approval-allow')).to_have_count(0)
    expect(page.locator('.command-label')).to_have_text('Allowed; command queued')
    assert requests == ['POST']
    assert page.evaluate('window.savedAnswer === document.querySelector(".msg.asst")')
    assert page.evaluate('window.otherCard === document.querySelector("[data-approval-id=other]")')


def test_new_approval_does_not_pull_a_reader_away_from_earlier_messages(page):
    prepare(page)
    page.locator('#chat-area').hover()
    page.mouse.wheel(0, -500)
    page.wait_for_function('!ConversationUI.isFollowing()')
    top = page.locator('#chat-area').evaluate('e => e.scrollTop')
    page.evaluate("""handleServerEvent({type:'shell_request',id:'later',session_id:'a',run_id:'run',
        command:'python later.py',cwd:'.'})""")
    expect(page.locator('[data-approval-id="later"]')).to_be_attached()
    page.wait_for_timeout(100)
    assert abs(page.locator('#chat-area').evaluate('e => e.scrollTop') - top) < 2
    assert page.locator('main').evaluate('e => e.scrollTop') == 0


def test_sending_disables_choices_and_preserves_the_shell_while_scrolling(page):
    prepare(page)
    pending = []
    page.route('**/api/shell_approval', lambda route: pending.append(route))
    page.get_by_role('button', name='Allow once', exact=True).click()
    expect(page.get_by_role('button', name='Submitting…', exact=True)).to_be_disabled()
    expect(page.get_by_role('button', name='Deny', exact=True)).to_be_disabled()
    page.locator('.approval-allow').evaluate('e => {e.click();e.click()}')
    assert len(pending) == 1
    page.locator('#chat-area').hover()
    page.mouse.wheel(0, -300)
    assert page.locator('main').evaluate('e => e.scrollTop') == 0
    assert page.locator('.composer-wrap').evaluate('e => Math.abs(e.getBoundingClientRect().bottom-innerHeight)<2')
    pending[0].fulfill(json={'type':'shell_approval_status','id':'pending','session_id':'a','run_id':'run',
                            'decision':'allow','execution':'queued','continuation':'waiting_for_result','revision':2})
    expect(page.get_by_role('button', name='Submitting…', exact=True)).to_have_count(0)
    assert page.evaluate('window.savedAnswer === document.querySelector(".msg.asst")')
