"""Turn navigation: responsive reading space, exclusive colors, and exact jump targets."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import model_settings_route
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser

CURRENT = 'rgb(54, 81, 73)'
HIGHLIGHT = 'rgb(111, 145, 131)'


def append_turns(page, count=28, session='a'):
    page.evaluate("""({count,session}) => {
        for (let i=0; i<count; i++) {
            addToSession(session,{type:'user',text:`Question ${i + 1}: 如何保持阅读位置？`});
            addToSession(session,{type:'asst',text:
                `Answer ${i + 1}. 保留文字选择和草稿。\\n\\n` + 'Native reading and copying. '.repeat(24)});
        }
    }""", {'count': count, 'session': session})


def read_turn(page, index):
    page.evaluate("""index => {
        const area=document.getElementById('chat-area');
        const anchor=document.querySelectorAll('#chat > .msg.user')[index];
        area.scrollTop+=anchor.getBoundingClientRect().top-area.getBoundingClientRect().top-24;
    }""", index)
    expect(page.locator('.conversation-tick').nth(index)).to_have_attribute('aria-current', 'location')


def expect_colors(page, *, dark, bright):
    page.wait_for_function("""({dark,bright}) => {
        const colors=Array.from(document.querySelectorAll('.conversation-tick:not([hidden]) span'),
            e=>getComputedStyle(e).backgroundColor);
        return colors.filter(c=>c==='rgb(54, 81, 73)').length===dark
            && colors.filter(c=>c==='rgb(111, 145, 131)').length===bright;
    }""", arg={'dark': dark, 'bright': bright})


def test_navigation_uses_available_space_and_does_not_shift_content(page):
    nav = page.get_by_role('navigation', name='Conversation navigation')
    expect(page.locator('#conversation-nav')).to_be_hidden()
    before = page.locator('#chat').evaluate('e => [e.getBoundingClientRect().x,e.getBoundingClientRect().width]')
    append_turns(page, 7)
    expect(page.locator('#conversation-nav')).to_be_hidden()
    append_turns(page, 1)
    expect(nav).to_be_visible()
    expect(nav.locator('button')).to_have_count(8)
    assert page.locator('#chat').evaluate(
        'e => [e.getBoundingClientRect().x,e.getBoundingClientRect().width]'
    ) == before
    for size, visible in [((1000, 800), False), ((1240, 800), True), ((1240, 480), False), ((1240, 800), True)]:
        page.set_viewport_size({'width': size[0], 'height': size[1]})
        if visible:
            expect(nav).to_be_visible()
        else:
            expect(page.locator('#conversation-nav')).to_be_hidden()
    page.set_viewport_size({'width': 1160, 'height': 800})
    expect(page.locator('#conversation-nav')).to_be_hidden()
    page.locator('#toggle-sidebar').click()
    expect(nav).to_be_visible()
    page.locator('#show-sidebar').click()
    expect(page.locator('#conversation-nav')).to_be_hidden()


@pytest.mark.parametrize('target', [8, 13])
def test_wave_hover_has_one_bright_green_and_no_dark_green_even_on_current_tick(page, target):
    append_turns(page)
    read_turn(page, 13)
    page.mouse.move(1200, 10)
    expect_colors(page, dark=1, bright=0)
    tick = page.locator('.conversation-tick').nth(target)
    scroll = page.locator('#chat-area').evaluate('e => e.scrollTop')
    tick.hover()
    expect_colors(page, dark=0, bright=1)
    preview = page.get_by_role('tooltip')
    expect(preview).to_be_visible()
    expect(preview.locator('.conversation-preview-title')).to_contain_text(f'Question {target + 1}:')
    expect(preview.locator('.conversation-preview-body')).to_contain_text(f'Answer {target + 1}.')
    expect(tick.locator('span')).to_have_css('width', '28px')
    expect(page.locator('.conversation-tick').nth(target + 1).locator('span')).to_have_css('width', '22px')
    expect(page.locator('.conversation-tick').nth(target + 2).locator('span')).to_have_css('width', '15px')
    assert page.locator('#chat-area').evaluate('e => e.scrollTop') == scroll
    page.mouse.move(1200, 10)
    expect(preview).to_be_hidden()
    expect_colors(page, dark=1, bright=0)


def test_pointer_jump_keeps_draft_caret_and_native_selection_and_updates_reading_position(page):
    append_turns(page)
    read_turn(page, 13)
    task = page.locator('#task')
    task.fill('An unsent draft 中文')
    task.evaluate('e => e.setSelectionRange(3, 8)')
    page.locator('.conversation-tick').nth(4).click()
    expect(page.locator('.conversation-tick').nth(4)).to_have_attribute('aria-current', 'location')
    assert page.locator('#chat > .msg.user').nth(4).evaluate(
        'e => Math.abs(e.getBoundingClientRect().top-document.getElementById("chat-area").getBoundingClientRect().top-24)<2'
    )
    expect(task).to_be_focused()
    expect(task).to_have_value('An unsent draft 中文')
    assert task.evaluate('e => [e.selectionStart,e.selectionEnd]') == [3, 8]
    page.mouse.move(1200, 10)
    expect_colors(page, dark=1, bright=0)
    body = page.locator('#chat > .msg.asst .body p').nth(8)
    body.evaluate("""e => {
        const range=document.createRange();range.selectNodeContents(e);
        getSelection().removeAllRanges();getSelection().addRange(range);
    }""")
    selected = page.evaluate('getSelection().toString()')
    assert selected
    page.locator('.conversation-tick').nth(6).click()
    assert page.evaluate('getSelection().toString()') == selected
    page.evaluate("switchSession('b');switchSession('a')")
    expect(task).to_have_value('An unsent draft 中文')
    expect(page.locator('.conversation-tick').nth(6)).to_have_attribute('aria-current', 'location')


def test_keyboard_preview_jump_escape_and_reduced_motion(page):
    page.emulate_media(reduced_motion='reduce')
    append_turns(page)
    read_turn(page, 10)
    task = page.locator('#task')
    task.focus()
    current = page.locator('.conversation-tick[aria-current="location"]')
    expect(current).to_have_attribute('tabindex', '0')
    current.focus()
    current.press('ArrowUp')
    focused = page.locator('.conversation-tick').nth(9)
    expect(focused).to_be_focused()
    expect(focused).to_have_attribute('aria-label', 'Jump to question 10: Question 10: 如何保持阅读位置？')
    expect(focused.locator('span')).to_have_css('transition-duration', '0s')
    expect(page.get_by_role('tooltip')).to_be_visible()
    expect_colors(page, dark=0, bright=1)
    focused.press('Enter')
    expect(focused).to_have_attribute('aria-current', 'location')
    focused.press('Home')
    expect(page.locator('.conversation-tick').first).to_be_focused()
    page.keyboard.press('End')
    expect(page.locator('.conversation-tick').last).to_be_focused()
    page.keyboard.press('Space')
    expect(page.locator('.conversation-tick').last).to_have_attribute('aria-current', 'location')
    page.keyboard.press('Escape')
    expect(task).to_be_focused()
    expect(page.get_by_role('tooltip')).to_be_hidden()
    expect_colors(page, dark=1, bright=0)


def test_turns_ignore_tools_stream_updates_preserve_focus_and_chat_switch_clears_preview(page):
    append_turns(page, 10)
    read_turn(page, 4)
    ticks = page.locator('.conversation-tick')
    ticks.nth(4).focus()
    expect(page.get_by_role('tooltip')).to_be_visible()
    page.evaluate("""() => {
        window.savedNavButton=document.querySelectorAll('.conversation-tick')[4];
        addToSession('a',{type:'tool',kind:'read',path:'safe.py',result:'12 lines'});
        document.querySelectorAll('#chat > .msg.asst .body')[4].textContent='Updated reply 中文';
    }""")
    expect(ticks).to_have_count(10)
    expect(page.locator('.conversation-preview-body')).to_have_text('Updated reply 中文')
    expect(ticks.nth(4)).to_be_focused()
    assert ticks.nth(4).evaluate('e => e === window.savedNavButton')
    page.evaluate("switchSession('b')")
    expect(page.locator('#conversation-nav')).to_be_hidden()
    expect(page.get_by_role('tooltip')).to_be_hidden()
    append_turns(page, 9, session='b')
    expect(page.locator('#conversation-nav')).to_be_visible()
    expect(ticks).to_have_count(9)
    page.evaluate("switchSession('a')")
    expect(ticks).to_have_count(10)
    expect(page.get_by_role('tooltip')).to_be_hidden()
    expect(ticks.nth(4)).to_have_attribute('aria-current', 'location')


def test_long_histories_keep_ticks_inside_view_and_keyboard_can_reach_every_question(page):
    append_turns(page, 100)
    read_turn(page, 50)
    nav = page.locator('#conversation-nav')
    visible = nav.locator('button:visible')
    assert visible.count() <= 48
    assert nav.evaluate("""e => {
        const nav=e.getBoundingClientRect(), area=document.getElementById('chat-area').getBoundingClientRect();
        return nav.top>=area.top+20 && nav.bottom<=area.bottom-20;
    }""")
    page.locator('.conversation-tick[aria-current="location"]').focus()
    page.keyboard.press('Home')
    expect(nav.locator('button').first).to_be_visible()
    expect(nav.locator('button').first).to_be_focused()
    page.keyboard.press('End')
    expect(nav.locator('button').last).to_be_visible()
    expect(nav.locator('button').last).to_be_focused()
    page.keyboard.press('Enter')
    expect(nav.locator('button').last).to_have_attribute('aria-current', 'location')


def test_drawers_and_settings_hide_navigation_and_cancel_delayed_previews(page):
    append_turns(page)
    read_turn(page, 13)
    nav = page.locator('#conversation-nav')
    page.locator('.conversation-tick').nth(8).hover()
    page.evaluate("CodeyUiState.setDrawerOpen('changes-drawer',true)")
    expect(nav).to_be_hidden()
    expect(page.get_by_role('tooltip')).to_be_hidden()
    page.wait_for_timeout(300)
    expect(page.get_by_role('tooltip')).to_be_hidden()
    page.evaluate("CodeyUiState.setDrawerOpen('changes-drawer',false)")
    expect(nav).to_be_visible()
    model_settings_route(page)
    page.locator('#btn-settings').click()
    expect(nav).to_be_hidden()
    page.keyboard.press('Escape')
    expect(nav).to_be_visible()


def test_preview_escapes_user_content_and_stays_within_reading_viewport(page):
    append_turns(page)
    page.evaluate("""() => {
        addToSession('a',{type:'user',text:'<img src=x onerror="window.navXss=true"> ' + 'x'.repeat(400)});
        addToSession('a',{type:'asst',text:'<script>window.navXss=true</script>\\n' + 'Long preview '.repeat(200)});
    }""")
    read_turn(page, 28)
    page.locator('.conversation-tick').last.hover()
    preview = page.get_by_role('tooltip')
    expect(preview).to_be_visible()
    expect(preview.locator('img,script')).to_have_count(0)
    expect(preview.locator('.conversation-preview-title')).to_contain_text('<img src=x')
    assert page.evaluate('window.navXss') is None
    assert preview.evaluate("""e => {
        const p=e.getBoundingClientRect(), a=document.getElementById('chat-area').getBoundingClientRect();
        return p.left>=a.left && p.right<=a.right && p.top>=a.top && p.bottom<=a.bottom;
    }""")
