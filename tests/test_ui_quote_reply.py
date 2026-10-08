"""Selected answer text becomes editable draft text, never an implicit send."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def select_answer(page, selector):
    page.locator(selector).evaluate("""e => {
      const r=document.createRange();r.selectNodeContents(e);const s=getSelection();s.removeAllRanges();s.addRange(r);
      e.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:400,clientY:250}));
    }""")


@pytest.mark.parametrize('answer,selector', [('Exact 中文 text', '.msg.asst .body p'),
    ('```python\n    first()\n\n\tsecond()\n```', '.msg.asst pre code')])
def test_quote_appends_exact_selection_without_overwriting_or_sending(page, answer, selector):
    runs = []
    page.route('**/api/run', lambda r: (runs.append(r.request.post_data_json), r.fulfill(json={'ok':True})))
    page.locator('#task').fill('Existing draft')
    page.evaluate("text=>addToSession('a',{type:'asst',text})", answer)
    select_answer(page, selector)
    selected = page.evaluate('getSelection().toString()')
    page.get_by_role('menuitem', name='Quote in reply').click()
    expected = 'Existing draft\n\n' + '\n'.join('> '+line for line in selected.split('\n')) + '\n\n'
    expect(page.locator('#task')).to_have_value(expected)
    expect(page.locator('#task')).to_be_focused()
    assert page.locator('#task').evaluate('e=>e.selectionStart===e.value.length && e.selectionEnd===e.value.length')
    assert page.evaluate('CodeyUiState.current().sessions[0].draft.text') == expected
    assert runs == []
    page.locator('#task').press('Control+z')
    expect(page.locator('#task')).to_have_value('Existing draft')
    page.locator('#task').press('Control+Shift+z')
    expect(page.locator('#task')).to_have_value(expected)


@pytest.mark.parametrize('kind', ['user', 'thinking', 'tool'])
def test_non_answer_selections_offer_copy_only_and_hidden_quote_is_not_keyboard_target(page, kind):
    page.evaluate("kind=>addToSession('a',{type:kind,runId:'fixture-run',text:'Copy only',kind:'read',result:'Copy only'})", kind)
    select_answer(page, '#chat > .msg:last-child')
    expect(page.get_by_role('menuitem', name='Quote in reply')).to_have_count(0)
    page.keyboard.press('End')
    expect(page.locator('#selection-copy')).to_be_focused()


def test_quote_menu_does_not_cross_chat_identity(page):
    page.evaluate("addToSession('a',{type:'asst',text:'Original answer'})")
    select_answer(page, '.msg.asst .body p')
    page.evaluate("switchSession('b')")
    expect(page.locator('#selection-menu')).not_to_have_class('open')
    expect(page.locator('#task')).to_have_value('')


def test_quote_keyboard_action_keeps_reader_position(page):
    page.evaluate("addToSession('a',{type:'asst',text:'First paragraph\\n\\n'+'long answer '.repeat(2000)})")
    page.locator('#chat-area').evaluate("e=>{e.dispatchEvent(new WheelEvent('wheel',{deltaY:-50}));e.scrollTop=0;}")
    select_answer(page, '.msg.asst .body p:first-child')
    page.keyboard.press('End')
    expect(page.get_by_role('menuitem', name='Quote in reply')).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.locator('#task')).to_be_focused()
    assert page.locator('#chat-area').evaluate('e=>e.scrollTop') == 0


def test_queued_scroll_notification_without_movement_keeps_selection_menu(page):
    page.evaluate("addToSession('a',{type:'asst',text:'Selected answer'})")
    select_answer(page, '.msg.asst .body p')
    page.locator('#chat-area').evaluate("e=>e.dispatchEvent(new Event('scroll'))")
    expect(page.locator('#selection-menu')).to_have_class('ctx-menu selection-menu open')
