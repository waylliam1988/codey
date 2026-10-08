"""Explicit reading intent controls following across output and chat changes."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def long_chat(page):
    page.evaluate("""() => {
      for(let i=0;i<15;i++) addToSession('a',{type:'asst',text:'Paragraph '+i+'\\n\\n'+ 'read me '.repeat(120)});
      document.getElementById('back-to-latest').click();
    }""")


@pytest.mark.parametrize('append', ['addToSession', 'pushMsgToSession'])
def test_upward_intent_near_bottom_stops_both_append_paths(page, append):
    long_chat(page)
    page.locator('#chat-area').evaluate("e => {e.dispatchEvent(new WheelEvent('wheel',{deltaY:-40,bubbles:true})); e.scrollTop=e.scrollHeight-e.clientHeight-40;}")
    before = page.locator('#chat-area').evaluate('e=>e.scrollTop')
    page.evaluate(f"{append}('a',{{type:'asst',text:'new answer '.repeat(300)}})")
    assert abs(page.locator('#chat-area').evaluate('e=>e.scrollTop') - before) < 2
    expect(page.locator('#back-to-latest')).to_be_visible()


def test_large_output_follows_until_reader_leaves_and_requires_actual_bottom(page):
    long_chat(page)
    page.evaluate("addToSession('a',{type:'asst',text:'large chunk '.repeat(1000)+'FINAL_LARGE_OUTPUT'})")
    expect(page.locator('.msg.asst').last).to_contain_text('FINAL_LARGE_OUTPUT')
    page.wait_for_function("()=>{const e=document.getElementById('chat-area');return e.scrollHeight-e.clientHeight-e.scrollTop<=2}")
    page.locator('#chat-area').evaluate("e=>{e.dispatchEvent(new WheelEvent('wheel',{deltaY:-40}));e.scrollTop=e.scrollHeight-e.clientHeight-30;}")
    page.evaluate("CodeyConversationUI.captureView();addToSession('a',{type:'asst',text:'do not follow'})")
    assert page.evaluate('CodeyConversationUI.isFollowing()') is False
    page.locator('#back-to-latest').click()
    assert page.evaluate('CodeyConversationUI.isFollowing()') is True
    assert page.locator('#chat-area').evaluate('e=>e.scrollHeight-e.clientHeight-e.scrollTop') <= 2


@pytest.mark.parametrize('key', ['PageUp', 'Home'])
def test_reading_keys_stop_follow_but_composer_keys_do_not(page, key):
    long_chat(page)
    page.locator('#task').focus()
    page.keyboard.press(key)
    assert page.evaluate('CodeyConversationUI.isFollowing()') is True
    page.locator('#chat-area').evaluate("(e,key)=>e.dispatchEvent(new KeyboardEvent('keydown',{key,bubbles:true}))", key)
    assert page.evaluate('CodeyConversationUI.isFollowing()') is False


def test_horizontal_wheel_and_content_expansion_keep_follow_state(page):
    long_chat(page)
    page.locator('#chat-area').evaluate("e=>e.dispatchEvent(new WheelEvent('wheel',{deltaX:70,deltaY:0,bubbles:true}))")
    page.evaluate("addToSession('a',{type:'asst',text:'expanded '.repeat(600)})")
    assert page.evaluate('CodeyConversationUI.isFollowing()') is True


def test_reader_position_survives_chat_switch_and_rebuild(page):
    long_chat(page)
    page.locator('#chat-area').evaluate("e=>{e.dispatchEvent(new WheelEvent('wheel',{deltaY:-80}));e.scrollTop=180;}")
    before = page.locator('#chat-area').evaluate('e=>e.scrollTop')
    page.evaluate("CodeyConversationUI.captureView();switchSession('b');switchSession('a');renderChat()")
    assert abs(page.locator('#chat-area').evaluate('e=>e.scrollTop') - before) < 2
    assert page.evaluate('CodeyConversationUI.isFollowing()') is False


def test_position_change_without_reading_intent_does_not_disable_follow(page):
    long_chat(page)
    page.locator('#chat-area').evaluate("e=>{e.scrollTop-=10;e.dispatchEvent(new Event('scroll'));}")
    assert page.evaluate('CodeyConversationUI.isFollowing()') is True


def test_scrollbar_release_at_actual_bottom_restores_follow_before_queued_scroll_event(page):
    long_chat(page)
    page.locator('#chat-area').evaluate("""e=>{
      const rect=e.getBoundingClientRect();
      e.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:rect.right-1}));
      e.scrollTop=100;e.dispatchEvent(new Event('scroll'));
      e.scrollTop=e.scrollHeight-e.clientHeight;
      document.dispatchEvent(new PointerEvent('pointerup'));
    }""")
    assert page.evaluate('CodeyConversationUI.isFollowing()') is True
