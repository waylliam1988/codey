"""The shipped empty-chat pond stays decorative and responds without a click."""
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def observe_paint(page):
    page.evaluate("""() => {
      window.pondPaints = 0;
      window.pondGreens = 0;
      const original = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.fillText = function(...args) {
        if (this.canvas.classList.contains('character-pond')) {
          window.pondPaints++;
          if (this.fillStyle === '#78a58b' && this.globalAlpha > .2) window.pondGreens++;
        }
        return original.apply(this, args);
      };
    }""")


def test_ambient_green_and_hover_wake_need_no_click(page):
    page.emulate_media(reduced_motion='reduce')
    field = page.locator('.character-pond')
    expect(field).to_have_css('opacity', '1')
    expect(field).to_have_css('pointer-events', 'none')
    assert field.get_attribute('aria-hidden') == 'true'
    observe_paint(page)
    page.wait_for_function('pondGreens > 0')
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    before = page.evaluate('pondPaints')
    page.wait_for_timeout(100)
    assert page.evaluate('pondPaints') == before
    box = field.bounding_box()
    page.mouse.move(box['x'] + 100, box['y'] + 120)
    page.mouse.move(box['x'] + 300, box['y'] + 200, steps=15)
    page.wait_for_timeout(150)
    assert page.evaluate('pondPaints') > before
    page.locator('#task').fill('Native input remains available')
    expect(page.locator('#task')).to_have_value('Native input remains available')
    assert page.locator('main').evaluate('e => e.scrollTop') == 0


def test_message_fades_stops_and_empty_chat_restarts(page):
    observe_paint(page)
    page.wait_for_function('pondPaints > 0')
    page.evaluate("addToSession('a', {type:'user', text:'First message'})")
    expect(page.locator('.character-pond')).to_have_css('opacity', '0')
    page.wait_for_timeout(550)
    before = page.evaluate('pondPaints')
    page.wait_for_timeout(120)
    assert page.evaluate('pondPaints') == before
    page.evaluate("switchSession('b')")
    expect(page.locator('.character-pond')).to_have_css('opacity', '1')
    page.wait_for_timeout(120)
    assert page.evaluate('pondPaints') > before
