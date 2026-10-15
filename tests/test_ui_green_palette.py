"""The shared green palette stays coherent and diff changes remain readable."""
import colorsys

import pytest

from tests.test_ui_selection import assert_readable_selection
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.fixture
def palette(page):
    page.evaluate("""() => {
      const fixture = document.createElement('section');
      fixture.id = 'palette-fixture';
      fixture.style.cssText = 'position:fixed;inset:30px;z-index:100;background:var(--bg);padding:24px;overflow:auto';
      fixture.innerHTML = `
        <p class="prose">Readable selected text 中文</p>
        <span class="dot ok"></span><span class="dot stale"></span>
        <nav class="conversation-nav" style="position:static">
          <button class="conversation-tick" aria-current="location"><span></span></button>
          <button class="conversation-tick selected"><span></span></button>
        </nav>
        <pre class="diff-pre"><span class="diff-line"><span class="ln">1</span><span class="mark"> </span>unchanged()</span><span class="diff-line del"><span class="ln">2</span><span class="mark">−</span>old_value = 1</span><span class="diff-line add"><span class="ln">2</span><span class="mark">+</span>new_value = 2</span></pre>`;
      document.body.append(fixture);
    }""")
    return page.locator('#palette-fixture')


def luminance(rgb):
    channels = [value / 255 for value in rgb]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    return sum(value * weight for value, weight in zip(linear, [.2126, .7152, .0722], strict=True))


def contrast(first, second):
    low, high = sorted((luminance(first), luminance(second)))
    return (high + .05) / (low + .05)


@pytest.mark.parametrize('channels', [[120, 165, 139], [100, 145, 119]])
def test_green_roles_follow_the_shared_palette(page, palette, channels):
    # A base-token change must reach every green surface, rather than leave
    # a hard-coded teal dot, ring or diff tint behind.
    page.evaluate("channels => document.documentElement.style.setProperty('--green-rgb', channels.join(', '))", channels)
    colors = palette.evaluate("""fixture => {
      const rgb = value => value.match(/[\\d.]+/g).map(Number);
      const color = (selector, property) => rgb(getComputedStyle(fixture.querySelector(selector))[property]);
      const nav = fixture.querySelector('.conversation-nav');
      const current = color('[aria-current] span', 'backgroundColor');
      nav.classList.add('selecting');
      return {
        current, online: color('.dot.ok', 'backgroundColor'), stale: color('.dot.stale', 'backgroundColor'),
        ring: color('.dot.ok', 'boxShadow'), add: color('.diff-line.add', 'backgroundColor'),
        edge: color('.diff-line.add', 'borderLeftColor'),
        highlight: color('.selected span', 'backgroundColor'), mark: color('.diff-line.add .mark', 'color'),
        selection: rgb(getComputedStyle(fixture.querySelector('.prose'), '::selection').backgroundColor)
      };
    }""")
    for role in ('current', 'online', 'stale', 'ring', 'add', 'edge'):
        assert colors[role][:3] == channels, (role, colors)
    assert colors['highlight'] == colors['mark']
    hues = [colorsys.rgb_to_hsv(*(value / 255 for value in colors[role][:3]))[0]
            for role in ('current', 'highlight', 'selection')]
    assert max(hues) - min(hues) < .06
    assert_readable_selection(palette.locator('.prose'))


def test_diff_has_visible_fills_readable_marks_and_aligned_code(palette):
    rows = palette.locator('.diff-line').evaluate_all("""rows => rows.map(row => {
      const rgb = value => value.match(/[\\d.]+/g).map(Number);
      const style = getComputedStyle(row), mark = row.querySelector('.mark'), ln = row.querySelector('.ln');
      return {
        background: rgb(style.backgroundColor), text: rgb(style.color),
        canvas: rgb(getComputedStyle(row.parentElement).backgroundColor),
        mark: rgb(getComputedStyle(mark).color), edge: rgb(style.borderLeftColor),
        border: parseFloat(style.borderLeftWidth), lnX: ln.getBoundingClientRect().left,
        codeX: mark.getBoundingClientRect().right, sign: mark.textContent
      };
    })""")
    for row in rows[1:]:
        alpha = row['background'][3]
        assert .15 <= alpha <= .25, row
        fill = [foreground * alpha + background * (1 - alpha)
                for foreground, background in zip(row['background'][:3], row['canvas'], strict=True)]
        assert contrast(fill, row['canvas']) >= 1.2
        assert contrast(row['text'], fill) >= 4.5
        assert contrast(row['mark'], fill) >= 4.5
        assert row['border'] == 2
        assert row['edge'] != rows[0]['edge']
        assert row['lnX'] == rows[0]['lnX']
        assert row['codeX'] == rows[0]['codeX']
    assert rows[1]['edge'] != rows[2]['edge']
    assert [row['sign'] for row in rows[1:]] == ['−', '+']
