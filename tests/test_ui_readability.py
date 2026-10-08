"""Readable secondary copy and the quiet UI's interaction hierarchy."""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import model_settings_route, open_connection_settings
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def assert_readable(locator):
    expect(locator).to_be_visible()
    ratio = locator.evaluate("""element => {
        const channels = value => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
        const luminance = rgb => rgb.map(v => {
            const c = v / 255;
            return c <= .04045 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4;
        }).reduce((total, c, i) => total + c * [.2126, .7152, .0722][i], 0);
        let background = element;
        while (getComputedStyle(background).backgroundColor === 'rgba(0, 0, 0, 0)') {
            background = background.parentElement;
        }
        const foreground = luminance(channels(getComputedStyle(element).color));
        const behind = luminance(channels(getComputedStyle(background).backgroundColor));
        return (Math.max(foreground, behind) + .05) / (Math.min(foreground, behind) + .05);
    }""")
    assert ratio >= 4.5, f"Secondary text contrast is only {ratio:.2f}:1"


def choose_local(page):
    local = model_settings_route(page)
    page.evaluate("CodeyProviderUI.applyLocalMetadata", local)
    page.locator("#provider-button").click()
    page.locator('.provider-item[data-provider="local"]').first.click()
    page.mouse.move(1000, 100)


@pytest.mark.parametrize("width", [1280, 640])
def test_enabled_context_and_effort_are_readable_at_desktop_and_narrow_widths(page, width):
    choose_local(page)
    page.set_viewport_size({"width": width, "height": 800})
    if width == 640:
        expect(page.locator("#aside")).to_be_hidden()
    page.mouse.move(width - 20, 100)
    for selector in ("#ctx-folder", "#ctx-research", "#effort-button"):
        assert_readable(page.locator(selector))
    expect(page.locator("#ctx-folder")).to_have_css("font-size", "11.5px")
    expect(page.locator("#effort-button")).to_have_css("font-size", "12.5px")
    expect(page.locator(".ctx-sep")).to_have_css("color", "rgb(74, 74, 74)")
    expect(page.locator("#task")).to_have_css("color", "rgb(230, 230, 230)")
    assert page.locator("#task").evaluate(
        "e => getComputedStyle(e, '::placeholder').color"
    ) == "rgb(107, 107, 107)"


def test_context_and_effort_keep_hover_active_keyboard_and_disabled_hierarchy(page):
    choose_local(page)
    research = page.locator("#ctx-research")
    research.hover()
    expect(research).to_have_css("color", "rgb(230, 230, 230)")
    research.click()
    page.mouse.move(1000, 100)
    expect(research).to_have_attribute("aria-pressed", "true")
    expect(research).to_have_css("color", "rgb(230, 230, 230)")
    expect(research).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(research).to_have_css("font-weight", "400")

    effort = page.locator("#effort-button")
    effort.hover()
    expect(effort).to_have_css("color", "rgb(230, 230, 230)")
    effort.click()
    expect(effort).to_have_css("color", "rgb(230, 230, 230)")
    page.keyboard.press("Escape")
    expect(effort).to_be_focused()
    page.mouse.move(1000, 100)
    assert_readable(effort)

    page.evaluate("applyRunState({busy:true,run_id:'readability-run',session_id:'a'})")
    for selector in ("#ctx-folder", "#effort-button"):
        control = page.locator(selector)
        expect(control).to_be_disabled()
        expect(control).to_have_css("color", "rgb(107, 107, 107)")
        expect(control).to_have_css("opacity", "0.55")
    expect(research).to_be_disabled()
    expect(research).to_have_css("color", "rgb(230, 230, 230)")
    expect(research).to_have_css("opacity", "0.55")


def test_settings_explanations_are_readable_and_load_errors_keep_error_tone(page):
    model_settings_route(page)
    open_connection_settings(page)
    expect(page.locator("#local-config-save")).to_be_enabled()
    page.locator("#local-advanced summary").click()
    hints = page.locator(".settings-hint:visible").all()
    assert len(hints) >= 4
    for hint in hints:
        assert_readable(hint)
        expect(hint).to_have_css("font-size", "11.5px")
    expect(page.locator(".settings-section-label")).to_have_css("color", "rgb(107, 107, 107)")
    expect(page.locator("#local-key-status")).to_have_css("color", "rgb(107, 107, 107)")
    page.keyboard.press("Escape")
    page.route("**/api/local_provider", lambda route: route.fulfill(status=503, json={}))
    open_connection_settings(page)
    expect(page.locator("#local-config-summary")).to_have_text("Could not load connection")
    expect(page.locator("#local-config-summary")).to_have_css("color", "rgb(210, 138, 138)")


def test_empty_projects_and_search_results_are_readable_without_brightening_group_labels(page):
    assert_readable(page.locator("#session-list .project-empty").first)
    page.evaluate("""() => {
        const state = CodeyUiState.current();
        CodeyUiState.apply({...state,
            projects:[{id:'empty',name:'empty-project',path:'E:/empty',expanded:true}]});
        renderSidebar();
    }""")
    assert_readable(page.locator(".project-children .project-empty"))
    page.locator("#chat-search-toggle").click()
    search = page.locator("#chat-search")
    search.fill("empty-project")
    assert_readable(page.locator(".project-children .project-empty"))
    expect(page.locator(".group-label:visible")).to_have_css("color", "rgb(107, 107, 107)")
    search.fill("no-such-project")
    assert_readable(page.locator("#search-empty"))


def test_failed_provider_probe_explanation_is_readable_in_model_menu(page):
    page.route("**/api/providers", lambda route: route.fulfill(status=503, json={}))
    page.evaluate("CodeyProviderUI.refreshStatus(true)")
    page.locator("#provider-button").click()
    warning = page.locator("#provider-probe-warning")
    assert_readable(warning)
    expect(warning).to_have_css("font-size", "11.5px")
