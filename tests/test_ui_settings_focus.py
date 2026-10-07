"""Settings text fields keep one frame during pointer and keyboard editing."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import model_settings_route
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.mark.parametrize("focus_method", ["pointer", "keyboard"])
def test_settings_text_fields_focus_without_an_extra_frame(page, focus_method):
    model_settings_route(page)
    page.locator("#btn-settings").click()
    expect(page.locator("#local-config-save")).to_be_enabled()
    page.locator("#local-advanced summary").click()
    page.locator("#local-context-preset-button").click()
    page.get_by_role("option", name="Custom…", exact=True).click()
    page.locator("#settings-dismiss").focus()

    for selector in ("#local-base-url", "#local-model-name", "#local-api-key",
                     "#local-display-name", "#local-context-window"):
        field = page.locator(selector)
        frame = field.evaluate("""e => {
            const s = getComputedStyle(e);
            return [s.borderTopWidth, s.borderTopColor, s.backgroundColor,
                    e.getBoundingClientRect().width, e.getBoundingClientRect().height];
        }""")
        if focus_method == "pointer":
            field.click()
        else:
            for _ in range(12):
                page.keyboard.press("Tab")
                if field.evaluate("e => e === document.activeElement"):
                    break
        expect(field).to_be_focused()
        expect(field).to_have_css("outline-style", "none")
        expect(field).to_have_css("box-shadow", "none")
        assert field.evaluate("""e => {
            const s = getComputedStyle(e);
            return [s.borderTopWidth, s.borderTopColor, s.backgroundColor,
                    e.getBoundingClientRect().width, e.getBoundingClientRect().height];
        }""") == frame

    page.locator("#local-api-key").fill("editable-test-key")
    expect(page.locator("#local-api-key")).to_have_value("editable-test-key")
    expect(page.locator("#local-api-key")).to_have_attribute("type", "password")
    page.keyboard.press("Escape")
    expect(page.locator("#local-config-pop")).to_be_hidden()
    expect(page.locator("#btn-settings")).to_be_focused()
