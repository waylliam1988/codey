"""Native text selection stays readable across prose, code, and editors."""
from __future__ import annotations

import colorsys

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import model_settings_route, open_connection_settings
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def assert_readable_selection(locator):
    colors = locator.evaluate("""element => {
        const rgb = value => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
        const selection = getComputedStyle(element, '::selection');
        let surface = element;
        while (getComputedStyle(surface).backgroundColor === 'rgba(0, 0, 0, 0)') {
            surface = surface.parentElement;
        }
        return {text:rgb(selection.color), selection:rgb(selection.backgroundColor),
            surface:rgb(getComputedStyle(surface).backgroundColor)};
    }""")

    def luminance(rgb):
        channels = [c / 255 for c in rgb]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
        return sum(c * weight for c, weight in zip(linear, [.2126, .7152, .0722], strict=True))

    def contrast(first, second):
        a, b = sorted([luminance(first), luminance(second)])
        return (b + .05) / (a + .05)

    assert contrast(colors['text'], colors['selection']) >= 4.5
    assert contrast(colors['selection'], colors['surface']) >= 1.5
    hue, saturation, value = colorsys.rgb_to_hsv(*(c / 255 for c in colors['selection']))
    assert .27 <= hue <= .48, 'Selection should be green rather than system blue'
    assert saturation <= .4 and value <= .4, 'Selection should stay subdued and dark'


def test_nested_message_and_code_selection_is_readable_and_preserves_exact_text(page):
    page.evaluate("""() => addToSession('a', {type:'asst',text:
        '选择 **中文**、`path.py` 和 [链接](https://example.com)。\\n\\n```python\\nprint("中文")\\n```'})""")
    body = page.locator('.msg.asst .body')
    for selector in ('p', 'strong', 'p code', 'a', 'pre code'):
        assert_readable_selection(body.locator(selector))
    code = body.locator('pre code')
    code.evaluate("""element => {
        const range = document.createRange(); range.selectNodeContents(element);
        const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
    }""")
    selected = page.evaluate('getSelection().toString()')
    assert selected.strip() == 'print("中文")'
    page.evaluate("addToSession('a', {type:'tool',kind:'read',path:'path.py',result:'1 line'})")
    assert page.evaluate('getSelection().toString()') == selected


@pytest.mark.parametrize('selector', ['#task', '#local-model-name'])
def test_keyboard_editor_selection_stays_readable_without_changing_value_or_focus(page, selector):
    if selector != '#task':
        model_settings_route(page)
        open_connection_settings(page)
    field = page.locator(selector)
    field.fill('Hello 中文 Codey')
    before = field.evaluate("e => ({border:getComputedStyle(e).border, shadow:getComputedStyle(e).boxShadow})")
    field.press('Control+A')
    assert_readable_selection(field)
    expect(field).to_be_focused()
    expect(field).to_have_value('Hello 中文 Codey')
    assert field.evaluate('e => [e.selectionStart,e.selectionEnd]') == [0, 14]
    assert field.evaluate(
        "e => ({border:getComputedStyle(e).border, shadow:getComputedStyle(e).boxShadow})"
    ) == before
    if selector != '#task':
        expect(field).to_have_css('outline-style', 'none')
